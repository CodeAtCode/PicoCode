import os
import sqlite3
import json
from typing import Any, Dict, List, Optional

# Simple connection helper: we open new connections per operation so the code is robust
# across threads. We set WAL journal mode for safer concurrency.
def _get_connection(db_path: str) -> sqlite3.Connection:
    dirname = os.path.dirname(os.path.abspath(db_path))
    if dirname and not os.path.isdir(dirname):
        os.makedirs(dirname, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
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
    - analyses
    - files
    - embeddings
    - chunks (with embedding BLOB column for sqlite-vector)
    """
    conn = _get_connection(database_path)
    try:
        cur = conn.cursor()
        # analyses table
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS analyses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                path TEXT NOT NULL,
                status TEXT NOT NULL,
                file_count INTEGER DEFAULT 0,
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

        # embeddings: audit/backup store of embeddings as JSON text
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS embeddings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id INTEGER NOT NULL,
                vector TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_embeddings_file ON embeddings(file_id);")

        # chunks table: metadata for chunked documents; includes embedding BLOB column
        # which sqlite-vector will operate on (via vector_as_f32 / vector_full_scan / vector_init, etc).
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


def update_analysis_counts(database_path: str, analysis_id: int, file_count: int, embedding_count: int) -> None:
    conn = _get_connection(database_path)
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE analyses SET file_count = ?, embedding_count = ? WHERE id = ?",
            (file_count, embedding_count, analysis_id),
        )
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


def store_embedding(database_path: str, file_id: int, vector: Any) -> None:
    """
    Store an embedding in the embeddings audit table as JSON text.
    Keep semantics backward-compatible: external code expects this to exist.
    """
    conn = _get_connection(database_path)
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO embeddings (file_id, vector) VALUES (?, ?)",
            (file_id, json.dumps(vector)),
        )
        conn.commit()
    finally:
        conn.close()


def insert_chunk_row_with_null_embedding(database_path: str, file_id: int, path: str, chunk_index: int) -> int:
    """
    Convenience to insert a chunk metadata row without populating embedding column.
    Returns the new chunks.id.
    (Typically you will later update chunks.embedding using the sqlite-vector API or via
    an INSERT that uses vector_as_f32(...) as done in analyzer._insert_chunk_vector.)
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
    conn = _get_connection(database_path)
    try:
        cur = conn.cursor()
        rows = cur.execute(
            "SELECT id, name, path, status, file_count, embedding_count, created_at FROM analyses ORDER BY id DESC"
        ).fetchall()
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
    Delete an analysis and cascade-delete associated files / embeddings / chunks.
    SQLite foreign keys require PRAGMA foreign_keys = ON for enforcement; do explicit deletes
    for safety across SQLite builds.
    """
    conn = _get_connection(database_path)
    try:
        cur = conn.cursor()
        # delete embeddings for files in analysis
        cur.execute(
            "DELETE FROM embeddings WHERE file_id IN (SELECT id FROM files WHERE analysis_id = ?)",
            (analysis_id,),
        )
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
