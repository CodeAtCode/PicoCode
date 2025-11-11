import os
import json
import time
import traceback
import math
from pathlib import Path
from typing import Optional, Dict, Any, List

import concurrent.futures
import threading

from db.operations import store_file, needs_reindex, set_project_metadata_batch, get_project_metadata
from db.vector_operations import (
    connect_db as _connect_db,
    load_sqlite_vector_extension as _load_sqlite_vector_extension,
    ensure_chunks_and_meta as _ensure_chunks_and_meta,
    insert_chunk_vector_with_retry as _insert_chunk_vector_with_retry,
    search_vectors as _search_vectors,
    get_chunk_text as _get_chunk_text,
)
from .openai import call_coding_api, EmbeddingClient
from llama_index.core import Document
from utils.logger import get_logger
from utils import compute_file_hash, chunk_text, norm, cosine
from .smart_chunker import smart_chunk
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
PROGRESS_LOG_INTERVAL = 10  # Log progress every N completed files
# Timeout for future.result() must account for retries: (max_retries + 1) × SDK_timeout + buffer
# With SDK timeout of 15s and max_retries=2, this allows 3 × 15s = 45s + 15s buffer = 60s
EMBEDDING_TIMEOUT = 60  # Timeout in seconds for each embedding API call (including retries)
FILE_PROCESSING_TIMEOUT = 300  # Timeout in seconds for processing a single file (5 minutes)

_FILE_EXECUTOR_WORKERS = 4
_EMBEDDING_EXECUTOR_WORKERS = 4
_FILE_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=_FILE_EXECUTOR_WORKERS)
_EMBEDDING_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=_EMBEDDING_EXECUTOR_WORKERS)

logger = get_logger(__name__)

# Initialize EmbeddingClient for structured logging and retry logic
_embedding_client = EmbeddingClient()

# Thread-local storage to track execution state inside futures
_thread_state = threading.local()


def _get_embedding_with_semaphore(semaphore: threading.Semaphore, text: str, file_path: str = "<unknown>", chunk_index: int = 0, model: Optional[str] = None):
    """
    Wrapper to acquire semaphore inside executor task to avoid deadlock.
    The semaphore is acquired in the worker thread, not the main thread.
    Tracks execution state for debugging timeout issues.
    """
    # Initialize thread state tracking
    _thread_state.stage = "acquiring_semaphore"
    _thread_state.file_path = file_path
    _thread_state.chunk_index = chunk_index
    _thread_state.start_time = time.time()
    
    semaphore.acquire()
    try:
        _thread_state.stage = "calling_embed_text"
        result = _embedding_client.embed_text(text, file_path=file_path, chunk_index=chunk_index)
        _thread_state.stage = "completed"
        return result
    except Exception as e:
        _thread_state.stage = f"exception: {type(e).__name__}"
        _thread_state.exception = str(e)
        logger.error(f"Worker thread exception in embed_text for {file_path} chunk {chunk_index}: {e}")
        raise
    finally:
        _thread_state.stage = "releasing_semaphore"
        semaphore.release()
        _thread_state.stage = "finished"


def detect_language(path: str):
    if "LICENSE.md" in path:
        return "text"
    if "__editable__" in path:
        return "text"
    if "_virtualenv.py" in path:
        return "text"
    if "activate_this.py" in path:
        return "text"
    ext = Path(path).suffix.lower()
    return EXT_LANG.get(ext, "text")



