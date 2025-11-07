"""
Shared application state module.

This module provides a central location for shared application state
to avoid circular dependencies between modules.
"""

from typing import Optional

# Global FileWatcher instance
file_watcher: Optional[object] = None


def set_file_watcher(watcher):
    """
    Set the global file watcher instance.
    
    Args:
        watcher: FileWatcher instance
    """
    global file_watcher
    file_watcher = watcher


def get_file_watcher():
    """
    Get the global file watcher instance.
    
    Returns:
        FileWatcher instance or None if not initialized
    """
    return file_watcher
