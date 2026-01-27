"""
File Watcher - Monitor project directories for file changes.

This module provides a background file watcher that monitors registered projects
for file system changes (new files, modifications, deletions) and can trigger
automatic re-indexing when changes are detected.
"""

import logging
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path


class FileWatcher:
    """
    Background file watcher for monitoring project directories.

    Monitors registered projects for file system changes and can trigger
    automatic re-indexing when changes are detected.
    """

    MIN_DEBOUNCE_SECONDS = 1
    MIN_CHECK_INTERVAL = 5

    def __init__(self, logger: logging.Logger | None = None, enabled: bool = True, debounce_seconds: int = 5, check_interval: int = 10):
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

        if logger:
            self.logger = logger
        else:
            self.logger = logging.getLogger(__name__)

        self._watched_projects: dict[str, dict] = {}
        self._lock = threading.Lock()

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = False

        self._on_change_callback: Callable | None = None

        self._monitored_extensions = {
            ".py",
            ".js",
            ".ts",
            ".jsx",
            ".tsx",
            ".java",
            ".go",
            ".rs",
            ".c",
            ".cpp",
            ".h",
            ".hpp",
            ".cs",
            ".php",
            ".rb",
            ".swift",
            ".kt",
            ".scala",
            ".sql",
            ".sh",
            ".bash",
            ".yaml",
            ".yml",
            ".json",
            ".xml",
            ".html",
            ".css",
            ".scss",
            ".md",
            ".txt",
        }

        self._ignored_dirs = {
            ".git",
            ".svn",
            ".hg",
            "node_modules",
            "__pycache__",
            ".venv",
            "venv",
            "env",
            "build",
            "dist",
            "target",
            ".idea",
            ".vscode",
            "bin",
            "obj",
            ".pytest_cache",
            ".mypy_cache",
            "coverage",
        }

        self.logger.info(f"FileWatcher initialized (debounce={self.debounce_seconds}s, interval={self.check_interval}s, enabled={self.enabled})")

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
        self._thread = threading.Thread(target=self._watch_loop, name="FileWatcher", daemon=False)
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
                self.logger.warning(f"FileWatcher thread did not stop within {timeout}s")
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

            self._watched_projects[project_id] = {"path": project_path, "last_scan": 0, "file_hashes": self._scan_directory(project_path), "pending_changes": set()}

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

    def set_on_change_callback(self, callback: Callable[[str, list[str]], None]) -> None:
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

    def _check_project(self, project_id: str, project_info: dict) -> None:
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

        current_hashes = self._scan_directory(project_path)
        old_hashes = project_info["file_hashes"]

        changed_files = []

        for filepath, filehash in current_hashes.items():
            if filepath not in old_hashes or old_hashes[filepath] != filehash:
                changed_files.append(filepath)

        for filepath in old_hashes:
            if filepath not in current_hashes:
                changed_files.append(filepath)

        if changed_files:
            self.logger.info(f"Detected {len(changed_files)} changed file(s) in project {project_id}")

            with self._lock:
                if project_id in self._watched_projects:
                    self._watched_projects[project_id]["file_hashes"] = current_hashes
                    self._watched_projects[project_id]["last_scan"] = time.time()

                    self._watched_projects[project_id]["pending_changes"].update(changed_files)

            if self._on_change_callback:
                try:
                    self._on_change_callback(project_id, changed_files)
                except Exception as e:
                    self.logger.error(f"Error in change callback: {e}")

    def _scan_directory(self, directory: str) -> dict[str, str]:
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
            stack = [directory]
            while stack:
                current_dir = stack.pop()
                try:
                    with os.scandir(current_dir) as it:
                        for entry in it:
                            if entry.is_dir(follow_symlinks=False):
                                if entry.name in self._ignored_dirs:
                                    continue
                                stack.append(entry.path)
                            elif entry.is_file(follow_symlinks=False):
                                ext = Path(entry.name).suffix.lower()
                                if ext not in self._monitored_extensions:
                                    continue
                                try:
                                    stat = entry.stat()
                                    mtime = stat.st_mtime
                                    size = stat.st_size
                                    relative_path = os.path.relpath(entry.path, directory)
                                    file_hashes[relative_path] = f"{mtime}|{size}"
                                except (OSError, ValueError):
                                    continue
                except PermissionError:
                    continue

        except Exception as e:
            self.logger.error(f"Error scanning directory {directory}: {e}")

        return file_hashes

    def get_watched_projects(self) -> list[str]:
        """
        Get list of currently watched project IDs.

        Returns:
            List of project IDs being watched
        """
        with self._lock:
            return list(self._watched_projects.keys())

    def get_status(self) -> dict:
        """
        Get the current status of the file watcher.

        Returns:
            Dictionary with watcher status information
        """
        with self._lock:
            watched_count = len(self._watched_projects)
            total_pending = sum(len(p.get("pending_changes", set())) for p in self._watched_projects.values())

        return {
            "enabled": self.enabled,
            "running": self._running,
            "check_interval": self.check_interval,
            "debounce_seconds": self.debounce_seconds,
            "watched_projects": watched_count,
            "pending_changes": total_pending,
            "thread_alive": self._thread.is_alive() if self._thread else False,
        }

    def is_running(self) -> bool:
        """
        Check if the watcher is currently running.

        Returns:
            True if watcher is running, False otherwise
        """
        return self._running
