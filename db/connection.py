"""
Unified database connection utilities.
Provides consistent connection management across all database operations.
"""

import os
import sqlite3
import threading
from contextlib import contextmanager

from utils.logger import get_logger

logger = get_logger(__name__)

# Connection pool for read operations (thread-local)
_connection_pool = {}
_pool_lock = threading.Lock()


def _ensure_vector_extension(conn: sqlite3.Connection) -> None:
    """
    Load sqlite-vector extension for the given connection.
    Must be called for every connection as SQLite extensions are per-connection.
    """
    from .vector_operations import load_sqlite_vector_extension

    load_sqlite_vector_extension(conn)


def get_db_connection(db_path: str, timeout: float = 30.0, enable_wal: bool = True, row_factory: bool = True) -> sqlite3.Connection:
    """
    Create a database connection with consistent configuration.

    Args:
        db_path: Path to the SQLite database file
        timeout: Timeout in seconds for waiting on locks (default: 30.0)
        enable_wal: Enable Write-Ahead Logging mode (default: True)
        (always loads vector extension)
        row_factory: Use sqlite3.Row factory for dict-like access (default: True)

    Returns:
        sqlite3.Connection object configured for the specified operations

    Raises:
        RuntimeError: If loading the vector extension fails
    """
    dirname = os.path.dirname(os.path.abspath(db_path))
    if dirname and not os.path.isdir(dirname):
        os.makedirs(dirname, exist_ok=True)

    conn = sqlite3.connect(db_path, timeout=timeout, check_same_thread=False)

    if row_factory:
        conn.row_factory = sqlite3.Row

    if enable_wal:
        try:
            conn.execute("PRAGMA journal_mode = WAL;")
        except Exception as e:
            logger.warning(f"Failed to enable WAL mode: {e}")

    try:
        conn.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)};")
    except Exception as e:
        logger.warning(f"Failed to set busy_timeout: {e}")

    _ensure_vector_extension(conn)

    return conn


def get_pooled_connection(db_path: str, timeout: float = 30.0, enable_wal: bool = True) -> sqlite3.Connection:
    """
    Get a connection from the pool or create a new one.
    Connections are thread-local and reused within the same thread.

    Args:
        db_path: Path to the SQLite database file
        timeout: Timeout in seconds for waiting on locks
        enable_wal: Enable Write-Ahead Logging mode

    Returns:
        sqlite3.Connection object from the pool or newly created
    """
    thread_id = threading.current_thread().ident
    pool_key = (thread_id, db_path)

    with _pool_lock:
        conn = _connection_pool.get(pool_key)
        if conn is None or _is_connection_closed(conn):
            conn = get_db_connection(db_path, timeout=timeout, enable_wal=enable_wal)
            _connection_pool[pool_key] = conn

    return conn


def _is_connection_closed(conn: sqlite3.Connection) -> bool:
    """Check if a connection is closed or invalid."""
    try:
        conn.execute("SELECT 1")
        return False
    except (sqlite3.ProgrammingError, sqlite3.Error):
        return True


def close_pooled_connection(db_path: str) -> None:
    """Close the pooled connection for the current thread."""
    thread_id = threading.current_thread().ident
    pool_key = (thread_id, db_path)

    with _pool_lock:
        conn = _connection_pool.pop(pool_key, None)
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def close_all_pooled_connections() -> None:
    """Close all connections in the pool."""
    with _pool_lock:
        for conn in _connection_pool.values():
            try:
                conn.close()
            except Exception:
                pass
        _connection_pool.clear()


@contextmanager
def db_connection(db_path: str, **kwargs):
    """
    Context manager for database connections with automatic cleanup.

    Args:
        db_path: Path to the SQLite database file
        **kwargs: Additional arguments passed to get_db_connection()

    Yields:
        sqlite3.Connection object

    Example:
        with db_connection(db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM files")
            results = cur.fetchall()
    """
    conn = get_db_connection(db_path, **kwargs)
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception as e:
            logger.warning(f"Error closing database connection: {e}")
