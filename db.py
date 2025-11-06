import os
import sqlite3
from typing import Any, Dict, List, Optional
from functools import lru_cache

from config import CFG  # config (keeps chunk_size etc if needed)
import atexit
import threading
import queue
from logger import get_logger

_LOG = get_logger(__name__)

# Prepared statements cache for frequently used queries
_PREPARED_STATEMENTS = {}
_PREPARED_LOCK = threading.Lock()

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


def _get_prepared_statement(conn: sqlite3.Connection, query_key: str, sql: str):
    """
    Cache and reuse prepared statement cursors for frequently used queries.
    Thread-safe with connection-specific caching.
    """
    conn_id = id(conn)
    cache_key = (conn_id, query_key)
    
    with _PREPARED_LOCK:
        if cache_key not in _PREPARED_STATEMENTS:
            _PREPARED_STATEMENTS[cache_key] = sql
    
    return conn.cursor()


def init_db(database_path: str) -> None:
    """
    Initialize database schema. Safe to call multiple times.
    Creates:
    - files (stores full content of indexed files)
    - chunks (with embedding BLOB column for sqlite-vector)
    """
    conn = _get_connection(database_path)
    try:
        cur = conn.cursor()
        
        # files table (stores full content, used to reconstruct chunks)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL,
                content TEXT,
                language TEXT,
                snippet TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_files_path ON files(path);")

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


def store_file(database_path, path, content, language):
    """
    Insert a file record into the DB using a queued single-writer to avoid
    sqlite 'database is locked' errors in multithreaded scenarios.
    Returns lastrowid (same as the previous store_file implementation).
    """
    snippet = (content[:512] if content else "")
    sql = "INSERT INTO files (path, content, language, snippet) VALUES (?, ?, ?, ?)"
    params = (path, content, language, snippet)

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


def get_project_stats(database_path: str) -> Dict[str, Any]:
    """
    Get statistics for a project database.
    Returns file_count and embedding_count.
    """
    conn = _get_connection(database_path)
    try:
        cur = conn.cursor()
        
        # Count files
        cur.execute("SELECT COUNT(*) FROM files")
        file_count = cur.fetchone()[0]
        
        # Count embeddings
        cur.execute("SELECT COUNT(*) FROM chunks WHERE embedding IS NOT NULL")
        embedding_count = cur.fetchone()[0]
        
        return {
            "file_count": int(file_count),
            "embedding_count": int(embedding_count)
        }
    finally:
        conn.close()


def list_files(database_path: str) -> List[Dict[str, Any]]:
    """
    List all files in a project database.
    """
    conn = _get_connection(database_path)
    try:
        rows = conn.execute(
            "SELECT id, path, snippet, language, created_at FROM files ORDER BY id DESC"
        ).fetchall()
        return [
            {
                "id": r["id"], 
                "path": r["path"], 
                "snippet": r["snippet"],
                "language": r["language"],
                "created_at": r["created_at"]
            } 
            for r in rows
        ]
    finally:
        conn.close()


def clear_project_data(database_path: str) -> None:
    """
    Clear all files and chunks from a project database.
    Used when re-indexing a project.
    """
    conn = _get_connection(database_path)
    try:
        cur = conn.cursor()
        # Delete chunks first due to foreign key
        cur.execute("DELETE FROM chunks")
        # Delete files
        cur.execute("DELETE FROM files")
        conn.commit()
    finally:
        conn.close()


# ============================================================================
# Project Registry Database Operations
# ============================================================================

# Default projects directory
PROJECTS_DIR = os.path.expanduser("~/.picocode/projects")

# Retry configuration for database operations
DB_RETRY_COUNT = 3
DB_RETRY_DELAY = 0.1  # seconds


def _ensure_projects_dir():
    """Ensure projects directory exists."""
    try:
        os.makedirs(PROJECTS_DIR, exist_ok=True)
    except Exception as e:
        _LOG.error(f"Failed to create projects directory {PROJECTS_DIR}: {e}")
        raise


