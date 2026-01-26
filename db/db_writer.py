"""
Database writer class for queued write operations.
Provides thread-safe database write access through a single-writer thread.
"""
import os
import sqlite3
import atexit
import threading
from typing import List
import queue
from utils.logger import get_logger
from .db_task import _DBTask

_LOG = get_logger(__name__)

# registry of DBWriter instances keyed by database path
_WRITERS = {}
_WRITERS_LOCK = threading.Lock()


class DBWriter:
    def __init__(self, database_path, timeout_seconds=30, num_workers: int = 1):
        self.database_path = database_path
        self._q = queue.Queue()
        self._stop = threading.Event()
        self._workers: List[threading.Thread] = []
        self._timeout_seconds = timeout_seconds
        # Determine number of worker threads (default 1, can be increased for higher concurrency)
        self._num_workers = max(1, num_workers)
        for i in range(self._num_workers):
            worker = threading.Thread(
                target=self._worker,
                daemon=False,
                name=f"DBWriter-{database_path}-worker{i+1}"
            )
            worker.start()
            self._workers.append(worker)
        _LOG.info(f"DBWriter started for database: {database_path} with {self._num_workers} worker(s)")

    def _open_conn(self):
        conn = sqlite3.connect(self.database_path, timeout=self._timeout_seconds, check_same_thread=False)
        # Reduce contention and allow concurrent readers during writes
        conn.execute("PRAGMA journal_mode=WAL;")
        # Make busy timeout explicit (milliseconds)
        conn.execute("PRAGMA busy_timeout = 30000;")
        # Optional: balance durability and performance
        conn.execute("PRAGMA synchronous = NORMAL;")
        _LOG.debug(f"Database connection opened for: {self.database_path}")
        return conn

    def _worker(self):
        conn = None
        try:
            conn = self._open_conn()
            cur = conn.cursor()
            while not self._stop.is_set():
                try:
                    task = self._q.get(timeout=0.5)
                except queue.Empty:
                    continue
                if task is None:
                    # sentinel to stop
                    break
                try:
                    cur.execute(task.sql, task.params)
                    conn.commit()
                    task.rowid = cur.lastrowid
                except Exception as e:
                    # store exception for the waiting thread to raise or handle
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
            # re-raise sqlite3.OperationalError or other exceptions
            raise task.exception
        return task.rowid

    def enqueue_no_wait(self, sql, params):
        """
        Fire-and-forget enqueue (no result returned).
        """
        task = _DBTask(sql, params)
        self._q.put(task)
        return task

    def stop(self, wait=True):
        """Stop all worker threads. If wait=True, block until all threads join."""
        _LOG.info(f"Stopping DBWriter for database: {self.database_path}")
        self._stop.set()
        # Enqueue sentinel for each worker to exit
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
    # Determine number of workers, default 2, capped at CPU count
    cpu = os.cpu_count() or 1
    default_workers = min(4, cpu)  # up to 4 workers
    num_workers = int(CFG.get('db_writer_workers', default_workers))
    if num_workers < 1:
        num_workers = 1
    with _WRITERS_LOCK:
        w = _WRITERS.get(database_path)
        if w is None:
            w = DBWriter(database_path, num_workers=num_workers)
            _WRITERS[database_path] = w
        return w


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


# ensure cleanup at exit
atexit.register(stop_all_writers)
