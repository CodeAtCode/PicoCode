import os
import sqlite3
from typing import Any, Dict, List, Optional
from functools import lru_cache
import threading

from utils.config import CFG  # config (keeps chunk_size etc if needed)
from utils.logger import get_logger
from utils.cache import project_cache, stats_cache, file_cache
from utils.retry import retry_on_db_locked
from .db_writer import get_writer
from .connection import get_db_connection

_LOG = get_logger(__name__)






def init_db(database_path: str) -> None:
    """
    Initialize database schema. Safe to call multiple times.
    Creates:
    - files (stores full content of indexed files with metadata for incremental indexing)
    - chunks (with embedding BLOB column for sqlite-vector)
    - project_metadata (project-level tracking)
    """
    conn = get_db_connection(database_path, timeout=5.0, enable_wal=True)
    try:
        cur = conn.cursor()
        
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL UNIQUE,
                language TEXT,
                snippet TEXT,
                last_modified REAL,
                file_hash TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_files_path ON files(path);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_files_hash ON files(file_hash);")

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
        
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS project_metadata (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


def store_file(database_path, path, content, language, last_modified=None, file_hash=None):
    """
    Insert or update a file record into the DB using a queued single-writer to avoid
    sqlite 'database is locked' errors in multithreaded scenarios.
    Supports incremental indexing with last_modified and file_hash tracking.
    Note: Does not store full file content in database (only snippet), content is read from filesystem when needed.
    The content parameter is still required to generate the snippet.
    Returns lastrowid (same as the previous store_file implementation).
    """
    snippet = (content[:512] if content else "")
    sql = """
        INSERT INTO files (path, language, snippet, last_modified, file_hash, updated_at) 
        VALUES (?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(path) DO UPDATE SET 
            language=excluded.language,
            snippet=excluded.snippet,
            last_modified=excluded.last_modified,
            file_hash=excluded.file_hash,
            updated_at=datetime('now')
    """
    params = (path, language, snippet, last_modified, file_hash)

    writer = get_writer(database_path)
    return writer.enqueue_and_wait(sql, params, wait_timeout=60.0)


def insert_chunk_row_with_null_embedding(database_path: str, file_id: int, path: str, chunk_index: int) -> int:
    """
    Insert a chunk metadata row without populating embedding column.
    Returns the new chunks.id.
    """
    conn = get_db_connection(database_path, timeout=5.0, enable_wal=True)
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO chunks (file_id, path, chunk_index) VALUES (?, ?, ?)",
            (file_id, path, chunk_index),
        )
        try:
            conn.commit()
            return int(cur.lastrowid)
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()
    finally:
        conn.close()


def get_project_stats(database_path: str) -> Dict[str, Any]:
    """
    Get statistics for a project database.
    Returns file_count and embedding_count.
    Uses caching with 60s TTL.
    """
    cache_key = f"stats:{database_path}"
    cached = stats_cache.get(cache_key)
    if cached is not None:
        return cached
    
    conn = get_db_connection(database_path, timeout=5.0, enable_wal=True)
    try:
        cur = conn.cursor()
        
        cur.execute("SELECT COUNT(*) FROM files")
        file_count = cur.fetchone()[0]
        
        cur.execute("SELECT COUNT(*) FROM chunks WHERE embedding IS NOT NULL")
        embedding_count = cur.fetchone()[0]
        
        stats = {
            "file_count": int(file_count),
            "embedding_count": int(embedding_count)
        }
        
        stats_cache.set(cache_key, stats)
        return stats
    finally:
        conn.close()


