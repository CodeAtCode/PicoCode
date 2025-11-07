"""
Unit tests for IndexSyncAgent.

Tests the background agent that synchronizes project indexing status.
"""

import unittest
import time
import tempfile
import os
from unittest.mock import Mock, MagicMock, patch
import sqlite3


class MockDBClient:
    """Mock database client for testing."""
    
    def __init__(self):
        self.projects = []
        self.updates = []
        
    def list_projects(self):
        return self.projects
    
    def get_project_stats(self, db_path):
        """Return mock stats based on db_path."""
        if "empty" in db_path:
            return {"file_count": 0, "embedding_count": 0}
        elif "indexed" in db_path:
            return {"file_count": 10, "embedding_count": 50}
        return {"file_count": 5, "embedding_count": 0}
    
    def update_project_status(self, project_id, status, last_indexed_at=None):
        """Record status updates."""
        self.updates.append({
            "project_id": project_id,
            "status": status,
            "last_indexed_at": last_indexed_at
        })


class TestIndexSyncAgent(unittest.TestCase):
    """Test cases for IndexSyncAgent."""
    
    def setUp(self):
        """Set up test fixtures."""
        # Import here to avoid module import errors during discovery
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
        
        from ai.agents.index_sync_agent import IndexSyncAgent
        self.IndexSyncAgent = IndexSyncAgent
        
        self.mock_db = MockDBClient()
        self.temp_dir = tempfile.mkdtemp()
        
    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)
    
    def test_agent_initialization(self):
        """Test agent can be initialized with correct parameters."""
        agent = self.IndexSyncAgent(
            db_client=self.mock_db,
            interval_seconds=10,
            enabled=True
        )
        
        self.assertEqual(agent.interval_seconds, 10)
        self.assertEqual(agent.enabled, True)
        self.assertFalse(agent.is_running())
    
    def test_agent_minimum_interval(self):
        """Test agent enforces minimum interval of 5 seconds."""
        agent = self.IndexSyncAgent(
            db_client=self.mock_db,
            interval_seconds=1,  # Too low
            enabled=True
        )
        
        # Should be clamped to 5
        self.assertEqual(agent.interval_seconds, 5)
    
    def test_agent_disabled(self):
        """Test disabled agent does not start."""
        agent = self.IndexSyncAgent(
            db_client=self.mock_db,
            interval_seconds=10,
            enabled=False
        )
        
        agent.start()
        self.assertFalse(agent.is_running())
    
    def test_agent_start_stop(self):
        """Test agent can be started and stopped."""
        agent = self.IndexSyncAgent(
            db_client=self.mock_db,
            interval_seconds=10,
            enabled=True
        )
        
        agent.start()
        self.assertTrue(agent.is_running())
        
        time.sleep(0.1)  # Give thread time to start
        
        agent.stop(timeout=2.0)
        self.assertFalse(agent.is_running())
    
    def test_agent_reconcile_empty_project(self):
        """Test reconciliation of project with no indexed files."""
        # Create a temporary database
        db_path = os.path.join(self.temp_dir, "empty.db")
        
        self.mock_db.projects = [{
            "id": "test123",
            "path": self.temp_dir,
            "database_path": db_path,
            "status": "indexing"
        }]
        
        agent = self.IndexSyncAgent(
            db_client=self.mock_db,
            interval_seconds=10,
            enabled=True
        )
        
        # Run one reconciliation
        agent._reconcile_once()
        
        # Should have updated status to "created" since no files
        self.assertEqual(len(self.mock_db.updates), 1)
        self.assertEqual(self.mock_db.updates[0]["status"], "created")
    
    def test_agent_reconcile_indexed_project(self):
        """Test reconciliation of fully indexed project."""
        db_path = os.path.join(self.temp_dir, "indexed.db")
        
        # Create a dummy database file so it "exists"
        open(db_path, 'w').close()
        
        self.mock_db.projects = [{
            "id": "test456",
            "path": self.temp_dir,
            "database_path": db_path,
            "status": "indexing"
        }]
        
        agent = self.IndexSyncAgent(
            db_client=self.mock_db,
            interval_seconds=10,
            enabled=True
        )
        
        # Run one reconciliation
        agent._reconcile_once()
        
        # Should have updated status to "ready" since has embeddings
        self.assertEqual(len(self.mock_db.updates), 1)
        self.assertEqual(self.mock_db.updates[0]["status"], "ready")
    
    def test_agent_reconcile_missing_path(self):
        """Test reconciliation of project with missing path."""
        db_path = os.path.join(self.temp_dir, "test.db")
        missing_path = "/nonexistent/path/that/does/not/exist"
        
        self.mock_db.projects = [{
            "id": "test789",
            "path": missing_path,
            "database_path": db_path,
            "status": "ready"
        }]
        
        agent = self.IndexSyncAgent(
            db_client=self.mock_db,
            interval_seconds=10,
            enabled=True
        )
        
        # Run one reconciliation
        agent._reconcile_once()
        
        # Should have updated status to "error" due to missing path
        self.assertEqual(len(self.mock_db.updates), 1)
        self.assertEqual(self.mock_db.updates[0]["status"], "error")
    
    def test_agent_get_status(self):
        """Test getting agent status."""
        agent = self.IndexSyncAgent(
            db_client=self.mock_db,
            interval_seconds=15,
            enabled=True
        )
        
        status = agent.get_status()
        
        self.assertEqual(status["enabled"], True)
        self.assertEqual(status["running"], False)
        self.assertEqual(status["interval_seconds"], 15)
    
    def test_agent_multiple_start_calls(self):
        """Test that multiple start calls are safe (no-op)."""
        agent = self.IndexSyncAgent(
            db_client=self.mock_db,
            interval_seconds=10,
            enabled=True
        )
        
        agent.start()
        self.assertTrue(agent.is_running())
        
        # Call start again - should be no-op
        agent.start()
        self.assertTrue(agent.is_running())
        
        agent.stop(timeout=2.0)


if __name__ == "__main__":
    unittest.main()
