import os
import json
import time
import traceback
import sqlite3
import importlib.resources
import hashlib
from pathlib import Path
from typing import Optional, Dict, Any, List

import concurrent.futures
import threading

from db import store_file, needs_reindex, set_project_metadata, get_project_metadata
from external_api import get_embedding_for_text, call_coding_api
from llama_index.core import Document
from logger import get_logger
import logging

# reduce noise from httpx used by external libs
logging.getLogger("httpx").setLevel(logging.WARNING)

# language detection by extension
EXT_LANG = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".java": "java",
    ".go": "go",
    ".rs": "rust",
    ".c": "c",
    ".cpp": "cpp",
    ".h": "c",
    ".html": "html",
    ".css": "css",
    ".md": "markdown",
}

# Chunking parameters (tunable)
CHUNK_SIZE = 800         # characters per chunk
CHUNK_OVERLAP = 100      # overlapping characters between chunks

EMBEDDING_CONCURRENCY = 4
# Increase batch size for parallel processing
EMBEDDING_BATCH_SIZE = 16  # Process embeddings in batches for better throughput
_THREADPOOL_WORKERS = max(16, EMBEDDING_CONCURRENCY + 8)
_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=_THREADPOOL_WORKERS)

# sqlite-vector defaults (sensible fixed defaults)
SQLITE_VECTOR_PKG = "sqlite_vector.binaries"
SQLITE_VECTOR_RESOURCE = "vector"
SQLITE_VECTOR_VERSION_FN = "vector_version"      # SELECT vector_version();

# Strict behavior: fail fast if extension can't be loaded or calls fail
STRICT_VECTOR_INTEGRATION = True

# Retry policy for DB-locked operations
DB_LOCK_RETRY_COUNT = 6
DB_LOCK_RETRY_BASE_DELAY = 0.05  # seconds, exponential backoff multiplier

logger = get_logger(__name__)


def detect_language(path: str):
    if "LICENSE.md" in path:
        return "text"
    if "__editable__" in path:
        return "text"
    if "_virtualenv.py" in path:
        return "text"
    ext = Path(path).suffix.lower()
    return EXT_LANG.get(ext, "text")


def compute_file_hash(content: str) -> str:
    """
    Compute SHA256 hash of file content for change detection.
    """
    return hashlib.sha256(content.encode('utf-8')).hexdigest()


# Simple chunker (character-based). Tunable CHUNK_SIZE, CHUNK_OVERLAP.
def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> List[str]:
    if chunk_size <= 0:
        return [text]
    step = max(1, chunk_size - overlap)
    chunks: List[str] = []
    start = 0
    L = len(text)
    while start < L:
        end = min(start + chunk_size, L)
        chunks.append(text[start:end])
        start += step
    return chunks


