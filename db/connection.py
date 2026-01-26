"""
Unified database connection utilities.
Provides consistent connection management across all database operations.
"""
import os
import sqlite3
from typing import Optional
from contextlib import contextmanager
from utils.logger import get_logger

logger = get_logger(__name__)


def get_db_connection(
    db_path: str,
    timeout: float = 30.0,
    enable_wal: bool = True,
    enable_vector: bool = False,
    row_factory: bool = True
) -> sqlite3.Connection:
    """
    Create a database connection with consistent configuration.
    
    Args:
        db_path: Path to the SQLite database file
        timeout: Timeout in seconds for waiting on locks (default: 30.0)
        enable_wal: Enable Write-Ahead Logging mode (default: True)
        enable_vector: Load sqlite-vector extension (default: False)
        row_factory: Use sqlite3.Row factory for dict-like access (default: True)
        
    Returns:
        sqlite3.Connection object configured for the specified operations
        
    Raises:
        RuntimeError: If vector extension fails to load when enable_vector=True
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
    
    if enable_vector:
        from .vector_operations import load_sqlite_vector_extension
        load_sqlite_vector_extension(conn)
        logger.debug(f"Vector extension loaded for connection to {db_path}")
    
    return conn


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
