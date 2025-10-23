import os
import sqlite3
from typing import Any, Dict, List, Optional

from config import CFG  # config (keeps chunk_size etc if needed)

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


def store_file(database_path: str, analysis_id: int, path: str, content: str, language: str) -> int:
    """
    Insert a file row. Returns the new file id.
    """
    conn = _get_connection(database_path)
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO files (analysis_id, path, content, language, snippet) VALUES (?, ?, ?, ?, ?)",
            (analysis_id, path, content, language, (content[:512] if content else "")),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


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
