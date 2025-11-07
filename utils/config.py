from dotenv import load_dotenv
import os

# Load .env from project root (if present). This populates os.environ.
load_dotenv(".env")

def _int_env(name, default):
    v = os.getenv(name)
    try:
        return int(v) if v is not None else default
    except Exception:
        return default

def _bool_env(name, default):
    v = os.getenv(name)
    if v is None:
        return default
    return v.lower() in ("1", "true", "yes")

# Expose a CFG dictionary for the rest of the app
CFG = {
    "local_path": os.getenv("LOCAL_PATH"),
    "venv_path": os.getenv("VENV_PATH"),
    "api_url": os.getenv("API_URL"),
    "api_key": os.getenv("API_KEY"),
    "database_path": os.getenv("DATABASE_PATH", "codebase.db"),
    "max_file_size": int(os.getenv("MAX_FILE_SIZE", "200000")),

    # model names for external APIs (optional)
    "embedding_model": os.getenv("EMBEDDING_MODEL"),
    "coding_model": os.getenv("CODING_MODEL"),

    # chunking parameters configurable via env
    "chunk_size": _int_env("CHUNK_SIZE", 800),
    "chunk_overlap": _int_env("CHUNK_OVERLAP", 100),

    # uvicorn host/port (from .env)
    "uvicorn_host": os.getenv("UVICORN_HOST", "127.0.0.1"),
    "uvicorn_port": int(os.getenv("UVICORN_PORT", "8000")),

    # IndexSyncAgent configuration
    "index_sync_enabled": _bool_env("INDEX_SYNC_ENABLED", True),
    "index_sync_interval": _int_env("INDEX_SYNC_INTERVAL", 30),
}
