"""
Simple LRU cache implementation for frequently accessed data.
"""
import time
import threading
from typing import Any, Optional, Dict
from collections import OrderedDict


class LRUCache:
    """
    Thread-safe Least Recently Used (LRU) cache with TTL support.
    """
    
    def __init__(self, max_size: int = 100, ttl: Optional[int] = None):
        """
        Initialize LRU cache.
        
        Args:
            max_size: Maximum number of items to cache
            ttl: Time-to-live in seconds (None for no expiration)
        """
        self.max_size = max_size
        self.ttl = ttl
        self._cache: OrderedDict = OrderedDict()
        self._timestamps: Dict[str, float] = {}
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0
    
    def get(self, key: str) -> Optional[Any]:
        """
        Get value from cache.
        
        Args:
            key: Cache key
        
        Returns:
            Cached value or None if not found/expired
        """
        with self._lock:
            if key not in self._cache:
                self._misses += 1
                return None
            
            # Check TTL
            if self.ttl is not None:
                timestamp = self._timestamps.get(key, 0)
                if time.time() - timestamp > self.ttl:
                    # Expired
                    del self._cache[key]
                    del self._timestamps[key]
                    self._misses += 1
                    return None
            
            # Move to end (most recently used)
            self._cache.move_to_end(key)
            self._hits += 1
            return self._cache[key]
    
    def set(self, key: str, value: Any):
        """
        Set value in cache.
        
        Args:
            key: Cache key
            value: Value to cache
        """
        with self._lock:
            if key in self._cache:
                # Update existing
                self._cache.move_to_end(key)
            else:
                # Add new
                self._cache[key] = value
                
                # Evict oldest if over max_size
                if len(self._cache) > self.max_size:
                    oldest_key = next(iter(self._cache))
                    del self._cache[oldest_key]
                    if oldest_key in self._timestamps:
                        del self._timestamps[oldest_key]
            
            self._cache[key] = value
            self._timestamps[key] = time.time()
    
    def invalidate(self, key: str):
        """Remove key from cache."""
        with self._lock:
            if key in self._cache:
                del self._cache[key]
            if key in self._timestamps:
                del self._timestamps[key]
    
    def clear(self):
        """Clear all cached items."""
        with self._lock:
            self._cache.clear()
            self._timestamps.clear()
            self._hits = 0
            self._misses = 0
    
    def stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        with self._lock:
            total = self._hits + self._misses
            hit_rate = self._hits / total if total > 0 else 0
            return {
                "size": len(self._cache),
                "max_size": self.max_size,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": hit_rate,
                "ttl": self.ttl
            }


# Global caches for different data types
# Project metadata cache (small, frequently accessed)
project_cache = LRUCache(max_size=50, ttl=300)  # 5 minutes TTL

# Project stats cache (small, changes during indexing)
stats_cache = LRUCache(max_size=100, ttl=60)  # 1 minute TTL

# Search results cache (larger, query results)
search_cache = LRUCache(max_size=500, ttl=600)  # 10 minutes TTL

# File content cache (medium size, for recently accessed files)
file_cache = LRUCache(max_size=200, ttl=300)  # 5 minutes TTL
