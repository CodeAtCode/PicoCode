import os
import sqlite3
from typing import Any, Dict, List, Optional

from config import CFG  # config (keeps chunk_size etc if needed)
import atexit
import threading
import queue
from logger import get_logger

_LOG = get_logger(__name__)

# registry of DBWriter instances keyed by database path
_WRITERS = {}
_WRITERS_LOCK = threading.Lock()

class _DBTask:
    def __init__(self, sql, params):
        self.sql = sql
        self.params = params
        self.event = threading.Event()
        self.rowid = None
        self.exception = None

class DBWriter:
    def __init__(self, database_path, timeout_seconds=30):
        self.database_path = database_path
        self._q = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._worker, daemon=True, name=f"DBWriter-{database_path}")
        self._timeout_seconds = timeout_seconds
        self._thread.start()

    def _open_conn(self):
        conn = sqlite3.connect(self.database_path, timeout=self._timeout_seconds, check_same_thread=False)
        # Reduce contention and allow concurrent readers during writes
        conn.execute("PRAGMA journal_mode=WAL;")
        # Make busy timeout explicit (milliseconds)
        conn.execute("PRAGMA busy_timeout = 30000;")
        # Optional: balance durability and performance
        conn.execute("PRAGMA synchronous = NORMAL;")
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
        """Stop the worker thread. If wait=True, block until thread joins."""
        self._stop.set()
        # enqueue sentinel for immediate exit
        self._q.put(None)
        if wait:
            self._thread.join(timeout=5.0)

def _get_writer(database_path):
    with _WRITERS_LOCK:
        w = _WRITERS.get(database_path)
        if w is None:
            w = DBWriter(database_path)
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

# Simple connection helper: we open new connections per operation so the code is robust
# across threads. We set WAL journal mode for safer concurrency.
# Added a small timeout to avoid long blocking if DB is locked.
def _get_connection(db_path: str) -> sqlite3.Connection:
    dirname = os.path.dirname(os.path.abspath(db_path))
    if dirname and not os.path.isdir(dirname):
        os.makedirs(dirname, exist_ok=True)
    # timeout in seconds for busy sqlite; small value to avoid long blocking in web requests
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode = WAL;")
    except Exception:
        # Not fatal — continue
        pass
    return conn


def init_db(database_path: str) -> None:
    """
    Initialize database schema. Safe to call multiple times.
    Creates:
    - analyses (embedding_count column kept for backward compat but not used as source of truth)
    - files
    - chunks (with embedding BLOB column for sqlite-vector)
    """
    conn = _get_connection(database_path)
    try:
        cur = conn.cursor()
        # analyses table: embedding_count column kept for compatibility but will be computed live
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS analyses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                path TEXT NOT NULL,
                status TEXT NOT NULL,
                embedding_count INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now'))
            )
            """
        )

        # files table (stores full content, used to reconstruct chunks)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                analysis_id INTEGER NOT NULL,
                path TEXT NOT NULL,
                content TEXT,
                language TEXT,
                snippet TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (analysis_id) REFERENCES analyses(id) ON DELETE CASCADE
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_files_analysis ON files(analysis_id);")

        # chunks table: metadata for chunked documents; includes embedding BLOB column
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id INTEGER NOT NULL,
                path TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                embedding BLOB,
                created_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file_id);")
        conn.commit()
    finally:
        conn.close()


def create_analysis(database_path: str, name: str, path: str, status: str = "pending") -> int:
    conn = _get_connection(database_path)
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO analyses (name, path, status) VALUES (?, ?, ?)",
            (name, path, status),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def update_analysis_status(database_path: str, analysis_id: int, status: str) -> None:
    conn = _get_connection(database_path)
    try:
        cur = conn.cursor()
        cur.execute("UPDATE analyses SET status = ? WHERE id = ?", (status, analysis_id))
        conn.commit()
    finally:
        conn.close()


def store_file(database_path, analysis_id, path, content, language):
    """
    Insert a file record into the DB using a queued single-writer to avoid
    sqlite 'database is locked' errors in multithreaded scenarios.
    Returns lastrowid (same as the previous store_file implementation).
    """
    snippet = (content[:512] if content else "")
    sql = "INSERT INTO files (analysis_id, path, content, language, snippet) VALUES (?, ?, ?, ?, ?)"
    params = (analysis_id, path, content, language, snippet)

    writer = _get_writer(database_path)
    # We wait for the background writer to complete the insert and then return the row id.
    # This preserves the synchronous semantics callers expect.
    return writer.enqueue_and_wait(sql, params, wait_timeout=60.0)


def insert_chunk_row_with_null_embedding(database_path: str, file_id: int, path: str, chunk_index: int) -> int:
    """
    Insert a chunk metadata row without populating embedding column.
    Returns the new chunks.id.
    """
    conn = _get_connection(database_path)
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO chunks (file_id, path, chunk_index) VALUES (?, ?, ?)",
            (file_id, path, chunk_index),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def list_analyses(database_path: str) -> List[Dict[str, Any]]:
    """
    Return analyses with computed file_count and computed embedding_count (from chunks.embedding).
    This ensures the UI shows accurate, up-to-date counts based on actual rows.
    """
    conn = _get_connection(database_path)
    try:
        cur = conn.cursor()
        rows = cur.execute(
            """
            SELECT
                a.id,
                a.name,
                a.path,
                a.status,
                (SELECT COUNT(*) FROM files f WHERE f.analysis_id = a.id) AS file_count,
                (SELECT COUNT(*) FROM chunks ch JOIN files f2 ON ch.file_id = f2.id
                    WHERE f2.analysis_id = a.id AND ch.embedding IS NOT NULL) AS embedding_count,
                a.created_at
            FROM analyses a
            ORDER BY a.id DESC
            """
        ).fetchall()
        results: List[Dict[str, Any]] = []
        for r in rows:
            results.append(
                {
                    "id": r["id"],
                    "name": r["name"],
                    "path": r["path"],
                    "status": r["status"],
                    "file_count": int(r["file_count"]),
                    "embedding_count": int(r["embedding_count"]),
                    "created_at": r["created_at"],
                }
            )
        return results
    finally:
        conn.close()


def list_files_for_analysis(database_path: str, analysis_id: int) -> List[Dict[str, Any]]:
    conn = _get_connection(database_path)
    try:
        rows = conn.execute(
            "SELECT id, path, snippet FROM files WHERE analysis_id = ? ORDER BY id DESC", (analysis_id,)
        ).fetchall()
        return [{"id": r["id"], "path": r["path"], "snippet": r["snippet"]} for r in rows]
    finally:
        conn.close()


def delete_analysis(database_path: str, analysis_id: int) -> None:
    """
    Delete an analysis and cascade-delete associated files / chunks.
    Foreign key enforcement varies by SQLite build; do explicit deletes for safety.
    """
    conn = _get_connection(database_path)
    try:
        cur = conn.cursor()
        # delete chunks for files in analysis
        cur.execute(
            "DELETE FROM chunks WHERE file_id IN (SELECT id FROM files WHERE analysis_id = ?)",
            (analysis_id,),
        )
        # delete files
        cur.execute("DELETE FROM files WHERE analysis_id = ?", (analysis_id,))
        # delete analysis row
        cur.execute("DELETE FROM analyses WHERE id = ?", (analysis_id,))
        conn.commit()
    finally:
        conn.close()
