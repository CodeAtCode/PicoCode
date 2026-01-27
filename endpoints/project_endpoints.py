"""
Project management API endpoints.
"""

import os
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse

from db.models import CreateProjectRequest, IndexProjectRequest
from db.operations import delete_project, get_or_create_project, get_project_by_id, get_project_metadata, init_db, list_projects, update_project_status
from services.dependency_service import get_project_dependencies
from utils.config import CFG
from utils.logger import get_logger

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
    """Create a new project and return its metadata, including dependency info if available."""
    """Create a new project and return its metadata, including dependency info if available."""
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

        # Append dependency metadata if available
        db_path = project.get("database_path")
        for meta_key in ["direct_deps_count", "direct_deps_indexed", "full_deps_count", "full_deps_indexed"]:
            val = get_project_metadata(db_path, meta_key)
            if val is not None:
                if meta_key.endswith("_count"):
                    try:
                        project[meta_key] = int(val)
                    except ValueError:
                        project[meta_key] = val
                elif meta_key.endswith("_indexed"):
                    project[meta_key] = int(val) if val.isdigit() else val
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
                from db.operations import get_project_metadata, get_project_stats

                stats = get_project_stats(db_path)

                total_files_str = get_project_metadata(db_path, "total_files")
                total_files = int(total_files_str) if total_files_str else 0

                project["indexing_stats"] = {
                    "file_count": stats.get("file_count", 0),
                    "embedding_count": stats.get("embedding_count", 0),
                    "total_files": total_files,
                    "is_indexed": stats.get("file_count", 0) > 0,
                }
            except Exception as e:
                logger.warning(f"Could not get stats for project {project_id}: {e}")
                project["indexing_stats"] = {"file_count": 0, "embedding_count": 0, "total_files": 0, "is_indexed": False}
        else:
            project["indexing_stats"] = {"file_count": 0, "embedding_count": 0, "total_files": 0, "is_indexed": False}

        # Append dependency metadata if available
        for meta_key in ["direct_deps_count", "direct_deps_indexed", "full_deps_count", "full_deps_indexed"]:
            val = get_project_metadata(db_path, meta_key)
            if val is not None:
                # Convert numeric strings to int where appropriate
                if meta_key.endswith("_count"):
                    try:
                        project[meta_key] = int(val)
                    except ValueError:
                        project[meta_key] = val
                elif meta_key.endswith("_indexed"):
                    # stored as "0" or "1"
                    project[meta_key] = int(val) if val.isdigit() else val
        return JSONResponse(project)
    except Exception as e:
        logger.exception(f"Error getting project: {e}")
        return JSONResponse({"error": "Failed to retrieve project"}, status_code=500)


# New endpoint to retrieve project dependencies (direct only)


