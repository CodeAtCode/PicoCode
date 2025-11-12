# PicoCode PyCharm Plugin

PyCharm/IntelliJ IDEA plugin for PicoCode RAG Assistant with per-project persistent storage.

## Features

- **Per-Project Storage**: Indexes each project into `.local_rag` directory
- **Secure API Key Storage**: Uses IDE's built-in password safe
- **Real-time Responses**: Streams responses from the coding model
- **File Navigation**: Click on retrieved files to open them in the editor
- **Progress Tracking**: Visual progress indicator during indexing
- **Status Bar Integration**: Shows indexing status in the IDE status bar

## Building the Plugin

```bash
cd ide-plugins
# Build with default version (0.2.0)
./gradlew buildPlugin

# Or build with a specific version
./gradlew buildPlugin -Pversion=0.2.1
```

The plugin ZIP will be in `build/distributions/` with the name `intellij-plugin-{version}.zip`.

## Installation

1. Build the plugin or download from releases
2. In PyCharm/IntelliJ IDEA: `Settings` → `Plugins` → `⚙️` → `Install Plugin from Disk`
3. Select the plugin ZIP file
4. Restart IDE

## Usage

1. Open the PicoCode RAG tool window (right sidebar)
2. Click "Start Server" to launch the Python backend
3. Click "Index Project" to index your current project
4. Ask questions in the query box and click "Query"

### Status Bar Widget

The status bar (bottom of the IDE) shows the current indexing status:

- **⚡ PicoCode: Indexing...** - Project is currently being indexed
- **✓ PicoCode: N files** - Project is indexed and ready (shows file count)
- **○ PicoCode: Not indexed** - Project created but not indexed yet
- **✗ PicoCode: Error** - Indexing error occurred
- **PicoCode** - Status unknown (server may not be running)

Hover over the status to see detailed information including file and embedding counts.

## Requirements

- PyCharm/IntelliJ IDEA 2023.1 or later
- Python 3.8+ installed and in PATH
- PicoCode backend dependencies installed (`pip install -r pyproject.toml`)

## Architecture

1. **Server Management**: Plugin starts Python server as subprocess in project directory
2. **API Communication**: HTTP REST API for project management and queries
3. **Secure Storage**: API keys stored using IntelliJ's `PasswordSafe` API
4. **File Navigation**: Uses IntelliJ's Open API to navigate to retrieved files
5. **Status Polling**: Status bar widget polls `/api/projects/{id}` endpoint every 5 seconds

## API Endpoints Used

- `POST /api/projects` - Create/get project
- `GET /api/projects/{id}` - Get project status and indexing stats
- `POST /api/projects/index` - Start indexing
- `POST /api/code` - Query with RAG context
- `GET /api/projects` - List projects

## Development

To modify the plugin:

1. Open `ide-plugins/` in IntelliJ IDEA
2. Make changes to Kotlin files
3. Run `./gradlew runIde` to test in a sandbox IDE
4. Build with `./gradlew buildPlugin` (optionally with `-Pversion=x.y.z`)

