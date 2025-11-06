[![License](https://img.shields.io/badge/License-GPL%20v3-blue.svg)](http://www.gnu.org/licenses/gpl-3.0)   

# PicoCode - Local Codebase Assistant

<img src="https://github.com/user-attachments/assets/146f5fd1-45cf-4164-b981-635e0db3b791" />

Are you looking for a simple way to asks question to your codebase using the inference provider you want without to be locked to a specific service?
This tool is a way to achieve this!

## Overview

- **Production-ready RAG backend** with per-project persistent storage
- **PyCharm/IDE integration** via REST API (see [PYCHARM_INTEGRATION.md](PYCHARM_INTEGRATION.md))
- **Per-project databases**: Each project gets isolated SQLite database
- Indexes files, computes embeddings using an OpenAI-compatible embedding endpoint
- Stores vector embeddings in SQLite using sqlite-vector for fast semantic search
- Analysis runs asynchronously (FastAPI BackgroundTasks) so the UI remains responsive
- Minimal web UI for starting analysis and asking questions (semantic search + coding model)
- Health check and monitoring endpoints for production deployment

## New Features (v0.2.0)

### Per-Project Persistent Storage
- Each opened project gets its own SQLite database
- Isolated storage prevents cross-project data leakage
- Project registry tracks all indexed projects
- Automatic project ID generation based on path

### PyCharm Plugin API
- RESTful API designed for IDE integration
- Create/manage projects via API
- Background indexing with status tracking
- Semantic search and code completion endpoints
- See [PYCHARM_INTEGRATION.md](PYCHARM_INTEGRATION.md) for full API documentation

Prerequisites
- Python 3.8+ (3.11+ recommended for builtin tomllib)
- Git (optional, if you clone the repo)
- If you use Astral `uv`, install/configure `uv` according to the official docs:
  https://docs.astral.sh/uv/

## Installation and run commands

First step: Example .env (copy `.env.example` -> `.env` and edit)

#### Astral uv
- Follow Astral uv installation instructions first: https://docs.astral.sh/uv/
- Typical flow (after `uv` is installed and you are in the project directory):

```
  uv pip install -r pyproject.toml

  uv run python ./main.py
```

Notes:
- The exact `uv` subcommands depend on the uv version/configuration. Check the Astral uv docs for the exact syntax for your uv CLI release. The analyzer only needs a Python executable in the venv to run `python -m pip list --format=json`; `uv` typically provides or creates that venv.

### Using plain virtualenv / pip (fallback)

- Create a virtual environment and install dependencies listed in `pyproject.toml` with your preferred tool.
- 
```
  # create venv
  python -m venv .venv

  # activate (UNIX)
  source .venv/bin/activate

  # activate (Windows PowerShell)
  .venv\Scripts\Activate.ps1

  uv pip install -r pyproject.toml

  # run the server
  python ./main.py
```

### Using Poetry

```
  poetry install
  poetry run main.py
```