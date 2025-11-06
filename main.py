from fastapi import FastAPI, Request, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from contextlib import asynccontextmanager
import os
import json
import uvicorn
from typing import Optional
from datetime import datetime

from db import init_db, get_project_stats
from analyzer import analyze_local_path_background, search_semantic, call_coding_model
from config import CFG
from projects import (
    get_project_by_id, list_projects,
    update_project_status, delete_project, get_or_create_project
)
from models import (
    CreateProjectRequest, IndexProjectRequest, 
    QueryRequest
)
from logger import get_logger
from rate_limiter import query_limiter, indexing_limiter, general_limiter

logger = get_logger(__name__)

MAX_FILE_SIZE = int(CFG.get("max_file_size", 200000))

# Controls how many characters of each snippet and total context we send to coding model
TOTAL_CONTEXT_LIMIT = 4000


def _get_client_ip(request: Request) -> str:
    """Get client IP address from request."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"

@asynccontextmanager
async def lifespan(app: FastAPI):
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
templates = Jinja2Templates(directory="templates")
if os.path.isdir("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")


# Project Management API (PyCharm-compatible)
@app.post("/api/projects", tags=["projects"], summary="Create or get a project")
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
        # Validate input
        if not request.path:
            return JSONResponse({"error": "Project path is required"}, status_code=400)
        
        project = get_or_create_project(request.path, request.name)
        return JSONResponse(project)
    except ValueError as e:
        # ValueError is expected for invalid inputs, safe to show message
        logger.warning(f"Validation error creating project: {e}")
        return JSONResponse({"error": "Invalid project path"}, status_code=400)
    except RuntimeError as e:
        # RuntimeError may contain sensitive details, use generic message
        logger.error(f"Runtime error creating project: {e}")
        return JSONResponse({"error": "Database operation failed"}, status_code=500)
    except Exception as e:
        logger.exception(f"Unexpected error creating project: {e}")
        return JSONResponse({"error": "Internal server error"}, status_code=500)


@app.get("/api/projects", tags=["projects"], summary="List all projects")
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


@app.get("/api/projects/{project_id}", tags=["projects"], summary="Get project by ID")
def api_get_project(project_id: str):
    """
    Get project details by ID.
    
    - **project_id**: Unique project identifier
    
    Returns project metadata or 404 if not found.
    """
    try:
        project = get_project_by_id(project_id)
        if not project:
            return JSONResponse({"error": "Project not found"}, status_code=404)
        return JSONResponse(project)
    except Exception as e:
        logger.exception(f"Error getting project: {e}")
        return JSONResponse({"error": "Failed to retrieve project"}, status_code=500)


@app.delete("/api/projects/{project_id}", tags=["projects"], summary="Delete a project")
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


@app.post("/api/projects/index", tags=["indexing"], summary="Index a project")
def api_index_project(http_request: Request, request: IndexProjectRequest, background_tasks: BackgroundTasks):
    """
    Index or re-index a project in the background.
    
    - **project_id**: Unique project identifier
    
    Starts background indexing process:
    - Scans project directory for code files
    - Generates embeddings for semantic search
    - Uses incremental indexing (skips unchanged files)
    
    Rate limit: 10 requests per minute per IP.
    
    Returns immediately with status "indexing".
    Poll project status to check completion.
    """
    # Rate limiting for indexing operations (more strict)
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
        
        # Update status to indexing
        update_project_status(request.project_id, "indexing")
        
        # Start background indexing
        venv_path = CFG.get("venv_path")
        
        def index_callback():
            try:
                analyze_local_path_background(project_path, db_path, venv_path, MAX_FILE_SIZE, CFG)
                update_project_status(request.project_id, "ready", datetime.utcnow().isoformat())
            except Exception as e:
                update_project_status(request.project_id, "error")
                raise
        
        background_tasks.add_task(index_callback)
        
        return JSONResponse({"status": "indexing", "project_id": request.project_id})
    except Exception as e:
        logger.exception(f"Error starting project indexing: {e}")
        return JSONResponse({"error": "Failed to start indexing"}, status_code=500)


@app.post("/api/query", tags=["query"], summary="Semantic search query")
def api_query(http_request: Request, request: QueryRequest):
    """
    Query a project using semantic search.
    
    - **project_id**: Unique project identifier
    - **query**: Search query text
    - **top_k**: Number of results to return (default: 5, max: 20)
    
    Performs semantic search using vector embeddings:
    - Generates embedding for query
    - Finds most similar code chunks
    - Returns ranked results with scores
    
    Rate limit: 100 requests per minute per IP.
    
    Returns:
    - **results**: Array of matching code chunks
    - **project_id**: Project identifier
    - **query**: Original query text
    """
    # Rate limiting
    client_ip = _get_client_ip(http_request)
    allowed, retry_after = query_limiter.is_allowed(client_ip)
    if not allowed:
        return JSONResponse(
            {"error": "Rate limit exceeded", "retry_after": retry_after},
            status_code=429,
            headers={"Retry-After": str(retry_after)}
        )
    
    try:
        project = get_project_by_id(request.project_id)
        if not project:
            return JSONResponse({"error": "Project not found"}, status_code=404)
        
        db_path = project["database_path"]
        
        # Check if project has been indexed
        stats = get_project_stats(db_path)
        if stats["file_count"] == 0:
            return JSONResponse({"error": "Project not indexed yet"}, status_code=400)
        
        # Perform semantic search
        results = search_semantic(request.query, db_path, top_k=request.top_k)
        
        return JSONResponse({
            "results": results,
            "project_id": request.project_id,
            "query": request.query
        })
    except Exception as e:
        logger.exception(f"Error querying project: {e}")
        return JSONResponse({"error": "Query failed"}, status_code=500)



@app.get("/api/health", tags=["health"], summary="Health check")
def api_health():
    """
    Health check endpoint for monitoring and status verification.
    
    Returns:
    - **status**: "ok" if service is running
    - **version**: API version
    - **features**: List of enabled features
    
    Use this endpoint for:
    - Load balancer health checks
    - Monitoring systems
    - Service availability verification
    """
    return JSONResponse({
        "status": "ok",
        "version": "0.2.0",
        "features": ["rag", "per-project-db", "pycharm-api", "incremental-indexing", "rate-limiting", "caching"]
    })


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    projects_list = list_projects()
    return templates.TemplateResponse("index.html", {
        "request": request, 
        "projects": projects_list,
        "config": CFG
    })


@app.get("/projects/status")
def projects_status():
    """Get list of all projects."""
    try:
        projects = list_projects()
        return JSONResponse(projects)
    except Exception as e:
        logger.exception(f"Error getting projects status: {e}")
        return JSONResponse({"error": "Failed to retrieve projects"}, status_code=500)


@app.delete("/projects/{project_id}")
def delete_project_endpoint(project_id: str):
    """Delete a project and its database."""
    try:
        delete_project(project_id)
        return JSONResponse({"deleted": True})
    except ValueError as e:
        logger.warning(f"Project not found for deletion: {e}")
        return JSONResponse({"deleted": False, "error": "Project not found"}, status_code=404)
    except Exception as e:
        logger.exception(f"Error deleting project: {e}")
        return JSONResponse({"deleted": False, "error": "Failed to delete project"}, status_code=500)


@app.post("/index")
def index_project(background_tasks: BackgroundTasks, project_path: str = None):
    """Index/re-index the default project or specified path."""
    try:
        # Use configured path or provided path
        path_to_index = project_path or CFG.get("local_path")
        if not path_to_index or not os.path.exists(path_to_index):
            raise HTTPException(status_code=400, detail="Project path does not exist")
        
        # Get or create project
        project = get_or_create_project(path_to_index)
        project_id = project["id"]
        db_path = project["database_path"]
        
        # Update status to indexing
        update_project_status(project_id, "indexing")
        
        # Start background indexing
        venv_path = CFG.get("venv_path")
        
        def index_callback():
            try:
                analyze_local_path_background(path_to_index, db_path, venv_path, MAX_FILE_SIZE, CFG)
                update_project_status(project_id, "ready", datetime.utcnow().isoformat())
            except Exception as e:
                logger.exception(f"Indexing failed: {e}")
                update_project_status(project_id, "error")
                raise
        
        background_tasks.add_task(index_callback)
        
        return RedirectResponse(url="/", status_code=303)
    except Exception as e:
        logger.exception(f"Error starting indexing: {e}")
        raise HTTPException(status_code=500, detail="Failed to start indexing")


@app.post("/code")
def code_endpoint(request: Request):
    """Code completion endpoint - uses project_id to find the right database."""
    payload = None
    try:
        payload = request.json()
    except Exception:
        try:
            payload = json.loads(request.body().decode("utf-8"))
        except Exception:
            payload = None

    if not payload or "prompt" not in payload:
        return JSONResponse({"error": "prompt required"}, status_code=400)

    prompt = payload["prompt"]
    explicit_context = payload.get("context", "") or ""
    use_rag = bool(payload.get("use_rag", True))
    
    # Get project_id - if not provided, use the first available project
    project_id = payload.get("project_id")
    
    if not project_id:
        # Try to get default project or first available
        projects = list_projects()
        if not projects:
            return JSONResponse({"error": "No projects available. Please index a project first."}, status_code=400)
        project_id = projects[0]["id"]
    
    # Get project and its database
    try:
        project = get_project_by_id(project_id)
        if not project:
            return JSONResponse({"error": "Project not found"}, status_code=404)
        
        database_path = project["database_path"]
        
        # Check if project has been indexed
        stats = get_project_stats(database_path)
        if stats["file_count"] == 0:
            return JSONResponse({"error": "Project not indexed yet. Please run indexing first."}, status_code=400)
    except Exception as e:
        logger.exception(f"Error getting project: {e}")
        return JSONResponse({"error": "Failed to retrieve project"}, status_code=500)
    
    try:
        top_k = int(payload.get("top_k", 5))
    except Exception:
        top_k = 5

    used_context = []
    combined_context = explicit_context or ""

    # If RAG requested, perform semantic search and build context
    if use_rag:
        try:
            retrieved = search_semantic(prompt, database_path, top_k=top_k)
            # Build context WITHOUT including snippets: only include file references and scores
            context_parts = []
            total_len = len(combined_context)
            for r in retrieved:
                part = f"File: {r.get('path')} (score: {r.get('score', 0):.4f})\n"
                if total_len + len(part) > TOTAL_CONTEXT_LIMIT:
                    break
                context_parts.append(part)
                total_len += len(part)
                used_context.append({"path": r.get("path"), "score": r.get("score")})
            if context_parts:
                retrieved_text = "\n".join(context_parts)
                if combined_context:
                    combined_context = combined_context + "\n\nRetrieved:\n" + retrieved_text
                else:
                    combined_context = "Retrieved:\n" + retrieved_text
        except Exception:
            used_context = []

    # Call the coding model with prompt and combined_context
    try:
        resp = call_coding_model(prompt, combined_context)
    except Exception as e:
        return JSONResponse({"error": f"coding model call failed: {e}"}, status_code=500)

    return JSONResponse({"response": resp, "used_context": used_context})


if __name__ == "__main__":
    uvicorn.run("main:app", host=CFG.get("uvicorn_host", "127.0.0.1"), port=int(CFG.get("uvicorn_port", 8000)), reload=True)