# New endpoint to index all dependencies (including transitive) for a project
@router.get("/projects/{project_id}/dependencies", summary="Get project dependencies")
def api_get_dependencies(project_id: str, request: Request):
    """Return dependencies.
    Uses caching: direct dependencies are cached with hash of manifest files.
    Full dependencies (include_transitive=True) are cached separately with a different hash.
    """
    """
    Return detected dependencies for a given project.
    The response format is:
    {
        "python": [{"name": "requests", "version": "2.25"}, ...],
        "javascript": [{"name": "react", "version": "^17.0.2"}, ...]
    }
    """
    try:
        project = get_project_by_id(project_id)
        if not project:
            return JSONResponse({"error": "Project not found"}, status_code=404)
        project_path = project.get("path")
        if not project_path or not os.path.isdir(project_path):
            return JSONResponse({"error": "Project path invalid"}, status_code=400)
        include_transitive = request.query_params.get("include_transitive", "false").lower() == "true"
        # Try to load cached dependencies first
        db_path = project.get("database_path")
        from db.operations import compute_dependency_usage, load_cached_dependencies, load_dependency_usage, store_dependency_usage

        is_transitive_flag = 1 if include_transitive else 0
        cached = load_cached_dependencies(db_path, project_id, is_transitive_flag)
        usage = load_dependency_usage(db_path, project_id)
        # Get total indexed file count for project
        from db.operations import get_project_stats

        stats = get_project_stats(db_path)
        if cached:
            # Merge usage counts into each dependency entry
            for lang, dep_list in cached.items():
                lang_usage = usage.get(lang, {})
                for dep in dep_list:
                    dep["file_count"] = lang_usage.get(dep.get("name"), 0)
            # Compute total dependency count
            total_deps = sum(len(v) for v in cached.values())
            # Attach metadata
            response_body = {"dependencies": cached, "metadata": {"indexed_file_count": stats.get("file_count", 0), "dependency_count": total_deps}}
            return JSONResponse(response_body)
        # Fallback: compute on the fly (should be rare)
        deps = get_project_dependencies(project_path, include_transitive=include_transitive)
        # Compute usage counts on the fly
        usage_counts = compute_dependency_usage(db_path, project_path, deps)
        # Store usage for future requests
        store_dependency_usage(db_path, project_id, usage_counts)
        # Merge usage into deps
        for lang, dep_list in deps.items():
            lang_usage = usage_counts.get(lang, {})
            for dep in dep_list:
                dep["file_count"] = lang_usage.get(dep.get("name"), 0)
        total_deps = sum(len(v) for v in deps.values())
        response_body = {"dependencies": deps, "metadata": {"indexed_file_count": stats.get("file_count", 0), "dependency_count": total_deps}}
        return JSONResponse(response_body)
        # Fallback: compute on the fly (should be rare)
        deps = get_project_dependencies(project_path, include_transitive=include_transitive)
        # Compute usage counts on the fly
        usage_counts = compute_dependency_usage(db_path, project_path, deps)
        # Store usage for future requests
        store_dependency_usage(db_path, project_id, usage_counts)
        # Merge usage into deps
        for lang, dep_list in deps.items():
            lang_usage = usage_counts.get(lang, {})
            for dep in dep_list:
                dep["file_count"] = lang_usage.get(dep.get("name"), 0)
        response_body = {"dependencies": deps, "metadata": {"indexed_file_count": stats.get("file_count", 0)}}
        return JSONResponse(response_body)
    except Exception as e:
        logger.exception(f"Error retrieving dependencies for project {project_id}: {e}")
        return JSONResponse({"error": "Failed to retrieve dependencies"}, status_code=500)


@router.delete("/projects/{project_id}", summary="Delete a project")
def api_delete_project(project_id: str):
    # Cancel any active indexing for this project
    if indexing_active.get(project_id):
        indexing_active[project_id] = False
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


