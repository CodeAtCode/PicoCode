"""Utility to manage a SimpleVectorStore per project database path.

The SimpleVectorStore stores embeddings in memory. For this codebase we use
an in‑memory store keyed by the database path, mimicking the previous custom
wrapper. This allows existing code that expects a per‑project vector store to
continue working while leveraging LlamaIndex's SimpleVectorStore implementation.
"""

from llama_index.core.vector_stores import SimpleVectorStore

_vector_stores: dict[str, SimpleVectorStore] = {}


def get_vector_store(database_path: str) -> SimpleVectorStore:
    """Return a SimpleVectorStore for the given database_path, creating if needed.

    The store is kept in memory for the lifetime of the process.
    """
    if database_path not in _vector_stores:
        _vector_stores[database_path] = SimpleVectorStore()
    return _vector_stores[database_path]
