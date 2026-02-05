"""
PicoCode - Local Codebase Assistant with RAG.
Main application entry point.
"""

import atexit
import os
import signal
import sys
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from db import operations as db_operations
from db.db_writer import stop_all_writers
from db.operations import get_or_create_project, update_project_status
from endpoints.project_endpoints import router as project_router
from endpoints.query_endpoints import router as query_router
from endpoints.web_endpoints import router as web_router
from utils.config import CFG
from utils.file_watcher import FileWatcher
from utils.logger import get_logger

logger = get_logger(__name__)

# Ensure NLTK data is available at startup
try:
    import nltk

    nltk.download("punkt", quiet=True)
    nltk.download("stopwords", quiet=True)
    logger.info("NLTK data downloaded successfully")
except Exception as e:
    logger.warning(f"Failed to download NLTK data: {e}")

MAX_FILE_SIZE = int(CFG.get("max_file_size", 200000))

_file_watcher = None


def cleanup_on_exit():
    """Cleanup function called on exit or error."""
    global _file_watcher

    logger.info("Cleaning up resources...")

    if _file_watcher:
        try:
            _file_watcher.stop(timeout=2.0)
            _file_watcher = None
            logger.info("FileWatcher stopped")
        except Exception as e:
            logger.error(f"Error stopping FileWatcher: {e}")

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


atexit.register(cleanup_on_exit)
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    global _file_watcher

    try:
        from db.connection import get_db_connection

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            tmp_db_path = tmp.name
        conn = get_db_connection(tmp_db_path)
        conn.close()
        os.unlink(tmp_db_path)
        logger.info("✓ database connection established successfully")
    except Exception as e:
        logger.error(f"FATAL: Failed to establish database connection at startup: {e}")
        sys.exit(1)

    local_path = CFG.get("local_path")
    if local_path and os.path.exists(local_path):
        try:
            get_or_create_project(local_path, "Default Project")
        except Exception as e:
            logger.warning(f"Could not create default project: {e}")

    if CFG.get("file_watcher_enabled", True):
        try:
            _file_watcher = FileWatcher(logger=logger, enabled=True, debounce_seconds=CFG.get("file_watcher_debounce", 5), check_interval=CFG.get("file_watcher_interval", 10))

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

    try:
        import threading

        projects = db_operations.list_projects()
        for project in projects:
            status = project.get("status")
            if status != "ready":
                project_path = project.get("path")
                db_path = project.get("database_path")
                if project_path and os.path.isdir(project_path) and db_path:
                    logger.info(f"Resuming indexing for project {project.get('id')}")
                    venv_path = CFG.get("venv_path")

                    def resume_index(p_id, p_path, d_path, venv_path_local):
                        try:
                            from ai.analyzer import analyze_local_path_sync

                            analyze_local_path_sync(p_path, d_path, venv_path_local, MAX_FILE_SIZE, CFG, incremental=False)
                            update_project_status(p_id, "ready", datetime.utcnow().isoformat())
                        except Exception as e:
                            logger.exception(f"Failed to resume indexing for project {p_id}: {e}")
                            update_project_status(p_id, "error")

                    thread = threading.Thread(target=resume_index, args=(project.get("id"), project_path, db_path, venv_path), daemon=True)
                    thread.start()
    except Exception as e:
        logger.error(f"Error while resuming indexing on startup: {e}")

    yield

    if _file_watcher:
        try:
            _file_watcher.stop()
            logger.info("FileWatcher stopped successfully")
        except Exception as e:
            logger.error(f"Error stopping FileWatcher: {e}")


app = FastAPI(
    lifespan=lifespan,
    title="PicoCode API",
    description="Local Codebase Assistant with RAG (Retrieval-Augmented Generation). Index codebases, perform semantic search, and query with AI assistance.",
    version="0.2.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_tags=[
        {"name": "projects", "description": "Project management operations"},
        {"name": "indexing", "description": "Code indexing operations"},
        {"name": "query", "description": "Semantic search and code queries"},
        {"name": "health", "description": "Health and status checks"},
    ],
)

if os.path.isdir("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(project_router)
app.include_router(query_router)
app.include_router(web_router)


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=CFG.get("uvicorn_host", "127.0.0.1"),
        port=int(CFG.get("uvicorn_port", 8080)),
        reload=True,
        access_log=False,  # Hide access logs
    )
