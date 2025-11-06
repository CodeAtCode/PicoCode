"""
Project management for per-project persistent storage.
Each project gets its own SQLite database for isolation.
"""
import os
import json
import sqlite3
import hashlib
import logging
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Default projects directory
PROJECTS_DIR = os.path.expanduser("~/.picocode/projects")

# Retry configuration for database operations
DB_RETRY_COUNT = 3
DB_RETRY_DELAY = 0.1  # seconds


def _ensure_projects_dir():
    """Ensure projects directory exists."""
    try:
        os.makedirs(PROJECTS_DIR, exist_ok=True)
    except Exception as e:
        logger.error(f"Failed to create projects directory {PROJECTS_DIR}: {e}")
        raise


def _retry_on_db_locked(func, *args, max_retries=DB_RETRY_COUNT, **kwargs):
    """Retry a database operation if it's locked."""
    import time
    last_error = None
    
    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs)
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e).lower() and attempt < max_retries - 1:
                last_error = e
                time.sleep(DB_RETRY_DELAY * (2 ** attempt))  # Exponential backoff
                continue
            raise
        except Exception as e:
            raise
    
    if last_error:
        raise last_error


def _get_project_id(project_path: str) -> str:
    """Generate a stable project ID from the project path."""
    return hashlib.sha256(project_path.encode()).hexdigest()[:16]


def _get_project_db_path(project_id: str) -> str:
    """Get the database path for a project."""
    _ensure_projects_dir()
    return os.path.join(PROJECTS_DIR, f"{project_id}.db")


def _get_projects_registry_path() -> str:
    """Get the path to the projects registry database."""
    _ensure_projects_dir()
    return os.path.join(PROJECTS_DIR, "registry.db")


