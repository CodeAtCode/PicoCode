"""
Project management API endpoints.
"""
from fastapi import APIRouter, Request, BackgroundTasks
from fastapi.responses import JSONResponse
import os
from datetime import datetime

from db.operations import (
    get_project_by_id, list_projects,
    update_project_status, delete_project, get_or_create_project
)
from db.models import CreateProjectRequest, IndexProjectRequest
from ai.analyzer import analyze_local_path_background
from utils.logger import get_logger
from utils.config import CFG
from .rate_limiter import indexing_limiter

logger = get_logger(__name__)
router = APIRouter(prefix="/api", tags=["projects"])

MAX_FILE_SIZE = int(CFG.get("max_file_size", 200000))


def _get_client_ip(request: Request) -> str:
    """Get client IP address from request."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@router.post("/projects", summary="Create or get a project")
def api_create_project(request: CreateProjectRequest):
    """
    Create or get a project with per-project database.
    
    - **path**: Absolute path to project directory (required)
    - **name**: Optional project name (defaults to directory name)
    
    Returns project metadata including:
    - **id**: Unique project identifier
    - **database_path**: Path to project's SQLite database
    - **status**: Current project status
    """
    try:
        project = get_or_create_project(request.path, request.name)
        
        try:
            from main import _file_watcher
            if _file_watcher and _file_watcher.is_running():
                _file_watcher.add_project(project["id"], project["path"])
        except Exception as e:
            logger.warning(f"Could not add project to file watcher: {e}")
        
        return JSONResponse(project)
    except ValueError as e:
        logger.warning(f"Validation error creating project: {e}")
        return JSONResponse({"error": "Invalid project path"}, status_code=400)
    except RuntimeError as e:
        logger.error(f"Runtime error creating project: {e}")
        return JSONResponse({"error": "Database operation failed"}, status_code=500)
    except Exception as e:
        logger.exception(f"Unexpected error creating project: {e}")
        return JSONResponse({"error": "Internal server error"}, status_code=500)


@router.get("/projects", summary="List all projects")
def api_list_projects():
    """
    List all registered projects.
    
    Returns array of project objects with metadata:
    - **id**: Unique project identifier  
    - **name**: Project name
    - **path**: Project directory path
    - **status**: Current status (created, indexing, ready, error)
    - **last_indexed_at**: Last indexing timestamp
    """
    try:
        projects = list_projects()
        return JSONResponse(projects)
    except Exception as e:
        logger.exception(f"Error listing projects: {e}")
        return JSONResponse({"error": "Failed to list projects"}, status_code=500)


@router.get("/projects/{project_id}", summary="Get project by ID")
def api_get_project(project_id: str):
    """
    Get project details by ID.
    
    - **project_id**: Unique project identifier
    
    Returns project metadata including indexing status and statistics or 404 if not found.
    """
    try:
        project = get_project_by_id(project_id)
        if not project:
            return JSONResponse({"error": "Project not found"}, status_code=404)
        
        db_path = project.get("database_path")
        
        if db_path and os.path.exists(db_path):
            try:
                from db.operations import get_project_stats, get_project_metadata
                stats = get_project_stats(db_path)
                
                total_files_str = get_project_metadata(db_path, "total_files")
                total_files = int(total_files_str) if total_files_str else 0
                
                project["indexing_stats"] = {
                    "file_count": stats.get("file_count", 0),
                    "embedding_count": stats.get("embedding_count", 0),
                    "total_files": total_files,
                    "is_indexed": stats.get("file_count", 0) > 0
                }
            except Exception as e:
                logger.warning(f"Could not get stats for project {project_id}: {e}")
                project["indexing_stats"] = {
                    "file_count": 0,
                    "embedding_count": 0,
                    "total_files": 0,
                    "is_indexed": False
                }
        else:
            project["indexing_stats"] = {
                "file_count": 0,
                "embedding_count": 0,
                "total_files": 0,
                "is_indexed": False
            }
        
        return JSONResponse(project)
    except Exception as e:
        logger.exception(f"Error getting project: {e}")
        return JSONResponse({"error": "Failed to retrieve project"}, status_code=500)


@router.delete("/projects/{project_id}", summary="Delete a project")
def api_delete_project(project_id: str):
    """
    Delete a project and its database.
    
    - **project_id**: Unique project identifier
    
    Permanently removes the project and all indexed data.
    Returns 404 if project not found.
    """
    try:
        delete_project(project_id)
        return JSONResponse({"success": True})
    except ValueError as e:
        logger.warning(f"Project not found for deletion: {e}")
        return JSONResponse({"error": "Project not found"}, status_code=404)
    except Exception as e:
        logger.exception(f"Error deleting project: {e}")
        return JSONResponse({"error": "Failed to delete project"}, status_code=500)


@router.post("/projects/index", tags=["indexing"], summary="Index a project")
def api_index_project(http_request: Request, request: IndexProjectRequest, background_tasks: BackgroundTasks):
    """
    Index or re-index a project in the background.
    
    - **project_id**: Unique project identifier
    - **incremental**: If True (default), only index new/changed files. If False, re-index all files.
    
    Starts background indexing process:
    - Scans project directory for code files
    - Generates embeddings for semantic search
    - Uses incremental indexing by default (skips unchanged files)
    
    Rate limit: 10 requests per minute per IP.
    
    Returns immediately with status "indexing".
    Poll project status to check completion.
    """
    client_ip = _get_client_ip(http_request)
    allowed, retry_after = indexing_limiter.is_allowed(client_ip)
    if not allowed:
        return JSONResponse(
            {"error": "Rate limit exceeded for indexing", "retry_after": retry_after},
            status_code=429,
            headers={"Retry-After": str(retry_after)}
        )
    
    try:
        project = get_project_by_id(request.project_id)
        if not project:
            return JSONResponse({"error": "Project not found"}, status_code=404)
        
        project_path = project["path"]
        db_path = project["database_path"]
        
        if not os.path.exists(project_path):
            return JSONResponse({"error": "Project path does not exist"}, status_code=400)
        
        update_project_status(request.project_id, "indexing")
        
        venv_path = CFG.get("venv_path")
        incremental = request.incremental if request.incremental is not None else True
        
        def index_callback():
            try:
                from ai.analyzer import analyze_local_path_sync
                analyze_local_path_sync(project_path, db_path, venv_path, MAX_FILE_SIZE, CFG, incremental=incremental)
                update_project_status(request.project_id, "ready", datetime.utcnow().isoformat())
            except Exception as e:
                logger.exception(f"Indexing failed for project {request.project_id}: {e}")
                update_project_status(request.project_id, "error")
                raise
        
        background_tasks.add_task(index_callback)
        
        indexing_type = "incremental" if incremental else "full"
        logger.info(f"Started {indexing_type} indexing for project {request.project_id}")
        
        return JSONResponse({
            "status": "indexing", 
            "project_id": request.project_id,
            "incremental": incremental
        })
    except Exception as e:
        logger.exception(f"Error starting project indexing: {e}")
        return JSONResponse({"error": "Failed to start indexing"}, status_code=500)
