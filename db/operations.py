import hashlib
import os
from typing import Any

from utils.cache import project_cache, stats_cache
from utils.logger import get_logger
from utils.retry import retry_on_db_locked

from .connection import get_db_connection
from .db_writer import get_writer

_LOG = get_logger(__name__)

DB_TIMEOUT = 5.0
DB_RETRY_COUNT = 3
DB_RETRY_DELAY = 0.1


def _execute_query(database_path: str, sql: str, params: tuple = (), fetch: str = "one") -> Any:
    """
    Helper to execute a single query with proper connection handling.

    Args:
        database_path: Path to the database
        sql: SQL query to execute
        params: Query parameters
        fetch: 'one', 'all', or None for no fetch

    Returns:
        Query result based on fetch parameter
    """
    if not os.path.exists(database_path):
        return None

    conn = get_db_connection(database_path, timeout=DB_TIMEOUT, enable_wal=True)
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        if fetch == "one":
            return cur.fetchone()
        elif fetch == "all":
            return cur.fetchall()
        else:
            conn.commit()
            return cur.lastrowid if sql.strip().upper().startswith("INSERT") else None
    finally:
        conn.close()


def init_db(database_path: str) -> None:
    """
    Initialize database schema. Safe to call multiple times.
    Creates:
    - files (stores full content of indexed files with metadata for incremental indexing)
    - chunks (with embedding BLOB column for sqlite-vector)
    - project_metadata (project-level tracking)
    - vector_meta (stores vector dimension metadata needed for vector operations)
    - project_dependencies (cached dependencies per project)
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

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS vector_meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS dependency_usage (
                project_id TEXT NOT NULL,
                language TEXT NOT NULL,
                name TEXT NOT NULL,
                file_count INTEGER NOT NULL,
                PRIMARY KEY (project_id, language, name)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS project_dependencies (
                project_id TEXT NOT NULL,
                language TEXT NOT NULL,
                name TEXT NOT NULL,
                version TEXT,
                is_transitive INTEGER NOT NULL,
                PRIMARY KEY (project_id, language, name, is_transitive)
            )
            """
        )

        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e from e
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
    snippet = content[:512] if content else ""
    sql = """
    INSERT INTO files (path, language, snippet, last_modified, file_hash, updated_at) 
    VALUES (?, ?, ?, ?, ?, datetime('now'))
    ON CONFLICT(path) DO UPDATE SET 
        language=excluded.language,
        snippet=excluded.snippet,
        last_modified=excluded.last_modified,
        file_hash=excluded.file_hash,
        updated_at=datetime('now')
    RETURNING id
    """
    params = (path, language, snippet, last_modified, file_hash)

    # Initialize database if it doesn't exist
    if not os.path.exists(database_path):
        try:
            init_db(database_path)
            _LOG.info(f"Initialized new database: {database_path}")
        except Exception as e:
            _LOG.error(f"Failed to initialize database {database_path}: {e}")
            return None

    writer = get_writer(database_path)
    try:
        rowid = writer.enqueue_and_wait(sql, params, wait_timeout=60.0)
    except Exception as e:
        if "no such table" in str(e).lower():
            try:
                init_db(database_path)
                rowid = writer.enqueue_and_wait(sql, params, wait_timeout=300.0)
                _LOG.info(f"store_file succeeded after re-initializing DB {database_path}")
                return rowid
            except Exception as e2:
                _LOG.error(f"store_file retry failed for {database_path}: {e2}")
                return None
        _LOG.error(f"store_file error for {database_path}: {e}")
        return None
    try:
        stats_cache.invalidate(f"stats:{database_path}")
    except Exception:
        pass
    return rowid


