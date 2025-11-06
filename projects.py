"""
Project management for per-project persistent storage.
Each project gets its own SQLite database for isolation.
"""
import os
import json
import sqlite3
import hashlib
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime

# Default projects directory
PROJECTS_DIR = os.path.expanduser("~/.picocode/projects")


def _ensure_projects_dir():
    """Ensure projects directory exists."""
    os.makedirs(PROJECTS_DIR, exist_ok=True)


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
    """Initialize the projects registry database."""
    registry_path = _get_projects_registry_path()
    conn = sqlite3.connect(registry_path)
    conn.row_factory = sqlite3.Row
    try:
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
    finally:
        conn.close()


def create_project(project_path: str, name: Optional[str] = None) -> Dict[str, Any]:
    """
    Create a new project entry with its own database.
    
    Args:
        project_path: Absolute path to the project directory
        name: Optional project name (defaults to directory name)
    
    Returns:
        Project metadata dictionary
    """
    _init_registry_db()
    
    # Normalize path
    project_path = os.path.abspath(project_path)
    
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
    
    registry_path = _get_projects_registry_path()
    conn = sqlite3.connect(registry_path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        
        # Check if project already exists
        cur.execute("SELECT * FROM projects WHERE path = ?", (project_path,))
        existing = cur.fetchone()
        if existing:
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
        from db import init_db
        init_db(db_path)
        
        # Return project metadata
        cur.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
        row = cur.fetchone()
        return dict(row)
    finally:
        conn.close()


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
