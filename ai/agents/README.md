# IndexSyncAgent

## Overview

The `IndexSyncAgent` is a background maintenance agent that periodically synchronizes project indexing status between the vector database/index storage and the application database. It ensures that the web UI displays accurate and up-to-date information about project indexing progress.

## English Prompt

The agent is guided by the following prompt (embedded in the agent as a constant):

```
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
```

## Features

- **Automatic Status Reconciliation**: Periodically checks actual indexing status against stored status
- **Background Operation**: Runs in a daemon thread, non-blocking for main application
- **Configurable Interval**: Adjustable reconciliation interval (default: 30 seconds, minimum: 5 seconds)
- **Graceful Shutdown**: Clean thread termination on application shutdown
- **Error Handling**: Robust error handling with logging
- **Enable/Disable**: Can be disabled via configuration
- **Status Reporting**: Provides status information via health endpoint

## Configuration

The agent can be configured via environment variables in your `.env` file:

```bash
# Enable/disable the agent (default: true)
INDEX_SYNC_ENABLED=true

# Reconciliation interval in seconds (default: 30, minimum: 5)
INDEX_SYNC_INTERVAL=30
```

## Usage

### Automatic Start/Stop

The agent automatically starts when the FastAPI application starts (if enabled) and stops when the application shuts down. No manual intervention is required.

```python
# In main.py, the agent is automatically managed:
# - Started during application lifespan startup
# - Stopped during application lifespan shutdown
```

### Manual Usage

If you need to use the agent programmatically:

```python
from ai.agents import IndexSyncAgent
from db import operations as db_operations
from utils.logger import get_logger

# Create and start the agent
agent = IndexSyncAgent(
    db_client=db_operations,
    interval_seconds=30,
    logger=get_logger(__name__),
    enabled=True
)

agent.start()

# Later, stop the agent
agent.stop(timeout=5.0)
```

### Checking Agent Status

The agent status is included in the `/api/health` endpoint:

```bash
curl http://localhost:8080/api/health
```

Response:
```json
{
  "status": "ok",
  "version": "0.2.0",
  "features": [...],
  "index_sync_agent": {
    "enabled": true,
    "running": true,
    "interval_seconds": 30,
    "thread_alive": true
  }
}
```

## How It Works

### Reconciliation Process

1. **List Projects**: Retrieves all registered projects from the database
2. **Check Each Project**: For each project:
   - Verifies the project path exists
   - Inspects the project's database to determine actual status
   - Computes file count and embedding count
3. **Determine Status**:
   - `created`: No database or no files indexed
   - `ready`: Has files and embeddings
   - `error`: Project path doesn't exist
4. **Update if Changed**: Updates the status in the registry if it differs from stored status

### Status Logic

```
Database exists? 
  No  → "created"
  Yes → Check file_count:
    file_count == 0 → "created"
    file_count > 0 AND embedding_count > 0 → "ready"
    file_count > 0 AND embedding_count == 0 → keep current (might be indexing)
```

## Architecture

```
┌─────────────────────────┐
│   FastAPI Application   │
│      (main.py)          │
└───────────┬─────────────┘
            │ creates & manages
            ▼
┌─────────────────────────┐
│   IndexSyncAgent        │
│   (Background Thread)   │
└───────────┬─────────────┘
            │ uses
            ▼
┌─────────────────────────┐
│   db.operations         │
│   - list_projects()     │
│   - get_project_stats() │
│   - update_status()     │
└─────────────────────────┘
```

## Testing

Run the unit tests:

```bash
python -m unittest tests.test_index_sync_agent
```

Or with pytest (if installed):

```bash
pytest tests/test_index_sync_agent.py
```

## Benefits

1. **Accurate UI Information**: Web UI always shows current indexing status
2. **Automatic Recovery**: Detects and corrects status inconsistencies
3. **Error Detection**: Identifies projects with missing paths or database issues
4. **Low Overhead**: Minimal resource usage with configurable interval
5. **Maintainable**: Clean separation of concerns with clear responsibilities

## Troubleshooting

### Agent Not Starting

Check the logs for errors:
```bash
# Look for "IndexSyncAgent started successfully" or error messages
```

Verify configuration:
```bash
# In your .env file:
INDEX_SYNC_ENABLED=true
```

### Status Not Updating

Increase logging verbosity and check reconciliation logs. The agent logs:
- When it starts/stops
- When status changes are detected
- Any errors during reconciliation

### Performance Issues

If the agent impacts performance:
1. Increase the reconciliation interval (e.g., 60 seconds)
2. Check database performance (ensure WAL mode is enabled)
3. Review logs for repeated errors

## Future Enhancements

Potential improvements for future versions:

- Track index version hashes for change detection
- Report progress percentage for in-progress indexing
- Support for custom status reconciliation logic
- Metrics collection (status change counts, reconciliation duration)
- Integration with notification systems
- Support for distributed deployments (multiple agents coordinating)