def get_project_stats(database_path: str) -> dict[str, Any]:
    """
    Get statistics for a project database.
    Returns file_count and embedding_count.
    Uses caching with 60s TTL.
    """
    cache_key = f"stats:{database_path}"
    cached = stats_cache.get(cache_key)
    if cached is not None:
        return cached

    files_row = _execute_query(database_path, "SELECT COUNT(*) FROM files")
    chunks_row = _execute_query(database_path, "SELECT COUNT(*) FROM chunks WHERE embedding IS NOT NULL")

    stats = {"file_count": int(files_row[0]) if files_row else 0, "embedding_count": int(chunks_row[0]) if chunks_row else 0}
    stats_cache.set(cache_key, stats)
    return stats


def get_file_by_path(database_path: str, path: str) -> dict[str, Any] | None:
    """
    Get file metadata by path for incremental indexing checks.
    Returns None if file doesn't exist or the database/table is missing.
    """
    row = _execute_query(database_path, "SELECT id, path, last_modified, file_hash FROM files WHERE path = ?", (path,))
    if row:
        return {"id": row["id"], "path": row["path"], "last_modified": row["last_modified"], "file_hash": row["file_hash"]}
    return None


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
            (key, value),
        )
        conn.commit()
        project_cache.clear()
    except Exception as e:
        conn.rollback()
        raise e from e
    finally:
        conn.close()


def set_project_metadata_batch(database_path: str, metadata: dict[str, str]) -> None:
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
                (key, value),
            )
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e from e
    finally:
        conn.close()


def _compute_deps_hash(project_path: str) -> str:
    """Hash the contents of all manifest files that affect direct dependencies.
    Used to invalidate cached dependency rows when a manifest changes.
    """
    manifests = ["requirements.txt", "pyproject.toml", "package.json", "Cargo.toml", "go.mod", "pom.xml", "build.gradle"]
    h = hashlib.sha256()
    for name in manifests:
        path = os.path.join(project_path, name)
        if os.path.isfile(path):
            try:
                with open(path, "rb") as fh:
                    h.update(fh.read())
            except Exception:
                pass
    return h.hexdigest()


def _compute_all_deps_hash(project_path: str) -> str:
    """Hash the contents of all files (manifest + lock) that affect the full dependency set.
    Used to invalidate cached full‑dependency rows when any relevant file changes.
    """
    files = [
        "requirements.txt",
        "pyproject.toml",
        "package.json",
        "Cargo.toml",
        "Cargo.lock",
        "go.mod",
        "go.sum",
        "pom.xml",
        "build.gradle",
    ]
    h = hashlib.sha256()
    for name in files:
        path = os.path.join(project_path, name)
        if os.path.isfile(path):
            try:
                with open(path, "rb") as fh:
                    h.update(fh.read())
            except Exception:
                pass
    return h.hexdigest()


def store_project_dependencies(database_path: str, project_id: str, deps: dict, is_transitive: int) -> None:
    """Store dependency rows for a project.
    `deps` format: { language: [{"name":..., "version":...}, ...] }
    `is_transitive` should be 0 for direct only, 1 for full.
    Existing rows for the same project_id and is_transitive are removed first.
    """
    conn = get_db_connection(database_path, timeout=5.0, enable_wal=True)
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM project_dependencies WHERE project_id = ? AND is_transitive = ?", (project_id, is_transitive))
        for lang, items in deps.items():
            for item in items:
                name = item.get("name")
                version = item.get("version")
                cur.execute(
                    "INSERT OR REPLACE INTO project_dependencies (project_id, language, name, version, is_transitive) VALUES (?, ?, ?, ?, ?)",
                    (project_id, lang, name, version, is_transitive),
                )
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e from e
    finally:
        conn.close()


def load_cached_dependencies(database_path: str, project_id: str, is_transitive: int) -> dict:
    """Load cached dependencies for a project and flag. Returns dict[language] = list of deps.
    If no rows exist, returns empty dict.
    """
    conn = get_db_connection(database_path, timeout=5.0, enable_wal=True)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT language, name, version FROM project_dependencies WHERE project_id = ? AND is_transitive = ? ORDER BY is_transitive ASC, name ASC",
            (project_id, is_transitive),
        )
        rows = cur.fetchall()
        result: dict = {}
        for row in rows:
            lang = row["language"]
            entry = {"name": row["name"], "version": row["version"]}
            result.setdefault(lang, []).append(entry)
        return result
    finally:
        conn.close()


