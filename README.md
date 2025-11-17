[![License](https://img.shields.io/badge/License-GPL%20v3-blue.svg)](http://www.gnu.org/licenses/gpl-3.0)   

# PicoCode - Local Codebase Assistant

> 🤖 **Note**: This project was fully generated and developed using GitHub Copilot.

## Screenshots

### Web UI

<img src="https://github.com/user-attachments/assets/a6dc6647-309a-4103-864a-e4cec94b7962" />

### PyCharm (Intellij Plugin)

<img width="734" height="708" alt="immagine" src="https://github.com/user-attachments/assets/79bc6f8a-6285-42ab-8b60-6bc426879d94" />


Are you looking for a simple way to asks question to your codebase using the inference provider you want without to be locked to a specific service?
This tool is a way to achieve this!

## Overview

Check the [blogpost about the project](https://daniele.tech/2025/11/picocode-ai-self-hosted-local-codebase-assistant-rag/) that has more info!

- **Production-ready RAG backend** with per-project persistent storage
- **PyCharm/IDE integration** via REST API (see [REST_API.md](REST_API.md))
- **Per-project databases**: Each project gets isolated SQLite database
- Indexes files, computes embeddings using an OpenAI-compatible embedding endpoint
- Stores vector embeddings in SQLite using sqlite-vector for fast semantic search
- Analysis runs asynchronously (FastAPI BackgroundTasks) so the UI remains responsive
- Minimal web UI for starting analysis and asking questions (semantic search + coding model)
- Health check and monitoring endpoints for production deployment

### PyCharm Plugin

A full-featured PyCharm/IntelliJ IDEA plugin is available:

- **Download**: Get the latest plugin from [Releases](https://github.com/CodeAtCode/PicoCode/releases)
- **Per-Project Indexing**: Automatically indexes current project
- **Secure API Keys**: Stores credentials in IDE password safe
- **Real-time Responses**: Streams answers from your coding model
- **File Navigation**: Click retrieved files to open in editor
- **Progress Indicators**: Visual feedback during indexing

See [ide-plugins/README.md](ide-plugins/README.md) for building and installation instructions.

## Prerequisites

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
