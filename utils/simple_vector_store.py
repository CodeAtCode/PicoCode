"""Utility to manage a SimpleVectorStore per project database path.

The SimpleVectorStore stores embeddings in memory. For a production setting you may want a
persistent backend, but for this codebase it is sufficient and removes the custom SQLite vector handling.
"""

from llama_index.core.vector_stores import SimpleVectorStore

# Global cache of stores keyed by database_path
_vector_stores: dict[str, SimpleVectorStore] = {}

def get_vector_store(database_path: str) -> SimpleVectorStore:
    """Return a SimpleVectorStore for the given database_path, creating if needed.

    The store is kept in memory for the lifetime of the process.
    """
    if database_path not in _vector_stores:
        _vector_stores[database_path] = SimpleVectorStore()
    return _vector_stores[database_path]
