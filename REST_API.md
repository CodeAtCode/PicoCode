# PicoCode REST API Documentation

This document describes the REST API for integrating with PicoCode's local RAG backend.

## Overview

PicoCode provides a production-ready local RAG (Retrieval-Augmented Generation) backend with per-project persistent storage. Each project gets its own SQLite database for isolation, making it ideal for IDE integration and custom tooling.

## Base URL

```
http://127.0.0.1:8080/api
```

## Authentication

Currently, the API does not require authentication. For production deployments, consider adding authentication via reverse proxy or API gateway.

## API Endpoints

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
  "features": [
    "rag",
    "per-project-db",
    "incremental-indexing",
    "rate-limiting",
    "caching"
  ]
}
```

---

## Project Management

### Create or Get Project

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

### List All Projects

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
    "created_at": "2025-11-06T14:30:00",
    "last_indexed_at": "2025-11-06T15:00:00"
  }
]
```

### Get Project Details

```http
GET /api/projects/{project_id}
```

Returns details for a specific project including indexing statistics.

**Response:**
```json
{
  "id": "1234567890abcdef",
  "name": "MyProject",
  "path": "/absolute/path/to/project",
  "database_path": "~/.picocode/projects/1234567890abcdef.db",
  "status": "ready",
  "created_at": "2025-11-06T14:30:00",
  "last_indexed_at": "2025-11-06T15:00:00",
  "indexing_stats": {
    "file_count": 150,
    "embedding_count": 450,
    "is_indexed": true
  }
}
```

**Indexing Stats Fields:**
- `file_count`: Number of files indexed in the project
- `embedding_count`: Number of embeddings (chunks) generated
- `is_indexed`: Boolean indicating if project has any indexed files

### Delete Project

```http
DELETE /api/projects/{project_id}
```

Permanently deletes project and its database.

**Response:**
```json
{
  "success": true
}
```

---

## Indexing

### Index Project

```http
POST /api/projects/index
Content-Type: application/json

{
  "project_id": "1234567890abcdef"
}
```

Starts background indexing of the project. This processes all files, generates embeddings, and stores them in the project's database.

**Features:**
- Incremental indexing (skips unchanged files)
- Code-aware chunking
- Background processing

**Rate Limit:** 10 requests per minute per IP

**Response:**
```json
{
  "status": "indexing",
  "project_id": "1234567890abcdef"
}
```

**Project Status Values:**
- `created` - Project created but not indexed
- `indexing` - Currently indexing
- `ready` - Indexed and ready for queries
- `error` - Indexing failed

---

## Code Intelligence

### Semantic Search

```http
POST /api/query
Content-Type: application/json

{
  "project_id": "1234567890abcdef",
  "query": "How does authentication work?",
  "top_k": 5
}
```

Performs semantic search across the indexed project using vector embeddings.

**Rate Limit:** 100 requests per minute per IP

**Response:**
```json
{
  "results": [
    {
      "file_id": 123,
      "path": "src/auth.py",
      "chunk_index": 0,
      "score": 0.8542,
      "content": "def authenticate(username, password):\n    ..."
    }
  ],
  "project_id": "1234567890abcdef",
  "query": "How does authentication work?",
  "count": 5
}
```

### Code Completion / Question Answering

```http
POST /code
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

**Parameters:**
- `project_id` - (optional) Project to search, uses first available if not provided
- `prompt` - (required) Question or request
- `context` - (optional) Additional context
- `use_rag` - (optional, default: true) Use semantic search
- `top_k` - (optional, default: 5) Number of results to retrieve

**Response:**
```json
{
  "response": "The authentication flow works as follows...",
  "used_context": [
    {
      "path": "src/auth.py",
      "score": 0.8542
    }
  ]
}
```

---

## Error Handling

All endpoints return standard HTTP status codes:

- **200** - Success
- **400** - Bad request (validation error)
- **404** - Resource not found
- **429** - Rate limit exceeded
- **500** - Server error

Error responses include a JSON object:
```json
{
  "error": "Description of the error"
}
```

For rate limiting errors:
```json
{
  "error": "Rate limit exceeded",
  "retry_after": 42
}
```

---

## Configuration

Create a `.env` file in the project root:

```bash
# OpenAI API configuration
API_URL=https://api.openai.com/v1/
API_KEY=your-api-key-here

