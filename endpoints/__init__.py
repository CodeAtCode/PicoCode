"""
API endpoints module.
"""
from .project_endpoints import router as project_router
from .query_endpoints import router as query_router
from .web_endpoints import router as web_router

__all__ = ['project_router', 'query_router', 'web_router']