def clear_project_dependencies(database_path: str, project_id: str) -> None:
    """Remove all cached dependency rows for a project."""
    conn = get_db_connection(database_path, timeout=5.0, enable_wal=True)
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM project_dependencies WHERE project_id = ?", (project_id,))
        conn.commit()
    finally:
        conn.close()


def compute_dependency_usage(database_path: str, project_path: str, deps: dict) -> dict:
    """Compute how many indexed files belong to each dependency.
    Returns a nested dict: { language: { name: file_count, ... }, ... }
    Matching is done using a regex that looks for the dependency name as a path component
    to reduce false positives (e.g., matching "log" inside "catalog").
    """
    import re

    conn = get_db_connection(database_path, timeout=5.0, enable_wal=True)
    try:
        cur = conn.cursor()
        cur.execute("SELECT path FROM files")
        rows = cur.fetchall()
        file_paths = [row["path"] for row in rows]
    finally:
        conn.close()
    usage: dict = {}
    for lang, dep_list in deps.items():
        usage.setdefault(lang, {})
        for dep in dep_list:
            name = dep.get("name")
            if not name:
                continue
            pattern = re.compile(rf"[/\\]({re.escape(name)})(?:[-_][\w]+)?[/\\]", re.IGNORECASE)
            count = sum(1 for p in file_paths if pattern.search(p))
            usage[lang][name] = count
    return usage


def store_dependency_usage(database_path: str, project_id: str, usage: dict) -> None:
    """Store per‑dependency file usage counts.
    `usage` format: { language: { name: count, ... }, ... }
    Existing rows for the same project and language/name are replaced.
    """
    conn = get_db_connection(database_path, timeout=5.0, enable_wal=True)
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM dependency_usage WHERE project_id = ?", (project_id,))
        for lang, deps in usage.items():
            for name, count in deps.items():
                cur.execute(
                    "INSERT INTO dependency_usage (project_id, language, name, file_count) VALUES (?, ?, ?, ?)",
                    (project_id, lang, name, int(count)),
                )
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


def load_dependency_usage(database_path: str, project_id: str) -> dict:
    """Load dependency usage counts.
    Returns dict[language][name] = file_count (empty dict if none).
    """
    conn = get_db_connection(database_path, timeout=5.0, enable_wal=True)
    try:
        cur = conn.cursor()
        cur.execute("SELECT language, name, file_count FROM dependency_usage WHERE project_id = ?", (project_id,))
        rows = cur.fetchall()
        result: dict = {}
        for row in rows:
            lang = row["language"]
            name = row["name"]
            count = row["file_count"]
            result.setdefault(lang, {})[name] = count
        return result
    finally:
        conn.close()


def clear_project_data(database_path: str) -> None:
    """Delete all files, chunks, and vector metadata for a project, and clear cached dependencies.
    Used before a full re‑index to start from a clean state.
    Also invalidates the stats cache.
    """
    conn = get_db_connection(database_path, timeout=5.0, enable_wal=True)
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM chunks")
        cur.execute("DELETE FROM files")
        cur.execute("DELETE FROM vector_meta WHERE key = 'dimension'")
        conn.commit()
        stats_cache.invalidate(f"stats:{database_path}")
    except Exception:
        pass
    finally:
        conn.close()


def get_project_metadata(database_path: str, key: str) -> str | None:
    """
    Get a project metadata value by key.
    """
    row = _execute_query(database_path, "SELECT value FROM project_metadata WHERE key = ?", (key,))
    return row["value"] if row else None


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
        raise e from e
    finally:
        conn.close()


