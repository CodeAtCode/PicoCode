"""
Custom LlamaIndex Vector Store implementation using sqlite-vector.
This bridges llama-index's vector store interface with our sqlite-vector backend.
"""
from typing import List, Optional, Any, Dict
from llama_index.core.vector_stores.types import (
    VectorStore,
    VectorStoreQuery,
    VectorStoreQueryResult,
)
from llama_index.core.schema import TextNode, BaseNode

from db.vector_operations import search_vectors, get_chunk_text
from utils.logger import get_logger

logger = get_logger(__name__)


class SQLiteVectorStore(VectorStore):
    """
    Custom vector store implementation that uses sqlite-vector backend.
    Compatible with llama-index's VectorStore interface.
    """
    
    def __init__(self, database_path: str):
        """
        Initialize the SQLite vector store.
        
        Args:
            database_path: Path to the SQLite database with vector extension
        """
        self.database_path = database_path
        self._is_embedding_query = True
        logger.info(f"Initialized SQLiteVectorStore with database: {database_path}")
    
    @property
    def client(self) -> Any:
        """Return the database path as the client."""
        return self.database_path
    
    def add(self, nodes: List[BaseNode], **add_kwargs: Any) -> List[str]:
        """
        Add nodes to the vector store.
        Note: In our implementation, nodes are added during the indexing process
        via the analyzer module, not through this interface.
        """
        logger.warning("add() called on SQLiteVectorStore - nodes should be added via analyzer module")
        return []
    
    def delete(self, ref_doc_id: str, **delete_kwargs: Any) -> None:
        """Delete a document from the vector store."""
        logger.warning(f"delete() called on SQLiteVectorStore for {ref_doc_id} - not implemented")
        pass
    
    def query(
        self,
        query: VectorStoreQuery,
        **kwargs: Any,
    ) -> VectorStoreQueryResult:
        """
        Query the vector store.
        
        Args:
            query: VectorStoreQuery with query embedding and parameters
            
        Returns:
            VectorStoreQueryResult with nodes, similarities, and ids
        """
        if query.query_embedding is None:
            logger.error("Query embedding is None")
            return VectorStoreQueryResult(nodes=[], similarities=[], ids=[])
        
        # Get top_k from query, default to 5
        top_k = query.similarity_top_k or 5
        
        try:
            # Use our existing search_vectors function
            results = search_vectors(
                database_path=self.database_path,
                q_vector=query.query_embedding,
                top_k=top_k
            )
            
            nodes: List[TextNode] = []
            similarities: List[float] = []
            ids: List[str] = []
            
            for result in results:
                file_id = result["file_id"]
                path = result["path"]
                chunk_index = result["chunk_index"]
                score = result["score"]
                
                # Retrieve the actual chunk text
                chunk_text = get_chunk_text(self.database_path, file_id, chunk_index)
                
                if chunk_text:
                    # Create a TextNode for llama-index
                    node = TextNode(
                        text=chunk_text,
                        metadata={
                            "file_id": file_id,
                            "path": path,
                            "chunk_index": chunk_index,
                        },
                        id_=f"{file_id}_{chunk_index}"
                    )
                    
                    nodes.append(node)
                    similarities.append(score)
                    ids.append(node.id_)
            
            logger.debug(f"Vector query returned {len(nodes)} results")
            
            return VectorStoreQueryResult(
                nodes=nodes,
                similarities=similarities,
                ids=ids
            )
            
        except Exception as e:
            logger.exception(f"Error querying vector store: {e}")
            return VectorStoreQueryResult(nodes=[], similarities=[], ids=[])
    
    def persist(
        self,
        persist_path: str,
        fs: Optional[Any] = None,
    ) -> None:
        """
        Persist the vector store.
        Note: Our SQLite database is already persistent.
        """
        logger.debug("persist() called - SQLite database is already persistent")
        pass
