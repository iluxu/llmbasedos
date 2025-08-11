import os
import asyncio
import json
import socket
import time
import uuid
from pathlib import Path
from memobase import AsyncMemoBaseClient, ChatBlob # On utilise le client ASYNCHRONE
from memobase.utils import string_to_uuid # L'utilitaire qu'ils fournissent

from llmbasedos_src.mcp_server_framework import MCPServer

CHAT = MCPServer("contextual_chat", str(Path(__file__).parent / "caps.json"))

@CHAT.set_startup_hook
async def on_startup(server: MCPServer):
    server.logger.info("Initializing AsyncMemoBaseClient for contextual_chat...")
    # On utilise le client ASYNCHRONE, c'est plus propre
    server.mb_client = AsyncMemoBaseClient(
        project_url=os.getenv("MEMOBASE_URL"),
        api_key=os.getenv("MEMOBASE_PROJECT_TOKEN"),
    )
    
    if not await server.mb_client.ping():
        raise ConnectionError("Could not connect to Memobase backend (ping failed).")
    server.logger.info("Contextual_chat connected to Memobase backend successfully.")

async def _mcp_call_llm(params: list) -> dict:
    # ... (cette fonction est correcte et reste inchangée)
    loop = asyncio.get_running_loop()
    def blocking_socket_call():
        socket_path = "/run/mcp/gateway.sock"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(300.0); sock.connect(socket_path)
            payload = {"jsonrpc": "2.0", "method": "mcp.llm.chat", "params": params, "id": f"chat-llm-call-{time.time()}"}
            sock.sendall(json.dumps(payload).encode('utf-8') + b'\0')
            buffer = bytearray()
            while b'\0' not in buffer:
                chunk = sock.recv(16384)
                if not chunk: raise ConnectionError("Gateway connection lost.")
                buffer.extend(chunk)
            response_bytes, _ = buffer.split(b'\0', 1)
            response_data = json.loads(response_bytes.decode('utf-8'))
            if "error" in response_data and response_data["error"]:
                raise RuntimeError(f"LLM call failed: {response_data['error'].get('message')}")
            return response_data.get("result", {})
    return await loop.run_in_executor(None, blocking_socket_call)

@CHAT.register_method("mcp.contextual_chat.ask")
async def handle_ask(server: MCPServer, request_id, params: list):
    user_id_str, question_text = params[0], params[1]
    
    # --- LA CORRECTION FINALE EST ICI ---
    # On utilise la méthode officielle et l'utilitaire de conversion
    user_uuid = string_to_uuid(user_id_str)
    user = await server.mb_client.get_or_create_user(user_uuid)
    
    # --- Le reste du code est maintenant beaucoup plus simple ---
    context = await user.context()
    
    system_prompt = f"Based on the user's memory, answer the current question.\n\nMEMORY:\n{context}"
    llm_params = [{"messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": question_text}]}]
    llm_result = await _mcp_call_llm(llm_params)
    answer = llm_result.get("choices", [{}])[0].get("message", {}).get("content", "I could not generate a response.")
    
    blob = ChatBlob(messages=[{"role": "user", "content": question_text}, {"role": "assistant", "content": answer}])
    await user.insert(blob)
    await user.flush() # On attend que ce soit traité pour la démo
    
    return {"answer": answer, "context_used_length": len(context)}

if __name__ == "__main__":
    asyncio.run(CHAT.start())