# --- sqlite-vector integration helpers ---------------------------------------
def _connect_db(db_path: str, timeout: float = 30.0) -> sqlite3.Connection:
    # timeout instructs sqlite to wait up to `timeout` seconds for locks
    conn = sqlite3.connect(db_path, timeout=timeout, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout = 30000;")  # 30s
    except Exception:
        pass
    return conn


def _load_sqlite_vector_extension(conn: sqlite3.Connection) -> None:
    """
    Loads sqlite-vector binary from the installed python package and performs a lightweight
    sanity check (calls vector_version() if available). Raises on error if STRICT_VECTOR_INTEGRATION.
    """
    try:
        ext_path = importlib.resources.files(SQLITE_VECTOR_PKG) / SQLITE_VECTOR_RESOURCE
        conn.load_extension(str(ext_path))
        try:
            conn.enable_load_extension(False)
        except Exception:
            pass
        # optional quick check: call vector_version()
        try:
            cur = conn.execute(f"SELECT {SQLITE_VECTOR_VERSION_FN}()")
            _ = cur.fetchone()
        except Exception:
            # version function may not be present; ignore
            pass
    except Exception as e:
        if STRICT_VECTOR_INTEGRATION:
            raise RuntimeError(f"Failed to load sqlite-vector extension: {e}") from e
        else:
            logger.warning("sqlite-vector extension not loaded: %s", e)


def _ensure_chunks_and_meta(conn: sqlite3.Connection):
    """
    Create chunks table (if not exist) with embedding column and meta table for vector dimension.
    Safe to call multiple times.
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


def _set_vector_dimension(conn: sqlite3.Connection, dim: int):
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO vector_meta(key, value) VALUES('dimension', ?)", (str(dim),))
    conn.commit()


def _insert_chunk_vector_with_retry(conn: sqlite3.Connection, file_id: int, path: str, chunk_index: int, vector: List[float]) -> int:
    """
    Insert a chunk row with embedding using vector_as_f32(json); retries on sqlite3.OperationalError 'database is locked'.
    Returns the chunks.rowid.
    """
    cur = conn.cursor()
    # Ensure schema/meta present
    _ensure_chunks_and_meta(conn)

    # dimension handling: store or verify
    cur.execute("SELECT value FROM vector_meta WHERE key = 'dimension'")
    row = cur.fetchone()
    dim = len(vector)
    if not row:
        _set_vector_dimension(conn, dim)
        try:
            conn.execute(f"SELECT vector_init('chunks', 'embedding', 'dimension={dim},type=FLOAT32,distance=COSINE')")
        except Exception as e:
            raise RuntimeError(f"vector_init failed: {e}") from e
    else:
        stored_dim = int(row[0])
        if stored_dim != dim:
            raise RuntimeError(f"Embedding dimension mismatch: stored={stored_dim}, new={dim}")

    q_vec = json.dumps(vector)

    attempt = 0
    while True:
        try:
            # use vector_as_f32(json) as per API so extension formats blob
            cur.execute("INSERT INTO chunks (file_id, path, chunk_index, embedding) VALUES (?, ?, ?, vector_as_f32(?))",
                        (file_id, path, chunk_index, q_vec))
            conn.commit()
            return int(cur.lastrowid)
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if "database is locked" in msg and attempt < DB_LOCK_RETRY_COUNT:
                attempt += 1
                delay = DB_LOCK_RETRY_BASE_DELAY * (2 ** (attempt - 1))
                time.sleep(delay)
                continue
            else:
                raise RuntimeError(f"Failed to INSERT chunk vector (vector_as_f32 call): {e}") from e
        except Exception as e:
            raise RuntimeError(f"Failed to INSERT chunk vector (vector_as_f32 call): {e}") from e


def _search_vectors(database_path: str, q_vector: List[float], top_k: int = 5) -> List[Dict[str, Any]]:
    """
    Uses vector_full_scan to retrieve nearest neighbors from the chunks table.
    Returns list of dicts: {file_id, path, chunk_index, score}
    """
    conn = _connect_db(database_path)
    try:
        _load_sqlite_vector_extension(conn)
        _ensure_chunks_and_meta(conn)

        q_json = json.dumps(q_vector)
        cur = conn.cursor()
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
        except Exception as e:
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


def _get_chunk_text(database_path: str, file_id: int, chunk_index: int) -> Optional[str]:
    conn = _connect_db(database_path)
    try:
        cur = conn.cursor()
        cur.execute("SELECT content FROM files WHERE id = ?", (file_id,))
        row = cur.fetchone()
        if not row:
            return None
        content = row[0] or ""
        if CHUNK_SIZE <= 0:
            return content
        step = max(1, CHUNK_SIZE - CHUNK_OVERLAP)
        start = chunk_index * step
        end = min(start + CHUNK_SIZE, len(content))
        return content[start:end]
    finally:
        conn.close()


# Main synchronous processing for a single file
def _process_file_sync(
    semaphore: threading.Semaphore,
    database_path: str,
    full_path: str,
    rel_path: str,
    cfg: Optional[Dict[str, Any]],
    incremental: bool = True,
):
    """
    Synchronous implementation of per-file processing.
    Intended to run on a ThreadPoolExecutor worker thread.
    Returns a dict: {"stored": bool, "embedded": bool, "skipped": bool}
    """
    try:
        # read file content
        try:
            with open(full_path, "r", encoding="utf-8", errors="ignore") as fh:
                content = fh.read()
            # Get file modification time
            mtime = os.path.getmtime(full_path)
        except Exception:
            return {"stored": False, "embedded": False, "skipped": False}

        if not content:
            return {"stored": False, "embedded": False, "skipped": False}

        lang = detect_language(rel_path)
        if lang == "text":
            return {"stored": False, "embedded": False, "skipped": False}

        # Compute hash for change detection
        file_hash = compute_file_hash(content)
        
        # Check if file needs reindexing (incremental mode)
        if incremental and not needs_reindex(database_path, rel_path, mtime, file_hash):
            return {"stored": False, "embedded": False, "skipped": True}

        # store file (synchronous DB writer) with metadata
        try:
            fid = store_file(database_path, rel_path, content, lang, mtime, file_hash)
        except Exception:
            logger.exception("Failed to store file %s", rel_path)
            return {"stored": False, "embedded": False, "skipped": False}

        _ = Document(text=content, extra_info={"path": rel_path, "lang": lang})

        embedding_model = None
        if isinstance(cfg, dict):
            embedding_model = cfg.get("embedding_model")

        chunks = chunk_text(content, chunk_size=CHUNK_SIZE, overlap=CHUNK_OVERLAP)
        if not chunks:
            chunks = [content]

        # Ensure extension present and tables created
        conn_test = _connect_db(database_path)
        try:
            _load_sqlite_vector_extension(conn_test)
            _ensure_chunks_and_meta(conn_test)
        finally:
            conn_test.close()

        embedded_any = False

        # Collect all chunks first for batch processing
        chunk_tasks = []
        for idx, chunk in enumerate(chunks):
            chunk_doc = Document(text=chunk, extra_info={"path": rel_path, "lang": lang, "chunk_index": idx, "chunk_count": len(chunks)})
            chunk_tasks.append((idx, chunk_doc))

        # Process embeddings in parallel batches for better throughput
        for batch_start in range(0, len(chunk_tasks), EMBEDDING_BATCH_SIZE):
            batch = chunk_tasks[batch_start:batch_start + EMBEDDING_BATCH_SIZE]
            embedding_futures = []
            
            for idx, chunk_doc in batch:
                # Acquire semaphore to bound concurrent embedding requests
                semaphore.acquire()
                try:
                    future = _EXECUTOR.submit(get_embedding_for_text, chunk_doc.text, embedding_model)
                    embedding_futures.append((idx, chunk_doc, future))
                except Exception:
                    semaphore.release()
                    raise

            # Wait for batch to complete and store results
            for idx, chunk_doc, future in embedding_futures:
                try:
                    emb = future.result()  # This will re-raise any exception from the worker
                except Exception as e:
                    logger.exception("Embedding retrieval failed for %s chunk %d: %s", rel_path, idx, e)
                    emb = None
                finally:
                    semaphore.release()

                if emb:
                    try:
                        conn2 = _connect_db(database_path)
                        try:
                            _load_sqlite_vector_extension(conn2)
                            _insert_chunk_vector_with_retry(conn2, fid, rel_path, idx, emb)
                        finally:
                            conn2.close()
                        embedded_any = True
                    except Exception as e:
                        try:
                            err_content = f"Failed to insert chunk vector: {e}\n\nTraceback:\n{traceback.format_exc()}"
                            print(err_content)
                        except Exception:
                            logger.exception("Failed to write chunk-insert error to disk for %s chunk %d", rel_path, idx)
                else:
                    try:
                        err_content = "Embedding API returned no vector for chunk."
                        print(err_content)
                    except Exception:
                        logger.exception("Failed to write empty-embedding error to disk for %s chunk %d", rel_path, idx)

        return {"stored": True, "embedded": embedded_any, "skipped": False}
    except Exception:
        tb = traceback.format_exc()
        try:
            error_payload = {"file": rel_path, "error": "processing error", "traceback": tb[:2000]}
            try:
                print(error_payload)
            except Exception:
                logger.exception("Failed to write exception error to disk for file %s", rel_path)
        except Exception:
            logger.exception("Failed while handling exception for file %s", rel_path)
        return {"stored": False, "embedded": False, "skipped": False}


def analyze_local_path_sync(
    local_path: str,
    database_path: str,
    venv_path: Optional[str] = None,
    max_file_size: int = 200000,
    cfg: Optional[dict] = None,
    incremental: bool = True,
):
    """
    Synchronous implementation of the analysis pipeline.
    Submits per-file tasks to a shared ThreadPoolExecutor.
    Supports incremental indexing to skip unchanged files.
    """
    semaphore = threading.Semaphore(EMBEDDING_CONCURRENCY)
    start_time = time.time()
    
    try:
        file_count = 0
        emb_count = 0
        skipped_count = 0
        file_paths: List[Dict[str, str]] = []

        # Collect files to process
        for root, dirs, files in os.walk(local_path):
            for fname in files:
                full = os.path.join(root, fname)
                rel = os.path.relpath(full, local_path)
                try:
                    size = os.path.getsize(full)
                    if size > max_file_size:
                        continue
                except Exception:
                    continue
                file_paths.append({"full": full, "rel": rel})

        # Process files in chunks to avoid too many futures at once.
        CHUNK_SUBMIT = 256
        for chunk_start in range(0, len(file_paths), CHUNK_SUBMIT):
            chunk = file_paths[chunk_start : chunk_start + CHUNK_SUBMIT]
            futures = []
            for f in chunk:
                fut = _EXECUTOR.submit(
                    _process_file_sync,
                    semaphore,
                    database_path,
                    f["full"],
                    f["rel"],
                    cfg,
                    incremental,
                )
                futures.append(fut)

            for fut in concurrent.futures.as_completed(futures):
                try:
                    r = fut.result()
                    if isinstance(r, dict):
                        if r.get("stored"):
                            file_count += 1
                        if r.get("embedded"):
                            emb_count += 1
                        if r.get("skipped"):
                            skipped_count += 1
                except Exception:
                    logger.exception("A per-file task failed")

        # Store indexing metadata
        end_time = time.time()
        duration = end_time - start_time
        
        try:
            set_project_metadata(database_path, "last_indexed_at", time.strftime("%Y-%m-%d %H:%M:%S"))
            set_project_metadata(database_path, "last_index_duration", str(duration))
            set_project_metadata(database_path, "files_indexed", str(file_count))
            set_project_metadata(database_path, "files_skipped", str(skipped_count))
        except Exception:
            logger.exception("Failed to store indexing metadata")

        # store uv_detected.json metadata if possible
        uv_info = None
        try:
            uv_info = None if local_path is None else local_path
        except Exception:
            uv_info = None

        try:
            # Metadata storage is non-critical, ignore return value
            _ = store_file(
                database_path,
                "uv_detected.json",
                json.dumps(uv_info, indent=2),
                "meta",
            )
        except Exception:
            try:
                print("Failed to store uv_detected.json in DB")
            except Exception:
                logger.exception("Failed to write uv_detected meta error")

    except Exception:
        traceback.print_exc()


def analyze_local_path_background(local_path: str, database_path: str, venv_path: Optional[str] = None, max_file_size: int = 200000, cfg: Optional[dict] = None):
    """
    Non-blocking wrapper intended to be scheduled by FastAPI BackgroundTasks.
    This function starts a daemon thread which runs the synchronous analyzer and returns immediately.
    Usage from FastAPI endpoint:
        background_tasks.add_task(analyze_local_path_background, local_path, database_path, venv_path, max_file_size, cfg)
    """
    def _worker():
        try:
            analyze_local_path_sync(local_path, database_path, venv_path=venv_path, max_file_size=max_file_size, cfg=cfg)
        except Exception:
            logger.exception("Background analysis worker failed for %s", local_path)

    t = threading.Thread(target=_worker, name=f"analysis-worker-{os.path.basename(local_path)}", daemon=True)
    t.start()


# Simple synchronous helpers preserved for compatibility --------------------------------
def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def norm(a):
    import math
    return math.sqrt(sum(x * x for x in a))


def cosine(a, b):
    na = norm(a)
    nb = norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return sum(x * y for x, y in zip(a, b)) / (na * nb)


def search_semantic(query: str, database_path: str, top_k: int = 5):
    """
    Uses sqlite-vector's vector_full_scan to retrieve best-matching chunks and returns
    a list of {file_id, path, chunk_index, score}.
    """
    q_emb = get_embedding_for_text(query)
    if not q_emb:
        return []

    try:
        return _search_vectors(database_path, q_emb, top_k=top_k)
    except Exception:
        raise


def call_coding_model(prompt: str, context: str = ""):
    combined = f"Context:\n{context}\n\nPrompt:\n{prompt}" if context else prompt
    return call_coding_api(combined)


# llama-index helper ---------------------------------------------------------
def llama_index_retrieve_documents(query: str, database_path: str, top_k: int = 5) -> List[Document]:
    """
    Return llama_index.core.Document objects for the top_k matching chunks using sqlite-vector.
    """
    q_emb = get_embedding_for_text(query)
    if not q_emb:
        return []

    rows = _search_vectors(database_path, q_emb, top_k=top_k)
    docs: List[Document] = []
    for r in rows:
        fid = r.get("file_id")
        path = r.get("path")
        chunk_idx = r.get("chunk_index", 0)
        score = r.get("score", 0.0)
        chunk_text = _get_chunk_text(database_path, fid, chunk_idx) or ""
        doc = Document(text=chunk_text, extra_info={"path": path, "file_id": fid, "chunk_index": chunk_idx, "score": score})
        docs.append(doc)
    return docs
