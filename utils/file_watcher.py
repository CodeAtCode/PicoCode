"""
File Watcher - Monitor project directories for file changes.

This module provides a background file watcher that monitors registered projects
for file system changes (new files, modifications, deletions) and can trigger
automatic re-indexing when changes are detected.

English Prompt for the File Watcher:
You are a File System Monitor for PicoCode projects.

Your responsibilities:
1. Watch all registered project directories for file system changes
2. Detect new files, modified files, and deleted files
3. Filter changes to only include relevant code files (exclude build artifacts, dependencies)
4. Trigger incremental re-indexing when significant changes are detected
5. Maintain a queue of pending changes to process efficiently
6. Handle errors gracefully without crashing the watcher

Guidelines:
- Use efficient file system monitoring (inotify on Linux, FSEvents on macOS, etc.)
- Debounce rapid changes to avoid excessive indexing
- Respect .gitignore patterns when monitoring
- Run in a background thread without blocking the main application
- Provide status information about monitored projects
"""

import os
import time
import threading
from typing import Dict, List, Optional, Callable, Set
from pathlib import Path
from datetime import datetime, timezone
import logging


class FileWatcher:
    """
    Background file watcher for monitoring project directories.
    
    Monitors registered projects for file system changes and can trigger
    automatic re-indexing when changes are detected.
    """
    
    # Class constants for configuration limits
    MIN_DEBOUNCE_SECONDS = 1
    MIN_CHECK_INTERVAL = 5
    
    def __init__(
        self,
        logger: Optional[logging.Logger] = None,
        enabled: bool = True,
        debounce_seconds: int = 5,
        check_interval: int = 10
    ):
        """
        Initialize the FileWatcher.
        
        Args:
            logger: Optional logger instance (creates default if None)
            enabled: Whether the watcher is enabled (default: True)
            debounce_seconds: Seconds to wait before processing changes (default: 5)
            check_interval: Seconds between directory scans (default: 10)
        """
        self.enabled = enabled
        self.debounce_seconds = max(self.MIN_DEBOUNCE_SECONDS, debounce_seconds)
        self.check_interval = max(self.MIN_CHECK_INTERVAL, check_interval)
        
        # Set up logger
        if logger:
            self.logger = logger
        else:
            self.logger = logging.getLogger(__name__)
        
        # Watched projects: {project_id: {"path": str, "last_scan": float, "file_hashes": dict}}
        self._watched_projects: Dict[str, Dict] = {}
        self._lock = threading.Lock()
        
        # Threading control
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        
        # Change callbacks
        self._on_change_callback: Optional[Callable] = None
        
        # File extensions to monitor
        self._monitored_extensions = {
            '.py', '.js', '.ts', '.jsx', '.tsx', '.java', '.go', '.rs', 
            '.c', '.cpp', '.h', '.hpp', '.cs', '.php', '.rb', '.swift',
            '.kt', '.scala', '.sql', '.sh', '.bash', '.yaml', '.yml',
            '.json', '.xml', '.html', '.css', '.scss', '.md', '.txt'
        }
        
        # Directories to ignore
        self._ignored_dirs = {
            '.git', '.svn', '.hg', 'node_modules', '__pycache__', '.venv',
            'venv', 'env', 'build', 'dist', 'target', '.idea', '.vscode',
            'bin', 'obj', '.pytest_cache', '.mypy_cache', 'coverage'
        }
        
        self.logger.info(
            f"FileWatcher initialized (debounce={self.debounce_seconds}s, "
            f"interval={self.check_interval}s, enabled={self.enabled})"
        )
    
    def start(self) -> None:
        """
        Start the background file watcher.
        
        Launches a daemon thread that periodically checks watched directories
        for changes. Safe to call multiple times (no-op if already running).
        """
        if not self.enabled:
            self.logger.info("FileWatcher is disabled, not starting")
            return
        
        if self._running:
            self.logger.warning("FileWatcher is already running")
            return
        
        self._stop_event.clear()
        self._running = True
        self._thread = threading.Thread(
            target=self._watch_loop,
            name="FileWatcher",
            daemon=False
        )
        self._thread.start()
    
    def stop(self, timeout: float = 5.0) -> None:
        """
        Stop the background watcher gracefully.
        
        Args:
            timeout: Maximum time to wait for thread to stop (seconds)
        """
        if not self._running:
            self.logger.debug("FileWatcher is not running")
            return
        
        self.logger.info("Stopping FileWatcher...")
        self._stop_event.set()
        
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                self.logger.warning(
                    f"FileWatcher thread did not stop within {timeout}s"
                )
            else:
                self.logger.info("FileWatcher stopped")
        
        self._running = False
        self._thread = None
    
    def add_project(self, project_id: str, project_path: str) -> None:
        """
        Add a project to watch.
        
        Args:
            project_id: Unique project identifier
            project_path: Absolute path to project directory
        """
        if not os.path.exists(project_path):
            self.logger.warning(f"Cannot watch non-existent path: {project_path}")
            return
        
        if not os.path.isdir(project_path):
            self.logger.warning(f"Cannot watch non-directory: {project_path}")
            return
        
        with self._lock:
            if project_id in self._watched_projects:
                self.logger.debug(f"Project {project_id} already watched")
                return
            
            self._watched_projects[project_id] = {
                "path": project_path,
                "last_scan": 0,
                "file_hashes": self._scan_directory(project_path),
                "pending_changes": set()
            }
            
            self.logger.info(f"Now watching project {project_id} at {project_path}")
    
    def remove_project(self, project_id: str) -> None:
        """
        Remove a project from watching.
        
        Args:
            project_id: Project identifier to stop watching
        """
        with self._lock:
            if project_id in self._watched_projects:
                del self._watched_projects[project_id]
                self.logger.info(f"Stopped watching project {project_id}")
    
    def set_on_change_callback(self, callback: Callable[[str, List[str]], None]) -> None:
        """
        Set a callback to be called when changes are detected.
        
        Args:
            callback: Function(project_id: str, changed_files: List[str]) to call on changes.
                     changed_files is a list of relative file paths that changed.
        """
        self._on_change_callback = callback
    
    def _watch_loop(self) -> None:
        """
        Main watcher loop that runs in the background thread.
        
        Periodically checks watched directories for changes.
        """
        
        while not self._stop_event.is_set():
            try:
                self._check_all_projects()
            except Exception as e:
                self.logger.exception(f"Error during watch loop: {e}")
            
            # Wait for the interval or until stop is signaled
            self._stop_event.wait(timeout=self.check_interval)
    
    def _check_all_projects(self) -> None:
        """Check all watched projects for changes."""
        with self._lock:
            projects_to_check = list(self._watched_projects.items())
        
        for project_id, project_info in projects_to_check:
            try:
                self._check_project(project_id, project_info)
            except Exception as e:
                self.logger.error(f"Error checking project {project_id}: {e}")
    
    def _check_project(self, project_id: str, project_info: Dict) -> None:
        """
        Check a single project for changes.
        
        Args:
            project_id: Project identifier
            project_info: Project information dictionary
        """
        project_path = project_info["path"]
        
        if not os.path.exists(project_path):
            self.logger.warning(f"Project path no longer exists: {project_path}")
            return
        
        # Scan current state
        current_hashes = self._scan_directory(project_path)
        old_hashes = project_info["file_hashes"]
        
        # Detect changes
        changed_files = []
        
        # New or modified files
        for filepath, filehash in current_hashes.items():
            if filepath not in old_hashes or old_hashes[filepath] != filehash:
                changed_files.append(filepath)
        
        # Deleted files
        for filepath in old_hashes:
            if filepath not in current_hashes:
                changed_files.append(filepath)
        
        if changed_files:
            self.logger.info(
                f"Detected {len(changed_files)} changed file(s) in project {project_id}"
            )
            
            # Update stored hashes
            with self._lock:
                if project_id in self._watched_projects:
                    self._watched_projects[project_id]["file_hashes"] = current_hashes
                    self._watched_projects[project_id]["last_scan"] = time.time()
                    
                    # Add to pending changes
                    self._watched_projects[project_id]["pending_changes"].update(changed_files)
            
            # Call callback if set
            if self._on_change_callback:
                try:
                    self._on_change_callback(project_id, changed_files)
                except Exception as e:
                    self.logger.error(f"Error in change callback: {e}")
    
    def _scan_directory(self, directory: str) -> Dict[str, str]:
        """
        Scan a directory and return a dictionary of file paths to file signatures.
        
        Uses both modification time and file size for better change detection.
        
        Args:
            directory: Directory path to scan
        
        Returns:
            Dictionary mapping relative file paths to signature (mtime:size)
        """
        file_hashes = {}
        
        try:
            for root, dirs, files in os.walk(directory):
                # Filter out ignored directories
                dirs[:] = [d for d in dirs if d not in self._ignored_dirs]
                
                for filename in files:
                    # Check if file extension is monitored
                    ext = Path(filename).suffix.lower()
                    if ext not in self._monitored_extensions:
                        continue
                    
                    filepath = os.path.join(root, filename)
                    
                    try:
                        # Use both modification time and file size as signature
                        # Format: "mtime|size" (using pipe as separator to avoid conflicts)
                        stat = os.stat(filepath)
                        mtime = stat.st_mtime
                        size = stat.st_size
                        relative_path = os.path.relpath(filepath, directory)
                        file_hashes[relative_path] = f"{mtime}|{size}"
                    except (OSError, ValueError):
                        # Skip files we can't access
                        continue
        
        except Exception as e:
            self.logger.error(f"Error scanning directory {directory}: {e}")
        
        return file_hashes
    
    def get_watched_projects(self) -> List[str]:
        """
        Get list of currently watched project IDs.
        
        Returns:
            List of project IDs being watched
        """
        with self._lock:
            return list(self._watched_projects.keys())
    
    def get_status(self) -> Dict:
        """
        Get the current status of the file watcher.
        
        Returns:
            Dictionary with watcher status information
        """
        with self._lock:
            watched_count = len(self._watched_projects)
            total_pending = sum(
                len(p.get("pending_changes", set())) 
                for p in self._watched_projects.values()
            )
        
        return {
            "enabled": self.enabled,
            "running": self._running,
            "check_interval": self.check_interval,
            "debounce_seconds": self.debounce_seconds,
            "watched_projects": watched_count,
            "pending_changes": total_pending,
            "thread_alive": self._thread.is_alive() if self._thread else False
        }
    
    def is_running(self) -> bool:
        """
        Check if the watcher is currently running.
        
        Returns:
            True if watcher is running, False otherwise
        """
        return self._running
