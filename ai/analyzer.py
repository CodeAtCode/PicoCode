import concurrent.futures
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from llama_index.core import Document
from llama_index.core.node_parser import SimpleNodeParser
from llama_index.core.vector_stores import SimpleVectorStore

from db.operations import (
    needs_reindex,
    store_file,
)
from utils.logger import get_logger

from .llama_embeddings import OpenAICompatibleEmbedding
from .openai import call_coding_api

logging.getLogger("httpx").setLevel(logging.WARNING)


def compute_file_hash(file_path: str) -> str:
    """Compute MD5 hash of a file for change detection."""
    import hashlib

    try:
        with open(file_path, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()
    except Exception:
        return ""


EXCLUDE_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".tox",
    ".idea",
    ".vscode",
    "dist",
    "build",
    ".eggs",
    "*.egg-info",
}

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
    "requirements.txt": "python-deps",
    "pyproject.toml": "python-deps",
    "package.json": "javascript-deps",
    "Cargo.toml": "rust-deps",
    "Cargo.lock": "rust-deps",
    "go.mod": "go-deps",
    "go.sum": "go-deps",
    "pom.xml": "java-deps",
    "build.gradle": "java-deps",
}

CHUNK_SIZE = 800
CHUNK_OVERLAP = 100

EMBEDDING_CONCURRENCY = 4
EMBEDDING_BATCH_SIZE = 16  # Process embeddings in batches for better throughput
PROGRESS_LOG_INTERVAL = 10  # Log progress every N completed files
EMBEDDING_TIMEOUT = 15  # Reduced timeout in seconds for each embedding API call (including retries)
FILE_PROCESSING_TIMEOUT = 120  # Reduced timeout in seconds for processing a single file (2 minutes)

