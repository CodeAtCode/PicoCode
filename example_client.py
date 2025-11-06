#!/usr/bin/env python3
"""
Example script demonstrating PicoCode API usage.
This shows how to integrate PicoCode with a PyCharm plugin or other IDE.
"""
import requests
import json
import time
from typing import Optional, Dict, Any

class PicoCodeClient:
    """Client for interacting with PicoCode API."""
    
    def __init__(self, base_url: str = "http://127.0.0.1:8000"):
        self.base_url = base_url
        self.api_base = f"{base_url}/api"
    
    def health_check(self) -> Dict[str, Any]:
        """Check if the server is running and healthy."""
        response = requests.get(f"{self.api_base}/health")
        response.raise_for_status()
        return response.json()
    
    def create_project(self, path: str, name: Optional[str] = None) -> Dict[str, Any]:
        """Create or get a project."""
        response = requests.post(
            f"{self.api_base}/projects",
            json={"path": path, "name": name}
        )
        response.raise_for_status()
        return response.json()
    
    def list_projects(self) -> list:
        """List all projects."""
        response = requests.get(f"{self.api_base}/projects")
        response.raise_for_status()
        return response.json()
    
    def get_project(self, project_id: str) -> Dict[str, Any]:
        """Get project details."""
        response = requests.get(f"{self.api_base}/projects/{project_id}")
        response.raise_for_status()
        return response.json()
    
    def delete_project(self, project_id: str) -> Dict[str, Any]:
        """Delete a project."""
        response = requests.delete(f"{self.api_base}/projects/{project_id}")
        response.raise_for_status()
        return response.json()
    
    def index_project(self, project_id: str) -> Dict[str, Any]:
        """Start indexing a project."""
        response = requests.post(
            f"{self.api_base}/projects/index",
            json={"project_id": project_id}
        )
        response.raise_for_status()
        return response.json()
    
    def query(self, project_id: str, query: str, top_k: int = 5) -> Dict[str, Any]:
        """Perform semantic search."""
        response = requests.post(
            f"{self.api_base}/query",
            json={
                "project_id": project_id,
                "query": query,
                "top_k": top_k
            }
        )
        response.raise_for_status()
        return response.json()
    
    def get_code_suggestion(
        self,
        project_id: str,
        prompt: str,
        context: str = "",
        use_rag: bool = True,
        top_k: int = 5
    ) -> Dict[str, Any]:
        """Get code suggestions using RAG + LLM."""
        response = requests.post(
            f"{self.api_base}/code",
            json={
                "project_id": project_id,
                "prompt": prompt,
                "context": context,
                "use_rag": use_rag,
                "top_k": top_k
            }
        )
        response.raise_for_status()
        return response.json()


def example_workflow():
    """Example workflow for IDE integration."""
    client = PicoCodeClient()
    
    print("=" * 60)
    print("PicoCode API Example Workflow")
    print("=" * 60)
    
    # 1. Health check
    print("\n1. Checking server health...")
    try:
        health = client.health_check()
        print(f"   ✓ Server is healthy: {health}")
    except Exception as e:
        print(f"   ✗ Server is not running: {e}")
        print("   Please start the server with: python main.py")
        return
    
    # 2. Create a project
    print("\n2. Creating/getting project...")
    project_path = "/tmp/example_project"
    try:
        project = client.create_project(project_path, "Example Project")
        project_id = project["id"]
        print(f"   ✓ Project ID: {project_id}")
        print(f"   ✓ Status: {project['status']}")
    except Exception as e:
        print(f"   ✗ Failed to create project: {e}")
        return
    
    # 3. List all projects
    print("\n3. Listing all projects...")
    try:
        projects = client.list_projects()
        print(f"   ✓ Found {len(projects)} project(s)")
        for p in projects[:3]:  # Show first 3
            print(f"     - {p['name']}: {p['path']} ({p['status']})")
    except Exception as e:
        print(f"   ✗ Failed to list projects: {e}")
    
    # 4. Index the project (this would take time in real use)
    print("\n4. Starting project indexing...")
    print("   Note: This starts background indexing.")
    print("   In a real project, you would poll for completion.")
    try:
        index_result = client.index_project(project_id)
        print(f"   ✓ Indexing started: {index_result}")
    except Exception as e:
        print(f"   ✗ Failed to start indexing: {e}")
    
    # 5. Query example (would fail if not indexed yet)
    print("\n5. Semantic search example...")
    print("   Note: This requires the project to be indexed first.")
    print("   Skipping in this demo as indexing takes time.")
    
    # 6. Code suggestion example (would fail if not indexed yet)
    print("\n6. Code suggestion example...")
    print("   Note: This requires the project to be indexed first.")
    print("   Skipping in this demo as indexing takes time.")
    
    print("\n" + "=" * 60)
    print("Example workflow completed!")
    print("=" * 60)
    print("\nFor full functionality:")
    print("1. Start the server: python main.py")
    print("2. Create a project with a real codebase path")
    print("3. Index the project: POST /api/projects/index")
    print("4. Wait for indexing to complete (poll /api/projects/{id})")
    print("5. Use /api/query and /api/code for RAG queries")


def print_api_reference():
    """Print API reference."""
    print("\n" + "=" * 60)
    print("PicoCode API Reference")
    print("=" * 60)
    print("""
    Base URL: http://127.0.0.1:8000/api
    
    Endpoints:
    
    1. Health Check
       GET /api/health
       Returns: {"status": "ok", "version": "0.2.0", ...}
    
    2. Create/Get Project
       POST /api/projects
       Body: {"path": "/path/to/project", "name": "Optional Name"}
       Returns: Project object
    
    3. List Projects
       GET /api/projects
       Returns: Array of project objects
    
    4. Get Project
       GET /api/projects/{project_id}
       Returns: Project object
    
    5. Delete Project
       DELETE /api/projects/{project_id}
       Returns: {"success": true}
    
    6. Index Project
       POST /api/projects/index
       Body: {"project_id": "..."}
       Returns: {"status": "indexing", ...}
    
    7. Semantic Search
       POST /api/query
       Body: {"project_id": "...", "query": "...", "top_k": 5}
       Returns: {"results": [...], ...}
    
    8. Code Suggestions
       POST /api/code
       Body: {"project_id": "...", "prompt": "...", "use_rag": true}
       Returns: {"response": "...", "used_context": [...], ...}
    
    For more details, see PYCHARM_INTEGRATION.md
    """)


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == "--help":
        print_api_reference()
    else:
        example_workflow()
