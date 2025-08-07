#!/bin/bash

# ==============================================================================
# SCRIPT DE CRÉATION DU SERVICE MCP-ARC-MANAGER
# ------------------------------------------------------------------------------
# Ce script génère toute la structure et les fichiers de base pour le nouveau
# service de gestion des Arcs et des Sentinelles.
# ==============================================================================

set -e

SERVICE_NAME="arc_manager"
SERVICE_DIR="llmbasedos_src/servers/${SERVICE_NAME}"

echo "### Création du service MCP : ${SERVICE_NAME} ###"
echo

# --- 1. Création de la structure de dossiers ---
echo "--> Création du répertoire de service : ${SERVICE_DIR}"
mkdir -p "${SERVICE_DIR}"
echo "    [+] Répertoire créé."
echo

# --- 2. Création des fichiers de base ---
echo "--> Création des fichiers de configuration et de code..."

# Fichier __init__.py
touch "${SERVICE_DIR}/__init__.py"
echo "    [+] Créé : ${SERVICE_DIR}/__init__.py"

# Fichier requirements.txt (vide pour l'instant, car il utilise les services de base)
touch "${SERVICE_DIR}/requirements.txt"
echo "    [+] Créé : ${SERVICE_DIR}/requirements.txt (vide)"

# Fichier caps.json
cat > "${SERVICE_DIR}/caps.json" << 'CAPS_EOL'
{
    "service_name": "arc_manager",
    "description": "Manages the lifecycle of Arcs and Sentinels.",
    "version": "0.1.0",
    "capabilities": [
        {
            "method": "mcp.arc.create",
            "description": "Creates the initial file structure for a new Arc.",
            "params_schema": {
                "type": "array",
                "minItems": 1,
                "maxItems": 1,
                "items": [
                    {
                        "type": "object",
                        "properties": {
                            "arc_name": { "type": "string", "description": "Unique name for the new Arc." },
                            "specialty": { "type": "string", "description": "Domain specialty (e.g., turf, crypto)." }
                        },
                        "required": ["arc_name", "specialty"]
                    }
                ]
            }
        },
        {
            "method": "mcp.arc.list",
            "description": "Lists all Arcs owned by the user (tenant).",
            "params_schema": { "type": "array", "maxItems": 0 }
        },
        {
            "method": "mcp.sentinel.list_public",
            "description": "Lists all available Sentinels on the marketplace.",
            "params_schema": { "type": "array", "maxItems": 0 }
        },
        {
            "method": "mcp.sentinel.run",
            "description": "Executes a public Sentinel for the current user.",
            "params_schema": {
                "type": "array",
                "minItems": 1,
                "maxItems": 1,
                "items": [
                    {
                        "type": "object",
                        "properties": {
                            "sentinel_id": { "type": "string" },
                            "input_params": { "type": "object" }
                        },
                        "required": ["sentinel_id"]
                    }
                ]
            }
        }
    ]
}
CAPS_EOL
echo "    [+] Créé : ${SERVICE_DIR}/caps.json"

# Fichier server.py (avec la logique de base)
cat > "${SERVICE_DIR}/server.py" << 'SERVER_EOL'
import asyncio
import json
import os
from pathlib import Path
import re
from llmbasedos_src.mcp_server_framework import MCPServer

# --- Configuration ---
SERVER_NAME = "arc_manager"
CAPS_FILE_PATH = str(Path(__file__).parent / "caps.json")
# Le chemin racine des données de tous les tenants, monté par Docker
TENANT_DATA_ROOT = Path(os.getenv("LLMBDO_TENANT_DATA_ROOT", "/data"))

arc_manager_server = MCPServer(SERVER_NAME, CAPS_FILE_PATH)

# --- Fonctions Utilitaires Internes ---

def _get_tenant_root(tenant_id: str) -> Path:
    """Valide et retourne le chemin racine pour un tenant donné."""
    if not tenant_id or not re.match(r"^[a-zA-Z0-9_-]+$", tenant_id):
        raise ValueError("Invalid or missing tenant_id format.")
    
    tenant_root = TENANT_DATA_ROOT / tenant_id
    tenant_root.mkdir(parents=True, exist_ok=True)
    return tenant_root

# --- Méthodes MCP ---

@arc_manager_server.register_method("mcp.arc.create")
async def handle_arc_create(server: MCPServer, request_id, params: list):
    tenant_id = "default_user" 
    options = params[0]
    arc_name = options.get("arc_name")
    specialty = options.get("specialty")

    server.logger.info(f"Tenant '{tenant_id}' is creating new Arc '{arc_name}' with specialty '{specialty}'.")

    tenant_root = _get_tenant_root(tenant_id)
    arc_path = tenant_root / "arcs" / arc_name

    if arc_path.exists():
        raise ValueError(f"Arc with name '{arc_name}' already exists for this user.")

    (arc_path / "prompts").mkdir(parents=True, exist_ok=True)
    (arc_path / "data").mkdir(exist_ok=True)

    arc_metadata = {
        "arc_id": f"arc_{arc_name.lower().replace(' ', '_')}",
        "name": arc_name,
        "specialty": specialty,
        "completion_rate": 0,
        "created_at": asyncio.get_event_loop().time()
    }
    (arc_path / "arc.json").write_text(json.dumps(arc_metadata, indent=4))
    
    (arc_path / "main_agent.py").touch()
    (arc_path / "requirements.txt").touch()
    (arc_path / "prompts/system_personality.txt").touch()
    (arc_path / "prompts/reasoning_rules.txt").touch()

    server.logger.info(f"Successfully created structure for Arc '{arc_name}'.")
    return {"status": "success", "arc_id": arc_metadata["arc_id"]}

@arc_manager_server.register_method("mcp.arc.list")
async def handle_arc_list(server: MCPServer, request_id, params: list):
    tenant_id = "default_user"
    arcs_path = _get_tenant_root(tenant_id) / "arcs"
    if not arcs_path.exists(): return []
    arc_list = []
    for arc_dir in arcs_path.iterdir():
        if arc_dir.is_dir() and (meta_file := arc_dir / "arc.json").exists():
            try:
                arc_list.append(json.loads(meta_file.read_text()))
            except json.JSONDecodeError:
                server.logger.warning(f"Could not parse arc.json for {arc_dir.name}")
    return arc_list

if __name__ == "__main__":
    asyncio.run(arc_manager_server.start())
SERVER_EOL
echo "    [+] Créé : ${SERVICE_DIR}/server.py"

echo
echo "### Terminé. Structure du service '${SERVICE_NAME}' prête. ###"
echo
echo "------------------ ACTIONS MANUELLES REQUISES ------------------"
echo "1. AJOUTE ce bloc à ton fichier 'supervisord.conf':"
echo
echo "[program:mcp-arc-manager]"
echo "command=/usr/local/bin/python -m llmbasedos_src.servers.arc_manager.server"
echo "user=llmuser"
echo "autostart=true"
echo "autorestart=true"
echo "priority=200"
echo "stdout_logfile=/var/log/supervisor/arc_manager-stdout.log"
echo "stderr_logfile=/var/log/supervisor/arc_manager-stderr.log"
echo
echo "2. AJOUTE cette ligne à ton 'Dockerfile' (avec les autres COPY):"
echo
echo "COPY llmbasedos_src/servers/arc_manager/requirements.txt /tmp/reqs/09-arc-manager.txt"
echo
echo "3. RECONSTRUIS et RELANCE : docker compose build && docker compose up"
echo "--------------------------------------------------------------"