"""
Utility functions for text processing and vector operations.
"""
import hashlib
import math
from typing import List


def compute_file_hash(content: str) -> str:
    """
    Compute SHA256 hash of file content for change detection.
    """
    return hashlib.sha256(content.encode('utf-8')).hexdigest()


def chunk_text(text: str, chunk_size: int = 800, overlap: int = 100) -> List[str]:
    """
    Split text into overlapping chunks.
    
    Args:
        text: Text to chunk
        chunk_size: Maximum size of each chunk in characters
        overlap: Number of overlapping characters between chunks
    
    Returns:
        List of text chunks
    """
    if chunk_size <= 0:
        return [text]
    step = max(1, chunk_size - overlap)
    chunks: List[str] = []
    start = 0
    L = len(text)
    while start < L:
        end = min(start + chunk_size, L)
        chunks.append(text[start:end])
        start += step
    return chunks


def dot(a, b):
    """
    Compute dot product of two vectors.
    """
    return sum(x * y for x, y in zip(a, b))


def norm(a):
    """
    Compute L2 norm (magnitude) of a vector.
    """
    return math.sqrt(sum(x * x for x in a))


def cosine(a, b):
    """
    Compute cosine similarity between two vectors.
    
    Returns:
        Cosine similarity value between 0 and 1
    """
    na = norm(a)
    nb = norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return sum(x * y for x, y in zip(a, b)) / (na * nb)
