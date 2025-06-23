# claude-stdio-server.py
import asyncio
import json
import sys
import logging
import websockets
from typing import Dict, Any, List

# --- Configuration ---
LLMBASEDO_GATEWAY_WS_URL = "ws://localhost:8000/ws"
LOG_FILE = "/tmp/llmbasedos_claude_bridge.log" # Utilise un chemin accessible en écriture

# --- Setup Logging ---
# On logue dans un fichier pour pouvoir déboguer ce qui se passe quand Claude le lance.
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
log = logging.getLogger("claude_bridge")

# Dictionnaire global pour faire le lien entre les requêtes et les réponses
pending_gateway_requests: Dict[str, asyncio.Future] = {}

async def gateway_listener(ws: websockets.WebSocketClientProtocol):
    """Tâche de fond qui écoute les messages du gateway llmbasedos."""
    log.info("Gateway listener started.")
    try:
        async for message in ws:
            log.debug(f"Received from gateway: {message[:500]}")
            try:
                response = json.loads(message)
                request_id = response.get("id")
                
                # Si c'est une réponse à une requête que nous suivons, on résout la Future
                if request_id and request_id in pending_gateway_requests:
                    future = pending_gateway_requests.pop(request_id)
                    future.set_result(response)
                else:
                    # Sinon, c'est probablement un message de stream à relayer
                    log.info(f"Relaying non-tracked message to Claude: {message[:200]}")
                    print(json.dumps(response), flush=True)
            except Exception as e:
                log.error(f"Error processing message from gateway: {e}", exc_info=True)
    except websockets.exceptions.ConnectionClosed as e:
        log.warning(f"Connection to gateway closed: {e}")
    except Exception as e:
        log.error(f"Unexpected error in gateway_listener: {e}", exc_info=True)
    log.info("Gateway listener stopped.")

async def send_to_gateway(ws: websockets.WebSocketClientProtocol, request: dict) -> dict:
    """Envoie une requête au gateway et attend une réponse unique (non-stream)."""
    request_id = request.get("id")
    if not request_id:
        log.error("Request to gateway has no ID, cannot track response.")
        return {"jsonrpc": "2.0", "error": {"code": -32603, "message": "Internal bridge error: request has no ID"}}

    future = asyncio.get_running_loop().create_future()
    pending_gateway_requests[str(request_id)] = future
    
    try:
        await ws.send(json.dumps(request))
        log.info(f"Sent request to gateway (id: {request_id}): {request.get('method')}")
        # Attendre la réponse avec un timeout
        response = await asyncio.wait_for(future, timeout=20.0)
        return response
    except asyncio.TimeoutError:
        log.error(f"Timeout waiting for response from gateway for request ID: {request_id}")
        pending_gateway_requests.pop(str(request_id), None)
        return {"jsonrpc": "2.0", "error": {"code": -32000, "message": "Request to llmbasedos service timed out."}, "id": request_id}
    except Exception as e:
        log.error(f"Error sending request to gateway: {e}", exc_info=True)
        pending_gateway_requests.pop(str(request_id), None)
        return {"jsonrpc": "2.0", "error": {"code": -32000, "message": f"Bridge error: {e}"}, "id": request_id}

async def handle_claude_request(ws: websockets.WebSocketClientProtocol, request: dict):
    """Traite une requête de Claude et envoie la réponse."""
    method = request.get("method")
    request_id = request.get("id")

    if method == "initialize":
        log.info("Handling 'initialize' request from Claude.")
        # Utiliser mcp.listCapabilities pour construire la liste des outils
        caps_response = await send_to_gateway(ws, {"jsonrpc": "2.0", "method": "mcp.listCapabilities", "id": f"init-caps-{request_id}"})
        
        tools = []
        if caps_response and "result" in caps_response:
            for service in caps_response["result"]:
                for cap in service.get("capabilities", []):
                    # Le schéma doit être un objet pour Claude
                    input_schema = cap.get("params_schema", {})
                    if not isinstance(input_schema, dict):
                        input_schema = {"type": "object", "properties": {}}
                    
                    tools.append({
                        "name": cap["method"],
                        "description": cap.get("description", f"Executes the {cap['method']} command."),
                        "input_schema": input_schema
                    })
        
        response = {"jsonrpc": "2.0", "id": request_id, "result": {"tools": tools}}
        log.info(f"Sending initialize response to Claude with {len(tools)} tools.")
        print(json.dumps(response), flush=True)

    elif method == "call-tool": # Le nom de la méthode pour les appels d'outils
        tool_name = request.get("params", {}).get("name")
        tool_params = request.get("params", {}).get("arguments") # 'arguments' au pluriel
        
        # Relayer l'appel d'outil au gateway
        mcp_request = {"jsonrpc": "2.0", "method": tool_name, "params": tool_params, "id": request_id}
        mcp_response = await send_to_gateway(ws, mcp_request)
        
        # Claude s'attend à une réponse 'tool-result'
        # Le contenu doit être une chaîne de caractères. On sérialise le résultat JSON.
        content_str = ""
        if "result" in mcp_response:
            content_str = json.dumps(mcp_response["result"])
        elif "error" in mcp_response:
            content_str = f"Error: {json.dumps(mcp_response['error'])}"

        response = {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": content_str
            }
        }
        log.info(f"Sending tool result to Claude: {str(response)[:200]}")
        print(json.dumps(response), flush=True)

    else:
        log.warning(f"Received unhandled method from Claude: {method}")
        response = {"jsonrpc": "2.0", "error": {"code": -32601, "message": f"Method not found: {method}"}, "id": request_id}
        print(json.dumps(response), flush=True)


async def main_async_loop():
    """Point d'entrée principal avec une boucle de lecture asynchrone pour stdin."""
    log.info("Starting Claude Stdio Server Bridge (Async Loop Version)...")
    
    # Créer un lecteur asynchrone pour stdin
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)

    try:
        async with websockets.connect(LLMBASEDO_GATEWAY_WS_URL, open_timeout=10) as ws:
            log.info(f"Connected to llmbasedos gateway at {LLMBASEDO_GATEWAY_WS_URL}")
            
            # Lancer le listener du gateway en tâche de fond
            listener_task = asyncio.create_task(gateway_listener(ws))

            while not reader.at_eof():
                line = await reader.readline()
                if not line: continue
                line_str = line.decode('utf-8').strip()
                if not line_str: continue

                try:
                    request = json.loads(line_str)
                    # Lancer le traitement de la requête en tâche de fond pour ne pas bloquer la lecture
                    asyncio.create_task(handle_claude_request(ws, request))
                except Exception as e:
                    log.error(f"Error processing line from Claude: '{line_str}'. Error: {e}", exc_info=True)

            # Attendre que les tâches se terminent
            listener_task.cancel()
            await asyncio.sleep(0.1) # Laisser le temps à la cancellation de se propager

    except Exception as e:
        log.error(f"Main loop critical error: {e}", exc_info=True)
    
    log.info("Claude Stdio Server Bridge shutting down.")


if __name__ == "__main__":
    asyncio.run(main_async_loop())