"""
Utility functions for computing and persisting per‑dependency file usage.
"""

from db.operations import compute_dependency_usage, store_dependency_usage


def compute_and_store_usage(db_path: str, project_id: str, deps: dict) -> dict:
    """Compute file‑count usage for each dependency and persist it.

    Args:
        db_path: Path to the project's SQLite database.
        project_id: Identifier of the project.
        deps: Dependency mapping as returned by ``get_project_dependencies``.

    Returns:
        The usage dictionary ``{language: {name: count, ...}, ...}``.
    """
    usage = compute_dependency_usage(db_path, None, deps)
    store_dependency_usage(db_path, project_id, usage)
    return usage
