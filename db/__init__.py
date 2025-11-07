"""
Database operations module.
"""
from .operations import (
    # Connection and initialization
    init_db,
    # File operations
    store_file,
    get_file_by_path,
    needs_reindex,
    list_files,
    delete_file_by_path,
    clear_project_data,
    # Project registry operations
    create_project,
    get_project,
    get_project_by_id,
    list_projects,
    update_project_status,
    update_project_settings,
    delete_project,
    get_or_create_project,
    # Metadata operations
    set_project_metadata,
    set_project_metadata_batch,
    get_project_metadata,
    get_project_stats,
    # Chunk operations
    insert_chunk_row_with_null_embedding,
)

from .models import (
    CreateProjectRequest,
    IndexProjectRequest,
    QueryRequest,
)

__all__ = [
    # Connection and initialization
    'init_db',
    # File operations
    'store_file',
    'get_file_by_path',
    'needs_reindex',
    'list_files',
    'delete_file_by_path',
    'clear_project_data',
    # Project registry operations
    'create_project',
    'get_project',
    'get_project_by_id',
    'list_projects',
    'update_project_status',
    'update_project_settings',
    'delete_project',
    'get_or_create_project',
    # Metadata operations
    'set_project_metadata',
    'set_project_metadata_batch',
    'get_project_metadata',
    'get_project_stats',
    # Chunk operations
    'insert_chunk_row_with_null_embedding',
    # Models
    'CreateProjectRequest',
    'IndexProjectRequest',
    'QueryRequest',
]
