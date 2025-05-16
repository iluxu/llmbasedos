# llmbasedos

`llmbasedos` is a minimal Arch Linux based distribution designed to expose local capabilities (files, mail, sync, agents) to various "host" applications (LLM frontends like Claude Desktop, VS Code plugins, ChatGPT, etc.) via the **Model Context Protocol (MCP)**.

It serves as a secure and standardized bridge between Large Language Models and your personal data and tools.

## Core Architecture

The system is built upon `systemd` services written primarily in Python, communicating via MCP:

1.  **Gateway (`llmbasedos/gateway/`)**:
    *   Central MCP router (FastAPI + WebSockets/UNIX Sockets).
    *   Handles authentication (licence key in `/etc/llmbasedos/lic.key`), authorization, and rate limiting.
    *   Dynamically discovers backend server capabilities from `/run/mcp/*.cap.json`.
    *   Proxies `mcp.llm.chat` to configured LLMs (OpenAI, llama.cpp), applying quotas.

2.  **MCP Servers (`llmbasedos/servers/*/`)**:
    *   Python daemons, each providing specific MCP capabilities over a UNIX socket.
    *   Built using a common `llmbasedos.mcp_server_framework.MCPServer` base class.
    *   Each server declares its methods in a `caps.json` file.
    *   **FS Server (`servers/fs/`)**: File system operations (list, read, write, delete, semantic embed/search via SentenceTransformers/FAISS). Path access is confined within a configurable "virtual root".
    *   **Sync Server (`servers/sync/`)**: Wrapper for `rclone` for file synchronization tasks.
    *   **Mail Server (`servers/mail/`)**: IMAP client for email access and iCalendar parsing. Accounts configured in `/etc/llmbasedos/mail_accounts.yaml`.
    *   **Agent Server (`servers/agent/`)**: Executes agentic workflows defined in YAML files (from `/etc/llmbasedos/workflows`), potentially interacting with Docker or HTTP services like n8n-lite.

3.  **Shell (`llmbasedos/shell/`)**:
    *   `luca-shell`: An interactive Python REPL (using `prompt_toolkit`) running on TTY1.
    *   Acts as an MCP client, translating shell commands (built-in aliases like `ls`, `cat`, or direct MCP calls) to the gateway.
    *   Supports command history, basic autocompletion, and LLM chat streaming.

## Communication Protocol

*   All inter-component communication uses **Model Context Protocol (MCP)**.
*   MCP is implemented as JSON-RPC 2.0 messages.
*   Transport:
    *   External hosts to Gateway: WebSocket (`ws://<host>:8000/ws`).
    *   Shell to Gateway: WebSocket (can be configured).
    *   Gateway to Backend Servers: UNIX domain sockets (e.g., `/run/mcp/fs.sock`) with JSON messages delimited by `\0`.

## Security Considerations

*   **Path Validation**: FS server operations are restricted by a "virtual root" (defaulting to user's home, configurable) to prevent arbitrary file system access.
*   **Licence & Auth**: Gateway enforces access based on a licence key, controlling rate limits, allowed capabilities, and LLM access.
*   **UNIX Sockets**: Permissions on `/run/mcp` and individual sockets are set to restrict access to authorized users/groups (e.g., `llmuser` and `llmgroup`).
*   **Secrets**: API keys (OpenAI, etc.) and email passwords should be managed securely (e.g., via environment files like `/etc/llmbasedos/gateway.env`, system keyring, or a vault solution), not hardcoded. The provided setup is for demonstration.

## Build & Installation

*   **ISO Build**: An Arch Linux ISO is built using `mkarchiso` via `iso/build.sh`.
    *   It installs system dependencies and Python packages (via pip within the chroot).
    *   Copies the `llmbasedos` application code to `/opt/llmbasedos`.
    *   Sets up `systemd` units (from `iso/systemd_units/`) for all services.
    *   Configures a live environment with user `llmuser` (password `livepass`) auto-logging into `luca-shell` on TTY1.
*   **Installation to Disk**:
    *   The ISO can be used to perform a standard Arch Linux installation.
    *   After base install, `/root/llmbasedos_postinstall.sh` (copied from `iso/postinstall.sh`) should be run to:
        *   Create `llmuser` and `llmgroup`.
        *   Set permissions for application, config, log, and runtime directories.
        *   Enable and configure systemd services for llmbasedos components to run on boot.
        *   Configure TTY1 for `luca-shell` for the `llmuser`.

## Development

*   **Framework**: `llmbasedos.mcp_server_framework.MCPServer` provides a base for creating new MCP servers.
*   **Utilities**: `llmbasedos.common_utils.py` contains shared functions like path validation.
*   **Capabilities**: Each server defines its MCP methods in its `caps.json` file, including `params_schema` for automatic input validation.
*   **Blocking Tasks**: Long-running or blocking operations in servers are handled using a `ThreadPoolExecutor` (`server.run_in_executor(...)`).
*   **Logging**: Structured JSON logging is available (configurable via `LLMBDO_..._LOG_FORMAT=json`).

## Future Improvements & TODOs

*   Robust OAuth2 support for mail server (Gmail, Microsoft 365).
*   Secure credential management (Vault, system keyring).
*   Advanced shell features (tab completion for paths/arguments, job control).
*   More sophisticated workflow engine for the agent server.
*   Web UI for managing services, workflows, and viewing logs.
*   Comprehensive test suite (unit, integration, E2E).
*   Hardening security (sandboxing, AppArmor/SELinux profiles).
*   Packaging for easier deployment beyond ISO (e.g., Docker containers for components, system packages).
