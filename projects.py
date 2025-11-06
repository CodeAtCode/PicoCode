"""
Project management for per-project persistent storage.
All database operations are now in db.py.
This module re-exports project functions for backward compatibility.
"""

# Re-export all project management functions from db.py
from db import (
    create_project,
    get_project,
    get_project_by_id,
    list_projects,
    update_project_status,
    update_project_settings,
    delete_project,
    get_or_create_project,
)

__all__ = [
    'create_project',
    'get_project',
    'get_project_by_id',
    'list_projects',
    'update_project_status',
    'update_project_settings',
    'delete_project',
    'get_or_create_project',
]
