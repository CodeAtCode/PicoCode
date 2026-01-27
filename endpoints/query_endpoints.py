"""
Query and search API endpoints.
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from db.models import QueryRequest
from services.search_service import SearchService
from utils.logger import get_logger

from .rate_limiter import query_limiter

logger = get_logger(__name__)
router = APIRouter(prefix="/api", tags=["query"])


def _get_client_ip(request: Request) -> str:
    """Get client IP address from request."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@router.post("/query", summary="Semantic search query")
def api_query(http_request: Request, request: QueryRequest):
    """
    Query a project using semantic search.

    - **project_id**: Unique project identifier
    - **query**: Search query text
    - **top_k**: Number of results to return (default: 5, max: 20)

    Performs semantic search using vector embeddings:
    - Generates embedding for query
    - Finds most similar code chunks
    - Returns ranked results with scores and content

    Note: Content is always included as it's needed for the coding model.

    Rate limit: 100 requests per minute per IP.

    Returns:
    - **results**: Array of matching code chunks with content
    - **project_id**: Project identifier
    - **query**: Original query text
    """
    client_ip = _get_client_ip(http_request)
    allowed, retry_after = query_limiter.is_allowed(client_ip)
    if not allowed:
        return JSONResponse({"error": "Rate limit exceeded", "retry_after": retry_after}, status_code=429, headers={"Retry-After": str(retry_after)})

    try:
        result = SearchService.semantic_search(project_id=request.project_id, query=request.query, top_k=request.top_k, use_cache=True)
        return JSONResponse(result)
    except ValueError as e:
        logger.warning(f"Query validation failed: {e}")
        if "not found" in str(e).lower():
            return JSONResponse({"error": "Project not found"}, status_code=404)
        elif "not indexed" in str(e).lower():
            return JSONResponse({"error": "Project not indexed yet"}, status_code=400)
        else:
            return JSONResponse({"error": "Invalid request"}, status_code=400)
    except Exception as e:
        logger.exception(f"Error querying project: {e}")
        return JSONResponse({"error": "Query failed"}, status_code=500)
