"""
Web UI and legacy endpoints.
"""

import json
import os
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ai.analyzer import analyze_local_path_background, call_coding_model, search_semantic
from db.operations import delete_project, get_or_create_project, get_project_by_id, get_project_stats, list_projects, update_project_status
from utils.config import CFG
from utils.logger import get_logger

logger = get_logger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="templates")

MAX_FILE_SIZE = int(CFG.get("max_file_size", 200000))
TOTAL_CONTEXT_LIMIT = 4000


@router.get("/api/health", tags=["health"], summary="Health check")
def api_health():
    """
    Health check endpoint for monitoring and status verification.

    Returns:
    - **status**: "ok" if service is running
    - **version**: API version
    - **features**: List of enabled features
    - **file_watcher**: Status of the FileWatcher (if enabled)

    Use this endpoint for:
    - Load balancer health checks
    - Monitoring systems
    - Service availability verification
    """
    from main import _file_watcher

    health_data = {"status": "ok", "version": "0.2.0", "features": ["rag", "per-project-db", "pycharm-api", "incremental-indexing", "rate-limiting", "caching", "file-watcher"]}

    if _file_watcher:
        health_data["file_watcher"] = _file_watcher.get_status()

    return JSONResponse(health_data)


@router.get("/", response_class=HTMLResponse)
def index(request: Request):
    projects_list = list_projects()
    return templates.TemplateResponse("index.html", {"request": request, "projects": projects_list, "config": CFG})


@router.get("/projects/status")
def projects_status():
    """Get list of all projects."""
    try:
        projects = list_projects()
        return JSONResponse(projects)
    except Exception as e:
        logger.exception(f"Error getting projects status: {e}")
        return JSONResponse({"error": "Failed to retrieve projects"}, status_code=500)


@router.delete("/projects/{project_id}")
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


@router.post("/index")
def index_project(background_tasks: BackgroundTasks, project_path: str = None):
    """Index/re-index the default project or specified path."""
    try:
        path_to_index = project_path or CFG.get("local_path")
        if not path_to_index or not os.path.exists(path_to_index):
            raise HTTPException(status_code=400, detail="Project path does not exist")

        project = get_or_create_project(path_to_index)
        project_id = project["id"]
        db_path = project["database_path"]

        update_project_status(project_id, "indexing")

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
        raise HTTPException(status_code=500, detail="Failed to start indexing") from e


@router.post("/code")
async def code_endpoint(request: Request):
    """Code completion endpoint - uses project_id to find the right database."""
    payload = None
    try:
        payload = await request.json()
    except Exception:
        try:
            body = await request.body()
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            payload = None

    if not payload or "prompt" not in payload:
        return JSONResponse({"error": "prompt required"}, status_code=400)

    prompt = payload["prompt"]
    explicit_context = payload.get("context", "") or ""
    use_rag = bool(payload.get("use_rag", True))

    project_id = payload.get("project_id")

    if not project_id:
        projects = list_projects()
        if not projects:
            return JSONResponse({"error": "No projects available. Please index a project first."}, status_code=400)
        project_id = projects[0]["id"]

    try:
        project = get_project_by_id(project_id)
        if not project:
            return JSONResponse({"error": "Project not found"}, status_code=404)

        database_path = project["database_path"]

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

    if use_rag:
        try:
            retrieved = search_semantic(prompt, database_path, top_k=top_k)
            context_parts = []
            total_len = len(combined_context)
            for r in retrieved:
                content = r.get("content", "")
                path = r.get("path", "")
                score = r.get("score", 0)

                part = f"File: {path} (score: {score:.4f})\n{content}\n"

                if total_len + len(part) > TOTAL_CONTEXT_LIMIT:
                    remaining = TOTAL_CONTEXT_LIMIT - total_len
                    if remaining > 200:  # Only include if we have meaningful space
                        truncated_content = content[: remaining - 100] + "..."
                        part = f"File: {path} (score: {score:.4f})\n{truncated_content}\n"
                        context_parts.append(part)
                        used_context.append({"path": path, "score": score, "file_id": r.get("file_id"), "chunk_index": r.get("chunk_index")})
                    break

                context_parts.append(part)
                total_len += len(part)
                used_context.append({"path": path, "score": score, "file_id": r.get("file_id"), "chunk_index": r.get("chunk_index")})

            if context_parts:
                retrieved_text = "\n---\n".join(context_parts)
                if combined_context:
                    combined_context = combined_context + "\n\nRetrieved Context:\n" + retrieved_text
                else:
                    combined_context = "Retrieved Context:\n" + retrieved_text
        except Exception as e:
            logger.exception(f"RAG search failed: {e}")
            used_context = []

    try:
        resp = call_coding_model(prompt, combined_context)
    except Exception as e:
        return JSONResponse({"error": f"coding model call failed: {e}"}, status_code=500)

    return JSONResponse({"response": resp, "used_context": used_context})
