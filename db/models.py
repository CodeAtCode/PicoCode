"""
Pydantic models for API request/response validation.
"""
from typing import Optional
from pydantic import BaseModel


class CreateProjectRequest(BaseModel):
    path: str
    name: Optional[str] = None


class IndexProjectRequest(BaseModel):
    project_id: str
    incremental: Optional[bool] = True  # Default to incremental indexing


class QueryRequest(BaseModel):
    project_id: str
    query: str
    top_k: Optional[int] = 5

