llmbasedos: The Local-First OS for Your AI Agents

llmbasedos is a secure, privacy-first operating system framework that connects Large Language Models (LLMs) to your local data and tools.

It acts as a standardized bridge, allowing AI agents and applications to securely read your files, search your documents, manage your cloud storage, and execute complex workflows—all without your data ever leaving your machine.

![alt text](https://img.shields.io/github/stars/iluxu/llmbasedos.svg?style=social&label=Star)

✨ Key Capabilities

llmbasedos provides a powerful set of local capabilities, accessible through a unified protocol:

🗂️ File System Agent (mcp.fs.*):

Secure, sandboxed access to your local file system (/mnt/user_data in Docker).

Structure-Aware Editing: Read and update complex files like .docx paragraph-by-paragraph without breaking formatting.

Local RAG: Generate embeddings for your documents (PDFs, text files) and perform semantic search, 100% offline.

🧠 LLM Gateway (mcp.llm.*):

A privacy-first proxy to external LLMs (OpenAI, Anthropic) or local models (Llama.cpp, Ollama).

Enforces rate limits, access controls, and centralizes API key management.

🤖 Agent Server (mcp.agent.*):

Orchestrates multi-step workflows defined in simple YAML files.

Chain multiple capabilities together (e.g., read a file, ask an LLM to summarize it, save the result).

Can execute external scripts or even manage Docker containers for complex tasks.

☁️ Sync Server (mcp.sync.*):

A secure wrapper around rclone to manage and run synchronization jobs with your cloud storage (Google Drive, Dropbox, etc.).

📧 Mail Server (mcp.mail.*):

IMAP client to read and process emails and calendar invites from your accounts.

🚀 Quick Start: Get Running in 2 Minutes

Get the entire llmbasedos environment running with Docker.

Prerequisites: Docker and Docker Compose.

1. Clone the Repository:

git clone https://github.com/iluxu/llmbasedos.git
cd llmbasedos


2. Configure Your Environment:
A .env file is needed for your OpenAI API key.

# Create the .env file from the example
cp .env.example .env

# Now, edit .env and add your key:
# OPENAI_API_KEY="sk-..."
IGNORE_WHEN_COPYING_START
content_copy
download
Use code with caution.
Bash
IGNORE_WHEN_COPYING_END

3. Build and Run:

docker compose up --build -d
IGNORE_WHEN_COPYING_START
content_copy
download
Use code with caution.
Bash
IGNORE_WHEN_COPYING_END

This will build the image and run all services in the background. The first build may take several minutes to download models and dependencies.

4. Interact with luca-shell:
luca is your command-line interface to llmbasedos.

Activate a virtual environment (first time only):

# Create the venv
python3 -m venv .venv
# Activate it (Linux/macOS)
source .venv/bin/activate
# Install shell dependencies
pip install -r llmbasedos_src/shell/requirements.txt
IGNORE_WHEN_COPYING_START
content_copy
download
Use code with caution.
Bash
IGNORE_WHEN_COPYING_END

Launch the shell:

# Make sure your venv is active
python -m llmbasedos_src.shell.luca
IGNORE_WHEN_COPYING_START
content_copy
download
Use code with caution.
Bash
IGNORE_WHEN_COPYING_END

First commands inside luca:

/ luca> mcp.hello
# This should list all discovered capabilities, like 'mcp.fs.list', 'mcp.agent.runWorkflow', etc.

/ luca> ls /
# This lists the files in your local './user_files' directory.
IGNORE_WHEN_COPYING_START
content_copy
download
Use code with caution.
IGNORE_WHEN_COPYING_END

You are now running llmbasedos!

💡 Example Use Case: AI-Powered Document Rewriting

Let's solve a common problem: rewriting a .docx file without destroying its formatting.

1. Place your Document:
Put a file named mydoc.docx inside the ./user_files directory on your host machine.

2. Extract Paragraphs (in luca-shell):
This command reads the docx and extracts only the text, indexed by paragraph.

/ luca> mcp.fs.read_docx_paragraphs '["/mydoc.docx"]'
IGNORE_WHEN_COPYING_START
content_copy
download
Use code with caution.
IGNORE_WHEN_COPYING_END

3. Rewrite with an LLM:
Take a paragraph's text from the output above and ask an LLM to improve it.

/ luca> llm "Rewrite this professionally: 'our sales went up a lot last quarter.'"
IGNORE_WHEN_COPYING_START
content_copy
download
Use code with caution.
IGNORE_WHEN_COPYING_END

Assistant: "The company experienced significant growth in sales during the most recent fiscal quarter."

4. Re-inject the Rewritten Text:
Update the original document with the new text, targeting the correct paragraph index (e.g., index 5).

/ luca> mcp.fs.update_docx_paragraphs '["/mydoc.docx", [{"index": 5, "new_text": "The company experienced significant growth in sales during the most recent fiscal quarter."}]]'
IGNORE_WHEN_COPYING_START
content_copy
download
Use code with caution.
IGNORE_WHEN_COPYING_END

Done. Open mydoc.docx on your computer—the text is updated, and all your formatting is perfectly intact.

Core Architecture

llmbasedos runs as a suite of microservices inside a single Docker container, managed by Supervisord. Communication happens via the Model Context Protocol (MCP), a JSON-RPC 2.0 implementation.

Gateway: The central entry point. Manages auth, discovery, and routing.

Backend Servers: Each server (fs, agent, sync, mail) exposes a specific set of capabilities over a UNIX socket.

luca-shell: A powerful Python-based client that connects to the Gateway via WebSocket.

This architecture is secure, modular, and easily extensible. Adding a new capability is as simple as creating a new server and its cap.json definition file.

Contributing

We are actively looking for contributors! Whether you want to build new agents, add capabilities, or improve the core framework, your help is welcome. Check out our CONTRIBUTING.md and join our Discord to get started.

Roadmap

Q3 2025:

Web UI for management and monitoring.

Marketplace for sharing and discovering agent workflows.

Robust OAuth2 support for mail and sync servers.

Q4 2025:

Secure credential management (Vault integration).

Advanced shell features (tab completion, job control).

First-party VS Code plugin.