def list_files(database_path: str) -> List[Dict[str, Any]]:
    """
    List all files in a project database.
    """
    conn = get_db_connection(database_path, timeout=5.0, enable_wal=True)
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
    Invalidates caches.
    """
    conn = get_db_connection(database_path, timeout=5.0, enable_wal=True)
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM chunks")
        cur.execute("DELETE FROM files")
        cur.execute("DELETE FROM vector_meta WHERE key = 'dimension'")

        conn.commit()
        stats_cache.invalidate(f"stats:{database_path}")
        file_cache.clear()  # Clear all file cache since we deleted everything
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


def get_file_by_path(database_path: str, path: str) -> Optional[Dict[str, Any]]:
    """
    Get file metadata by path for incremental indexing checks.
    Returns None if file doesn't exist.
    """
    conn = get_db_connection(database_path, timeout=5.0, enable_wal=True)
    try:
        row = conn.execute(
            "SELECT id, path, last_modified, file_hash FROM files WHERE path = ?",
            (path,)
        ).fetchone()
        if row:
            return {
                "id": row["id"],
                "path": row["path"],
                "last_modified": row["last_modified"],
                "file_hash": row["file_hash"]
            }
        return None
    finally:
        conn.close()


def needs_reindex(database_path: str, path: str, current_mtime: float, current_hash: str) -> bool:
    """
    Check if a file needs to be re-indexed based on modification time and hash.
    Returns True if file is new or has changed.
    """
    existing = get_file_by_path(database_path, path)
    if not existing:
        return True
    
    if existing["last_modified"] is None or existing["file_hash"] is None:
        return True
    
    if existing["last_modified"] != current_mtime or existing["file_hash"] != current_hash:
        return True
    
    return False


def set_project_metadata(database_path: str, key: str, value: str) -> None:
    """
    Set a project metadata key-value pair and invalidate cache.
    """
    conn = get_db_connection(database_path, timeout=5.0, enable_wal=True)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO project_metadata (key, value, updated_at) 
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(key) DO UPDATE SET 
                value=excluded.value,
                updated_at=datetime('now')
            """,
            (key, value)
        )
        conn.commit()
        project_cache.clear()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


def set_project_metadata_batch(database_path: str, metadata: Dict[str, str]) -> None:
    """
    Set multiple project metadata key-value pairs in a single transaction.
    More efficient than multiple set_project_metadata calls.
    
    Args:
        database_path: Path to the database
        metadata: Dictionary of key-value pairs to set
    """
    conn = get_db_connection(database_path, timeout=5.0, enable_wal=True)
    try:
        cur = conn.cursor()
        for key, value in metadata.items():
            cur.execute(
                """
                INSERT INTO project_metadata (key, value, updated_at) 
                VALUES (?, ?, datetime('now'))
                ON CONFLICT(key) DO UPDATE SET 
                    value=excluded.value,
                    updated_at=datetime('now')
                """,
                (key, value)
            )
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


def get_project_metadata(database_path: str, key: str) -> Optional[str]:
    """
    Get a project metadata value by key.
    """
    conn = get_db_connection(database_path, timeout=5.0, enable_wal=True)
    try:
        row = conn.execute(
            "SELECT value FROM project_metadata WHERE key = ?",
            (key,)
        ).fetchone()
        return row["value"] if row else None
    finally:
        conn.close()


def delete_file_by_path(database_path: str, path: str) -> None:
    """
    Delete a file and its chunks by path.
    Used for incremental indexing when files are removed.
    """
    conn = get_db_connection(database_path, timeout=5.0, enable_wal=True)
    try:
        cur = conn.cursor()
        row = cur.execute("SELECT id FROM files WHERE path = ?", (path,)).fetchone()
        if row:
            file_id = row["id"]
            cur.execute("DELETE FROM chunks WHERE file_id = ?", (file_id,))
            cur.execute("DELETE FROM files WHERE id = ?", (file_id,))

        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()



PROJECTS_DIR = os.path.expanduser("~/.picocode/projects")

DB_RETRY_COUNT = 3
DB_RETRY_DELAY = 0.1  # seconds


def _ensure_projects_dir():
    """Ensure projects directory exists."""
    try:
        os.makedirs(PROJECTS_DIR, exist_ok=True)
    except Exception as e:
        _LOG.error(f"Failed to create projects directory {PROJECTS_DIR}: {e}")
        raise


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


@retry_on_db_locked(max_retries=DB_RETRY_COUNT, base_delay=DB_RETRY_DELAY)
def _init_registry_db():
    """Initialize the projects registry database with proper configuration."""
    registry_path = _get_projects_registry_path()
    
    conn = get_db_connection(registry_path, timeout=5.0, enable_wal=True)
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
        conn.rollback()
        raise e
    finally:
        conn.close()


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
    
    if not project_path or not isinstance(project_path, str):
        raise ValueError("Project path must be a non-empty string")
    
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
    
    @retry_on_db_locked(max_retries=DB_RETRY_COUNT, base_delay=DB_RETRY_DELAY)
    def _create():
        conn = get_db_connection(registry_path, timeout=5.0, enable_wal=True)
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
            result = dict(row) if row else None
            if result:
                project_cache.set(f"project:id:{project_id}", result)
                project_cache.set(f"project:path:{project_path}", result)
            return result
        finally:
            conn.close()
    
    try:
        result = _create()
        return result
    except Exception as e:
        _LOG.error(f"Failed to create project: {e}")
        raise


