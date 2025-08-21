# LLMbasedOS

**A local-first runtime for tool-using AI agents**

LLMbasedOS lets you run AI agents on your own machine with explicit, narrow permissions. Everything runs locally by default. No background monitoring. No hidden capabilities. You decide which tools exist, what they can see, and when they run.

## Why this exists

Most people want help from AI without giving up privacy or control. LLMbasedOS provides a small, observable runtime that executes on your computer, inside containers, with clear boundaries. If a tool is not enabled, it does not exist. If a folder is not mounted, it is invisible.

## Trust and safety

- **Local-first**: Services run in Docker on Windows, macOS, and Linux
- **Narrow scope**: Agents only see the folders you mount. The demo mounts the bundled data folder
- **No ambient tracking**: Nothing watches your activity. Agents act only when you ask
- **Explicit tools**: Capabilities are small MCP tools you enable by name. No tool, no power
- **Local model by default**: Works with Ollama on your machine. Cloud models are opt-in
- **Full stop anytime**: Stop the stack in Docker Desktop. Your system is unchanged outside the mounted folder

## MCP in plain words

Think of MCP as a toolbox with labeled drawers. Each drawer is a tool with a clear contract: a name, inputs, and outputs. An agent cannot invent new drawers. When you type a command, the agent asks the gateway to open a specific drawer. The gateway logs the request, forwards it to the tool process, and returns only the defined result. Since the container mounts only the paths you choose, a file tool can read or write only there. New powers appear only if you add and enable a new tool and mount extra paths on purpose.

## What ships by default

- **Gateway**: routes requests to tools and logs calls
- **LLM router**: sends prompts to your chosen backend, local by default
- **Contextual chat**: short-lived context for conversations
- **Key-value store**: simple memory for demos
- **File tool**: read and write inside the mounted data folder

**Not included by default**: email sender, host shell executor, screen or keyboard access, system installers, network scanners.

## Quick start

### Requirements

- Windows 11 with Docker Desktop and WSL 2 enabled, or
- macOS with Docker Desktop, or
- Linux with Docker Engine and Docker Compose

### Steps

1. Download the release .zip and unzip it

2. Open a terminal in the unzipped folder

3. Start the stack:
   ```bash
   docker compose -f docker-compose.pc.yml up -d --build
   ```

4. Pull a local model (first time only):
   ```bash
   docker exec -it llmbasedos_ollama ollama pull gemma:2b
   ```

5. Open the interactive console:
   ```bash
   docker exec -it llmbasedos_pc python -m llmbasedos_src.shell.luca
   ```

   You should see a prompt like: `/ [default_session] luca>`

6. Exit with `exit`

7. Stop the stack when you are done:
   ```bash
   docker compose -f docker-compose.pc.yml down
   ```

## Safety-first settings

You control the scope at the container boundary. These settings are simple and effective.

### Read-only data mount
In `docker-compose.pc.yml`, change `./data:/data:rw` to `./data:/data:ro`.

### Local-only model
In `.env`, set `LLM_PROVIDER_BACKEND=ollama` and do not add cloud API keys.

### No network for the app (optional)
Set `network_mode: "none"` on the paas service if you want a fully offline run after the model is pulled.

## Visibility and control

- Tools are listed in `supervisord.conf`. If a tool is not listed, it does not run
- Calls are logged by the gateway and by each tool process
- The app can read or write only inside the mounted paths you control
- Start, stop, and remove containers from Docker Desktop at any time

## FAQ

### Can it see my other files?
No. It only sees what you mount. The demo mounts the bundled data folder.

### Does anything run by itself?
No. There is no background monitoring. Agents act when you ask them to. Scheduled jobs are off by default and must be created explicitly.

### Can it send my documents to the internet?
Not unless you opt-in to a cloud model or enable a network tool. The default setup uses a local model with no external API calls.

### Can it execute system commands on my host?
No. There is no host shell tool in the default setup. Adding such a tool would require an explicit change and is not recommended for sensitive machines.

### What happens if I remove it?
Use Docker Desktop or `docker compose down`. The stack stops and your computer is unchanged outside the mounted folder.

## Terms

- **Agent**: the decision loop that asks for tools by name
- **Tool (Arc)**: a small MCP server exposing one capability with a strict contract
- **Gateway**: the mediator that routes and logs tool calls
- **Plan (Quest)**: a simple, human-readable task file that chains tool calls
- **Router**: picks the configured LLM backend, local by default

## Who this is for

- Privacy-sensitive users who want AI help without giving up control
- Teams that need offline or on-prem workflows
- Developers and researchers who want observable, reproducible agent runs

## Roadmap

- More first-party tools with clear permissions and tests
- Signed tool bundles and manifest-based installs
- Policy layer for tool whitelists and read-only mounts by default

## Contact

Questions or concerns: open an issue on GitHub or email [luca@llmbasedos.com](mailto:luca@llmbasedos.com).
