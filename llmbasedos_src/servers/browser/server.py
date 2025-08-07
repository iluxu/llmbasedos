import asyncio
import httpx
import json
from llmbasedos_src.mcp_server_framework import MCPServer
from pathlib import Path
import os

SERVER_NAME = "browser"
CAPS_FILE_PATH = str(Path(__file__).parent / "caps.json")
browser_server = MCPServer(SERVER_NAME, CAPS_FILE_PATH)

PLAYWRIGHT_URL = os.getenv("PLAYWRIGHT_URL", "http://playwright-mcp:5678")

@browser_server.register_method("mcp.browser.scrape")
async def handle_scrape(server: MCPServer, request_id, params: list):
    url = params[0]
    server.logger.info(f"Using permanent Playwright service to scrape: {url}")

    async with httpx.AsyncClient(timeout=60.0) as client:
        # 1. Obtenir la session
        sse_res = await client.get(f"{PLAYWRIGHT_URL}/sse")
        sse_res.raise_for_status()
        session_path = sse_res.text.split("data: ")[1].strip()
        session_url = f"{PLAYWRIGHT_URL}{session_path}"
        
        # 2. Initialiser
        init_payload = {"method": "initialize", "params": {}, "id": f"{request_id}-init"}
        await client.post(session_url, json=init_payload)
        
        # 3. Naviguer
        nav_payload = {"method": "browser_navigate", "params": {"url": url}, "id": f"{request_id}-nav"}
        await client.post(session_url, json=nav_payload)
        
        await asyncio.sleep(3) # Laisse le temps à la page de se charger
        
        # 4. Snapshot
        snap_payload = {"method": "browser_snapshot", "id": f"{request_id}-snap"}
        resp = await client.post(session_url, json=snap_payload)
        
        return resp.json().get("result", {})

if __name__ == "__main__":
    asyncio.run(browser_server.start())