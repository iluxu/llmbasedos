# llmbasedos_src/servers/onlyvue/server.py
import asyncio
import os
from pathlib import Path
from typing import List, Dict, Any

from llmbasedos_src.mcp_server_framework import MCPServer

SERVER_NAME = "onlyvue"
CAPS_FILE_PATH = str(Path(__file__).parent / "caps.json")
onlyvue_server = MCPServer(SERVER_NAME, CAPS_FILE_PATH)

# En prod : mettre les vrais creds API
ONLYVUE_API_URL = os.getenv("ONLYVUE_API_URL", "https://api.mock-onlyvue.local")
ONLYVUE_API_KEY = os.getenv("ONLYVUE_API_KEY", "mock-key")

@onlyvue_server.register_method("mcp.onlyvue.get_chat_history")
async def handle_get_chat_history(server: MCPServer, request_id, params: list):
    """
    params[0] = user_id (str)
    Retourne : liste de messages {timestamp, from, text}
    """
    user_id = params[0]
    server.logger.info(f"[DEV] Fetching chat history for {user_id}")
    # En prod : requête API REST OnlyVue
    return [
        {"timestamp": "2025-08-08T14:00:00Z", "from": "user", "text": "Salut Divista 👋"},
        {"timestamp": "2025-08-08T14:01:00Z", "from": "divista", "text": "Coucou ❤️ comment tu vas ?"}
    ]

@onlyvue_server.register_method("mcp.onlyvue.send_message")
async def handle_send_message(server: MCPServer, request_id, params: list):
    """
    params = [user_id, message_text]
    """
    user_id, message_text = params
    server.logger.info(f"[DEV] Sending to {user_id}: {message_text}")
    # En prod : POST message via API OnlyVue
    return {"status": "success", "recipient": user_id, "message": message_text}

if __name__ == "__main__":
    asyncio.run(onlyvue_server.start())
