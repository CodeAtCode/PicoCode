# PicoCode PyCharm Plugin

PyCharm/IntelliJ IDEA plugin for PicoCode RAG Assistant with per-project persistent storage.

## Features

- **Per-Project Storage**: Indexes each project into `.local_rag` directory
- **Secure API Key Storage**: Uses IDE's built-in password safe
- **Real-time Responses**: Streams responses from the coding model
- **File Navigation**: Click on retrieved files to open them in the editor
- **Progress Tracking**: Visual progress indicator during indexing

## Building the Plugin

```bash
cd plugin
./gradlew buildPlugin
```

The plugin ZIP will be in `build/distributions/`.

## Installation

1. Build the plugin or download from releases
2. In PyCharm/IntelliJ IDEA: `Settings` → `Plugins` → `⚙️` → `Install Plugin from Disk`
3. Select the plugin ZIP file
4. Restart IDE

## Usage

1. Open the PicoCode RAG tool window (right sidebar)
2. Configure your OpenAI-compatible API:
   - API Base URL (e.g., `https://api.openai.com/v1`)
   - API Key (stored securely in IDE password safe)
   - Embedding Model (e.g., `text-embedding-3-small`)
   - Coding Model (e.g., `gpt-4`)
3. Click "Save API Key" to store it securely
4. Click "Start Server" to launch the Python backend
5. Click "Index Project" to index your current project
6. Ask questions in the query box and click "Query"

## Requirements

- PyCharm/IntelliJ IDEA 2023.1 or later
- Python 3.8+ installed and in PATH
- PicoCode backend dependencies installed (`pip install -r pyproject.toml`)

## Architecture

1. **Server Management**: Plugin starts Python server as subprocess in project directory
2. **API Communication**: HTTP REST API for project management and queries
3. **Secure Storage**: API keys stored using IntelliJ's `PasswordSafe` API
4. **File Navigation**: Uses IntelliJ's Open API to navigate to retrieved files

## API Endpoints Used

- `POST /api/projects` - Create/get project
- `POST /api/projects/index` - Start indexing
- `POST /api/code` - Query with RAG context
- `GET /api/projects` - List projects

## Development

To modify the plugin:

1. Open `plugin/` in IntelliJ IDEA
2. Make changes to Kotlin files
3. Run `./gradlew runIde` to test in a sandbox IDE
4. Build with `./gradlew buildPlugin`