def _retry_on_db_locked(func, *args, max_retries=DB_RETRY_COUNT, **kwargs):
    """Retry a database operation if it's locked."""
    import time
    last_error = None
    
    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs)
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e).lower() and attempt < max_retries - 1:
                last_error = e
                time.sleep(DB_RETRY_DELAY * (2 ** attempt))  # Exponential backoff
                continue
            raise
        except Exception as e:
            raise
    
    if last_error:
        raise last_error


def _get_project_id(project_path: str) -> str:
    """Generate a stable project ID from the project path."""
    import hashlib
    return hashlib.sha256(project_path.encode()).hexdigest()[:16]


def _get_project_db_path(project_id: str) -> str:
    """Get the database path for a project."""
    _ensure_projects_dir()
    return os.path.join(PROJECTS_DIR, f"{project_id}.db")


def _get_projects_registry_path() -> str:
    """Get the path to the projects registry database."""
    _ensure_projects_dir()
    return os.path.join(PROJECTS_DIR, "registry.db")


def _init_registry_db():
    """Initialize the projects registry database with proper configuration."""
    registry_path = _get_projects_registry_path()
    
    def _init():
        conn = _get_connection(registry_path)
        try:
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    path TEXT NOT NULL UNIQUE,
                    database_path TEXT NOT NULL,
                    created_at TEXT DEFAULT (datetime('now')),
                    last_indexed_at TEXT,
                    status TEXT DEFAULT 'created',
                    settings TEXT
                )
                """
            )
            conn.commit()
        except Exception as e:
            _LOG.error(f"Failed to initialize registry database: {e}")
            raise
        finally:
            conn.close()
    
    try:
        _retry_on_db_locked(_init)
    except Exception as e:
        _LOG.error(f"Failed to initialize registry after retries: {e}")
        raise


def create_project(project_path: str, name: Optional[str] = None) -> Dict[str, Any]:
    """
    Create a new project entry with its own database.
    
    Args:
        project_path: Absolute path to the project directory
        name: Optional project name (defaults to directory name)
    
    Returns:
        Project metadata dictionary
    
    Raises:
        ValueError: If project path is invalid
        RuntimeError: If database operations fail
    """
    try:
        _init_registry_db()
    except Exception as e:
        _LOG.error(f"Failed to initialize registry: {e}")
        raise RuntimeError(f"Database initialization failed: {e}")
    
    # Validate and normalize path
    if not project_path or not isinstance(project_path, str):
        raise ValueError("Project path must be a non-empty string")
    
    # Check for path traversal attempts
    if ".." in project_path or project_path.startswith("~"):
        raise ValueError("Path traversal not allowed in project path")
    
    try:
        project_path = os.path.abspath(os.path.realpath(project_path))
    except Exception as e:
        raise ValueError(f"Invalid project path: {e}")
    
    try:
        path_exists = os.path.exists(project_path)  # nosec
        if not path_exists:
            raise ValueError(f"Project path does not exist")
        
        is_directory = os.path.isdir(project_path)  # nosec
        if not is_directory:
            raise ValueError(f"Project path is not a directory")
    except (OSError, ValueError) as e:
        if isinstance(e, ValueError):
            raise
        raise ValueError(f"Cannot access project path")
    
    project_id = _get_project_id(project_path)
    db_path = _get_project_db_path(project_id)
    
    if not name:
        name = os.path.basename(project_path)
    
    if name and len(name) > 255:
        name = name[:255]
    
    registry_path = _get_projects_registry_path()
    
    def _create():
        conn = _get_connection(registry_path)
        try:
            cur = conn.cursor()
            
            cur.execute("SELECT * FROM projects WHERE path = ?", (project_path,))
            existing = cur.fetchone()
            if existing:
                _LOG.info(f"Project already exists: {project_path}")
                return dict(existing)
            
            cur.execute(
                """
                INSERT INTO projects (id, name, path, database_path, status)
                VALUES (?, ?, ?, ?, 'created')
                """,
                (project_id, name, project_path, db_path)
            )
            conn.commit()
            
            try:
                init_db(db_path)
                _LOG.info(f"Created project {project_id} at {db_path}")
            except Exception as e:
                _LOG.error(f"Failed to initialize project database: {e}")
                cur.execute("DELETE FROM projects WHERE id = ?", (project_id,))
                conn.commit()
                raise RuntimeError(f"Failed to initialize project database: {e}")
            
            cur.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()
    
    try:
        result = _retry_on_db_locked(_create)
        # Invalidate cache after creating a new project
        _get_project_by_id_cached.cache_clear()
        return result
    except Exception as e:
        _LOG.error(f"Failed to create project: {e}")
        raise


def get_project(project_path: str) -> Optional[Dict[str, Any]]:
    """Get project metadata by path."""
    _init_registry_db()
    project_path = os.path.abspath(project_path)
    
    registry_path = _get_projects_registry_path()
    
    def _get():
        conn = _get_connection(registry_path)
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM projects WHERE path = ?", (project_path,))
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()
    
    return _retry_on_db_locked(_get)


@lru_cache(maxsize=128)
def _get_project_by_id_cached(project_id: str, registry_path: str) -> Optional[tuple]:
    """Internal cached function that returns immutable tuple."""
    def _get():
        conn = _get_connection(registry_path)
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
            row = cur.fetchone()
            if row:
                # Convert row to tuple of key-value pairs for immutability
                return tuple(dict(row).items())
            return None
        finally:
            conn.close()
    
    return _retry_on_db_locked(_get)


def get_project_by_id(project_id: str) -> Optional[Dict[str, Any]]:
    """Get project metadata by ID with caching."""
    _init_registry_db()
    
    registry_path = _get_projects_registry_path()
    cached_result = _get_project_by_id_cached(project_id, registry_path)
    
    # Convert tuple back to dict
    return dict(cached_result) if cached_result else None


def list_projects() -> List[Dict[str, Any]]:
    """List all registered projects."""
    _init_registry_db()
    
    registry_path = _get_projects_registry_path()
    
    def _list():
        conn = _get_connection(registry_path)
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM projects ORDER BY created_at DESC")
            rows = cur.fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()
    
    return _retry_on_db_locked(_list)


def update_project_status(project_id: str, status: str, last_indexed_at: Optional[str] = None):
    """Update project indexing status and invalidate cache."""
    _init_registry_db()
    
    registry_path = _get_projects_registry_path()
    
    def _update():
        conn = _get_connection(registry_path)
        try:
            cur = conn.cursor()
            if last_indexed_at:
                cur.execute(
                    "UPDATE projects SET status = ?, last_indexed_at = ? WHERE id = ?",
                    (status, last_indexed_at, project_id)
                )
            else:
                cur.execute(
                    "UPDATE projects SET status = ? WHERE id = ?",
                    (status, project_id)
                )
            conn.commit()
        finally:
            conn.close()
    
    _retry_on_db_locked(_update)
    # Invalidate cache after update
    _get_project_by_id_cached.cache_clear()


def update_project_settings(project_id: str, settings: Dict[str, Any]):
    """Update project settings (stored as JSON) and invalidate cache."""
    import json
    _init_registry_db()
    
    registry_path = _get_projects_registry_path()
    
    def _update():
        conn = _get_connection(registry_path)
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE projects SET settings = ? WHERE id = ?",
                (json.dumps(settings), project_id)
            )
            conn.commit()
        finally:
            conn.close()
    
    _retry_on_db_locked(_update)
    # Invalidate cache after update
    _get_project_by_id_cached.cache_clear()


def delete_project(project_id: str):
    """Delete a project and its database, invalidating cache."""
    _init_registry_db()
    
    project = get_project_by_id(project_id)
    if not project:
        raise ValueError(f"Project not found: {project_id}")
    
    db_path = project.get("database_path")
    if db_path and os.path.exists(db_path):
        try:
            os.remove(db_path)
        except Exception:
            pass
    
    registry_path = _get_projects_registry_path()
    
    def _delete():
        conn = _get_connection(registry_path)
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM projects WHERE id = ?", (project_id,))
            conn.commit()
        finally:
            conn.close()
    
    _retry_on_db_locked(_delete)
    # Invalidate cache after deletion
    _get_project_by_id_cached.cache_clear()


def get_or_create_project(project_path: str, name: Optional[str] = None) -> Dict[str, Any]:
    """Get existing project or create new one."""
    project = get_project(project_path)
    if project:
        return project
    return create_project(project_path, name)

