import asyncio, os, base64
from pathlib import Path
from datetime import datetime, timezone
from llmbasedos_src.mcp_server_framework import MCPServer
from llmbasedos_src.common_utils import validate_mcp_path_param

FILE = MCPServer("file", __file__.replace("server.py","caps.json"))
VIRTUAL_ROOT = Path(os.getenv("LLMBDO_TENANT_DATA_ROOT", "/data/default_user"))

@FILE.register_method("mcp.file.list")
async def list_files(server: MCPServer, _id, params: list):
    path_str = params[0]
    disk_path, err = validate_mcp_path_param(path_str.lstrip('/'), str(VIRTUAL_ROOT), check_exists=True, must_be_dir=True)
    if err: raise ValueError(err)
    
    items = []
    for item in disk_path.iterdir():
        stat = item.stat()
        items.append({
            "name": item.name,
            "type": "directory" if item.is_dir() else "file",
            "size": stat.st_size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
        })
    return items

@FILE.register_method("mcp.file.read")
async def read_file(server: MCPServer, _id, params: list):
    path_str, encoding = params[0], params[1] if len(params) > 1 else "text"
    disk_path, err = validate_mcp_path_param(path_str.lstrip('/'), str(VIRTUAL_ROOT), check_exists=True, must_be_file=True)
    if err: raise ValueError(err)

    if encoding == "base64":
        content = base64.b64encode(disk_path.read_bytes()).decode('ascii')
    else:
        content = disk_path.read_text('utf-8')
    return {"path": path_str, "content": content, "encoding": encoding}

@FILE.register_method("mcp.file.write")
async def write_file(server: MCPServer, _id, params: list):
    path_str, content, encoding = params[0], params[1], params[2] if len(params) > 2 else "text"
    disk_path, err = validate_mcp_path_param(path_str.lstrip('/'), str(VIRTUAL_ROOT))
    if err: raise ValueError(err)
    
    disk_path.parent.mkdir(parents=True, exist_ok=True)
    if encoding == "base64":
        bytes_written = disk_path.write_bytes(base64.b64decode(content))
    else:
        bytes_written = disk_path.write_text(content, 'utf-8')
    return {"path": path_str, "bytes_written": bytes_written, "status": "success"}

if __name__ == "__main__":
    asyncio.run(FILE.start())