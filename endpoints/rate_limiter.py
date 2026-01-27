"""
Simple rate limiter middleware for FastAPI endpoints.
"""

import threading
import time
from collections import defaultdict


class RateLimiter:
    """
    Token bucket rate limiter for API endpoints.
    Thread-safe implementation.
    """

    def __init__(self, calls: int = 100, window: int = 60):
        """
        Initialize rate limiter.

        Args:
            calls: Maximum number of calls allowed
            window: Time window in seconds
        """
        self.calls = calls
        self.window = window
        self._storage: dict[str, list] = defaultdict(list)
        self._lock = threading.Lock()

    def is_allowed(self, key: str) -> tuple[bool, int]:
        """
        Check if request is allowed under rate limit.

        Args:
            key: Identifier for rate limit (e.g., IP address)

        Returns:
            Tuple of (allowed: bool, retry_after: int seconds)
        """
        with self._lock:
            now = time.time()
            timestamps = self._storage[key]

            timestamps[:] = [ts for ts in timestamps if ts > now - self.window]

            if len(timestamps) >= self.calls:
                retry_after = int(timestamps[0] + self.window - now) + 1
                return False, retry_after

            timestamps.append(now)
            return True, 0

    def reset(self, key: str):
        """Reset rate limit for a key."""
        with self._lock:
            if key in self._storage:
                del self._storage[key]


query_limiter = RateLimiter(calls=100, window=60)  # 100 queries per minute
indexing_limiter = RateLimiter(calls=10, window=60)  # 10 indexing operations per minute
general_limiter = RateLimiter(calls=200, window=60)  # 200 general requests per minute
