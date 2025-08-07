import asyncio
import json
import logging
from pathlib import Path
import httpx
from llmbasedos_src.mcp_server_framework import MCPServer

SERVER_NAME = "browser"
CAPS_FILE_PATH_STR = f"/run/mcp/{SERVER_NAME}.cap.json"
# L'URL du service Playwright natif, qui tournera dans un autre conteneur
NATIVE_PLAYWRIGHT_MCP_URL = "http://localhost:8811"

browser_server = MCPServer(
    server_name=SERVER_NAME,
    caps_file_path_str=CAPS_FILE_PATH_STR,
    load_caps_on_init=False
)

@browser_server.register_method("mcp.browser.call")
async def handle_browser_call(server: MCPServer, request_id: str, params: list):
    if len(params) != 2 or not isinstance(params[0], str) or not isinstance(params[1], dict):
        raise ValueError("mcp.browser.call expects params: [\"tool_name_string\", {arguments_object}]")
    
    tool_name, arguments = params
    tool_name_for_playwright = f"browser_{tool_name}"
    
    server.logger.info(f"Proxying call for tool '{tool_name_for_playwright}' to native Playwright server.")
    
    payload = {
        "jsonrpc": "2.0",
        "method": tool_name_for_playwright,
        "id": request_id,
        "params": arguments
    }
    
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(NATIVE_PLAYWRIGHT_MCP_URL, json=payload)
            response.raise_for_status()
            response_data = response.json()
    except httpx.RequestError as e:
        server.logger.error(f"Could not connect to native Playwright server at {NATIVE_PLAYWRIGHT_MCP_URL}: {e}")
        raise ConnectionError(f"Unable to reach the Playwright service. Is it running?")

    if "error" in response_data and response_data["error"]:
        error_details = response_data['error']
        raise RuntimeError(f"Playwright Error: {error_details.get('message', 'Unknown error')}")
        
    return response_data.get("result")

async def on_startup(server: MCPServer):
    # On crée un caps.json statique car on expose une seule méthode générique
    caps_content = {
        "service_name": "browser",
        "description": "Proxy to the native Playwright MCP Server.",
        "version": "1.0.0",
        "capabilities": [
            {
                "method": "mcp.browser.call",
                "description": "Calls any Playwright tool. Params: [\"tool_name\", {arguments_object}]",
                "params_schema": { "type": "array" }
            }
        ]
    }
    with open(server.caps_file_path, "w") as f:
        json.dump(caps_content, f, indent=4)
    server._publish_capability_descriptor()
    server.logger.info("Browser proxy server started. Ready to forward requests.")

browser_server.set_startup_hook(on_startup)

if __name__ == "__main__":
    logging.basicConfig(level="INFO")
    asyncio.run(browser_server.start())