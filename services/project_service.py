"""
Service layer for project operations.
Separates business logic from database operations.
"""

import os
from typing import Any

from db.operations import (
    create_project as db_create_project,
    delete_project as db_delete_project,
    get_or_create_project as db_get_or_create_project,
    get_project as db_get_project,
    get_project_by_id as db_get_project_by_id,
    get_project_stats,
    list_projects as db_list_projects,
    update_project_status as db_update_project_status,
)
from utils.logger import get_logger

logger = get_logger(__name__)


class ProjectService:
    """
    Service layer for project management operations.
    Provides high-level business logic for projects.
    """

    @staticmethod
    def create_project(project_path: str, name: str | None = None) -> dict[str, Any]:
        """
        Create a new project with validation.

        Args:
            project_path: Path to project directory
            name: Optional project name

        Returns:
            Project metadata dictionary

        Raises:
            ValueError: If path is invalid
            RuntimeError: If creation fails
        """
        if not project_path:
            raise ValueError("Project path cannot be empty")

        abs_path = os.path.abspath(project_path)

        if not os.path.exists(abs_path):
            raise ValueError(f"Project path does not exist: {abs_path}")

        if not os.path.isdir(abs_path):
            raise ValueError(f"Project path is not a directory: {abs_path}")

        try:
            project = db_create_project(abs_path, name)
            logger.info(f"Created project {project['id']} at {abs_path}")
            return project
        except Exception as e:
            logger.error(f"Failed to create project: {e}")
            raise RuntimeError(f"Failed to create project: {e}") from e

    @staticmethod
    def get_project(project_path: str) -> dict[str, Any] | None:
        """Get project by path."""
        return db_get_project(project_path)

    @staticmethod
    def get_project_by_id(project_id: str) -> dict[str, Any] | None:
        """Get project by ID."""
        return db_get_project_by_id(project_id)

    @staticmethod
    def list_all_projects() -> list:
        """List all projects."""
        return db_list_projects()

    @staticmethod
    def delete_project(project_id: str) -> None:
        """
        Delete a project with validation.

        Args:
            project_id: Project identifier

        Raises:
            ValueError: If project not found
        """
        project = db_get_project_by_id(project_id)
        if not project:
            raise ValueError(f"Project not found: {project_id}")

        try:
            db_delete_project(project_id)
            logger.info(f"Deleted project {project_id}")
        except Exception as e:
            logger.error(f"Failed to delete project: {e}")
            raise RuntimeError(f"Failed to delete project: {e}") from e

    @staticmethod
    def update_status(project_id: str, status: str, timestamp: str | None = None) -> None:
        """
        Update project status.

        Args:
            project_id: Project identifier
            status: New status (created, indexing, ready, error)
            timestamp: Optional timestamp
        """
        db_update_project_status(project_id, status, timestamp)
        logger.debug(f"Updated project {project_id} status to {status}")

    @staticmethod
    def get_or_create(project_path: str, name: str | None = None) -> dict[str, Any]:
        """Get existing project or create new one."""
        return db_get_or_create_project(project_path, name)

    @staticmethod
    def get_stats(project_id: str) -> dict[str, Any]:
        """
        Get project statistics.

        Args:
            project_id: Project identifier

        Returns:
            Statistics dictionary with file_count and embedding_count

        Raises:
            ValueError: If project not found
        """
        project = db_get_project_by_id(project_id)
        if not project:
            raise ValueError(f"Project not found: {project_id}")

        db_path = project["database_path"]
        return get_project_stats(db_path)

    @staticmethod
    def is_indexed(project_id: str) -> bool:
        """
        Check if project has been indexed.

        Args:
            project_id: Project identifier

        Returns:
            True if project has indexed files
        """
        try:
            stats = ProjectService.get_stats(project_id)
            return stats.get("file_count", 0) > 0
        except ValueError:
            return False

    @staticmethod
    def validate_project_ready(project_id: str) -> tuple:
        """
        Validate that project is ready for queries.

        Args:
            project_id: Project identifier

        Returns:
            Tuple of (is_ready: bool, error_message: Optional[str])
        """
        project = db_get_project_by_id(project_id)
        if not project:
            return False, "Project not found"

        if not os.path.exists(project["path"]):
            return False, "Project path does not exist"

        if not ProjectService.is_indexed(project_id):
            return False, "Project not indexed yet"

        return True, None
