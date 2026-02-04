"""
Generic retry utilities with exponential backoff.
Provides consistent retry behavior across all operations.
"""

import functools
import time
from collections.abc import Callable
from typing import Any

from utils.logger import get_logger

logger = get_logger(__name__)


def retry_on_exception(
    exceptions: tuple[type[Exception], ...] = (Exception,), max_retries: int = 3, base_delay: float = 0.1, exponential_backoff: bool = True, log_retries: bool = True
):
    """
    Decorator for retrying operations with exponential backoff.

    Args:
        exceptions: Tuple of exception types to catch and retry
        max_retries: Maximum number of retry attempts
        base_delay: Base delay in seconds between retries
        exponential_backoff: Use exponential backoff (delay = base_delay * 2^attempt)
        log_retries: Log retry attempts

    Returns:
        Decorated function that retries on specified exceptions

    Example:
        @retry_on_exception(
            exceptions=(sqlite3.OperationalError,),
            max_retries=5,
            base_delay=0.05
        )
        def insert_data(conn, data):
            conn.execute("INSERT INTO table VALUES (?)", data)
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            last_exception = None

            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e

                    if attempt == max_retries - 1:
                        raise

                    if exponential_backoff:
                        delay = base_delay * (2**attempt)
                    else:
                        delay = base_delay

                    if log_retries:
                        logger.warning(f"Retry {attempt + 1}/{max_retries} for {func.__name__} after {delay:.3f}s due to: {type(e).__name__}: {e}")

                    time.sleep(delay)

            if last_exception:
                raise last_exception

        return wrapper

    return decorator


def retry_on_db_locked(max_retries: int = 3, base_delay: float = 0.1):
    """
    Specialized retry decorator for database locked errors.
    Wrapper around retry_on_exception with sqlite3.OperationalError filter.

    Args:
        max_retries: Maximum number of retry attempts
        base_delay: Base delay in seconds between retries

    Returns:
        Decorated function that retries on database locked errors
    """
    import sqlite3

    def is_db_locked(e: Exception) -> bool:
        """Check if exception is a database locked error."""
        return isinstance(e, sqlite3.OperationalError) and "database is locked" in str(e).lower()

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            last_exception = None

            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except sqlite3.OperationalError as e:
                    if not is_db_locked(e):
                        raise

                    last_exception = e

                    if attempt == max_retries - 1:
                        raise

                    delay = base_delay * (2**attempt)
                    logger.warning(f"Database locked, retry {attempt + 1}/{max_retries} for {func.__name__} after {delay:.3f}s")
                    time.sleep(delay)

            if last_exception:
                raise last_exception

        return wrapper

    return decorator