# Model selection
EMBEDDING_MODEL=text-embedding-3-small
CODING_MODEL=gpt-4o

# Server configuration
UVICORN_HOST=127.0.0.1
UVICORN_PORT=8080

# File processing
MAX_FILE_SIZE=200000
```

---

## Example Client (Python)

```python
import requests

class PicoCodeClient:
    def __init__(self, base_url="http://127.0.0.1:8080/api"):
        self.base_url = base_url
    
    def create_project(self, path, name=None):
        """Create or get a project."""
        response = requests.post(
            f"{self.base_url}/projects",
            json={"path": path, "name": name}
        )
        response.raise_for_status()
        return response.json()
    
    def index_project(self, project_id):
        """Start indexing a project."""
        response = requests.post(
            f"{self.base_url}/projects/index",
            json={"project_id": project_id}
        )
        response.raise_for_status()
        return response.json()
    
    def get_project(self, project_id):
        """Get project details."""
        response = requests.get(f"{self.base_url}/projects/{project_id}")
        response.raise_for_status()
        return response.json()
    
    def query(self, project_id, query, top_k=5):
        """Perform semantic search."""
        response = requests.post(
            f"{self.base_url}/query",
            json={
                "project_id": project_id,
                "query": query,
                "top_k": top_k
            }
        )
        response.raise_for_status()
        return response.json()
    
    def get_code_suggestion(self, project_id, prompt, use_rag=True):
        """Get code suggestion or answer."""
        response = requests.post(
            f"{self.base_url}/../code",  # Note: /code is at root level
            json={
                "project_id": project_id,
                "prompt": prompt,
                "use_rag": use_rag
            }
        )
        response.raise_for_status()
        return response.json()

# Usage example
client = PicoCodeClient()

# Create project
project = client.create_project("/path/to/my/project", "MyProject")
print(f"Created project: {project['id']}")

# Index project
result = client.index_project(project["id"])
print(f"Indexing status: {result['status']}")

# Wait for indexing to complete (poll status)
import time
while True:
    proj = client.get_project(project["id"])
    if proj["status"] == "ready":
        break
    elif proj["status"] == "error":
        print("Indexing failed")
        break
    time.sleep(2)

# Perform semantic search
results = client.query(project["id"], "authentication flow")
for result in results["results"]:
    print(f"- {result['path']} (score: {result['score']:.4f})")

# Get code suggestion
suggestion = client.get_code_suggestion(project["id"], "Explain auth")
print(suggestion["response"])
```

---

## Interactive Documentation

PicoCode provides interactive API documentation:

- **Swagger UI**: http://127.0.0.1:8080/docs
- **ReDoc**: http://127.0.0.1:8080/redoc

These interfaces allow you to:
- Browse all endpoints
- See request/response schemas
- Test API calls directly
- View example requests

---

## Rate Limiting

Rate limits are enforced per IP address:

| Endpoint Type | Limit | Window |
|--------------|-------|--------|
| Queries | 100 requests | 1 minute |
| Indexing | 10 requests | 1 minute |
| General | 200 requests | 1 minute |

When rate limited, the response includes a `Retry-After` header indicating seconds to wait.

---

## Caching

The API uses LRU caching for performance:

- Project metadata: 5 minute TTL
- Project stats: 1 minute TTL
- Search results: 10 minute TTL
- File content: 5 minute TTL

Caches are automatically invalidated on data updates.

---

## Support

For issues, feature requests, or questions:
- GitHub Repository: [CodeAtCode/PicoCode](https://github.com/CodeAtCode/PicoCode)
- Documentation: Check `/docs` and `/redoc` endpoints
