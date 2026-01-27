"""
Pydantic models for API request/response validation.
"""

from pydantic import BaseModel


class CreateProjectRequest(BaseModel):
    path: str
    name: str | None = None


class IndexProjectRequest(BaseModel):
    project_id: str
    incremental: bool | None = True  # Default to incremental indexing


class QueryRequest(BaseModel):
    project_id: str
    query: str
    top_k: int | None = 5