# Global dict to track active indexing tasks per project
indexing_active: dict[str, bool] = {}


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
    - Verifies dependencies at start and populates the `project_dependencies` table

    Rate limit: 10 requests per minute per IP.

    Returns immediately with status "indexing".
    """
    client_ip = _get_client_ip(http_request)
    allowed, retry_after = indexing_limiter.is_allowed(client_ip)
    if not allowed:
        return JSONResponse({"error": "Rate limit exceeded for indexing", "retry_after": retry_after}, status_code=429, headers={"Retry-After": str(retry_after)})

    try:
        project = get_project_by_id(request.project_id)
        if not project:
            return JSONResponse({"error": "Project not found"}, status_code=404)

        project_path = project["path"]
        db_path = project["database_path"]
        project_id = project["id"]
        # Ensure the project's database schema is initialized (in case the DB file was missing or corrupted)
        init_db(db_path)
        # Reset any existing DBWriter for this DB so it uses the fresh schema
        try:
            from db.db_writer import stop_writer

            stop_writer(db_path)
        except Exception:
            pass

        if not os.path.exists(project_path):
            return JSONResponse({"error": "Project path does not exist"}, status_code=400)

        # If full re-index requested, clear existing data and dependency cache first
        if request.incremental is False:
            from db.operations import clear_project_data, clear_project_dependencies, set_project_metadata

            clear_project_data(db_path)
            clear_project_dependencies(db_path, project_id)
            set_project_metadata(db_path, "total_files", "0")

        update_project_status(request.project_id, "indexing")
        set_project_metadata(db_path, "direct_deps_count", "0")
        set_project_metadata(db_path, "direct_deps_indexed", "0")
        set_project_metadata(db_path, "full_deps_count", "0")
        set_project_metadata(db_path, "full_deps_indexed", "0")

        venv_path = CFG.get("venv_path")
        incremental = request.incremental if request.incremental is not None else True

        # Full dependencies will be computed inside the callback after they are gathered.
        # Initialize metadata for full dependencies.
        set_project_metadata(db_path, "full_deps_count", "0")
        set_project_metadata(db_path, "full_deps_indexed", "0")

        def index_callback():
            try:
                from ai.analyzer import analyze_local_path_sync
                from db.operations import set_project_metadata, store_project_dependencies
                from services.dependency_service import get_project_dependencies
                from services.dependency_usage import compute_and_store_usage

                # Perform the code indexing first
                analyze_local_path_sync(project_path, db_path, venv_path, MAX_FILE_SIZE, CFG, incremental=incremental)
                print("Processed project files for indexing")
                # Check cancellation before proceeding
                if not indexing_active.get(project_id, False):
                    logger.info(f"Indexing for project {project_id} cancelled after file processing")
                    update_project_status(request.project_id, "error")
                    return
                # After code indexing, recompute and store direct dependencies (and full if needed)
                direct_deps = get_project_dependencies(project_path, include_transitive=False)
                print("Processed direct dependencies")
                if not indexing_active.get(project_id, False):
                    logger.info(f"Indexing for project {project_id} cancelled after dependency extraction")
                    return
                store_project_dependencies(db_path, project_id, direct_deps, is_transitive=0)
                # Compute and store usage for direct deps
                compute_and_store_usage(db_path, project_id, direct_deps)
                # Update direct deps metadata
                direct_deps_count = sum(len(v) for v in direct_deps.values())
                set_project_metadata(db_path, "direct_deps_count", str(direct_deps_count))
                set_project_metadata(db_path, "direct_deps_indexed", "1")
                if not incremental:
                    full_deps = get_project_dependencies(project_path, include_transitive=True)
                    if not indexing_active.get(project_id, False):
                        logger.info(f"Indexing for project {project_id} cancelled before full dependency storage")
                        return
                    store_project_dependencies(db_path, project_id, full_deps, is_transitive=1)
                    # Compute and store usage for full deps
                    compute_and_store_usage(db_path, project_id, full_deps)
                    # Update full deps metadata
                    full_deps_count = sum(len(v) for v in full_deps.values())
                    set_project_metadata(db_path, "full_deps_count", str(full_deps_count))
                    set_project_metadata(db_path, "full_deps_indexed", "1")
                update_project_status(request.project_id, "ready", datetime.utcnow().isoformat())
                indexing_active[project_id] = False
            except Exception as e:
                logger.exception(f"Indexing failed for project {request.project_id}: {e}")
                update_project_status(request.project_id, "error")
                raise

        # Mark indexing as active
        indexing_active[project_id] = True
        try:
            background_tasks.add_task(index_callback)
        except TypeError:
            # Fallback for non‑FastAPI dummy BackgroundTasks used in tests
            index_callback()

        indexing_type = "incremental" if incremental else "full"
        logger.info(f"Started {indexing_type} indexing for project {request.project_id}")

        return JSONResponse({"status": "indexing", "project_id": request.project_id, "incremental": incremental})
    except Exception as e:
        logger.exception(f"Error starting project indexing: {e}")
        return JSONResponse({"error": "Failed to start indexing"}, status_code=500)
