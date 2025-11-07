"""
LlamaIndex integration for document retrieval.
"""
from typing import List
from llama_index.core import Document

from external_api import get_embedding_for_text
from utils.logger import get_logger

logger = get_logger(__name__)


def llama_index_retrieve_documents(query: str, database_path: str, top_k: int = 5, 
                                   search_func=None, get_chunk_func=None) -> List[Document]:
    """
    Return llama_index.core.Document objects for the top_k matching chunks using sqlite-vector.
    
    Args:
        query: Search query text
        database_path: Path to project database
        top_k: Number of results to return
        search_func: Function to search vectors (injected from analyzer)
        get_chunk_func: Function to get chunk text (injected from analyzer)
    
    Returns:
        List of Document objects with chunk text and metadata
    """
    if search_func is None or get_chunk_func is None:
        raise ValueError("search_func and get_chunk_func must be provided")
    
    q_emb = get_embedding_for_text(query)
    if not q_emb:
        return []

    rows = search_func(database_path, q_emb, top_k=top_k)
    docs: List[Document] = []
    for r in rows:
        fid = r.get("file_id")
        path = r.get("path")
        chunk_idx = r.get("chunk_index", 0)
        score = r.get("score", 0.0)
        chunk_text = get_chunk_func(database_path, fid, chunk_idx) or ""
        doc = Document(text=chunk_text, extra_info={"path": path, "file_id": fid, "chunk_index": chunk_idx, "score": score})
        docs.append(doc)
    return docs