# Main synchronous processing for a single file
def _process_file_sync(
    semaphore: threading.Semaphore,
    database_path: str,
    full_path: str,
    rel_path: str,
    cfg: Optional[Dict[str, Any]],
    incremental: bool = True,
    file_num: int = 0,
    total_files: int = 0,
):
    """
    Synchronous implementation of per-file processing.
    Intended to run on a ThreadPoolExecutor worker thread.
    Returns a dict: {"stored": bool, "embedded": bool, "skipped": bool}
    
    Args:
        file_num: The current file number being processed (1-indexed)
        total_files: Total number of files to process
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

        # Use smart chunking for supported code languages
        use_smart_chunking = cfg.get("smart_chunking", True) if isinstance(cfg, dict) else True
        supported_languages = ["python", "javascript", "typescript", "java", "go", "rust", "c", "cpp"]
        
        if use_smart_chunking and lang in supported_languages:
            chunks = smart_chunk(content, language=lang, chunk_size=CHUNK_SIZE, overlap=CHUNK_OVERLAP)
        else:
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
        num_batches = math.ceil(len(chunk_tasks) / EMBEDDING_BATCH_SIZE)
        for batch_num, batch_start in enumerate(range(0, len(chunk_tasks), EMBEDDING_BATCH_SIZE), 1):
            batch = chunk_tasks[batch_start:batch_start + EMBEDDING_BATCH_SIZE]
            
            # Log batch processing start
            batch_start_time = time.time()
            
            embedding_futures = []
            
            for idx, chunk_doc in batch:
                # Submit task to executor; semaphore will be acquired inside the worker
                embedding_start_time = time.time()
                future = _EMBEDDING_EXECUTOR.submit(_get_embedding_with_semaphore, semaphore, chunk_doc.text, rel_path, idx, embedding_model)
                embedding_futures.append((idx, chunk_doc, future, embedding_start_time))

            # Wait for batch to complete and store results
            saved_count = 0
            failed_count = 0
            for idx, chunk_doc, future, embedding_start_time in embedding_futures:
                try:
                    emb = future.result(timeout=EMBEDDING_TIMEOUT)  # Add timeout to prevent hanging indefinitely
                except concurrent.futures.TimeoutError:
                    elapsed = time.time() - embedding_start_time
                    
                    # Try to get exception info from the future if available
                    future_exception = None
                    try:
                        future_exception = future.exception(timeout=0.1)
                    except concurrent.futures.TimeoutError:
                        future_exception = None  # Still running
                    except Exception as e:
                        future_exception = e
                    
                    # Build diagnostic information
                    diagnostic_info = [
                        f"Future timeout ({EMBEDDING_TIMEOUT}s) for {rel_path} chunk {idx}:",
                        f"  - Elapsed time: {elapsed:.2f}s",
                        f"  - Future state: {future._state if hasattr(future, '_state') else 'unknown'}",
                    ]
                    
                    if future_exception:
                        diagnostic_info.append(f"  - Future exception: {type(future_exception).__name__}: {future_exception}")
                    else:
                        diagnostic_info.append(f"  - Future exception: None (still running or completed)")
                    
                    # Add information about running status
                    if future.running():
                        diagnostic_info.append(f"  - Future.running(): True - worker thread is still executing")
                    elif future.done():
                        diagnostic_info.append(f"  - Future.done(): True - worker thread completed but future.result() timed out retrieving result")
                    else:
                        diagnostic_info.append(f"  - Future status: Pending/Unknown")
                    
                    emb = None
                    failed_count += 1
                except Exception as e:
                    logger.exception("Embedding retrieval failed for %s chunk %d: %s", rel_path, idx, e)
                    emb = None
                    failed_count += 1

                if emb:
                    try:
                        db_start_time = time.time()
                        conn2 = _connect_db(database_path)
                        try:
                            _load_sqlite_vector_extension(conn2)
                            _insert_chunk_vector_with_retry(conn2, fid, rel_path, idx, emb)
                            saved_count += 1
                        finally:
                            conn2.close()
                        embedded_any = True
                    except Exception as e:
                        failed_count += 1
                        logger.error(f"Failed to insert chunk vector for {rel_path} chunk {idx}: {e}")
                else:
                    pass

        return {"stored": True, "embedded": embedded_any, "skipped": False}
    except Exception:
        logger.exception("Failed to process file %s", rel_path)
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
    from db.operations import set_project_metadata
    
    semaphore = threading.Semaphore(EMBEDDING_CONCURRENCY)
    start_time = time.time()
    
    # Store project path in metadata for filesystem access
    try:
        set_project_metadata(database_path, "project_path", local_path)
        logger.info(f"Starting indexing for project at: {local_path}")
    except Exception as e:
        logger.warning(f"Failed to store project path in metadata: {e}")
    
    try:
        file_count = 0
        emb_count = 0
        skipped_count = 0
        file_paths: List[Dict[str, str]] = []

        # Collect files to process
        logger.info("Collecting files to index...")
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
        
        total_files = len(file_paths)
        logger.info(f"Found {total_files} files to process")
        
        # Thread-safe counters: [submitted_count, completed_count, lock]
        counters = [0, 0, threading.Lock()]

        # Process files in chunks to avoid too many futures at once.
        CHUNK_SUBMIT = 256
        for chunk_start in range(0, len(file_paths), CHUNK_SUBMIT):
            chunk = file_paths[chunk_start : chunk_start + CHUNK_SUBMIT]
            futures = []
            for f in chunk:
                # Increment counter before starting file processing
                with counters[2]:
                    counters[0] += 1
                    file_num = counters[0]
                
                fut = _FILE_EXECUTOR.submit(
                    _process_file_sync,
                    semaphore,
                    database_path,
                    f["full"],
                    f["rel"],
                    cfg,
                    incremental,
                    file_num,
                    total_files,
                )
                futures.append(fut)

            for fut in concurrent.futures.as_completed(futures):
                try:
                    r = fut.result(timeout=FILE_PROCESSING_TIMEOUT)
                    
                    # Increment completed counter and check for periodic logging
                    with counters[2]:
                        counters[1] += 1
                        completed_count = counters[1]
                        should_log = completed_count % PROGRESS_LOG_INTERVAL == 0
                    
                    if isinstance(r, dict):
                        if r.get("stored"):
                            file_count += 1
                        if r.get("embedded"):
                            emb_count += 1
                        if r.get("skipped"):
                            skipped_count += 1
                        
                        # Log periodic progress updates (every 10 files)
                        if should_log:
                            logger.info(f"Progress: {completed_count}/{total_files} files processed ({file_count} stored, {emb_count} with embeddings, {skipped_count} skipped)")
                except concurrent.futures.TimeoutError:
                    logger.error(f"File processing timeout ({FILE_PROCESSING_TIMEOUT}s exceeded)")
                    with counters[2]:
                        counters[1] += 1
                except Exception:
                    logger.exception("A per-file task failed")

        # Store indexing metadata
        end_time = time.time()
        duration = end_time - start_time
        
        # Log summary
        logger.info(f"Indexing completed: {file_count} files processed, {emb_count} embeddings created, {skipped_count} files skipped in {duration:.2f}s")
        
        try:
            # Use batch update for efficiency - single database transaction
            set_project_metadata_batch(database_path, {
                "last_indexed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "last_index_duration": str(duration),
                "files_indexed": str(file_count),
                "files_skipped": str(skipped_count)
            })
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
            pass

    except Exception:
        logger.exception("Analysis failed for %s", local_path)


def analyze_local_path_background(local_path: str, database_path: str, venv_path: Optional[str] = None, max_file_size: int = 200000, cfg: Optional[dict] = None):
    """
    Wrapper intended to be scheduled by FastAPI BackgroundTasks.
    This function runs the synchronous analyzer in the FastAPI background task.
    Usage from FastAPI endpoint:
        background_tasks.add_task(analyze_local_path_background, local_path, database_path, venv_path, max_file_size, cfg)
    """
    try:
        analyze_local_path_sync(local_path, database_path, venv_path=venv_path, max_file_size=max_file_size, cfg=cfg)
    except Exception:
        logger.exception("Background analysis worker failed for %s", local_path)



def search_semantic(query: str, database_path: str, top_k: int = 5):
    """
    Uses sqlite-vector's vector_full_scan to retrieve best-matching chunks and returns
    a list of {file_id, path, chunk_index, score}.
    """
    q_emb = _embedding_client.embed_text(query, file_path="<query>", chunk_index=0)
    if not q_emb:
        return []

    try:
        return _search_vectors(database_path, q_emb, top_k=top_k)
    except Exception:
        raise


def call_coding_model(prompt: str, context: str = ""):
    combined = f"Context:\n{context}\n\nPrompt:\n{prompt}" if context else prompt
    return call_coding_api(combined)
