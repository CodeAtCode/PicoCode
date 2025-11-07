"""
Database module initialization.
Provides organized access to database operations.
"""
from .connection import get_connection, init_db
from .files import (
    store_file, get_file_by_path, needs_reindex, 
    list_files, delete_file_by_path, clear_project_data
)
from .projects import (
    create_project, get_project, get_project_by_id, list_projects,
    update_project_status, update_project_settings, delete_project,
    get_or_create_project
)
from .metadata import (
    set_project_metadata, set_project_metadata_batch,
    get_project_metadata, get_project_stats
)
from .chunks import insert_chunk_row_with_null_embedding

__all__ = [
    # Connection
    'get_connection',
    'init_db',
    # Files
    'store_file',
    'get_file_by_path',
    'needs_reindex',
    'list_files',
    'delete_file_by_path',
    'clear_project_data',
    # Projects
    'create_project',
    'get_project',
    'get_project_by_id',
    'list_projects',
    'update_project_status',
    'update_project_settings',
    'delete_project',
    'get_or_create_project',
    # Metadata
    'set_project_metadata',
    'set_project_metadata_batch',
    'get_project_metadata',
    'get_project_stats',
    # Chunks
    'insert_chunk_row_with_null_embedding',
]
