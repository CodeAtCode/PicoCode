"""
Service layer for search operations.
Handles semantic search and query processing.
"""

import hashlib
from typing import Any

from ai.analyzer import search_semantic
from db.operations import get_project_by_id, get_project_stats
from utils.cache import search_cache
from utils.logger import get_logger

logger = get_logger(__name__)


class SearchService:
    """
    Service layer for search operations.
    Provides high-level search functionality with caching.
    """

    @staticmethod
    def semantic_search(project_id: str, query: str, top_k: int = 5, use_cache: bool = True) -> dict[str, Any]:
        """
        Perform semantic search on a project.
        Content is always included as it's required for the coding model.

        Args:
            project_id: Project identifier
            query: Search query text
            top_k: Number of results to return
            use_cache: Whether to use result caching

        Returns:
            Dictionary with results (including content), project_id, and query

        Raises:
            ValueError: If project not found or not indexed
        """
        project = get_project_by_id(project_id)
        if not project:
            raise ValueError(f"Project not found: {project_id}")

        db_path = project["database_path"]

        stats = get_project_stats(db_path)
        if stats.get("file_count", 0) == 0:
            raise ValueError(f"Project not indexed: {project_id}")

        try:
            results = search_semantic(query, db_path, top_k=top_k)

            response = {"results": results, "project_id": project_id, "query": query, "count": len(results)}

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
    def invalidate_cache(project_id: str | None = None):
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
            search_cache.clear()
            logger.info(f"Cleared search cache for project {project_id}")

    @staticmethod
    def get_cache_stats() -> dict[str, Any]:
        """Get search cache statistics."""
        return search_cache.stats()
