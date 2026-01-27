import os

from dotenv import load_dotenv

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


CFG = {
    "local_path": os.getenv("LOCAL_PATH"),
    "venv_path": os.getenv("VENV_PATH"),
    "api_url": os.getenv("API_URL"),
    "api_key": os.getenv("API_KEY"),
    "database_path": os.getenv("DATABASE_PATH", "codebase.db"),
    "max_file_size": int(os.getenv("MAX_FILE_SIZE", "200000")),
    "embedding_model": os.getenv("EMBEDDING_MODEL"),
    "coding_model": os.getenv("CODING_MODEL"),
    "chunk_size": _int_env("CHUNK_SIZE", 800),
    "chunk_overlap": _int_env("CHUNK_OVERLAP", 100),
    "uvicorn_host": os.getenv("UVICORN_HOST", "127.0.0.1"),
    "uvicorn_port": int(os.getenv("UVICORN_PORT", "8080")),
    "file_watcher_enabled": _bool_env("FILE_WATCHER_ENABLED", True),
    "file_watcher_interval": _int_env("FILE_WATCHER_INTERVAL", 10),
    "create_default_project": _bool_env("CREATE_DEFAULT_PROJECT", True),
    "file_watcher_debounce": _int_env("FILE_WATCHER_DEBOUNCE", 5),
    "debug": _bool_env("DEBUG", False),
    "db_writer_workers": _int_env("DB_WRITER_WORKERS", 2),
}
