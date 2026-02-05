"""
Database writer class for queued write operations.
Provides thread-safe database write access through a single-writer thread.
"""

import atexit
import os
import queue
import sqlite3
import threading

from utils.logger import get_logger

from .db_task import _DBTask

_LOG = get_logger(__name__)

_WRITERS = {}
_WRITERS_LOCK = threading.Lock()


class DBWriter:
    def __init__(self, database_path, timeout_seconds=30, num_workers: int = 1):
        self.database_path = database_path
        self._q = queue.Queue()
        self._stop = threading.Event()
        self._workers: list[threading.Thread] = []
        self._timeout_seconds = timeout_seconds
        self._num_workers = max(1, num_workers)
        for i in range(self._num_workers):
            worker = threading.Thread(target=self._worker, daemon=False, name=f"DBWriter-{database_path}-worker{i + 1}")
            worker.start()
            self._workers.append(worker)
        _LOG.info(f"DBWriter started for database: {database_path} with {self._num_workers} worker(s)")

    def _open_conn(self):
        # Ensure parent directory exists
        db_dir = os.path.dirname(self.database_path)
        if db_dir and not os.path.exists(db_dir):
            try:
                os.makedirs(db_dir, exist_ok=True)
            except Exception as e:
                _LOG.warning(f"Could not create database directory: {e}")

        conn = sqlite3.connect(self.database_path, timeout=self._timeout_seconds, check_same_thread=False)
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
        except sqlite3.OperationalError as e:
            # WAL mode may not work on all filesystems (e.g., tmpfs, NFS)
            _LOG.warning(f"Could not enable WAL mode (continuing without): {e}")
        conn.execute("PRAGMA busy_timeout = 30000;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        _LOG.debug(f"Database connection opened for: {self.database_path}")
        return conn

    def _worker(self):
        conn = None
        try:
            # Check if database file exists before trying to connect
            if not os.path.exists(self.database_path):
                # Wait briefly to see if database is being created
                import time

                time.sleep(0.5)
                if not os.path.exists(self.database_path):
                    _LOG.warning(f"Database does not exist, worker stopping: {self.database_path}")
                    # Process remaining tasks with error
                    while not self._stop.is_set():
                        try:
                            task = self._q.get(timeout=0.1)
                            if task is None:
                                break
                            task.exception = sqlite3.OperationalError(f"Database does not exist: {self.database_path}")
                            task.event.set()
                            self._q.task_done()
                        except queue.Empty:
                            continue
                    return

            conn = self._open_conn()
            cur = conn.cursor()
            while not self._stop.is_set():
                try:
                    task = self._q.get(timeout=0.5)
                except queue.Empty:
                    continue
                if task is None:
                    break
                if not os.path.exists(self.database_path):
                    # Database was deleted while running
                    task.exception = sqlite3.OperationalError(f"Database was deleted: {self.database_path}")
                    task.event.set()
                    self._q.task_done()
                    break
                try:
                    cur.execute(task.sql, task.params)
                    # Handle RETURNING clause - must fetch before commit
                    if "RETURNING" in task.sql.upper():
                        result = cur.fetchone()
                        task.rowid = result[0] if result else None
                        conn.commit()
                    else:
                        conn.commit()
                        task.rowid = cur.lastrowid
                except Exception as e:
                    task.exception = e
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    _LOG.exception("Error executing DB task")
                finally:
                    task.event.set()
                    self._q.task_done()
        except Exception:
            _LOG.exception("DBWriter thread initialization failed")
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    def enqueue_and_wait(self, sql, params, wait_timeout=60.0):
        """
        Enqueue an SQL write and wait for the background thread to perform it.
        Returns the lastrowid or raises the exception raised during execution.
        """
        task = _DBTask(sql, params)
        self._q.put(task)
        completed = task.event.wait(wait_timeout)
        if not completed:
            raise TimeoutError(f"Timed out waiting for DB write to {self.database_path}")
        if task.exception:
            raise task.exception
        return task.rowid

    def enqueue_no_wait(self, sql, params):
        """
        Fire-and-forget enqueue (no result returned).
        """
        task = _DBTask(sql, params)
        self._q.put(task)
        return task

    def clear_queue(self):
        """Clear all pending tasks from the queue without processing them."""
        try:
            while True:
                task = self._q.get_nowait()
                if task is not None:
                    task.exception = sqlite3.OperationalError("Database deleted - operation cancelled")
                    task.event.set()
                self._q.task_done()
        except queue.Empty:
            pass

    def stop(self, wait=True):
        """Stop all worker threads. If wait=True, block until all threads join."""
        _LOG.info(f"Stopping DBWriter for database: {self.database_path}")
        self._stop.set()
        self.clear_queue()
        for _ in range(self._num_workers):
            self._q.put(None)
        if wait:
            for worker in self._workers:
                worker.join(timeout=5.0)
                if worker.is_alive():
                    _LOG.warning(f"DBWriter worker thread for {self.database_path} did not stop within 5s")
            _LOG.info(f"DBWriter stopped for database: {self.database_path}")


def get_writer(database_path):
    """Get or create a DBWriter instance for a database path.
    Uses multiple worker threads based on configuration to reduce lock contention.
    """
    from utils.config import CFG

    cpu = os.cpu_count() or 1
    default_workers = min(8, cpu)  # up to 8 workers for higher throughput
    num_workers = int(CFG.get("db_writer_workers", default_workers))
    if num_workers < 1:
        num_workers = 1
    with _WRITERS_LOCK:
        w = _WRITERS.get(database_path)
        if w is None:
            w = DBWriter(database_path, num_workers=num_workers)
            _WRITERS[database_path] = w
        return w


def stop_writer(database_path: str, wait: bool = True):
    """Stop the DBWriter for a specific database path."""
    with _WRITERS_LOCK:
        writer = _WRITERS.pop(database_path, None)
    if writer:
        writer.stop(wait=wait)


def stop_all_writers():
    """Stop all DBWriter threads (called automatically at process exit)."""
    with _WRITERS_LOCK:
        writers = list(_WRITERS.values())
        _WRITERS.clear()
    for w in writers:
        try:
            w.stop(wait=True)
        except Exception:
            _LOG.exception("Error stopping DBWriter")


atexit.register(stop_all_writers)
