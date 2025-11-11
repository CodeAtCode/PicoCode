"""
PicoCode - Local Codebase Assistant with RAG.
Main application entry point.
"""
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
import os
import sys
import tempfile
import uvicorn
import signal
import atexit

from db import operations as db_operations
from db.operations import get_or_create_project
from db.db_writer import stop_all_writers
from utils.config import CFG
from utils.logger import get_logger
from endpoints.project_endpoints import router as project_router
from endpoints.query_endpoints import router as query_router
from endpoints.web_endpoints import router as web_router
from utils.file_watcher import FileWatcher

logger = get_logger(__name__)

# Global FileWatcher instance
_file_watcher = None


def cleanup_on_exit():
    """Cleanup function called on exit or error."""
    global _file_watcher
    
    logger.info("Cleaning up resources...")
    
    # Stop FileWatcher
    if _file_watcher:
        try:
            _file_watcher.stop(timeout=2.0)
            _file_watcher = None
            logger.info("FileWatcher stopped")
        except Exception as e:
            logger.error(f"Error stopping FileWatcher: {e}")
    
    # Stop all database writers
    try:
        stop_all_writers()
        logger.info("Database writers stopped")
    except Exception as e:
        logger.error(f"Error stopping database writers: {e}")


def signal_handler(signum, frame):
    """Handle termination signals."""
    logger.info(f"Received signal {signum}, shutting down...")
    cleanup_on_exit()
    sys.exit(0)


# Register cleanup handlers
atexit.register(cleanup_on_exit)
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    global _file_watcher
    
    # Test sqlite-vector extension loading at startup
    logger.info("Testing sqlite-vector extension loading...")
    try:
        from db.vector_operations import connect_db, load_sqlite_vector_extension
        
        # Create a temporary database to test the extension
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp:
            tmp_db_path = tmp.name
        
        try:
            conn = connect_db(tmp_db_path)
            try:
                load_sqlite_vector_extension(conn)
                logger.info("✓ sqlite-vector extension loaded successfully")
            finally:
                conn.close()
        finally:
            # Clean up temporary database
            try:
                os.unlink(tmp_db_path)
            except Exception:
                pass
    except Exception as e:
        logger.error(f"FATAL: Failed to load sqlite-vector extension at startup: {e}")
        # Force immediate exit - cannot continue without vector extension
        sys.exit(1)
    
    # Project registry is auto-initialized when needed via create_project
    
    # Auto-create default project from configured local_path if it exists
    local_path = CFG.get("local_path")
    if local_path and os.path.exists(local_path):
        try:
            get_or_create_project(local_path, "Default Project")
        except Exception as e:
            logger.warning(f"Could not create default project: {e}")
    
    # Start FileWatcher if enabled
    if CFG.get("file_watcher_enabled", True):
        try:
            _file_watcher = FileWatcher(
                logger=logger,
                enabled=True,
                debounce_seconds=CFG.get("file_watcher_debounce", 5),
                check_interval=CFG.get("file_watcher_interval", 10)
            )
            
            # Add all existing projects to the watcher
            try:
                projects = db_operations.list_projects()
                for project in projects:
                    if project.get("path") and os.path.exists(project["path"]):
                        _file_watcher.add_project(project["id"], project["path"])
            except Exception as e:
                logger.warning(f"Could not add projects to file watcher: {e}")
            
            _file_watcher.start()
            logger.info("FileWatcher started successfully")
        except Exception as e:
            logger.error(f"Failed to start FileWatcher: {e}")
            _file_watcher = None
    else:
        logger.info("FileWatcher is disabled in configuration")
    
    yield
    
    # Cleanup is handled by atexit and signal handlers
    # Just ensure FileWatcher stops gracefully here
    if _file_watcher:
        try:
            _file_watcher.stop()
            logger.info("FileWatcher stopped successfully")
        except Exception as e:
            logger.error(f"Error stopping FileWatcher: {e}")


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
    # Configure uvicorn to hide access logs
    uvicorn.run(
        "main:app", 
        host=CFG.get("uvicorn_host", "127.0.0.1"), 
        port=int(CFG.get("uvicorn_port", 8000)), 
        reload=True,
        access_log=False  # Hide access logs
    )
