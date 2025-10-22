import sqlite3
import json
import threading
from typing import List, Optional, Dict, Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    path TEXT,
    status TEXT DEFAULT 'pending',
    file_count INTEGER DEFAULT 0,
    embedding_count INTEGER DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_id INTEGER,
    path TEXT,
    content TEXT,
    language TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS embeddings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER,
    vector TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""

# Module-level registry so repeated calls with the same database_path return the same Database instance.
_DB_INSTANCES: Dict[str, "Database"] = {}
_DB_INSTANCES_LOCK = threading.Lock()


class Database:
    """
    Lightweight wrapper around sqlite3.Connection that keeps a persistent connection open,
    is safe for multi-threaded use (via an internal lock), and exposes convenience methods
    for common queries used in the original module.

    Use get_database(database_path) to obtain a singleton instance per path.
    """

    def __init__(self, database_path: str, timeout: float = 30.0, check_same_thread: bool = False):
        # check_same_thread=False allows using the connection from multiple threads,
        # but we enforce safety with _lock anyway.
        self.path = database_path
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(database_path, timeout=timeout, check_same_thread=check_same_thread)
        self.conn.row_factory = sqlite3.Row
        # Optimize SQLite for concurrent reads/writes
        with self._lock:
            self.conn.execute("PRAGMA journal_mode=WAL;")
            self.conn.execute("PRAGMA synchronous=NORMAL;")
            self.conn.execute("PRAGMA foreign_keys=ON;")
            self.conn.commit()

    def init_db(self) -> None:
        """Create schema if it doesn't exist."""
        with self._lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def execute(self, sql: str, params: tuple = (), commit: bool = False) -> sqlite3.Cursor:
        """
        Execute a single statement and return the cursor.
        If commit=True, commits after execution.
        """
        with self._lock:
            cur = self.conn.execute(sql, params)
            if commit:
                self.conn.commit()
            return cur

    def executemany(self, sql: str, seq_of_params: List[tuple], commit: bool = False) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.executemany(sql, seq_of_params)
            if commit:
                self.conn.commit()
            return cur

    def query_all(self, sql: str, params: tuple = ()) -> List[sqlite3.Row]:
        with self._lock:
            cur = self.conn.execute(sql, params)
            rows = cur.fetchall()
            return rows

    def close(self) -> None:
        """Close the persistent connection. Call at application shutdown."""
        with self._lock:
            try:
                self.conn.commit()
            except Exception:
                pass
            finally:
                self.conn.close()


def get_database(database_path: str) -> Database:
    """
    Return a singleton Database instance for the given path.
    Repeated calls will return the same Database (so the connection remains open).
    """
    with _DB_INSTANCES_LOCK:
        db = _DB_INSTANCES.get(database_path)
        if db is None:
            db = Database(database_path)
            db.init_db()
            _DB_INSTANCES[database_path] = db
        return db


# Backward-compatible functions that use the persistent Database instance.
def init_db(database_path: str) -> None:
    """
    Initialize the database schema. Leaves the connection open (via the singleton instance).
    """
    db = get_database(database_path)
    db.init_db()


def create_analysis(database_path: str, name: str, path: str, status: str = "pending") -> int:
    db = get_database(database_path)
    cur = db.execute(
        "INSERT INTO analyses (name, path, status) VALUES (?, ?, ?)",
        (name, path, status),
        commit=True,
    )
    return cur.lastrowid


def update_analysis_status(database_path: str, analysis_id: int, status: str) -> None:
    db = get_database(database_path)
    db.execute("UPDATE analyses SET status = ? WHERE id = ?", (status, analysis_id), commit=True)


def update_analysis_counts(database_path: str, analysis_id: int, file_count: int, embedding_count: int) -> None:
    db = get_database(database_path)
    db.execute(
        "UPDATE analyses SET file_count = ?, embedding_count = ? WHERE id = ?",
        (file_count, embedding_count, analysis_id),
        commit=True,
    )


def store_file(database_path: str, analysis_id: int, path: str, content: str, language: str) -> int:
    db = get_database(database_path)
    cur = db.execute(
        "INSERT INTO files (analysis_id, path, content, language) VALUES (?, ?, ?, ?)",
        (analysis_id, path, content, language),
        commit=True,
    )
    return cur.lastrowid


def store_embedding(database_path: str, file_id: int, vector: Any) -> None:
    db = get_database(database_path)
    db.execute(
        "INSERT INTO embeddings (file_id, vector) VALUES (?, ?)",
        (file_id, json.dumps(vector)),
        commit=True,
    )


def list_analyses(database_path: str) -> List[Dict[str, Any]]:
    db = get_database(database_path)
    rows = db.query_all(
        "SELECT id, name, path, status, file_count, embedding_count, created_at FROM analyses ORDER BY id DESC"
    )
    results: List[Dict[str, Any]] = []
    for r in rows:
        results.append(
            {
                "id": r["id"],
                "name": r["name"],
                "path": r["path"],
                "status": r["status"],
                "file_count": r["file_count"],
                "embedding_count": r["embedding_count"],
                "created_at": r["created_at"],
            }
        )
    return results


def list_files_for_analysis(database_path: str, analysis_id: int) -> List[Dict[str, Any]]:
    db = get_database(database_path)
    rows = db.query_all(
        "SELECT id, path, snippet FROM files WHERE analysis_id = ? ORDER BY id DESC", (analysis_id,)
    )
    return [{"id": r["id"], "path": r["path"], "snippet": r["snippet"]} for r in rows]


def delete_analysis(database_path: str, analysis_id: int) -> None:
    """
    Delete an analysis and all related files and embeddings.
    Uses the persistent connection and keeps it open afterwards.
    """
    db = get_database(database_path)
    with db._lock:
        # Find file IDs for the analysis
        cur = db.conn.execute("SELECT id FROM files WHERE analysis_id = ?", (analysis_id,))
        file_ids = [r["id"] for r in cur.fetchall()]

        # Delete embeddings for those files
        if file_ids:
            db.conn.executemany("DELETE FROM embeddings WHERE file_id = ?", [(fid,) for fid in file_ids])

        # Delete files
        db.conn.execute("DELETE FROM files WHERE analysis_id = ?", (analysis_id,))

        # Delete analysis row
        db.conn.execute("DELETE FROM analyses WHERE id = ?", (analysis_id,))

        db.conn.commit()


def close_database(database_path: Optional[str] = None) -> None:
    """
    Close the persistent connection.
    - If database_path is provided, close that database's connection.
    - If None, close all opened connections.
    """
    with _DB_INSTANCES_LOCK:
        if database_path is None:
            keys = list(_DB_INSTANCES.keys())
            for k in keys:
                try:
                    _DB_INSTANCES[k].close()
                except Exception:
                    pass
                del _DB_INSTANCES[k]
        else:
            db = _DB_INSTANCES.pop(database_path, None)
            if db:
                try:
                    db.close()
                except Exception:
                    pass
