[![MseeP.ai Security Assessment Badge](https://mseep.net/pr/iluxu-llmbasedos-badge.png)](https://mseep.ai/app/iluxu-llmbasedos)

# llmbasedos

`llmbasedos` is not just a framework or set of plugins. It is a **cognitive operating system** designed to transform your computer from a passive executor into an **autonomous partner** — capable of perceiving, reasoning, and acting across both local and cloud contexts.

It does this by exposing all system capabilities (files, mail, APIs, agents) to any intelligent model — LLM or not — via the **Model Context Protocol (MCP)**: a simple, powerful JSON-RPC layer running over UNIX sockets and WebSockets.

The vision is to make **personal agentivity** real — by empowering AI agents to perform meaningful tasks on your behalf with minimal plumbing, friction, or boilerplate.

---

## ✨ What Makes llmbasedos Different?

* 🔌 **Unified Abstraction Layer**: All capabilities (LLM calls, file ops, mail, browser, rclone, etc.) are exposed as MCP methods, fully discoverable and callable.
* 🧠 **LLM-Agnostic**: Use OpenAI, Gemini, LLaMA.cpp, or local models interchangeably. The system routes `mcp.llm.chat` requests via your preferred backend.
* 🧰 **Script-first, not YAML**: Agent workflows are Python scripts, not rigid YAML trees. That means full logic, full debugging, and full flexibility.
* 🔒 **Local-first, Secure-by-default**: Data stays local unless explicitly bridged. The OS abstracts I/O without exposing sensitive paths or tokens.

---

## 🧠 Philosophy & Paradigm Shift

> "The true power of AI is not in the model, but in its ability to act contextually."

Where most projects focus on “the agent,” llmbasedos focuses on the **substrate**: a runtime and interface that lets agents — whether LLM-driven or human-written — perform intelligent tasks, access context, and automate real workflows.

Just like Unix abstracted away hardware with file descriptors, **llmbasedos abstracts cognitive capabilities** with the MCP.

---

## 🚀 Core Architecture

* **Docker-first** deployment with `supervisord` managing microservices.
* **Gateway**: routes MCP traffic, exposes LLM abstraction, enforces license tiers.
* **MCP Servers**: plug-and-play Python services exposing files, email, web, and more.
* **Shell**: `luca-shell`, a REPL for exploring and scripting against your MCP system.

---

## 🔁 From YAML to Scripts: A Strategic Pivot

Old approach: YAML workflows (rigid, hard to debug, logic hell).

New approach: Python scripts using `mcp_call()` for everything.

Example:

```python
history = json.loads(mcp_call("mcp.fs.read", ["/outreach/contact_history.json"]).get("result", {}).get("content", "[]"))

prompt = f"Find 5 new agencies not in: {json.dumps(history)}"
llm_response = mcp_call("mcp.llm.chat", [[{"role": "user", "content": prompt}], {"model": "gemini-1.5-pro"}])

new_prospects = json.loads(llm_response.get("result", {}).get("choices", [{}])[0].get("message", {}).get("content", "[]"))

if new_prospects:
    updated = history + new_prospects
    mcp_call("mcp.fs.write", ["/outreach/contact_history.json", json.dumps(updated, indent=2), "text"])
```

That’s it. You just built an LLM-powered outreach agent with **3 calls and zero boilerplate**.

---

## 🧱 Gateway + Servers Overview

* `gateway/` (FastAPI):

  * WebSocket + TCP endpoints.
  * Auth & license tiers (`lic.key`, `licence_tiers.yaml`).
  * LLM multiplexer (OpenAI, Gemini, local models).
* `servers/fs/`: virtualized file system + FAISS semantic search.
* `servers/mail/`: IMAP email parsing + draft handling.
* `servers/sync/`: rclone for file sync ops.
* `servers/agent/`: (legacy) YAML workflow engine (to be deprecated).

---

## 🔧 Deployment Guide (Docker)

1. Install Docker + Docker Compose.
2. Clone the repo, and organize `llmbasedos_src/`.
3. Add your `.env`, `lic.key`, `mail_accounts.yaml`, and user files.
4. Build:

```bash
docker compose build
```

5. Run:

```bash
docker compose up
```

6. Connect via `luca-shell` and start issuing `mcp.*` calls.

---

## 🧬 Roadmap: From Execution to Intention

Next milestone: `orchestrator_server`

It listens to natural language intentions ("Find 5 leads & draft intro emails"), auto-generates Python scripts to execute the plan, then optionally runs them.

→ the OS becomes **a compiler for intention**.

---

## 🔐 Security Highlights

* Virtual path jail (e.g., `/mnt/user_data`)
* Licence-based tier enforcement
* No keys baked in: `.env`-only secrets
* Containers use readonly volumes for config

---

## 🧠 Who is llmbasedos For?

* Builders tired of gluing APIs together manually
* Agents researchers needing a clean substrate
* Indie hackers who want GPT to *actually* do things

---

## 🌐 Technologies Used

* Python 3.10+ / FastAPI / WebSockets / Supervisord
* Docker / Compose / Volume Mounting
* JSON-RPC 2.0 (MCP)
* FAISS + SentenceTransformers
* OpenAI / Gemini / LLaMA.cpp

---

## 🧭 Stay Updated

Stars, forks, PRs and radical experiments welcome.

> llmbasedos is what happens when you stop asking "how can I call GPT" and start asking "what if GPT could *call everything else*?"
