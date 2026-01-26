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

from llama_index.core.schema import TextNode
from llama_index.core.vector_stores import SimpleVectorStore

from .openai import call_coding_api
from .llama_embeddings import OpenAICompatibleEmbedding
from llama_index.core.node_parser import SimpleNodeParser
from llama_index.core import Document
from utils.logger import get_logger

import logging

logging.getLogger("httpx").setLevel(logging.WARNING)

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

CHUNK_SIZE = 800         # characters per chunk
CHUNK_OVERLAP = 100      # overlapping characters between chunks

EMBEDDING_CONCURRENCY = 4
EMBEDDING_BATCH_SIZE = 16  # Process embeddings in batches for better throughput
PROGRESS_LOG_INTERVAL = 10  # Log progress every N completed files
EMBEDDING_TIMEOUT = 15  # Reduced timeout in seconds for each embedding API call (including retries)
FILE_PROCESSING_TIMEOUT = 120  # Reduced timeout in seconds for processing a single file (2 minutes)

import os
cpu_count = os.cpu_count() or 1
_FILE_EXECUTOR_WORKERS = max(2, min(8, cpu_count // 2))
_EMBEDDING_EXECUTOR_WORKERS = max(2, min(8, cpu_count // 2))
_FILE_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=_FILE_EXECUTOR_WORKERS)
_EMBEDDING_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=_EMBEDDING_EXECUTOR_WORKERS)

logger = get_logger(__name__)

try:
    _embedding_client = OpenAICompatibleEmbedding()
except Exception as e:
    _embedding_client = None
    logger.warning(f"OpenAICompatibleEmbedding could not be initialized: {e}")

_thread_state = threading.local()


def _get_embedding_with_semaphore(semaphore: threading.Semaphore, text: str, file_path: str = "<unknown>", chunk_index: int = 0, model: Optional[str] = None):
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
        try:
            with open(full_path, "r", encoding="utf-8", errors="ignore") as fh:
                content = fh.read()
            mtime = os.path.getmtime(full_path)
        except Exception:
            return {"stored": False, "embedded": False, "skipped": False}

        if not content:
            return {"stored": False, "embedded": False, "skipped": False}

        lang = detect_language(rel_path)
        if lang == "text":
            return {"stored": False, "embedded": False, "skipped": False}

        import hashlib
        file_hash = hashlib.sha256(content.encode('utf-8')).hexdigest()
        
        if incremental and not needs_reindex(database_path, rel_path, mtime, file_hash):
            return {"stored": False, "embedded": False, "skipped": True}

        try:
            fid = store_file(database_path, rel_path, content, lang, mtime, file_hash)
        except Exception:
            logger.exception("Failed to store file %s", rel_path)
            return {"stored": False, "embedded": False, "skipped": False}

        _ = Document(text=content, extra_info={"path": rel_path, "lang": lang})

        embedding_model = None
        if isinstance(cfg, dict):
            embedding_model = cfg.get("embedding_model")

        parser = SimpleNodeParser(chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP)
        doc_obj = Document(text=content, extra_info={"path": rel_path, "lang": lang})
        nodes = parser.get_nodes_from_documents([doc_obj])
        chunks = [node.text for node in nodes if node.text]
        if not chunks:
            chunks = [content]

        _ = SimpleVectorStore()

        embedded_any = False

        chunk_tasks = []
        for idx, chunk in enumerate(chunks):
            chunk_doc = Document(text=chunk, extra_info={"path": rel_path, "lang": lang, "chunk_index": idx, "chunk_count": len(chunks)})
            chunk_tasks.append((idx, chunk_doc))

        num_batches = math.ceil(len(chunk_tasks) / EMBEDDING_BATCH_SIZE)
        for batch_num, batch_start in enumerate(range(0, len(chunk_tasks), EMBEDDING_BATCH_SIZE), 1):
            batch = chunk_tasks[batch_start:batch_start + EMBEDDING_BATCH_SIZE]
            
            # Collect texts for the batch and request embeddings in a single call
            batch_texts = [chunk_doc.text for _, chunk_doc in batch]
            try:
                batch_embeddings = _embedding_client._get_text_embeddings(batch_texts)
            except Exception as e:
                logger.exception("Batch embedding generation failed for %s: %s", rel_path, e)
                batch_embeddings = [None] * len(batch_texts)
            
            saved_count = 0
            failed_count = 0
            for (idx, chunk_doc), emb in zip(batch, batch_embeddings):
                if emb:
                    # Insert embedding into SQLite-vector database
                    from db.connection import db_connection
                    from db.vector_operations import insert_chunk_vector_with_retry
                    try:
                        with db_connection(database_path, enable_vector=True) as conn:
                            insert_chunk_vector_with_retry(conn, fid, rel_path, idx, emb)
                        saved_count += 1
                        embedded_any = True
                    except Exception as e:
                        failed_count += 1
                        logger.error(f"Failed to insert embedding into DB for {rel_path} chunk {idx}: {e}")
                else:
                    failed_count += 1
                    logger.error(f"Embedding missing for {rel_path} chunk {idx}")


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
    Simplified indexing pipeline using LlamaIndex ingestion.
    Collects Document objects for each source file and builds a VectorStoreIndex.
    """
    from db.operations import set_project_metadata
    from llama_index.core import VectorStoreIndex, Document
    from llama_index.core.node_parser import SimpleNodeParser
    from utils.simple_vector_store import get_vector_store
    from .llama_embeddings import OpenAICompatibleEmbedding
    
    start_time = time.time()
    
    try:
        set_project_metadata(database_path, "project_path", local_path)
    except Exception as e:
        logger.warning(f"Failed to store project path metadata: {e}")
    
    logger.info("Collecting files to index...")
    file_paths: List[Dict[str, str]] = []
    for root, _, files in os.walk(local_path):
        for fname in files:
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, local_path)
            try:
                if os.path.getsize(full) > max_file_size:
                    continue
            except Exception:
                continue
            file_paths.append({"full": full, "rel": rel})
    # Prioritize project files over dependencies (.venv, node_modules)
    project_files = []
    dep_files = []
    for entry in file_paths:
        rel_path = entry["rel"].replace(os.sep, "/")
        if ".venv/" in rel_path or "node_modules/" in rel_path:
            dep_files.append(entry)
        else:
            project_files.append(entry)
    file_paths = project_files + dep_files
    total_files = len(file_paths)
    logger.info(f"Found {total_files} files to index (project files first)")
    try:
        from db.operations import set_project_metadata
        set_project_metadata(database_path, "total_files", str(total_files))
    except Exception as e:
        logger.warning(f"Failed to store early total_files metadata: {e}")
    
    # Process files to generate and store embeddings
    semaphore = threading.Semaphore(EMBEDDING_CONCURRENCY)
    for f in file_paths:
        _process_file_sync(
            semaphore,
            database_path,
            f["full"],
            f["rel"],
            cfg,
            incremental=True,
        )
    
    # Collect documents for optional vector store index (if needed for other features)
    documents: List[Document] = []
    for f in file_paths:
        try:
            with open(f["full"], "r", encoding="utf-8", errors="ignore") as fh:
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
        set_project_metadata_batch(database_path, {
            "last_indexed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "last_index_duration": str(duration),
            "files_indexed": str(len(documents)),
            "total_files": str(total_files),
        })
    except Exception:
        logger.exception("Failed to store indexing metadata")
    
    try:
        store_file(
            database_path,
            "uv_detected.json",
            json.dumps(local_path, indent=2),
            "meta",
        )
    except Exception:
        pass
    
    return index


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
                "content": doc.text or ""  # Always include content for LLM context
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