def _init_registry_db():
    """Initialize the projects registry database with proper configuration."""
    registry_path = _get_projects_registry_path()
    
    def _init():
        conn = sqlite3.connect(registry_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            # Enable WAL mode for better concurrency
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    path TEXT NOT NULL UNIQUE,
                    database_path TEXT NOT NULL,
                    created_at TEXT DEFAULT (datetime('now')),
                    last_indexed_at TEXT,
                    status TEXT DEFAULT 'created',
                    settings TEXT
                )
                """
            )
            conn.commit()
        except Exception as e:
            logger.error(f"Failed to initialize registry database: {e}")
            raise
        finally:
            conn.close()
    
    try:
        _retry_on_db_locked(_init)
    except Exception as e:
        logger.error(f"Failed to initialize registry after retries: {e}")
        raise


def create_project(project_path: str, name: Optional[str] = None) -> Dict[str, Any]:
    """
    Create a new project entry with its own database.
    
    Args:
        project_path: Absolute path to the project directory
        name: Optional project name (defaults to directory name)
    
    Returns:
        Project metadata dictionary
    
    Raises:
        ValueError: If project path is invalid
        RuntimeError: If database operations fail
    """
    try:
        _init_registry_db()
    except Exception as e:
        logger.error(f"Failed to initialize registry: {e}")
        raise RuntimeError(f"Database initialization failed: {e}")
    
    # Validate and normalize path
    if not project_path or not isinstance(project_path, str):
        raise ValueError("Project path must be a non-empty string")
    
    try:
        project_path = os.path.abspath(project_path)
    except Exception as e:
        raise ValueError(f"Invalid project path: {e}")
    
    if not os.path.exists(project_path):
        raise ValueError(f"Project path does not exist: {project_path}")
    
    if not os.path.isdir(project_path):
        raise ValueError(f"Project path is not a directory: {project_path}")
    
    # Generate project ID and database path
    project_id = _get_project_id(project_path)
    db_path = _get_project_db_path(project_id)
    
    # Use directory name as default project name
    if not name:
        name = os.path.basename(project_path)
    
    # Sanitize project name
    if name and len(name) > 255:
        name = name[:255]
    
    registry_path = _get_projects_registry_path()
    
    def _create():
        conn = sqlite3.connect(registry_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.cursor()
            
            # Check if project already exists
            cur.execute("SELECT * FROM projects WHERE path = ?", (project_path,))
            existing = cur.fetchone()
            if existing:
                logger.info(f"Project already exists: {project_path}")
                return dict(existing)
            
            # Insert new project
            cur.execute(
                """
                INSERT INTO projects (id, name, path, database_path, status)
                VALUES (?, ?, ?, ?, 'created')
                """,
                (project_id, name, project_path, db_path)
            )
            conn.commit()
            
            # Initialize project database
            try:
                from db import init_db
                init_db(db_path)
                logger.info(f"Created project {project_id} at {db_path}")
            except Exception as e:
                logger.error(f"Failed to initialize project database: {e}")
                # Rollback project creation
                cur.execute("DELETE FROM projects WHERE id = ?", (project_id,))
                conn.commit()
                raise RuntimeError(f"Failed to initialize project database: {e}")
            
            # Return project metadata
            cur.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()
    
    try:
        return _retry_on_db_locked(_create)
    except Exception as e:
        logger.error(f"Failed to create project: {e}")
        raise


def get_project(project_path: str) -> Optional[Dict[str, Any]]:
    """
    Get project metadata by path.
    
    Args:
        project_path: Absolute path to the project directory
    
    Returns:
        Project metadata dictionary or None if not found
    """
    _init_registry_db()
    project_path = os.path.abspath(project_path)
    
    registry_path = _get_projects_registry_path()
    conn = sqlite3.connect(registry_path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM projects WHERE path = ?", (project_path,))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_project_by_id(project_id: str) -> Optional[Dict[str, Any]]:
    """
    Get project metadata by ID.
    
    Args:
        project_id: Project ID
    
    Returns:
        Project metadata dictionary or None if not found
    """
    _init_registry_db()
    
    registry_path = _get_projects_registry_path()
    conn = sqlite3.connect(registry_path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_projects() -> List[Dict[str, Any]]:
    """
    List all registered projects.
    
    Returns:
        List of project metadata dictionaries
    """
    _init_registry_db()
    
    registry_path = _get_projects_registry_path()
    conn = sqlite3.connect(registry_path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM projects ORDER BY created_at DESC")
        rows = cur.fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def update_project_status(project_id: str, status: str, last_indexed_at: Optional[str] = None):
    """
    Update project indexing status.
    
    Args:
        project_id: Project ID
        status: New status (e.g., 'indexing', 'ready', 'error')
        last_indexed_at: Optional timestamp of last successful index
    """
    _init_registry_db()
    
    registry_path = _get_projects_registry_path()
    conn = sqlite3.connect(registry_path)
    try:
        cur = conn.cursor()
        if last_indexed_at:
            cur.execute(
                "UPDATE projects SET status = ?, last_indexed_at = ? WHERE id = ?",
                (status, last_indexed_at, project_id)
            )
        else:
            cur.execute(
                "UPDATE projects SET status = ? WHERE id = ?",
                (status, project_id)
            )
        conn.commit()
    finally:
        conn.close()


def update_project_settings(project_id: str, settings: Dict[str, Any]):
    """
    Update project settings (stored as JSON).
    
    Args:
        project_id: Project ID
        settings: Settings dictionary to store
    """
    _init_registry_db()
    
    registry_path = _get_projects_registry_path()
    conn = sqlite3.connect(registry_path)
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE projects SET settings = ? WHERE id = ?",
            (json.dumps(settings), project_id)
        )
        conn.commit()
    finally:
        conn.close()


def delete_project(project_id: str):
    """
    Delete a project and its database.
    
    Args:
        project_id: Project ID
    """
    _init_registry_db()
    
    # Get project info
    project = get_project_by_id(project_id)
    if not project:
        raise ValueError(f"Project not found: {project_id}")
    
    # Delete database file
    db_path = project.get("database_path")
    if db_path and os.path.exists(db_path):
        try:
            os.remove(db_path)
        except Exception:
            pass  # Continue even if file deletion fails
    
    # Remove from registry
    registry_path = _get_projects_registry_path()
    conn = sqlite3.connect(registry_path)
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        conn.commit()
    finally:
        conn.close()


def get_or_create_project(project_path: str, name: Optional[str] = None) -> Dict[str, Any]:
    """
    Get existing project or create new one.
    
    Args:
        project_path: Absolute path to the project directory
        name: Optional project name (used only when creating)
    
    Returns:
        Project metadata dictionary
    """
    project = get_project(project_path)
    if project:
        return project
    return create_project(project_path, name)
