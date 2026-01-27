"""
LlamaIndex integration for document retrieval.
Provides RAG functionality using llama-index with sqlite-vector backend.
"""

from llama_index.core import Document

from db.vector_operations import get_chunk_text, search_vectors
from utils.logger import get_logger

from .llama_embeddings import OpenAICompatibleEmbedding

logger = get_logger(__name__)

_embedding_client = OpenAICompatibleEmbedding()


def llama_index_search(query: str, database_path: str, top_k: int = 5) -> list[Document]:
    """
    Perform semantic search using llama-index with sqlite-vector backend.

    Args:
        query: Search query text
        database_path: Path to project database
        top_k: Number of results to return

    Returns:
        List of Document objects with chunk text and metadata
    """
    try:
        q_emb = _embedding_client._get_query_embedding(query)
        if not q_emb:
            logger.warning("Failed to generate query embedding")
            return []

        results = search_vectors(database_path, q_emb, top_k=top_k)

        docs: list[Document] = []
        for result in results:
            file_id = result.get("file_id")
            path = result.get("path")
            chunk_index = result.get("chunk_index")
            score = result.get("score")
            chunk_text = get_chunk_text(database_path, file_id, chunk_index)
            if not chunk_text:
                continue
            doc = Document(
                text=chunk_text,
                metadata={
                    "file_id": file_id,
                    "path": path,
                    "chunk_index": chunk_index,
                    "score": score,
                },
            )
            docs.append(doc)

        logger.info(f"Custom search returned {len(docs)} documents")
        return docs

    except Exception as e:
        logger.exception(f"llama-index search failed: {e}")
        return []
