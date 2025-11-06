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

from db import init_db, list_analyses, delete_analysis
from analyzer import analyze_local_path_background, search_semantic, call_coding_model
from config import CFG
from projects import (
    create_project, get_project, get_project_by_id, list_projects,
    update_project_status, delete_project, get_or_create_project
)
from models import (
    CreateProjectRequest, IndexProjectRequest, 
    QueryRequest
)
from logger import get_logger

logger = get_logger(__name__)

DATABASE = CFG.get("database_path", "codebase.db")
MAX_FILE_SIZE = int(CFG.get("max_file_size", 200000))

# Controls how many characters of each snippet and total context we send to coding model
TOTAL_CONTEXT_LIMIT = 4000
_ANALYSES_CACHE = []

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db(DATABASE)
    yield

app = FastAPI(lifespan=lifespan)
templates = Jinja2Templates(directory="templates")
if os.path.isdir("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")


# Project Management API (PyCharm-compatible)
@app.post("/api/projects")
def api_create_project(request: CreateProjectRequest):
    """Create or get a project with per-project database."""
    
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


@app.get("/api/projects")
def api_list_projects():
    """List all projects."""
    try:
        projects = list_projects()
        return JSONResponse(projects)
    except Exception as e:
        logger.exception(f"Error listing projects: {e}")
        return JSONResponse({"error": "Failed to list projects"}, status_code=500)


@app.get("/api/projects/{project_id}")
def api_get_project(project_id: str):
    """Get project details by ID."""
    try:
        project = get_project_by_id(project_id)
        if not project:
            return JSONResponse({"error": "Project not found"}, status_code=404)
        return JSONResponse(project)
    except Exception as e:
        logger.exception(f"Error getting project: {e}")
        return JSONResponse({"error": "Failed to retrieve project"}, status_code=500)


@app.delete("/api/projects/{project_id}")
def api_delete_project(project_id: str):
    """Delete a project and its database."""
    try:
        delete_project(project_id)
        return JSONResponse({"success": True})
    except ValueError as e:
        logger.warning(f"Project not found for deletion: {e}")
        return JSONResponse({"error": "Project not found"}, status_code=404)
    except Exception as e:
        logger.exception(f"Error deleting project: {e}")
        return JSONResponse({"error": "Failed to delete project"}, status_code=500)


@app.post("/api/projects/index")
def api_index_project(request: IndexProjectRequest, background_tasks: BackgroundTasks):
    """Index/re-index a project in the background."""
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


@app.post("/api/query")
def api_query(request: QueryRequest):
    """Query a project using semantic search (PyCharm-compatible)."""
    try:
        project = get_project_by_id(request.project_id)
        if not project:
            return JSONResponse({"error": "Project not found"}, status_code=404)
        
        db_path = project["database_path"]
        
        # Get the first analysis ID from the project database
        analyses = list_analyses(db_path)
        if not analyses:
            return JSONResponse({"error": "Project not indexed yet"}, status_code=400)
        
        analysis_id = analyses[0]["id"]
        
        # Perform semantic search
        results = search_semantic(request.query, db_path, analysis_id=analysis_id, top_k=request.top_k)
        
        return JSONResponse({
            "results": results,
            "project_id": request.project_id,
            "query": request.query
        })
    except Exception as e:
        logger.exception(f"Error querying project: {e}")
        return JSONResponse({"error": "Query failed"}, status_code=500)



@app.get("/api/health")
def api_health():
    """Health check endpoint."""
    return JSONResponse({
        "status": "ok",
        "version": "0.2.0",
        "features": ["rag", "per-project-db", "pycharm-api"]
    })


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    analyses = list_analyses(DATABASE)
    projects_list = list_projects()
    return templates.TemplateResponse("index.html", {
        "request": request, 
        "analyses": analyses, 
        "projects": projects_list,
        "config": CFG
    })


@app.get("/analyses/status")
def analyses_status():
    global _ANALYSES_CACHE
    try:
        analyses = list_analyses(DATABASE)
        # If the DB returned a non-empty list, update cache and return it.
        if analyses:
            _ANALYSES_CACHE = analyses
            return JSONResponse(analyses)
        # If DB returned empty but we have a cached non-empty list, return cache
        if not analyses and _ANALYSES_CACHE:
            return JSONResponse(_ANALYSES_CACHE)
        # else return whatever (empty list) — first-run or truly empty
        return JSONResponse(analyses)
    except Exception as e:
        # On DB errors (e.g., locked) return last known cache to avoid empty responses spam.
        if _ANALYSES_CACHE:
            return JSONResponse(_ANALYSES_CACHE)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/analyses/{analysis_id}/delete")
def delete_analysis_endpoint(analysis_id: int):
    try:
        delete_analysis(DATABASE, analysis_id)
        return JSONResponse({"deleted": True})
    except Exception as e:
        return JSONResponse({"deleted": False, "error": str(e)}, status_code=500)


@app.post("/analyze")
def analyze(background_tasks: BackgroundTasks):
    local_path = CFG.get("local_path")
    if not local_path or not os.path.exists(local_path):
        raise HTTPException(status_code=400, detail="Configured LOCAL_PATH does not exist")
    venv_path = CFG.get("venv_path")
    background_tasks.add_task(analyze_local_path_background, local_path, DATABASE, venv_path, MAX_FILE_SIZE, CFG)
    return RedirectResponse(url="/", status_code=303)


@app.post("/code")
def code_endpoint(request: Request):
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
    
    # Support both analysis_id (old) and project_id (new for plugin)
    analysis_id = payload.get("analysis_id")
    project_id = payload.get("project_id")
    
    # If project_id is provided, get the database path and find the first analysis
    database_path = DATABASE  # default to main database
    if project_id and not analysis_id:
        try:
            project = get_project_by_id(project_id)
            if not project:
                return JSONResponse({"error": "Project not found"}, status_code=404)
            
            database_path = project["database_path"]
            
            # Get the first analysis from this project
            analyses = list_analyses(database_path)
            if not analyses:
                return JSONResponse({"error": "Project not indexed yet"}, status_code=400)
            
            analysis_id = analyses[0]["id"]
        except Exception as e:
            logger.exception(f"Error getting project analysis: {e}")
            return JSONResponse({"error": "Failed to get project analysis"}, status_code=500)
    
    try:
        top_k = int(payload.get("top_k", 5))
    except Exception:
        top_k = 5

    used_context = []
    combined_context = explicit_context or ""

    # If RAG requested and an analysis_id provided, perform semantic search and build context
    if use_rag and analysis_id:
        try:
            retrieved = search_semantic(prompt, database_path, analysis_id=int(analysis_id), top_k=top_k)
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
