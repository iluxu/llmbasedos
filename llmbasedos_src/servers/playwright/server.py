# llmbasedos_src/servers/playwright/server.py
import asyncio
import json
import logging
import os
from pathlib import Path
import uuid
from typing import Any, Dict

from llmbasedos.mcp_server_framework import MCPServer

SERVER_NAME = "playwright"
CAPS_FILE_PATH_STR = f"/run/mcp/{SERVER_NAME}.cap.json"

playwright_server = MCPServer(
    server_name=SERVER_NAME,
    caps_file_path_str=CAPS_FILE_PATH_STR,
    load_caps_on_init=False
)

# --- État global pour le conteneur Playwright ---
playwright_server.container_proc = None
playwright_server.container_reader = None
playwright_server.container_writer = None
playwright_server.pending_requests = {}
playwright_server.reader_task = None

async def read_container_output():
    while True:
        try:
            if not playwright_server.container_reader or playwright_server.container_reader.at_eof():
                playwright_server.logger.warning("Playwright container stdout is at EOF.")
                break
            
            line_bytes = await playwright_server.container_reader.readline()
            if not line_bytes: continue

            line_str = line_bytes.decode().strip()
            if not line_str: continue

            playwright_server.logger.info(f"PLAYWRIGHT_MCP_STDOUT: {line_str}")
            response = json.loads(line_str)
            req_id = response.get("id")
            
            if req_id in playwright_server.pending_requests:
                future = playwright_server.pending_requests.pop(req_id)
                if not future.done():
                    future.set_result(response)
        except asyncio.CancelledError:
            break
        except Exception as e:
            playwright_server.logger.error(f"Error in Playwright container reader task: {e}", exc_info=True)
            break
    playwright_server.logger.info("Playwright container reader task finished.")

async def send_to_container_and_get_response(payload: Dict[str, Any], timeout: float = 120.0) -> Dict[str, Any]:
    future = asyncio.get_running_loop().create_future()
    playwright_server.pending_requests[payload["id"]] = future
    playwright_server.container_writer.write(json.dumps(payload).encode() + b'\n')
    await playwright_server.container_writer.drain()
    return await asyncio.wait_for(future, timeout=timeout)

async def on_playwright_startup(server: MCPServer):
    server.logger.info("Starting Playwright MCP Docker container...")
    cmd = [
        "docker", "run", "-i", "--rm", "--init",
        "mcr.microsoft.com/playwright/mcp",
        "--headless" # IMPORTANT: Toujours lancer en headless dans un conteneur
    ]

    try:
        server.container_proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        server.container_reader = server.container_proc.stdout
        server.container_writer = server.container_proc.stdin
        
        async def log_stderr():
            while server.container_proc and not server.container_proc.stderr.at_eof():
                line = await server.container_proc.stderr.readline()
                if not line: break
                server.logger.info(f"PLAYWRIGHT_MCP_STDERR: {line.decode().strip()}")
        
        server.reader_task = asyncio.create_task(read_container_output())
        asyncio.create_task(log_stderr())

        server.logger.info(f"Playwright MCP container started with PID: {server.container_proc.pid}")

        init_payload = {"jsonrpc": "2.0", "method": "initialize", "id": f"playwright_init_{uuid.uuid4().hex}", "params": {"client_name": "llmbasedos"}}
        init_response = await send_to_container_and_get_response(init_payload)
        if "error" in init_response: raise RuntimeError(f"Initialization failed: {init_response['error']}")
        
        tools_list_payload = {"jsonrpc": "2.0", "method": "tools/list", "id": f"playwright_tools_list_{uuid.uuid4().hex}"}
        tools_list_response = await send_to_container_and_get_response(tools_list_payload)
        if "error" in tools_list_response: raise RuntimeError(f"Failed to get tools list: {tools_list_response['error']}")
        
        tools = tools_list_response.get("result", {}).get("tools", [])
        server.logger.info(f"Discovered {len(tools)} tools from Playwright MCP.")
        
        capabilities = []
        for tool in tools:
            method_name = f"mcp.browser.{tool.get('name')}" # On préfixe pour éviter les conflits (ex: 'search')
            server._method_handlers[method_name] = forward_call_to_container
            capabilities.append({"method": method_name, "description": tool.get("description", ""), "params_schema": tool.get("input_schema", {})})
        
        caps_content = {"service_name": SERVER_NAME, "description": "Native Playwright MCP Integration", "version": "1.0.0", "capabilities": capabilities}
        with open(server.caps_file_path, "w") as f:
            json.dump(caps_content, f, indent=4)
        server._publish_capability_descriptor()
        server.logger.info("Playwright server configured and capabilities published.")

    except Exception as e:
        server.logger.error(f"Critical failure during Playwright server startup: {e}", exc_info=True)
        if hasattr(server, 'container_proc') and server.container_proc and server.container_proc.returncode is None:
            server.container_proc.kill()

async def on_playwright_shutdown(server: MCPServer):
    if hasattr(server, 'reader_task') and server.reader_task: server.reader_task.cancel()
    if server.container_proc and server.container_proc.returncode is None:
        server.logger.info("Terminating Playwright MCP container...")
        server.container_proc.terminate()
        try: await asyncio.wait_for(server.container_proc.wait(), timeout=5.0)
        except asyncio.TimeoutError: server.container_proc.kill()
    server.logger.info("Playwright server shutdown complete.")

async def forward_call_to_container(server: MCPServer, request_id: str, params: Any): pass

original_handle_request = playwright_server._handle_single_request
async def custom_handle_request(self, request_data: Dict[str, Any]) -> Dict[str, Any]:
    method_name = request_data.get("method") # ex: "mcp.browser.navigate"
    tool_name = method_name.split('.')[-1] # ex: "navigate"

    container_req_payload = {
        "jsonrpc": "2.0", "method": "call-tool", "id": request_data.get("id"),
        "params": {"name": tool_name, "arguments": request_data.get("params")}
    }
    response = await send_to_container_and_get_response(container_req_payload)
    return response

playwright_server._handle_single_request = custom_handle_request.__get__(playwright_server, MCPServer)
playwright_server.set_startup_hook(on_playwright_startup)
playwright_server.set_shutdown_hook(on_playwright_shutdown)

if __name__ == "__main__":
    logging.basicConfig(level="INFO")
    asyncio.run(playwright_server.start())
