# PyCharm Plugin Integration Guide

This document describes the REST API for integrating PicoCode with PyCharm and other IDEs.

## Overview

PicoCode provides a production-ready local RAG backend with per-project persistent storage. Each project gets its own SQLite database for isolation, and the API is designed to be compatible with IDE plugins.

## API Endpoints

### Base URL
```
http://127.0.0.1:8000/api
```

### Health Check
```http
GET /api/health
```

Returns server status and available features.

**Response:**
```json
{
  "status": "ok",
  "version": "0.2.0",
  "features": ["rag", "per-project-db", "pycharm-api"]
}
```

### Project Management

#### Create/Get Project
```http
POST /api/projects
Content-Type: application/json

{
  "path": "/absolute/path/to/project",
  "name": "Optional Project Name"
}
```

Creates a new project or returns existing one. Each project gets its own database.

**Response:**
```json
{
  "id": "1234567890abcdef",
  "name": "MyProject",
  "path": "/absolute/path/to/project",
  "database_path": "~/.picocode/projects/1234567890abcdef.db",
  "created_at": "2025-11-06T14:30:00",
  "last_indexed_at": null,
  "status": "created",
  "settings": null
}
```

#### List All Projects
```http
GET /api/projects
```

Returns list of all registered projects.

**Response:**
```json
[
  {
    "id": "1234567890abcdef",
    "name": "MyProject",
    "path": "/absolute/path/to/project",
    "status": "ready",
    ...
  }
]
```

#### Get Project Details
```http
GET /api/projects/{project_id}
```

Returns details for a specific project.

#### Delete Project
```http
DELETE /api/projects/{project_id}
```

Deletes project and its database.

**Response:**
```json
{
  "success": true
}
```

### Indexing

#### Index Project
```http
POST /api/projects/index
Content-Type: application/json

{
  "project_id": "1234567890abcdef"
}
```

Starts background indexing of the project. This processes all files, generates embeddings, and stores them in the project's database.

**Response:**
```json
{
  "status": "indexing",
  "project_id": "1234567890abcdef"
}
```

### Code Intelligence

#### Semantic Search
```http
POST /api/query
Content-Type: application/json

{
  "project_id": "1234567890abcdef",
  "query": "How does authentication work?",
  "top_k": 5
}
```

Performs semantic search across the indexed project.

**Response:**
```json
{
  "results": [
    {
      "file_id": 123,
      "path": "src/auth.py",
      "chunk_index": 0,
      "score": 0.8542
    }
  ],
  "project_id": "1234567890abcdef",
  "query": "How does authentication work?"
}
```

#### Code Completion / Question Answering
```http
POST /api/code
Content-Type: application/json

{
  "project_id": "1234567890abcdef",
  "prompt": "Explain the authentication flow",
  "context": "",
  "use_rag": true,
  "top_k": 5
}
```

Gets code suggestions or answers using RAG + LLM.

**Response:**
```json
{
  "response": "The authentication flow works as follows...",
  "used_context": [
    {
      "path": "src/auth.py",
      "score": 0.8542
    }
  ],
  "project_id": "1234567890abcdef"
}
```

## PyCharm Plugin Workflow

### 1. On Project Open
When a project is opened in PyCharm:
```
1. POST /api/projects with project path
2. Store returned project_id
3. Check if project needs indexing (status != "ready")
4. If needed, POST /api/projects/index
```

### 2. Code Assistance
When user requests code help:
```
1. POST /api/code with prompt and project_id
2. Display response in IDE
3. Show used_context sources if available
```

### 3. Semantic Search
When user searches for code:
```
1. POST /api/query with search term and project_id
2. Display matching files and scores
3. Allow navigation to results
```

### 4. Background Monitoring
Poll project status periodically:
```
1. GET /api/projects/{project_id}
2. Check status field
3. Update UI indicators
```

## Configuration

Create a `.env` file with:

```bash
# API endpoint for embeddings and LLM
API_URL=https://api.openai.com/v1/
API_KEY=your-api-key-here

# Model names
EMBEDDING_MODEL=text-embedding-3-small
CODING_MODEL=gpt-4o

# Server configuration
UVICORN_HOST=127.0.0.1
UVICORN_PORT=8000

# File processing
MAX_FILE_SIZE=200000
```

## Error Handling

All endpoints return standard HTTP status codes:
- 200: Success
- 400: Bad request (validation error)
- 404: Resource not found
- 500: Server error

Error responses include a JSON object:
```json
{
  "error": "Description of the error"
}
```

## Example Integration (Python)

```python
import requests

class PicoCodeClient:
    def __init__(self, base_url="http://127.0.0.1:8000/api"):
        self.base_url = base_url
    
    def create_project(self, path, name=None):
        response = requests.post(
            f"{self.base_url}/projects",
            json={"path": path, "name": name}
        )
        return response.json()
    
    def index_project(self, project_id):
        response = requests.post(
            f"{self.base_url}/projects/index",
            json={"project_id": project_id}
        )
        return response.json()
    
    def query(self, project_id, query, top_k=5):
        response = requests.post(
            f"{self.base_url}/query",
            json={
                "project_id": project_id,
                "query": query,
                "top_k": top_k
            }
        )
        return response.json()
    
    def get_code_suggestion(self, project_id, prompt, use_rag=True):
        response = requests.post(
            f"{self.base_url}/code",
            json={
                "project_id": project_id,
                "prompt": prompt,
                "use_rag": use_rag
            }
        )
        return response.json()

# Usage
client = PicoCodeClient()
project = client.create_project("/path/to/my/project", "MyProject")
client.index_project(project["id"])
results = client.query(project["id"], "authentication flow")
suggestion = client.get_code_suggestion(project["id"], "Explain auth")
```

## Support

For issues or questions, please refer to the main PicoCode repository.
