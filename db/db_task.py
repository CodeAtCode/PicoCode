"""
Database task class for queued write operations.
"""
import threading


class _DBTask:
    """Internal task class for queuing database write operations."""
    def __init__(self, sql, params):
        self.sql = sql
        self.params = params
        self.event = threading.Event()
        self.rowid = None
        self.exception = None
