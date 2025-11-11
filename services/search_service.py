"""
Service layer for search operations.
Handles semantic search and query processing.
"""
from typing import Dict, Any, List, Optional

from ai.analyzer import search_semantic
from db.operations import get_project_by_id, get_project_stats
from utils.cache import search_cache
from utils.logger import get_logger
import hashlib

logger = get_logger(__name__)


class SearchService:
    """
    Service layer for search operations.
    Provides high-level search functionality with caching.
    """
    
    @staticmethod
    def semantic_search(
        project_id: str,
        query: str,
        top_k: int = 5,
        use_cache: bool = True,
        include_content: bool = True
    ) -> Dict[str, Any]:
        """
        Perform semantic search on a project.
        
        Args:
            project_id: Project identifier
            query: Search query text
            top_k: Number of results to return
            use_cache: Whether to use result caching
            include_content: Whether to include actual file content in results
        
        Returns:
            Dictionary with results, project_id, and query
        
        Raises:
            ValueError: If project not found or not indexed
        """
        # Validate project
        project = get_project_by_id(project_id)
        if not project:
            raise ValueError(f"Project not found: {project_id}")
        
        db_path = project["database_path"]
        
        # Check if indexed
        stats = get_project_stats(db_path)
        if stats.get("file_count", 0) == 0:
            raise ValueError(f"Project not indexed: {project_id}")
        
        # Check cache (only if content is not required, as content makes cache key complex)
        if use_cache and not include_content:
            cache_key = SearchService._make_cache_key(project_id, query, top_k)
            cached = search_cache.get(cache_key)
            if cached is not None:
                logger.debug(f"Cache hit for query: {query[:50]}")
                return cached
        
        # Perform search
        try:
            results = search_semantic(query, db_path, top_k=top_k, include_content=include_content)
            
            response = {
                "results": results,
                "project_id": project_id,
                "query": query,
                "count": len(results)
            }
            
            # Cache results (only if content not included to keep cache size reasonable)
            if use_cache and not include_content:
                search_cache.set(cache_key, response)
            
            logger.info(f"Search completed: {len(results)} results for '{query[:50]}'")
            return response
            
        except Exception as e:
            logger.error(f"Search failed: {e}")
            raise RuntimeError(f"Search failed: {e}") from e
    
    @staticmethod
    def _make_cache_key(project_id: str, query: str, top_k: int) -> str:
        """Generate cache key for search query using SHA-256."""
        key_str = f"{project_id}:{query}:{top_k}"
        key_hash = hashlib.sha256(key_str.encode()).hexdigest()[:16]  # Use first 16 chars
        return f"search:{key_hash}"
    
    @staticmethod
    def invalidate_cache(project_id: Optional[str] = None):
        """
        Invalidate search cache.
        
        Args:
            project_id: If provided, only invalidate for this project.
                       If None, clear entire cache.
        """
        if project_id is None:
            search_cache.clear()
            logger.info("Cleared entire search cache")
        else:
            # For now, just clear entire cache
            # Could be optimized to only clear specific project
            search_cache.clear()
            logger.info(f"Cleared search cache for project {project_id}")
    
    @staticmethod
    def get_cache_stats() -> Dict[str, Any]:
        """Get search cache statistics."""
        return search_cache.stats()
