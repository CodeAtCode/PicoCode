"""
SQLite-vector database operations.
All sqlite-vector extension operations are centralized here.
The program will ALWAYS crash if the sqlite-vector extension fails to load (strict mode is mandatory).
"""
import os
import json
import time
import sqlite3
import importlib.resources
from typing import List, Dict, Any, Optional
from utils.logger import get_logger

logger = get_logger(__name__)

# sqlite-vector defaults (sensible fixed defaults)
SQLITE_VECTOR_PKG = "sqlite_vector.binaries"
SQLITE_VECTOR_RESOURCE = "vector"
SQLITE_VECTOR_VERSION_FN = "vector_version"      # SELECT vector_version();

# Retry policy for DB-locked operations
DB_LOCK_RETRY_COUNT = 6
DB_LOCK_RETRY_BASE_DELAY = 0.05  # seconds, exponential backoff multiplier


def connect_db(db_path: str, timeout: float = 30.0) -> sqlite3.Connection:
    """
    Create a database connection with appropriate timeout and settings.
    
    Args:
        db_path: Path to the SQLite database file
        timeout: Timeout in seconds for waiting on locks
        
    Returns:
        sqlite3.Connection object configured for vector operations
    """
    # timeout instructs sqlite to wait up to `timeout` seconds for locks
    conn = sqlite3.connect(db_path, timeout=timeout, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout = 30000;")  # 30s
    except Exception:
        pass
    return conn


def load_sqlite_vector_extension(conn: sqlite3.Connection) -> None:
    """
    Loads sqlite-vector binary from the installed python package and performs a lightweight
    sanity check (calls vector_version() if available).
    
    CRITICAL: This function will ALWAYS crash the program if the extension fails to load.
    STRICT mode is mandatory and cannot be disabled.
    
    NOTE: SQLite extensions are loaded per-connection, not per-process. This function must be
    called for each connection that needs vector operations.
    
    Args:
        conn: SQLite database connection
        
    Raises:
        RuntimeError: If the extension fails to load
    """
    try:
        ext_path = importlib.resources.files(SQLITE_VECTOR_PKG) / SQLITE_VECTOR_RESOURCE
        conn.load_extension(str(ext_path))
        logger.debug(f"sqlite-vector extension loaded for connection {id(conn)}")
        # optional quick check: call vector_version()
        try:
            cur = conn.execute(f"SELECT {SQLITE_VECTOR_VERSION_FN}()")
            _ = cur.fetchone()
        except Exception:
            # version function may not be present; ignore
            pass
    except Exception as e:
        raise RuntimeError(f"Failed to load sqlite-vector extension: {e}") from e


def ensure_chunks_and_meta(conn: sqlite3.Connection):
    """
    Create chunks table (if not exist) with embedding column and meta table for vector dimension.
    Safe to call multiple times.
    
    Args:
        conn: SQLite database connection
    """
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id INTEGER NOT NULL,
            path TEXT NOT NULL,
            chunk_index INTEGER NOT NULL,
            embedding BLOB,
            created_at TEXT DEFAULT (datetime('now'))
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
    conn.commit()


def set_vector_dimension(conn: sqlite3.Connection, dim: int):
    """
    Store the vector dimension in metadata table.
    
    Args:
        conn: SQLite database connection
        dim: Vector dimension to store
    """
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO vector_meta(key, value) VALUES('dimension', ?)", (str(dim),))
    conn.commit()


def insert_chunk_vector_with_retry(conn: sqlite3.Connection, file_id: int, path: str, chunk_index: int, vector: List[float]) -> int:
    """
    Insert a chunk row with embedding using vector_as_f32(json); retries on sqlite3.OperationalError 'database is locked'.
    
    Args:
        conn: SQLite database connection
        file_id: ID of the file this chunk belongs to
        path: File path
        chunk_index: Index of this chunk within the file
        vector: Embedding vector as list of floats
        
    Returns:
        The chunks.rowid of the inserted row
        
    Raises:
        RuntimeError: If vector operations fail or dimension mismatch occurs
    """
    cur = conn.cursor()
    # Ensure schema/meta present
    ensure_chunks_and_meta(conn)

    # dimension handling: store or verify
    cur.execute("SELECT value FROM vector_meta WHERE key = 'dimension'")
    row = cur.fetchone()
    dim = len(vector)
    if not row:
        set_vector_dimension(conn, dim)
        logger.info(f"Initialized vector dimension: {dim}")
        try:
            conn.execute(f"SELECT vector_init('chunks', 'embedding', 'dimension={dim},type=FLOAT32,distance=COSINE')")
            logger.debug(f"Vector index initialized for dimension {dim}")
        except Exception as e:
            logger.error(f"vector_init failed: {e}")
            raise RuntimeError(f"vector_init failed: {e}") from e
    else:
        stored_dim = int(row[0])
        if stored_dim != dim:
            logger.error(f"Embedding dimension mismatch: stored={stored_dim}, new={dim}")
            raise RuntimeError(f"Embedding dimension mismatch: stored={stored_dim}, new={dim}")

    q_vec = json.dumps(vector)

    attempt = 0
    while True:
        try:
            # use vector_as_f32(json) as per API so extension formats blob
            cur.execute("INSERT INTO chunks (file_id, path, chunk_index, embedding) VALUES (?, ?, ?, vector_as_f32(?))",
                        (file_id, path, chunk_index, q_vec))
            conn.commit()
            rowid = int(cur.lastrowid)
            logger.debug(f"Inserted chunk vector for {path} chunk {chunk_index}, rowid={rowid}")
            return rowid
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if "database is locked" in msg and attempt < DB_LOCK_RETRY_COUNT:
                attempt += 1
                delay = DB_LOCK_RETRY_BASE_DELAY * (2 ** (attempt - 1))
                logger.warning(f"Database locked, retrying in {delay}s (attempt {attempt}/{DB_LOCK_RETRY_COUNT})")
                time.sleep(delay)
                continue
            else:
                logger.error(f"Failed to insert chunk vector after {attempt} retries: {e}")
                raise RuntimeError(f"Failed to INSERT chunk vector (vector_as_f32 call): {e}") from e
        except Exception as e:
            logger.error(f"Failed to insert chunk vector: {e}")
            raise RuntimeError(f"Failed to INSERT chunk vector (vector_as_f32 call): {e}") from e


def search_vectors(database_path: str, q_vector: List[float], top_k: int = 5) -> List[Dict[str, Any]]:
    """
    Uses vector_full_scan to retrieve nearest neighbors from the chunks table.
    
    Args:
        database_path: Path to the SQLite database
        q_vector: Query vector as list of floats
        top_k: Number of top results to return
        
    Returns:
        List of dicts: {file_id, path, chunk_index, score}
        
    Raises:
        RuntimeError: If vector search operations fail
    """
    logger.debug(f"Searching vectors in database: {database_path}, top_k={top_k}")
    conn = connect_db(database_path)
    try:
        load_sqlite_vector_extension(conn)
        ensure_chunks_and_meta(conn)

        # Ensure vector index is initialized before searching
        cur = conn.cursor()
        cur.execute("SELECT value FROM vector_meta WHERE key = 'dimension'")
        row = cur.fetchone()
        if not row:
            # No dimension stored means no vectors have been indexed yet
            logger.info("No vector dimension found in metadata - no chunks indexed yet")
            return []
        
        dim = int(row[0])
        try:
            conn.execute(f"SELECT vector_init('chunks', 'embedding', 'dimension={dim},type=FLOAT32,distance=COSINE')")
            logger.debug(f"Vector index initialized for search with dimension {dim}")
        except Exception as e:
            logger.error(f"vector_init failed during search: {e}")
            raise RuntimeError(f"vector_init failed during search: {e}") from e

        q_json = json.dumps(q_vector)
        try:
            cur.execute(
                """
                SELECT c.file_id, c.path, c.chunk_index, v.distance
                FROM vector_full_scan('chunks', 'embedding', vector_as_f32(?), ?) AS v
                JOIN chunks AS c ON c.rowid = v.rowid
                ORDER BY v.distance ASC
                LIMIT ?
                """,
                (q_json, top_k, top_k),
            )
            rows = cur.fetchall()
            logger.debug(f"Vector search returned {len(rows)} results")
        except Exception as e:
            logger.error(f"Vector search failed: {e}")
            raise RuntimeError(f"vector_full_scan call failed: {e}") from e

        results: List[Dict[str, Any]] = []
        for file_id, path, chunk_index, distance in rows:
            try:
                score = 1.0 - float(distance)
            except Exception:
                score = float(distance)
            results.append({"file_id": int(file_id), "path": path, "chunk_index": int(chunk_index), "score": score})
        return results
    finally:
        conn.close()


def get_chunk_text(database_path: str, file_id: int, chunk_index: int) -> Optional[str]:
    """
    Get chunk text by reading from filesystem instead of database.
    Uses project_path metadata and file path to read the actual file.
    
    Args:
        database_path: Path to the SQLite database
        file_id: ID of the file
        chunk_index: Index of the chunk within the file
        
    Returns:
        The chunk text, or None if not found
    """
    from .operations import get_project_metadata
    
    conn = connect_db(database_path)
    try:
        cur = conn.cursor()
        # Get file path from database
        cur.execute("SELECT path FROM files WHERE id = ?", (file_id,))
        row = cur.fetchone()
        if not row:
            logger.warning(f"File not found in database: file_id={file_id}")
            return None
        
        file_path = row[0]
        if not file_path:
            logger.warning(f"File path is empty for file_id={file_id}")
            return None
        
        # Get project path from metadata
        project_path = get_project_metadata(database_path, "project_path")
        if not project_path:
            logger.error("Project path not found in metadata, cannot read file from filesystem")
            raise RuntimeError("Project path metadata is missing - ensure the indexing process has stored project metadata properly")
        
        # Construct full file path and resolve to absolute path
        full_path = os.path.abspath(os.path.join(project_path, file_path))
        normalized_project_path = os.path.abspath(project_path)
        
        # Security check: ensure the resolved path is within the project directory
        try:
            common = os.path.commonpath([full_path, normalized_project_path])
            if common != normalized_project_path:
                logger.error(f"Path traversal attempt detected: {file_path} resolves outside project directory")
                return None
            if full_path != normalized_project_path and not full_path.startswith(normalized_project_path + os.sep):
                logger.error(f"Path traversal attempt detected: {file_path} does not start with project directory")
                return None
        except ValueError:
            logger.error(f"Path traversal attempt detected: {file_path} is on a different drive or incompatible path")
            return None
        
        # Read file content from filesystem
        try:
            with open(full_path, "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read()
        except Exception as e:
            logger.warning(f"Failed to read file from filesystem: {full_path}, error: {e}")
            return None
        
        if not content:
            return None
        
        # Import chunk size parameters from analyzer module
        from ai.analyzer import CHUNK_SIZE, CHUNK_OVERLAP
        
        # Extract the chunk
        if CHUNK_SIZE <= 0:
            return content
        
        # Validate chunk_index
        if chunk_index < 0:
            logger.warning(f"Invalid chunk_index {chunk_index} for file_id={file_id}")
            return None
        
        step = max(1, CHUNK_SIZE - CHUNK_OVERLAP)
        start = chunk_index * step
        end = min(start + CHUNK_SIZE, len(content))
        return content[start:end]
    finally:
        conn.close()
