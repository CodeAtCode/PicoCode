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


def _add_dependency_metadata(project: dict, db_path: str) -> None:
    """Append dependency count and indexed flags to the project dict if present in DB."""
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


@router.post("/projects", summary="Create or get a project")
def api_create_project(request: CreateProjectRequest):
    """Create a new project and return its metadata, including dependency info if available."""
    try:
        project = get_or_create_project(request.path, request.name)

        try:
            from main import _file_watcher

            if _file_watcher and _file_watcher.is_running():
                _file_watcher.add_project(project["id"], project["path"])
        except Exception as e:
            logger.warning(f"Could not add project to file watcher: {e}")

        db_path = project.get("database_path")
        _add_dependency_metadata(project, db_path)
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

        _add_dependency_metadata(project, db_path)
        return JSONResponse(project)
    except Exception as e:
        logger.exception(f"Error getting project: {e}")
        return JSONResponse({"error": "Failed to retrieve project"}, status_code=500)


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
        db_path = project.get("database_path")
        from db.operations import compute_dependency_usage, load_cached_dependencies, load_dependency_usage, store_dependency_usage

        direct_cached = load_cached_dependencies(db_path, project_id, 0)
        for deps in direct_cached.values():
            for dep in deps:
                dep["is_transitive"] = 0
        transitive_cached = load_cached_dependencies(db_path, project_id, 1) if include_transitive else {}
        for deps in transitive_cached.values():
            for dep in deps:
                dep["is_transitive"] = 1
        cached = direct_cached
        for lang, deps in transitive_cached.items():
            cached.setdefault(lang, []).extend(deps)
        usage = load_dependency_usage(db_path, project_id)
        from db.operations import get_project_stats

        stats = get_project_stats(db_path)
        if cached:
            for lang, dep_list in cached.items():
                lang_usage = usage.get(lang, {})
                for dep in dep_list:
                    dep["file_count"] = lang_usage.get(dep.get("name"), 0)
                dep_list.sort(key=lambda d: (d.get("is_transitive", 0), d.get("name", "").lower()))
            total_deps = sum(len(v) for v in cached.values())
            response_body = {"dependencies": cached, "metadata": {"indexed_file_count": stats.get("file_count", 0), "dependency_count": total_deps}}
            return JSONResponse(response_body)
        deps = get_project_dependencies(project_path, include_transitive=include_transitive)
        usage_counts = compute_dependency_usage(db_path, project_path, deps)
        store_dependency_usage(db_path, project_id, usage_counts)
        for lang, dep_list in deps.items():
            lang_usage = usage_counts.get(lang, {})
            for dep in dep_list:
                dep["file_count"] = lang_usage.get(dep.get("name"), 0)
        total_deps = sum(len(v) for v in deps.values())
        response_body = {"dependencies": deps, "metadata": {"indexed_file_count": stats.get("file_count", 0), "dependency_count": total_deps}}
        return JSONResponse(response_body)
    except Exception as e:
        logger.exception(f"Error retrieving dependencies for project {project_id}: {e}")
        return JSONResponse({"error": "Failed to retrieve dependencies"}, status_code=500)