def create_project(project_path: str, name: str | None = None) -> dict[str, Any]:
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
        raise RuntimeError(f"Database initialization failed: {e}") from e

    if not project_path or not isinstance(project_path, str):
        raise ValueError("Project path must be a non-empty string")

    if ".." in project_path or project_path.startswith("~"):
        raise ValueError("Path traversal not allowed in project path")

    try:
        project_path = os.path.abspath(os.path.realpath(project_path))
    except Exception as e:
        raise ValueError(f"Invalid project path: {e}") from e

    try:
        path_exists = os.path.exists(project_path)  # nosec
        if not path_exists:
            raise ValueError("Project path does not exist")

        is_directory = os.path.isdir(project_path)  # nosec
        if not is_directory:
            raise ValueError("Project path is not a directory")
    except (OSError, ValueError) as e:
        if isinstance(e, ValueError):
            raise
        raise ValueError("Cannot access project path") from e

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
                (project_id, name, project_path, db_path),
            )
            conn.commit()

            try:
                init_db(db_path)
                _LOG.info(f"Created project {project_id} at {db_path}")
            except Exception as e:
                _LOG.error(f"Failed to initialize project database: {e}")
                cur.execute("DELETE FROM projects WHERE id = ?", (project_id,))

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


def get_project(project_path: str) -> dict[str, Any] | None:
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


def get_project_by_id(project_id: str) -> dict[str, Any] | None:
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


def list_projects() -> list[dict[str, Any]]:
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


def update_project_status(project_id: str, status: str, last_indexed_at: str | None = None):
    """Update project indexing status and invalidate cache."""
    _init_registry_db()

    registry_path = _get_projects_registry_path()

    @retry_on_db_locked(max_retries=DB_RETRY_COUNT, base_delay=DB_RETRY_DELAY)
    def _update():
        conn = get_db_connection(registry_path, timeout=5.0, enable_wal=True)
        try:
            cur = conn.cursor()
            if last_indexed_at:
                cur.execute("UPDATE projects SET status = ?, last_indexed_at = ? WHERE id = ?", (status, last_indexed_at, project_id))
            else:
                cur.execute("UPDATE projects SET status = ? WHERE id = ?", (status, project_id))
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise e from e
        finally:
            conn.close()

    _update()
    project_cache.invalidate(f"project:id:{project_id}")


def update_project_settings(project_id: str, settings: dict[str, Any]):
    """Update project settings (stored as JSON) and invalidate cache."""
    import json

    _init_registry_db()

    registry_path = _get_projects_registry_path()

    @retry_on_db_locked(max_retries=DB_RETRY_COUNT, base_delay=DB_RETRY_DELAY)
    def _update():
        conn = get_db_connection(registry_path, timeout=5.0, enable_wal=True)
        try:
            cur = conn.cursor()
            cur.execute("UPDATE projects SET settings = ? WHERE id = ?", (json.dumps(settings), project_id))
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise e from e
        finally:
            conn.close()

    _update()
    project_cache.invalidate(f"project:id:{project_id}")


def delete_project(project_id: str):
    """Delete a project and its database, invalidating cache.

    Also stops any active DBWriter workers for the project's database to avoid
    background indexing threads writing to a removed file, which can corrupt the
    SQLite file and produce ``NOT NULL constraint failed: chunks.file_id`` errors.
    """
    _init_registry_db()

    project = get_project_by_id(project_id)
    if not project:
        raise ValueError(f"Project not found: {project_id}")

    db_path = project.get("database_path")
    if db_path and os.path.exists(db_path):
        try:
            from db.db_writer import stop_writer

            stop_writer(db_path)
        except Exception:
            pass
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
            raise e from e
        finally:
            conn.close()

    _delete()
    project_cache.invalidate(f"project:id:{project_id}")
    if project.get("path"):
        project_cache.invalidate(f"project:path:{project['path']}")


def get_or_create_project(project_path: str, name: str | None = None) -> dict[str, Any]:
    """Get existing project or create new one."""
    project = get_project(project_path)
    if project:
        return project
    return create_project(project_path, name)