cpu_count = os.cpu_count() or 1
_FILE_EXECUTOR_WORKERS = max(2, min(8, cpu_count // 2))
_EMBEDDING_EXECUTOR_WORKERS = max(2, min(8, cpu_count // 2))
_FILE_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=_FILE_EXECUTOR_WORKERS)
_EMBEDDING_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=_EMBEDDING_EXECUTOR_WORKERS)

logger = get_logger(__name__)


def _should_index_file(rel_path: str, max_file_size: int) -> bool:
    """
    Check if a file should be indexed based on extension and size.

    Args:
        rel_path: Relative path of the file
        max_file_size: Maximum file size in bytes

    Returns:
        True if file should be indexed, False otherwise
    """
    # Check file extension
    ext = os.path.splitext(rel_path)[1].lower()
    filename = os.path.basename(rel_path)

    # Support both by extension and by filename
    if ext not in EXT_LANG and filename not in EXT_LANG:
        return False

    # Check excluded directories
    for excluded in EXCLUDE_DIRS:
        if excluded in rel_path:
            return False

    return True


try:
    _embedding_client = OpenAICompatibleEmbedding()
except Exception as e:
    _embedding_client = None
    logger.warning(f"OpenAICompatibleEmbedding could not be initialized: {e}")

_thread_state = threading.local()


def _get_embedding_with_semaphore(semaphore: threading.Semaphore, text: str, file_path: str = "<unknown>", chunk_index: int = 0, model: str | None = None):
    """
    Wrapper to acquire semaphore inside executor task to avoid deadlock.
    The semaphore is acquired in the worker thread, not the main thread.
    Tracks execution state for debugging timeout issues.
    """
    _thread_state.stage = "acquiring_semaphore"
    _thread_state.file_path = file_path
    _thread_state.chunk_index = chunk_index
    _thread_state.start_time = time.time()

    semaphore.acquire()
    try:
        _thread_state.stage = "calling_embed_text"
        if _embedding_client is None:
            logger.error("Embedding client not initialized; cannot generate embedding.")
            raise RuntimeError("Embedding client not initialized")
        result = _embedding_client._get_text_embedding(text)
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
    """Detect language or dependency type based on file name or extension.

    First checks the base filename against EXT_LANG (allows entries like
    "requirements.txt" or "package.json"). If not found, falls back to the
    file extension mapping.
    """
    if "LICENSE.md" in path:
        return "text"
    if "__editable__" in path:
        return "text"
    if "_virtualenv.py" in path:
        return "text"
    if "activate_this.py" in path:
        return "text"
    filename = os.path.basename(path)
    if filename in EXT_LANG:
        return EXT_LANG[filename]
    ext = Path(path).suffix.lower()
    return EXT_LANG.get(ext, "text")


def _process_file_sync(
    semaphore: threading.Semaphore,
    database_path: str,
    full_path: str,
    rel_path: str,
    cfg: dict,
    incremental: bool = False,
) -> dict:
    """
    Process a single file: store metadata, chunk, embed, and persist chunks/vectors.
    """
    from llama_index.core import Document
    from llama_index.core.node_parser import SimpleNodeParser
    from db.connection import db_connection
    from db.vector_operations import insert_chunk_vector_with_retry
    from db.operations import store_file
    from .llama_embeddings import OpenAICompatibleEmbedding

    start_time = time.time()

    try:
        with open(full_path, encoding="utf-8", errors="ignore") as fh:
            content = fh.read()
    except Exception as e:
        logger.error(f"Failed to read file {full_path}: {e}")
        return {"stored": False, "embedded": False, "skipped": False}

    if not content:
        return {"stored": False, "embedded": False, "skipped": True}

    lang = detect_language(rel_path)
    mtime = os.path.getmtime(full_path)
    file_hash = compute_file_hash(full_path)

    try:
        fid = store_file(database_path, rel_path, content, lang, mtime, file_hash)
    except Exception:
        logger.exception("Failed to store file %s", rel_path)
        return {"stored": False, "embedded": False, "skipped": False}

    if not fid:
        logger.error(f"Database error while storing file (file_id is None): {rel_path}")
        return {"stored": False, "embedded": False, "skipped": False}

    try:
        parser = SimpleNodeParser(chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP)
        doc_obj = Document(text=content, extra_info={"path": rel_path, "lang": lang})
        nodes = parser.get_nodes_from_documents([doc_obj])
        chunks = [node.text for node in nodes if node.text]
        if not chunks:
            chunks = [content]

        embedded_any = False
        chunk_tasks = []
        for idx, chunk in enumerate(chunks):
            chunk_doc = Document(text=chunk, extra_info={"path": rel_path, "lang": lang, "chunk_index": idx, "chunk_count": len(chunks)})
            chunk_tasks.append((idx, chunk_doc))

        for batch_start in range(0, len(chunk_tasks), EMBEDDING_BATCH_SIZE):
            batch = chunk_tasks[batch_start : batch_start + EMBEDDING_BATCH_SIZE]
            batch_texts = [chunk_doc.text for _, chunk_doc in batch]

            try:
                batch_embeddings = _embedding_client._get_text_embeddings(batch_texts)
            except Exception as e:
                logger.exception("Batch embedding generation failed for %s: %s", rel_path, e)
                batch_embeddings = [None] * len(batch_texts)

            for (idx, _chunk_doc), emb in zip(batch, batch_embeddings, strict=True):
                if emb:
                    try:
                        with db_connection(database_path) as conn:
                            insert_chunk_vector_with_retry(conn, fid, rel_path, idx, emb)
                        embedded_any = True
                    except Exception as e:
                        logger.error(f"Failed to insert embedding into DB for {rel_path} chunk {idx}: {e}")
                else:
                    logger.error(f"Embedding missing for {rel_path} chunk {idx}")

        return {"stored": True, "embedded": embedded_any, "skipped": False}
    except AttributeError as e:
        if "nltk" in str(e).lower() or "punkt" in str(e).lower() or "stopwords" in str(e).lower():
            logger.error(f"NLTK data missing for {rel_path}. Please run: python3 -c \"import nltk; nltk.download('punkt'); nltk.download('stopwords')\"")
        else:
            logger.exception("Failed to process file %s", rel_path)
        return {"stored": True, "embedded": False, "skipped": False}
    except Exception:
        logger.exception("Failed to process file %s", rel_path)
        return {"stored": True, "embedded": False, "skipped": False}


def analyze_local_path_sync(
    local_path: str,
    database_path: str,
    venv_path: str | None = None,
    max_file_size: int = 200000,
    cfg: dict | None = None,
    incremental: bool = False,
) -> tuple:
    """
    Synchronous version to analyze and index a local path.

    Args:
        local_path: Root path to analyze
        database_path: SQLite database path for storing metadata and embeddings
        venv_path: Path to virtual environment (optional)
        max_file_size: Maximum file size to process
        cfg: Configuration dictionary
        incremental: Whether to perform incremental indexing

    Returns:
        Tuple of (index, excluded_paths)
    """
    import time

    start_time = time.time()
    logger.info(f"Starting synchronous analysis of {local_path}")

    # Reuse the existing implementation from analyze_local_path_background
    # but adapted for synchronous execution
    from llama_index.core import Document, VectorStoreIndex
    from llama_index.core.node_parser import SimpleNodeParser
    from llama_index.core.vector_stores.simple import SimpleVectorStore
    from ai.llama_embeddings import OpenAICompatibleEmbedding
    from db.operations import set_project_metadata, store_file
    import json

    excluded_paths = []
    file_paths = []
    local_path = str(Path(local_path).resolve())

    for root, dirs, files in os.walk(local_path):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for f in files:
            full = os.path.join(root, f)
            rel = os.path.relpath(full, local_path).replace(os.sep, "/")
            if _should_index_file(rel, max_file_size):
                file_paths.append({"full": full, "rel": rel})

    # Separate project files from dependencies
    project_files = []
    dep_files = []
    for entry in file_paths:
        rel_path = entry["rel"].replace(os.sep, "/")
        if ".git/" not in rel_path:
            if ".venv/" in rel_path or "node_modules/" in rel_path:
                dep_files.append(entry)
            else:
                project_files.append(entry)

    # By default, only index project files
    file_paths = project_files
    total_files = len(file_paths)
    logger.info(f"Found {total_files} files to index (project files only)")

    # Process files in parallel using ThreadPoolExecutor
    semaphore = threading.Semaphore(EMBEDDING_CONCURRENCY)
    batch_size = 10
    total_processed = 0

    def process_file_batch(file_batch):
        """Process a batch of files."""
        results = []
        for f in file_batch:
            try:
                result = _process_file_sync(
                    semaphore,
                    database_path,
                    f["full"],
                    f["rel"],
                    cfg or {},
                    incremental=incremental,
                )
                results.append(result)
            except Exception as e:
                logger.error(f"Failed to process {f['rel']}: {e}")
                results.append(None)
        return results

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = []
        for i in range(0, len(file_paths), batch_size):
            batch = file_paths[i : i + batch_size]
            future = executor.submit(process_file_batch, batch)
            futures.append(future)

        for future in as_completed(futures):
            try:
                results = future.result()
                total_processed += len([r for r in results if r is not None])
                logger.info(f"Processed {total_processed}/{total_files} files")
            except Exception as e:
                logger.error(f"Batch processing error: {e}")

    logger.info(f"Completed processing {total_processed} files for embedding")

    # Create documents for indexing
    documents: list[Document] = []
    for f in file_paths:
        try:
            with open(f["full"], encoding="utf-8", errors="ignore") as fh:
                content = fh.read()
            if not content:
                continue
            lang = detect_language(f["rel"])
            doc = Document(text=content, extra_info={"path": f["rel"], "lang": lang})
            documents.append(doc)
        except Exception:
            logger.exception("Failed to read %s", f["full"])

    vector_store = SimpleVectorStore()
    embed_model = OpenAICompatibleEmbedding()
    parser = SimpleNodeParser(chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP)
    index = VectorStoreIndex.from_documents(
        documents,
        vector_store=vector_store,
        embed_model=embed_model,
        node_parser=parser,
    )

    duration = time.time() - start_time
    logger.info(f"Indexing completed: {len(documents)} documents indexed in {duration:.2f}s")

    try:
        from db.operations import set_project_metadata_batch

        set_project_metadata_batch(
            database_path,
            {
                "last_indexed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "last_index_duration": str(duration),
                "files_indexed": str(len(documents)),
                "total_files": str(total_files),
            },
        )
    except Exception:
        logger.exception("Failed to store indexing metadata")

    return index, excluded_paths


def analyze_local_path_background(local_path: str, database_path: str, venv_path: str | None = None, max_file_size: int = 200000, cfg: dict | None = None):
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
    Uses llama-index with sqlite-vector backend to retrieve best-matching chunks.
    Always includes content as it's needed for the coding model context.

    Args:
        query: Search query text
        database_path: Path to the SQLite database
        top_k: Number of results to return

    Returns:
        List of dicts with file_id, path, chunk_index, score, and content
    """
    try:
        from .llama_integration import llama_index_search

        docs = llama_index_search(query, database_path, top_k=top_k)

        results = []
        for doc in docs:
            metadata = doc.metadata or {}
            result = {
                "file_id": metadata.get("file_id", 0),
                "path": metadata.get("path", ""),
                "chunk_index": metadata.get("chunk_index", 0),
                "score": metadata.get("score", 0.0),
                "content": doc.text or "",  # Always include content for LLM context
            }
            results.append(result)

        logger.info(f"llama-index search returned {len(results)} results")
        return results

    except Exception as e:
        logger.exception(f"Semantic search failed: {e}")
        raise


def call_coding_model(prompt: str, context: str = ""):
    combined = f"Context:\n{context}\n\nPrompt:\n{prompt}" if context else prompt
    return call_coding_api(combined)


def analyze_dependencies_sync(
    local_path: str,
    database_path: str,
    venv_path: str | None,
    max_file_size: int,
    cfg: dict,
    incremental: bool = False,
) -> None:
    """
    Index direct dependencies (Phase 2).
    This should be called after analyze_local_path_sync completes.
    Only indexes files in .venv/ and node_modules/ directories.

    Args:
        local_path: Root path of the project (used to find dependency directories)
        database_path: Path to the SQLite database
        venv_path: Path to virtual environment (if applicable)
        max_file_size: Maximum file size to process
        cfg: Configuration dictionary
        incremental: Whether to perform incremental indexing
    """
    import time

    start_time = time.time()
    logger.info(f"Starting Phase 2: Indexing direct dependencies for {local_path}")

    # Collect dependency files only
    file_paths = []

    # Python dependencies in .venv
    if venv_path and os.path.exists(venv_path):
        for root, dirs, files in os.walk(venv_path):
            # Skip non-essential directories
            dirs[:] = [d for d in dirs if d not in {"__pycache__", ".git", "test", "tests"}]
            for f in files:
                full = os.path.join(root, f)
                rel = os.path.relpath(full, local_path).replace(os.sep, "/")
                if _should_index_file(rel, max_file_size):
                    file_paths.append({"full": full, "rel": rel})

    # Node.js dependencies
    node_modules = os.path.join(local_path, "node_modules")
    if os.path.exists(node_modules):
        for root, dirs, files in os.walk(node_modules):
            # Skip non-essential directories
            dirs[:] = [d for d in dirs if d not in {".git", "test", "tests", "docs"}]
            for f in files:
                full = os.path.join(root, f)
                rel = os.path.relpath(full, local_path).replace(os.sep, "/")
                if _should_index_file(rel, max_file_size):
                    file_paths.append({"full": full, "rel": rel})

    total_files = len(file_paths)
    logger.info(f"Found {total_files} dependency files to index")

    if total_files == 0:
        logger.info("No dependency files to index")
        return

    # Process files in parallel batches
    semaphore = threading.Semaphore(EMBEDDING_CONCURRENCY)
    batch_size = 10
    total_processed = 0

    def process_file_batch(file_batch):
        """Process a batch of dependency files."""
        results = []
        for f in file_batch:
            try:
                result = _process_file_sync(
                    semaphore,
                    database_path,
                    f["full"],
                    f["rel"],
                    cfg,
                    incremental=incremental,
                )
                results.append(result)
            except Exception as e:
                logger.error(f"Failed to process dependency {f['rel']}: {e}")
                results.append(None)
        return results

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = []
        for i in range(0, len(file_paths), batch_size):
            batch = file_paths[i : i + batch_size]
            future = executor.submit(process_file_batch, batch)
            futures.append(future)

        for future in as_completed(futures):
            try:
                results = future.result()
                total_processed += len([r for r in results if r is not None])
                logger.info(f"Indexed {total_processed}/{total_files} dependency files")
            except Exception as e:
                logger.error(f"Dependency batch processing error: {e}")

    elapsed = time.time() - start_time
    logger.info(f"Phase 2 complete: Indexed {total_processed}/{total_files} dependency files in {elapsed:.1f}s")
