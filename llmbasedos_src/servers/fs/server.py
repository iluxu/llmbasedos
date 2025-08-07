# llmbasedos_src/servers/fs/server.py
import asyncio
import os
import shutil
import magic
import base64
import re
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

from llmbasedos_src.mcp_server_framework import MCPServer
from llmbasedos_src.common_utils import validate_mcp_path_param

SERVER_NAME = "fs"
CAPS_FILE_PATH_STR = str(Path(__file__).parent / "caps.json")
# La racine où sont stockées les données de tous les tenants, injectée par docker-compose.yml
TENANT_DATA_ROOT = Path(os.getenv("LLMBDO_TENANT_DATA_ROOT", "/data"))

fs_server = MCPServer(SERVER_NAME, CAPS_FILE_PATH_STR)

def _get_tenant_root(tenant_id: str) -> Path:
    """Valide et retourne le chemin racine pour un tenant donné."""
    if not tenant_id or not re.match(r"^[a-zA-Z0-9_-]+$", tenant_id):
        raise ValueError("Invalid or missing tenant_id format.")
    
    tenant_root = TENANT_DATA_ROOT / tenant_id
    # Crée le dossier du tenant à la volée.
    tenant_root.mkdir(parents=True, exist_ok=True)
    return tenant_root

def _get_validated_disk_path(tenant_id: str, client_path: str, check_exists: bool = False, **kwargs) -> Path:
    """Valide un chemin client dans le contexte d'un tenant spécifique."""
    tenant_root = _get_tenant_root(tenant_id)
    resolved_path, error_msg = validate_mcp_path_param(
        path_param_relative_to_root=client_path.lstrip('/'),
        virtual_root_str=str(tenant_root),
        check_exists=check_exists,
        **kwargs
    )
    if error_msg:
        raise ValueError(f"Path error for tenant '{tenant_id}': {error_msg}")
    return resolved_path

def _get_client_facing_path(tenant_id: str, disk_path: Path) -> str:
    """Convertit un chemin disque absolu en chemin virtuel relatif au tenant."""
    tenant_root = _get_tenant_root(tenant_id)
    try:
        relative_path = disk_path.relative_to(tenant_root)
        return "/" + str(relative_path)
    except ValueError:
        fs_server.logger.error(f"Cannot make client-facing path for {disk_path}, not under {tenant_root}")
        return str(disk_path)

@fs_server.register_method("mcp.fs.list")
async def handle_fs_list(server: MCPServer, request_id, params: List[Any]):
    # On utilise un tenant_id par défaut, comme pour arc_manager.
    # L'authentification multi-tenant sera une évolution future.
    tenant_id = "default_user" 
    client_path = params[0] # On ne prend que le premier paramètre : le chemin

    target_path = _get_validated_disk_path(tenant_id, client_path, check_exists=True, must_be_dir=True)

    def list_dir_sync():
        items = []
        for item in target_path.iterdir():
            try:
                stat = item.stat()
                item_type = "directory" if item.is_dir() else "file" if item.is_file() else "link"
                items.append({
                    "name": item.name,
                    "path": _get_client_facing_path(tenant_id, item),
                    "type": item_type,
                    "size": stat.st_size,
                    "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
                })
            except Exception as e:
                server.logger.warning(f"Could not stat {item}: {e}")
        return items
    
    return await server.run_in_executor(list_dir_sync)

@fs_server.register_method("mcp.fs.read")
async def handle_fs_read(server: MCPServer, request_id, params: List[Any]):
    tenant_id, client_path, encoding = params
    target_file = _get_validated_disk_path(tenant_id, client_path, check_exists=True, must_be_file=True)

    def read_sync():
        mime_type = magic.from_file(str(target_file), mime=True)
        if encoding == "text":
            content = target_file.read_text('utf-8')
        elif encoding == "base64":
            content = base64.b64encode(target_file.read_bytes()).decode('ascii')
        else:
            raise ValueError(f"Unsupported encoding: {encoding}")
        return {"content": content, "mime_type": mime_type}
    
    return await server.run_in_executor(read_sync)

@fs_server.register_method("mcp.fs.write")
async def handle_fs_write(server: MCPServer, request_id, params: List[Any]):
    tenant_id, client_path, content, encoding = params
    target_file = _get_validated_disk_path(tenant_id, client_path)

    def write_sync():
        target_file.parent.mkdir(parents=True, exist_ok=True)
        if encoding == "text":
            target_file.write_text(content, 'utf-8')
        elif encoding == "base64":
            target_file.write_bytes(base64.b64decode(content))
        return {"status": "success", "path": client_path}

    return await server.run_in_executor(write_sync)

if __name__ == "__main__":
    asyncio.run(fs_server.start())