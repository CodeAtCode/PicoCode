"""
Llama-index retriever wrapping existing search functionality.
Provides a clean query interface using llama-index patterns.
"""
from typing import Dict, Any, List
from llama_index.core.query import QueryEngine
from ai.llama_vector_store import SQLiteVectorStore
from ai.llama_embeddings import OpenAICompatibleEmbedding
from utils.logger import get_logger

logger = get_logger(__name__)


class LlamaIndexRetriever(QueryEngine):
    """Query engine using llama-index patterns with sqlite-vector backend."""
    
    def __init__(self, database_path: str):
        """Initialize retriever."""
        self.database_path = database_path
        
        # Initialize embedding client
        self.embedding_client = OpenAICompatibleEmbedding()
        
        # Initialize vector store
        self.vector_store = SQLiteVectorStore(database_path)
    
    def retrieve(self, query: str, top_k: int = 5):
        """Retrieve results."""
        try:
            from .llama_integration import llama_index_search
            
            results = llama_integration.search_semantic(
                query, 
                self.database_path, 
                top_k
            )
            
            return [{'content': r['content'], 'metadata': r} for r in results]
        except Exception as e:
            logger.exception(f"Retrieval failed: {e}")
            return []


def create_retriever(database_path: str):
    """Create retriever."""
    return LlamaIndexRetriever(database_path)