def get_project(project_path: str) -> Optional[Dict[str, Any]]:
    """Get project metadata by path with caching."""
    _init_registry_db()
    project_path = os.path.abspath(project_path)
    
    cache_key = f"project:path:{project_path}"
    cached = project_cache.get(cache_key)
    if cached is not None:
        return cached
    
    registry_path = _get_projects_registry_path()
    
    @retry_on_db_locked(max_retries=DB_RETRY_COUNT, base_delay=DB_RETRY_DELAY)
    def _get():
        conn = get_db_connection(registry_path, timeout=5.0, enable_wal=True)
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM projects WHERE path = ?", (project_path,))
            row = cur.fetchone()
            result = dict(row) if row else None
            if result:
                project_cache.set(cache_key, result)
            return result
        finally:
            conn.close()
    
    return _get()


def get_project_by_id(project_id: str) -> Optional[Dict[str, Any]]:
    """Get project metadata by ID with caching."""
    _init_registry_db()
    
    cache_key = f"project:id:{project_id}"
    cached = project_cache.get(cache_key)
    if cached is not None:
        return cached
    
    registry_path = _get_projects_registry_path()
    
    @retry_on_db_locked(max_retries=DB_RETRY_COUNT, base_delay=DB_RETRY_DELAY)
    def _get():
        conn = get_db_connection(registry_path, timeout=5.0, enable_wal=True)
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
            row = cur.fetchone()
            result = dict(row) if row else None
            if result:
                project_cache.set(cache_key, result)
            return result
        finally:
            conn.close()
    
    return _get()


def list_projects() -> List[Dict[str, Any]]:
    """List all registered projects."""
    _init_registry_db()
    
    registry_path = _get_projects_registry_path()
    
    @retry_on_db_locked(max_retries=DB_RETRY_COUNT, base_delay=DB_RETRY_DELAY)
    def _list():
        conn = get_db_connection(registry_path, timeout=5.0, enable_wal=True)
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM projects ORDER BY created_at DESC")
            rows = cur.fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()
    
    return _list()


def update_project_status(project_id: str, status: str, last_indexed_at: Optional[str] = None):
    """Update project indexing status and invalidate cache."""
    _init_registry_db()
    
    registry_path = _get_projects_registry_path()
    
    @retry_on_db_locked(max_retries=DB_RETRY_COUNT, base_delay=DB_RETRY_DELAY)
    def _update():
        conn = get_db_connection(registry_path, timeout=5.0, enable_wal=True)
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
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()
    
    _update()
    project_cache.invalidate(f"project:id:{project_id}")


def update_project_settings(project_id: str, settings: Dict[str, Any]):
    """Update project settings (stored as JSON) and invalidate cache."""
    import json
    _init_registry_db()
    
    registry_path = _get_projects_registry_path()
    
    @retry_on_db_locked(max_retries=DB_RETRY_COUNT, base_delay=DB_RETRY_DELAY)
    def _update():
        conn = get_db_connection(registry_path, timeout=5.0, enable_wal=True)
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE projects SET settings = ? WHERE id = ?",
                (json.dumps(settings), project_id)
            )
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()
    
    _update()
    project_cache.invalidate(f"project:id:{project_id}")


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
    
    @retry_on_db_locked(max_retries=DB_RETRY_COUNT, base_delay=DB_RETRY_DELAY)
    def _delete():
        conn = get_db_connection(registry_path, timeout=5.0, enable_wal=True)
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM projects WHERE id = ?", (project_id,))
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()
    
    _delete()
    project_cache.invalidate(f"project:id:{project_id}")
    if project.get("path"):
        project_cache.invalidate(f"project:path:{project['path']}")


def get_or_create_project(project_path: str, name: Optional[str] = None) -> Dict[str, Any]:
    """Get existing project or create new one."""
    project = get_project(project_path)
    if project:
        return project
    return create_project(project_path, name)

