# llmbasedos_src/servers/memobase/server.py
import asyncio
import os
from pathlib import Path
from memobase import MemoBaseClient, ChatBlob

from llmbasedos_src.mcp_server_framework import MCPServer

SERVER_NAME = "memobase"
CAPS_FILE_PATH = str(Path(__file__).parent / "caps.json")
memobase_server = MCPServer(SERVER_NAME, CAPS_FILE_PATH)

# Configuration depuis les variables d'environnement du conteneur
MEMOBASE_URL = os.getenv("MEMOBASE_URL", "http://memobase:8019")
MEMOBASE_TOKEN = os.getenv("MEMOBASE_PROJECT_TOKEN", "un-token-secret-pour-memobase")

try:
    client = MemoBaseClient(project_url=MEMOBASE_URL, api_key=MEMOBASE_TOKEN)
except Exception as e:
    memobase_server.logger.critical(f"Failed to connect to Memobase server at {MEMOBASE_URL}: {e}")
    client = None

@memobase_server.register_method("mcp.memobase.get_context")
async def get_user_context(server: MCPServer, request_id, params: list):
    if not client: raise RuntimeError("Memobase client not initialized.")
    user_id = params[0]
    
    # S'assurer que l'utilisateur existe, sinon le créer
    try:
        user = client.get_user(user_id)
    except:
        user = client.add_user(user_id)
    
    context = await asyncio.to_thread(user.context, max_token_size=2000)
    return {"context": context}

@memobase_server.register_method("mcp.memobase.insert")
async def insert_user_chat(server: MCPServer, request_id, params: list):
    if not client: raise RuntimeError("Memobase client not initialized.")
    user_id, messages = params
    
    try:
        user = client.get_user(user_id)
    except:
        user = client.add_user(user_id)
        
    blob = ChatBlob(messages=messages)
    await asyncio.to_thread(user.insert, blob)
    await asyncio.to_thread(user.flush, sync=False) # Flush en arrière-plan
    return {"status": "success"}

if __name__ == "__main__":
    asyncio.run(memobase_server.start())