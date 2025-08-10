# llmbasedos — Local-First OS Where AI Agents Wake Up and Work

most “ai agents” are fancy chatbots with a to-do list problem: they wait for you.

llmbasedos flips it. it’s a local-first runtime where agents perceive, decide, and act — without asking permission — using any tool your machine can expose via the Model Context Protocol (MCP).

`llmbasedos` isn't just another agent framework. It's a **runtime for autonomous agents**, designed to turn your machine into a proactive partner that can perceive, reason, and act.

It achieves this through the **Model Context Protocol (MCP)**: a simple, powerful JSON-RPC layer that exposes all system capabilities—local LLMs, KV stores, web browsers, publishing APIs—as plug-and-play tools called **Arcs**.

The vision is to make agentivity real: empower AI **Sentinels** to execute complex, multi-step missions on your behalf with minimal friction and maximum autonomy.

> llmbasedos is what happens when you stop asking "how can I call a LLM?" and start asking "what if a LLM could call *everything else*?"

---

## ✨ What Makes llmbasedos Different?

*   🤖 **Truly Autonomous Agents ("Sentinels")**: Sentinels aren't just reactive chatbots. They have an "Awake" Arc, allowing them to proactively think, plan, and act based on events or internal triggers.
*   🧠 **LLM-Agnostic & Local-First**: The built-in `llm_router` intelligently routes requests to any backend—local Ollama, Gemini, OpenAI—based on policies you define. Privacy and offline capability are built-in, not afterthoughts.
*   📜 **LLM-Generated Plans (TOML, not YAML)**: Missions are defined in clean, human-readable TOML files called "Quests." A Sentinel can draft its own Quest using a LLM, which you can then approve and run.
*   🔌 **Lightweight, Composable Arcs**: Every tool is a simple, standalone MCP microservice. Forget monolithic agents; here, you build Sentinels by composing lightweight, single-purpose Arcs.

---

## 🚀 Core Architecture

*   **Docker-First Infrastructure**: Core services (`redis`, `ollama`, etc.) are managed via `docker-compose`.
*   **Supervisord-Managed Arcs**: All Arcs (MCP servers) run as independent processes inside a single container, managed by `supervisord` for resilience.
*   **Gateway**: A central FastAPI server that routes all external (WebSocket) and internal (UNIX Socket) MCP traffic.
*   **LLM Router**: The intelligent switchboard that selects the right LLM for the job based on cost, privacy, and purpose (`rank`, `copy`, `chat`).
*   **`luca-run` & `luca-shell`**: Your command center. A REPL and a non-interactive client for inspecting, debugging, and commanding your Sentinels.

 <!-- Suggestion: Create a simple Mermaid diagram and upload it -->

---

## 🤖 The Magic: An Autonomous Sentinel in Action

Instead of writing complex Python scripts, you witness the system work. Here’s the "Quick Money" Sentinel's autonomous loop:

1.  **Awakening**: The `awake` Arc "thinks" and decides it's time to act. It calls the `llm_router`.
2.  **Ideation**: It asks Gemini/Llama3 for a single, trending SEO topic.
    > *"Give me one specific, trending topic for a crypto beginner's blog."*
3.  **Action**: It calls the `seo_affiliate` Arc with the new topic.
4.  **Creation**: The `seo_affiliate` Arc uses the LLM to write a complete, SEO-optimized article (title, meta, FAQ, schema.org) and generates the static HTML files.
5.  **Result**: A new, ready-to-publish mini-site is created in the `/data/sites` directory.

All of this happens with **zero human intervention**, triggered by a single `tick`.

---

## 🔧 Quick Start Guide (Dev Environment)

1.  **Prerequisites**: Docker & Docker Compose.
2.  **Clone the Repo**:
    ```bash
    git clone https://github.com/your-username/llmbasedos.git
    cd llmbasedos
    ```
3.  **Configuration**:
    *   Create a `.env` file from the example.
    *   Add your `GEMINI_API_KEY` if you want to use Gemini.
4.  **Launch Infrastructure & Arcs**:
    ```bash
    # This command handles Docker permissions for you
    DOCKER_GID=$(getent group docker | cut -d: -f3)
    docker compose -f docker-compose.dev.yml build --build-arg DOCKER_GID=$DOCKER_GID
    docker compose -f docker-compose.dev.yml up -d
    ```
5.  **Test the KV Arc**:
    ```bash
    # Connect to the shell as the correct user
    docker exec -it --user llmuser llmbasedos_dev python -m llmbasedos_src.shell.luca

    # Inside luca-shell, test the KV store
    mcp.kv.set '["hello", "world"]'
    mcp.kv.get '["hello"]'
    ```
6.  **Trigger the Autonomous Sentinel**:
    *   From your host machine (not in the shell), run the ticker:
        ```bash
        ./ticker.sh
        ```
    *   Watch the logs and see a new site appear in the `./data/sites` directory!

---

## 🧬 Roadmap: The Adoptan.ai Marketplace

`llmbasedos` is the engine. **Adoptan.ai** is the destination.

The next milestone is to build a marketplace where developers can submit, certify, and sell/lease their Arcs and Sentinels.
*   **Sentinel Gauntlet**: An automated certification process to ensure Arcs are secure, efficient, and reliable.
*   **Composable Economy**: Users can acquire new Arcs to upgrade their Sentinels, or lease fully-trained, specialized Sentinels for specific missions (trading, lead-gen, social media).

---

## 🧠 Who is llmbasedos For?

*   **Indie Hackers** who want to build AI-powered businesses, not just chatbot features.
*   **Developers** tired of wrestling with complex agent frameworks and YAML.
*   **Researchers** who need a stable, observable runtime to experiment with agent autonomy.

---

## 🌐 Technologies Used

*   Python 3.10+, FastAPI, Supervisord
*   Docker, Docker Compose
*   **MCP**: JSON-RPC over UNIX Sockets & WebSockets
*   **LLMs**: Ollama (Llama3, Gemma), Gemini
*   **Data**: Redis (KV Store), Memobase (Contextual Memory)
*   **Plans**: TOML