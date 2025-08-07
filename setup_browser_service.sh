#!/bin/bash

# ==============================================================================
# SCRIPT DE CRÉATION DU SERVICE MCP-BROWSER (MANAGER)
# ------------------------------------------------------------------------------
# Ce script crée le service qui pilotera les conteneurs Playwright à la demande.
# Il est conçu pour être exécuté une seule fois.
# ==============================================================================

# Stoppe le script immédiatement si une commande échoue
set -e

SERVICE_NAME="browser"
SERVICE_DIR="llmbasedos_src/servers/${SERVICE_NAME}"

echo "### Création du service MCP : ${SERVICE_NAME} ###"
echo

# --- 1. Création de la structure ---
echo "--> Création du répertoire : ${SERVICE_DIR}"
mkdir -p "${SERVICE_DIR}"
echo "    [+] Répertoire créé."
echo

echo "--> Création des fichiers de configuration et de code..."

# Fichier __init__.py
touch "${SERVICE_DIR}/__init__.py"
echo "    [+] Créé : ${SERVICE_DIR}/__init__.py"

# Fichier requirements.txt
echo "docker" > "${SERVICE_DIR}/requirements.txt"
echo "httpx" >> "${SERVICE_DIR}/requirements.txt"
echo "" >> "${SERVICE_DIR}/requirements.txt"
echo "    [+] Créé : ${SERVICE_DIR}/requirements.txt"

# Fichier caps.json
cat > "${SERVICE_DIR}/caps.json" << 'CAPS_EOL'
{
    "service_name": "browser",
    "description": "Manages and interacts with ephemeral Playwright browser instances.",
    "version": "1.0.0",
    "capabilities": [
        {
            "method": "mcp.browser.scrape",
            "description": "Starts a browser, navigates to a URL, captures a snapshot, and shuts down.",
            "params_schema": {
                "type": "array",
                "minItems": 1,
                "maxItems": 1,
                "items": [
                    {
                        "type": "string",
                        "description": "The URL to scrape."
                    }
                ]
            }
        }
    ]
}
CAPS_EOL
echo "    [+] Créé : ${SERVICE_DIR}/caps.json"

# Fichier server.py
cat > "${SERVICE_DIR}/server.py" << 'SERVER_EOL'
import asyncio
import docker
import httpx
import json
from pathlib import Path
from llmbasedos_src.mcp_server_framework import MCPServer

SERVER_NAME = "browser"
CAPS_FILE_PATH = str(Path(__file__).parent / "caps.json")
browser_server = MCPServer(SERVER_NAME, CAPS_FILE_PATH)
docker_client = docker.from_env()

@browser_server.register_method("mcp.browser.scrape")
async def handle_scrape(server: MCPServer, request_id, params: list):
    url_to_scrape = params[0]
    server.logger.info(f"Starting ephemeral Playwright container for URL: {url_to_scrape}")
    
    container = None
    try:
        container = docker_client.containers.run(
            "mcr.microsoft.com/playwright/mcp:latest",
            detach=True,
            auto_remove=True,
            network_mode="host",
            stdin_open=True,
            tty=True
        )
        
        server.logger.info(f"Waiting 15s for Playwright container ({container.short_id}) to be ready...")
        await asyncio.sleep(15)

        async with httpx.AsyncClient(timeout=60.0) as client:
            sse_res = await client.get("http://localhost:5678/sse", headers={"Accept": "text/event-stream"})
            sse_res.raise_for_status()
            session_url_part = sse_res.text.split("data: ")[1].strip()
            session_url = f"http://localhost:5678{session_url_part}"
            server.logger.info(f"Obtained session URL: {session_url}")
            
            headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
            
            init_payload = {"jsonrpc": "2.0", "method": "initialize", "params": {"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"llmbasedos-manager","version":"1.0"}}, "id": "init-1"}
            await client.post(session_url, json=init_payload, headers=headers)
            server.logger.info("Playwright session initialized.")

            nav_payload = {"jsonrpc": "2.0", "method": "browser_navigate", "params": {"url": url_to_scrape}, "id": "nav-1"}
            await client.post(session_url, json=nav_payload, headers=headers)
            server.logger.info(f"Navigated to {url_to_scrape}.")

            snap_payload = {"jsonrpc": "2.0", "method": "browser_snapshot", "params": {}, "id": "snap-1"}
            response = await client.post(session_url, json=snap_payload, headers=headers)
            server.logger.info("Snapshot captured.")
            
            final_result_text = [line.split("data: ")[1] for line in response.text.strip().split("\n") if line.startswith("data: ")][-1]
            final_result = json.loads(final_result_text)

            return final_result.get("result", {})
    except Exception as e:
        server.logger.error(f"Error during Playwright interaction: {e}", exc_info=True)
        raise RuntimeError(f"Failed to scrape URL: {e}")
    finally:
        if container:
            server.logger.info(f"Stopping Playwright container ({container.short_id})...")
            try:
                container.stop()
            except Exception as e_stop:
                server.logger.warning(f"Could not stop container {container.short_id}: {e_stop}")

if __name__ == "__main__":
    asyncio.run(browser_server.start())
SERVER_EOL
echo "    [+] Créé : ${SERVICE_DIR}/server.py"

echo
echo "### Terminé. Structure du service '${SERVICE_NAME}' prête. ###"
echo
echo "------------------ ACTIONS MANUELLES REQUISES ------------------"
echo "1. SUPPRIME le dossier 'playwright_service' et son contenu s'il existe encore."
echo "   (commande: rm -rf playwright_service)"
echo
echo "2. VÉRIFIE que ton 'docker-compose.yml' est revenu à sa version simple (un seul service 'llmbasedos')."
echo
echo "3. AJOUTE ce bloc à ton fichier 'supervisord.conf':"
echo
echo "[program:mcp-browser]
command=/usr/local/bin/python -m llmbasedos_src.servers.browser.server
user=llmuser
autostart=true
autorestart=true
priority=200
stdout_logfile=/var/log/supervisor/browser-stdout.log
stderr_logfile=/var/log/supervisor/browser-stderr.log"
echo
echo "4. AJOUTE cette ligne à ton 'Dockerfile' (avec les autres COPY de requirements.txt):"
echo
echo "COPY llmbasedos_src/servers/browser/requirements.txt /tmp/reqs/10-browser.txt"
echo
echo "5. RECONSTRUIS et RELANCE : docker compose build && docker compose up"
echo "--------------------------------------------------------------"

