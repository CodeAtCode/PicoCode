"""
LlamaIndex integration for document retrieval.
Provides RAG functionality using llama-index with sqlite-vector backend.
"""
from typing import List
from llama_index.core import Document
from llama_index.core.vector_stores.types import VectorStoreQuery

from .llama_embeddings import OpenAICompatibleEmbedding
# Use LlamaIndex native SQLiteVectorStore
from llama_index.vector_stores import SQLiteVectorStore
from utils.logger import get_logger

logger = get_logger(__name__)

# Create a module-level embedding client instance
_embedding_client = OpenAICompatibleEmbedding()


def llama_index_search(query: str, database_path: str, top_k: int = 5) -> List[Document]:
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
        # Get query embedding using llama-index embedding client
        q_emb = _embedding_client._get_query_embedding(query)
        if not q_emb:
            logger.warning("Failed to generate query embedding")
            return []
        
        # Create vector store
        vector_store = SQLiteVectorStore(database_path)
        
        # Create query
        vector_query = VectorStoreQuery(
            query_embedding=q_emb,
            similarity_top_k=top_k
        )
        
        # Execute query
        query_result = vector_store.query(vector_query)
        
        # Convert TextNodes to Documents
        docs: List[Document] = []
        for node, score in zip(query_result.nodes, query_result.similarities):
            doc = Document(
                text=node.text,
                metadata={
                    **node.metadata,
                    "score": score
                }
            )
            docs.append(doc)
        
        logger.info(f"llama-index search returned {len(docs)} documents")
        return docs
        
    except Exception as e:
        logger.exception(f"llama-index search failed: {e}")
        return []


