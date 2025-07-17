[![MseeP.ai Security Assessment Badge](https://mseep.net/pr/iluxu-llmbasedos-badge.png)](https://mseep.ai/app/iluxu-llmbasedos)

# llmbasedos

`llmbasedos` is a system designed to expose local capabilities (files, mail, sync, agents) to various "host" applications (LLM frontends, VS Code plugins, etc.) via the **Model Context Protocol (MCP)**. It serves as a secure and standardized bridge between Large Language Models and your personal data and tools.

Primarily deployed via **Docker**, `llmbasedos` can also be built as a minimal Arch Linux based ISO for dedicated appliances.

## Core Architecture (Docker Deployment)

The system is composed of several key Python components, typically running within a single Docker container managed by **Supervisord**:

1.  **Gateway (`llmbasedos_pkg/gateway/`)**:
    *   Central MCP router (FastAPI + WebSockets/UNIX Sockets).
    *   Handles authentication (licence key from `/etc/llmbasedos/lic.key`, tiers from `/etc/llmbasedos/licence_tiers.yaml`), authorization, and rate limiting.
    *   Dynamically discovers backend server capabilities by reading `/run/mcp/*.cap.json` files.
    *   Proxies `mcp.llm.chat` to configured LLMs (OpenAI, llama.cpp, etc., defined in `AVAILABLE_LLM_MODELS` in gateway config), applying quotas.

2.  **MCP Servers (`llmbasedos_pkg/servers/*/`)**:
    *   Python daemons, each providing specific MCP capabilities over a UNIX socket.
    *   Built using a common `llmbasedos.mcp_server_framework.MCPServer` base class.
    *   Each server publishes its `SERVICE_NAME.cap.json` to `/run/mcp/` for discovery by the gateway.
    *   **FS Server (`servers/fs/`)**: File system operations (list, read, write, delete, semantic embed/search via SentenceTransformers/FAISS). Path access is confined within a configurable "virtual root" (e.g., `/mnt/user_data` in Docker). FAISS index stored in a persistent volume.
    *   **Sync Server (`servers/sync/`)**: Wrapper for `rclone` for file synchronization tasks. Requires `rclone.conf`.
    *   **Mail Server (`servers/mail/`)**: IMAP client for email access and iCalendar parsing. Accounts configured in `/etc/llmbasedos/mail_accounts.yaml`.
    *   **Agent Server (`servers/agent/`)**: Executes agentic workflows defined in YAML files (from `/etc/llmbasedos/workflows`), potentially interacting with Docker (if Docker-in-Docker setup or socket passthrough) or HTTP services.

3.  **Shell (`llmbasedos_pkg/shell/`)**:
    *   `luca-shell`: An interactive Python REPL (using `prompt_toolkit`) that runs on your **host machine** (or wherever you need a client).
    *   Acts as an MCP client, connecting to the gateway's WebSocket endpoint.
    *   Translates shell commands (built-in aliases like `ls`, `cat`, or direct MCP calls) to the gateway.
    *   Supports command history, basic autocompletion, and LLM chat streaming.

## Communication Protocol

*   All inter-component communication uses **Model Context Protocol (MCP)**.
*   MCP is implemented as JSON-RPC 2.0 messages.
*   Transport:
    *   External hosts (like `luca-shell`) to Gateway: WebSocket (e.g., `ws://localhost:8000/ws`).
    *   Gateway to Backend Servers (within Docker): UNIX domain sockets (e.g., `/run/mcp/fs.sock`) with JSON messages delimited by `\0`.

## Security Considerations

*   **Path Validation**: FS server operations are restricted by a "virtual root" to prevent arbitrary file system access.
*   **Licence & Auth**: Gateway enforces access based on a licence key and configured tiers.
*   **Secrets**: API keys (OpenAI, etc.) and email passwords **must be provided via environment variables** (e.g., through an `.env` file with `docker-compose`) and are not part of the image.
*   **Docker Volumes**: Sensitive configuration files (`lic.key`, `mail_accounts.yaml`, `rclone.conf`) are mounted as read-only volumes into the container.

## Deployment (Docker - Recommended)

1.  **Prerequisites**: Docker and Docker Compose (or `docker compose` CLI v2).
2.  **Clone the repository.**
3.  **Project Structure**: Ensure your Python application code (`gateway/`, `servers/`, `shell/`, `mcp_server_framework.py`, `common_utils.py`) is inside a top-level directory (e.g., `llmbasedos_src/`) within your project root. This `llmbasedos_src/` directory will be treated as the `llmbasedos` Python package inside the Docker image.
4.  **Configuration**:
    *   At the project root (next to `docker-compose.yml`):
        *   Create/Edit `.env`: Define `OPENAI_API_KEY` and other environment variables (e.g., `LLMBDO_LOG_LEVEL`).
        *   Create/Edit `lic.key`: Example: `FREE:youruser:2025-12-31`
        *   Create/Edit `mail_accounts.yaml`: For mail server accounts.
        *   Create/Edit `gateway/licence_tiers.yaml`: To define licence tiers (if you want to override defaults that might be in `gateway/config.py`).
        *   Create `./workflows/` directory and add your agent workflow YAML files.
        *   Create `./user_files/` directory and add any files you want the FS server to access.
        *   Ensure `supervisord.conf` is present and correctly configured (especially the `directory` for each program).
5.  **Build the Docker Image**:
    ```bash
    docker compose build
    ```
6.  **Run the Services**:
    ```bash
    docker compose up
    ```
7.  **Interact**:
    *   The MCP Gateway will be accessible on `ws://localhost:8000/ws` (or the port configured via `LLMBDO_GATEWAY_EXPOSED_PORT` in `.env`).
    *   Run `luca-shell` from your host machine (ensure its Python environment has dependencies from `llmbasedos_src/shell/requirements.txt` installed):
        ```bash
        # From project root, assuming venv is activated
        python -m llmbasedos_src.shell.luca
        ```
    *   Inside `luca-shell`, type `connect` (if not auto-connected), then `mcp.hello`.

## Development Cycle (with Docker)

*   **Initial Build**: `docker compose build` (needed if `Dockerfile` or `requirements.txt` files change).
*   **Code Changes**: Modify Python code in your local `llmbasedos_src/` directory.
*   **Apply Changes**:
    *   The `docker-compose.yml` is set up to mount `./llmbasedos_src` into `/opt/app/llmbasedos` in the container.
    *   Restart services to pick up Python code changes:
        ```bash
        docker compose restart llmbasedos_instance 
        # OR, for specific service restart:
        # docker exec -it llmbasedos_instance supervisorctl restart mcp-gateway 
        ```
*   **Configuration Changes**: If you modify mounted config files (`supervisord.conf`, `licence_tiers.yaml`, etc.), a `docker-compose restart llmbasedos_instance` is also sufficient.

## ISO Build (Alternative/Legacy)

The `iso/` directory contains scripts for building a bootable Arch Linux ISO. This is a more complex deployment method, with Docker being the preferred route for most use cases. (Refer to older README versions or `iso/build.sh` for details if needed).

## Changelog (Recent Major Changes)

*   **[2025-05-22] - Dockerization & Framework Refactor**
    *   Primary deployment model shifted to Docker using a single image managed by Supervisord.
    *   Introduced `MCPServer` framework in `llmbasedos_pkg/mcp_server_framework.py` for all backend servers (`fs`, `sync`, `mail`, `agent`), standardizing initialization, MCP method registration, socket handling, and capability publishing.
    *   Project source code refactored into a main Python package (e.g., `llmbasedos_src/` on host, becoming `llmbasedos` package in Docker) for cleaner imports and module management.
    *   Gateway (`gateway/main.py`) updated to use FastAPI's `lifespan` manager for startup/shutdown events.
    *   Shell (`shell/luca.py`) refactored into `ShellApp` class for better state and connection management.
    *   Corrected numerous import errors and runtime issues related to module discovery, Python path, and library API changes (e.g., `websockets`, `logging.config`).
    *   Configuration for licence tiers (`gateway/licence_tiers.yaml`) and mail accounts (`mail_accounts.yaml`) externalized.
    *   Hugging Face cache directory configured via `HF_HOME` for `fs_server` to resolve permission issues.
    *   Added `jsonschema` dependency for MCP parameter validation within `MCPServer` framework.
    *   `supervisord.conf` now correctly sets working directories and includes sections for `supervisorctl` interaction.
    *   `Dockerfile` optimized with multi-stage builds and correct user/permission setup.
    *   `docker-compose.yml` configured for easy launch, volume mounting (including live code mounting for development), and environment variable setup.

## Future Improvements & TODOs

*   Robust OAuth2 support for mail server.
*   Secure credential management (Vault, system keyring integration).
*   Advanced shell features (path/argument tab completion, job control).
*   More sophisticated workflow engine and step types for the agent server.
*   Web UI for management.
*   Comprehensive test suite.
*   Security hardening.
*   (Consider removing or clearly marking the ISO build الجزء as legacy/advanced if Docker is the main focus).