@router.delete("/projects/{project_id}", summary="Delete a project")
def api_delete_project(project_id: str):
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
        from db.operations import set_project_metadata

        if not project:
            return JSONResponse({"error": "Project not found"}, status_code=404)

        project_path = project["path"]
        db_path = project["database_path"]
        project_id = project["id"]
        init_db(db_path)
        try:
            from db.db_writer import stop_writer

            stop_writer(db_path)
        except Exception:
            pass

        if not os.path.exists(project_path):
            return JSONResponse({"error": "Project path does not exist"}, status_code=400)

        if request.incremental is False:
            from db.operations import clear_project_data, clear_project_dependencies

            clear_project_data(db_path)
            clear_project_dependencies(db_path, project_id)

        update_project_status(request.project_id, "indexing")
        set_project_metadata(db_path, "direct_deps_count", "0")
        set_project_metadata(db_path, "direct_deps_indexed", "0")
        set_project_metadata(db_path, "full_deps_count", "0")
        set_project_metadata(db_path, "full_deps_indexed", "0")

        venv_path = CFG.get("venv_path")
        incremental = False

        set_project_metadata(db_path, "full_deps_count", "0")
        set_project_metadata(db_path, "full_deps_indexed", "0")

        def index_callback():
            try:
                from ai.analyzer import analyze_local_path_sync
                from db.operations import set_project_metadata, store_project_dependencies
                from services.dependency_service import get_project_dependencies
                from services.dependency_usage import compute_and_store_usage

                # Phase 1: Index only project files (not dependencies)
                analyze_local_path_sync(project_path, db_path, venv_path, MAX_FILE_SIZE, CFG, incremental=incremental)
                print("Phase 1 complete: Indexed project files only")

                if not indexing_active.get(project_id, False):
                    logger.info(f"Indexing for project {project_id} cancelled after project file processing")
                    update_project_status(request.project_id, "error")
                    return

                # Phase 2: Index direct dependencies
                logger.info(f"Starting Phase 2: Indexing direct dependencies for project {project_id}")

                # Index dependency files (only .venv and node_modules)
                from ai.analyzer import analyze_dependencies_sync

                analyze_dependencies_sync(project_path, db_path, venv_path, MAX_FILE_SIZE, CFG, incremental=incremental)
                print("Phase 2 complete: Indexed direct dependencies")
                if not indexing_active.get(project_id, False):
                    logger.info(f"Indexing for project {project_id} cancelled after file processing")
                    update_project_status(request.project_id, "error")
                    return
                direct_deps = get_project_dependencies(project_path, include_transitive=False)
                print("Processed direct dependencies")
                if not indexing_active.get(project_id, False):
                    logger.info(f"Indexing for project {project_id} cancelled after dependency extraction")
                    return
                store_project_dependencies(db_path, project_id, direct_deps, is_transitive=0)
                try:
                    from db.connection import get_db_connection

                    conn = get_db_connection(db_path, timeout=5.0, enable_wal=False)
                    cur = conn.cursor()
                    cur.execute("SELECT COUNT(*) FROM project_dependencies WHERE project_id = ? AND is_transitive = 0", (project_id,))
                    dep_count = cur.fetchone()[0]
                    logger.info(f"Inserted {dep_count} direct dependency rows for project {project_id}")
                finally:
                    conn.close()
                compute_and_store_usage(db_path, project_id, direct_deps)
                direct_deps_count = sum(len(v) for v in direct_deps.values())
                set_project_metadata(db_path, "direct_deps_count", str(direct_deps_count))
                set_project_metadata(db_path, "direct_deps_indexed", "1")
                if not incremental:
                    full_deps = get_project_dependencies(project_path, include_transitive=True)
                    if not indexing_active.get(project_id, False):
                        logger.info(f"Indexing for project {project_id} cancelled before full dependency storage")
                        return
                    store_project_dependencies(db_path, project_id, full_deps, is_transitive=1)
                    try:
                        from db.connection import get_db_connection

                        conn_full = get_db_connection(db_path, timeout=5.0, enable_wal=False)
                        cur_full = conn_full.cursor()
                        cur_full.execute("SELECT COUNT(*) FROM project_dependencies WHERE project_id = ? AND is_transitive = 1", (project_id,))
                        dep_full_count = cur_full.fetchone()[0]
                        logger.debug(f"Inserted {dep_full_count} full dependency rows for project {project_id}")
                    finally:
                        conn_full.close()
                    compute_and_store_usage(db_path, project_id, full_deps)
                    full_deps_count = sum(len(v) for v in full_deps.values())
                    set_project_metadata(db_path, "full_deps_count", str(full_deps_count))
                    set_project_metadata(db_path, "full_deps_indexed", "1")
                update_project_status(request.project_id, "ready", datetime.utcnow().isoformat())
                indexing_active[project_id] = False
            except Exception as e:
                logger.exception(f"Indexing failed for project {request.project_id}: {e}")
                update_project_status(request.project_id, "error")
                raise

        indexing_active[project_id] = True
        logger.info(f"indexing_active set to True for project {project_id}")
        try:
            background_tasks.add_task(index_callback)
        except TypeError:
            index_callback()

        indexing_type = "incremental" if incremental else "full"
        logger.info(f"Started {indexing_type} indexing for project {request.project_id}")

        return JSONResponse({"status": "indexing", "project_id": request.project_id, "incremental": incremental})
    except Exception as e:
        logger.exception(f"Error starting project indexing: {e}")
        return JSONResponse({"error": "Failed to start indexing"}, status_code=500)
