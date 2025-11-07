"""
PicoCode - Local Codebase Assistant with RAG.
Main application entry point.
"""
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
import os
import uvicorn

from db.operations import get_or_create_project
from utils.config import CFG
from utils.logger import get_logger
from endpoints.project_endpoints import router as project_router
from endpoints.query_endpoints import router as query_router
from endpoints.web_endpoints import router as web_router

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    # Project registry is auto-initialized when needed via create_project
    
    # Auto-create default project from configured local_path if it exists
    local_path = CFG.get("local_path")
    if local_path and os.path.exists(local_path):
        try:
            get_or_create_project(local_path, "Default Project")
        except Exception as e:
            logger.warning(f"Could not create default project: {e}")
    
    yield


app = FastAPI(
    lifespan=lifespan,
    title="PicoCode API",
    description="Local Codebase Assistant with RAG (Retrieval-Augmented Generation). "
                "Index codebases, perform semantic search, and query with AI assistance.",
    version="0.2.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_tags=[
        {"name": "projects", "description": "Project management operations"},
        {"name": "indexing", "description": "Code indexing operations"},
        {"name": "query", "description": "Semantic search and code queries"},
        {"name": "health", "description": "Health and status checks"},
    ]
)

# Mount static files if directory exists
if os.path.isdir("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

# Include routers
app.include_router(project_router)
app.include_router(query_router)
app.include_router(web_router)


if __name__ == "__main__":
    uvicorn.run(
        "main:app", 
        host=CFG.get("uvicorn_host", "127.0.0.1"), 
        port=int(CFG.get("uvicorn_port", 8000)), 
        reload=True
    )
