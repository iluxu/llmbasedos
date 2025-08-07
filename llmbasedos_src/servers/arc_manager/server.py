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
