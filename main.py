"""
PicoCode - Local Codebase Assistant with RAG.
Main application entry point.
"""
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
import os
import uvicorn

from db import operations as db_operations
from db.operations import get_or_create_project
from utils.config import CFG
from utils.logger import get_logger
from endpoints.project_endpoints import router as project_router
from endpoints.query_endpoints import router as query_router
from endpoints.web_endpoints import router as web_router
from ai.agents import IndexSyncAgent

logger = get_logger(__name__)

# Global IndexSyncAgent instance
_index_sync_agent = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    global _index_sync_agent
    
    # Project registry is auto-initialized when needed via create_project
    
    # Auto-create default project from configured local_path if it exists
    local_path = CFG.get("local_path")
    if local_path and os.path.exists(local_path):
        try:
            get_or_create_project(local_path, "Default Project")
        except Exception as e:
            logger.warning(f"Could not create default project: {e}")
    
    # Start IndexSyncAgent if enabled
    if CFG.get("index_sync_enabled", True):
        try:
            _index_sync_agent = IndexSyncAgent(
                db_client=db_operations,
                interval_seconds=CFG.get("index_sync_interval", 30),
                logger=logger,
                enabled=True
            )
            _index_sync_agent.start()
            logger.info("IndexSyncAgent started successfully")
        except Exception as e:
            logger.error(f"Failed to start IndexSyncAgent: {e}")
            _index_sync_agent = None
    else:
        logger.info("IndexSyncAgent is disabled in configuration")
    
    yield
    
    # Stop IndexSyncAgent on shutdown
    if _index_sync_agent:
        try:
            _index_sync_agent.stop()
            logger.info("IndexSyncAgent stopped successfully")
        except Exception as e:
            logger.error(f"Error stopping IndexSyncAgent: {e}")


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
