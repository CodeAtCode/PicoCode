"""
IndexSyncAgent - Background agent for synchronizing project indexing status.

This agent periodically reconciles the indexing status of projects between
the vector database/index storage and the application database, ensuring
the web UI displays accurate and up-to-date information.
"""

import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Callable
import os

# English-only agent prompt for maintainability
AGENT_PROMPT = """
You are the Index Synchronization Agent for PicoCode.

Your responsibilities:
1. Monitor all registered projects in the system
2. Check the actual indexing status by inspecting project databases and storage
3. Reconcile differences between stored status and actual status
4. Update project metadata with accurate status information
5. Track indexing progress, completion times, and version information
6. Report any errors or inconsistencies found during reconciliation

Guidelines:
- Run reconciliation at regular intervals (configurable)
- Be efficient - only update when status has changed
- Handle errors gracefully and log them appropriately
- Never block the main application thread
- Provide clear status information for the web UI

Status values:
- "created": Project registered but not yet indexed
- "indexing": Indexing in progress
- "ready": Indexing completed successfully
- "error": Indexing failed or error detected
"""


class IndexSyncAgent:
    """
    Background agent that synchronizes project indexing status.
    
    This agent runs in a background thread and periodically checks the
    actual indexing status of projects by inspecting their databases,
    then updates the project registry with accurate status information.
    """
    
    def __init__(
        self,
        db_client: Any,
        index_client: Optional[Any] = None,
        interval_seconds: int = 30,
        logger: Optional[Any] = None,
        enabled: bool = True
    ):
        """
        Initialize the IndexSyncAgent.
        
        Args:
            db_client: Database client or operations module for accessing project data
            index_client: Optional index client for checking vector storage status
            interval_seconds: Interval between reconciliation runs (default: 30)
            logger: Optional logger instance (creates default if None)
            enabled: Whether the agent is enabled (default: True)
        """
        self.db_client = db_client
        self.index_client = index_client
        self.interval_seconds = max(5, interval_seconds)  # Minimum 5 seconds
        self.enabled = enabled
        
        # Set up logger
        if logger:
            self.logger = logger
        else:
            import logging
            self.logger = logging.getLogger(__name__)
        
        # Threading control
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        
        self.logger.info(
            f"IndexSyncAgent initialized (interval={self.interval_seconds}s, enabled={self.enabled})"
        )
    
    def start(self) -> None:
        """
        Start the background synchronization agent.
        
        Launches a daemon thread that periodically reconciles project
        indexing status. Safe to call multiple times (no-op if already running).
        """
        if not self.enabled:
            self.logger.info("IndexSyncAgent is disabled, not starting")
            return
        
        if self._running:
            self.logger.warning("IndexSyncAgent is already running")
            return
        
        self._stop_event.clear()
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop,
            name="IndexSyncAgent",
            daemon=True
        )
        self._thread.start()
        self.logger.info("IndexSyncAgent started")
    
    def stop(self, timeout: float = 5.0) -> None:
        """
        Stop the background agent gracefully.
        
        Args:
            timeout: Maximum time to wait for thread to stop (seconds)
        """
        if not self._running:
            self.logger.debug("IndexSyncAgent is not running")
            return
        
        self.logger.info("Stopping IndexSyncAgent...")
        self._stop_event.set()
        
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                self.logger.warning(
                    f"IndexSyncAgent thread did not stop within {timeout}s"
                )
            else:
                self.logger.info("IndexSyncAgent stopped")
        
        self._running = False
        self._thread = None
    
    def _run_loop(self) -> None:
        """
        Main agent loop that runs in the background thread.
        
        Periodically calls _reconcile_once() until stopped.
        """
        self.logger.info("IndexSyncAgent loop started")
        
        while not self._stop_event.is_set():
            try:
                self._reconcile_once()
            except Exception as e:
                self.logger.exception(f"Error during reconciliation: {e}")
            
            # Wait for the interval or until stop is signaled
            self._stop_event.wait(timeout=self.interval_seconds)
        
        self.logger.info("IndexSyncAgent loop exited")
    
    def _reconcile_once(self) -> None:
        """
        Perform one reconciliation pass.
        
        This method:
        1. Lists all projects from the registry
        2. For each project, checks actual indexing status
        3. Computes status, progress, and metadata
        4. Updates project record if status has changed
        """
        try:
            # Get all projects from the registry
            projects = self._get_all_projects()
            
            if not projects:
                self.logger.debug("No projects to reconcile")
                return
            
            self.logger.debug(f"Reconciling {len(projects)} project(s)")
            
            for project in projects:
                try:
                    self._reconcile_project(project)
                except Exception as e:
                    project_id = project.get("id", "unknown")
                    self.logger.error(
                        f"Error reconciling project {project_id}: {e}"
                    )
        
        except Exception as e:
            self.logger.exception(f"Error getting projects list: {e}")
    
    def _reconcile_project(self, project: Dict[str, Any]) -> None:
        """
        Reconcile a single project's indexing status.
        
        Args:
            project: Project metadata dictionary
        """
        project_id = project.get("id")
        project_path = project.get("path")
        db_path = project.get("database_path")
        current_status = project.get("status", "created")
        
        if not project_id or not db_path:
            self.logger.warning(f"Project missing required fields: {project}")
            return
        
        # Check if project path exists
        if project_path and not os.path.exists(project_path):
            # Project path no longer exists
            if current_status != "error":
                self.logger.warning(
                    f"Project {project_id} path does not exist: {project_path}"
                )
                self._update_project_status(
                    project_id,
                    status="error",
                    metadata={"error": "Project path not found"}
                )
            return
        
        # Check actual indexing status by inspecting the database
        actual_status = self._compute_actual_status(project_id, db_path)
        
        # If status has changed, update it
        if actual_status and actual_status != current_status:
            self.logger.info(
                f"Project {project_id} status changed: {current_status} -> {actual_status}"
            )
            
            metadata = {}
            if actual_status == "ready":
                metadata["last_indexed_at"] = datetime.utcnow().isoformat()
            
            self._update_project_status(
                project_id,
                status=actual_status,
                metadata=metadata
            )
    
    def _compute_actual_status(
        self, 
        project_id: str, 
        db_path: str
    ) -> Optional[str]:
        """
        Compute the actual indexing status by inspecting the project database.
        
        Args:
            project_id: Project identifier
            db_path: Path to project database
        
        Returns:
            Status string ("created", "ready", "error") or None if cannot determine
        """
        try:
            # Check if database exists
            if not os.path.exists(db_path):
                return "created"
            
            # Get project statistics
            stats = self._get_project_stats(db_path)
            
            if stats is None:
                # Could not read stats, but database exists
                return None
            
            file_count = stats.get("file_count", 0)
            embedding_count = stats.get("embedding_count", 0)
            
            if file_count == 0:
                # No files indexed yet
                return "created"
            
            if embedding_count > 0:
                # Has embeddings, considered ready
                return "ready"
            
            # Has files but no embeddings - could be mid-indexing or error
            # Keep current status to avoid flapping
            return None
            
        except Exception as e:
            self.logger.error(
                f"Error computing status for project {project_id}: {e}"
            )
            return None
    
    def _get_all_projects(self) -> List[Dict[str, Any]]:
        """
        Get all projects from the registry.
        
        Returns:
            List of project dictionaries
        """
        try:
            # Try to call list_projects from db_client
            if hasattr(self.db_client, "list_projects"):
                return self.db_client.list_projects()
            
            # If db_client is a module, try calling the function directly
            if callable(getattr(self.db_client, "__call__", None)):
                return self.db_client.list_projects()
            
            self.logger.error("db_client does not have list_projects method")
            return []
            
        except Exception as e:
            self.logger.exception(f"Error listing projects: {e}")
            return []
    
    def _get_project_stats(self, db_path: str) -> Optional[Dict[str, Any]]:
        """
        Get statistics for a project database.
        
        Args:
            db_path: Path to project database
        
        Returns:
            Stats dictionary with file_count and embedding_count, or None
        """
        try:
            # Try to call get_project_stats from db_client
            if hasattr(self.db_client, "get_project_stats"):
                return self.db_client.get_project_stats(db_path)
            
            self.logger.error("db_client does not have get_project_stats method")
            return None
            
        except Exception as e:
            self.logger.exception(f"Error getting project stats: {e}")
            return None
    
    def _update_project_status(
        self,
        project_id: str,
        status: str,
        metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Update project status in the registry.
        
        Args:
            project_id: Project identifier
            status: New status value
            metadata: Optional metadata to update (e.g., last_indexed_at)
        """
        try:
            # Try to call update_project_status from db_client
            if hasattr(self.db_client, "update_project_status"):
                last_indexed_at = None
                if metadata:
                    last_indexed_at = metadata.get("last_indexed_at")
                
                self.db_client.update_project_status(
                    project_id,
                    status,
                    last_indexed_at
                )
                return
            
            self.logger.error("db_client does not have update_project_status method")
            
        except Exception as e:
            self.logger.exception(f"Error updating project status: {e}")
    
    def is_running(self) -> bool:
        """
        Check if the agent is currently running.
        
        Returns:
            True if agent is running, False otherwise
        """
        return self._running
    
    def get_status(self) -> Dict[str, Any]:
        """
        Get the current status of the agent.
        
        Returns:
            Dictionary with agent status information
        """
        return {
            "enabled": self.enabled,
            "running": self._running,
            "interval_seconds": self.interval_seconds,
            "thread_alive": self._thread.is_alive() if self._thread else False
        }
