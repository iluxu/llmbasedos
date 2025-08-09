# Fichier: llmbasedos_src/servers/canva/server.py
import asyncio
import json
import uuid
from pathlib import Path
from llmbasedos_src.mcp_server_framework import MCPServer

SERVER_NAME = "canva"
CAPS_FILE_PATH = str(Path(__file__).parent / "caps.json")
canva_server = MCPServer(SERVER_NAME, CAPS_FILE_PATH)

# Variables globales pour gérer le sous-processus Canva
canva_process = None
canva_reader = None
canva_writer = None

async def read_from_canva_mcp():
    """Tâche de fond pour lire les réponses du serveur Canva."""
    while canva_reader and not canva_reader.at_eof():
        try:
            line = await canva_reader.readline()
            if line:
                response_str = line.decode().strip()
                canva_server.logger.info(f"CANVA_MCP_RECV: {response_str}")
                # Ici, il faudrait une logique pour router la réponse au bon appelant,
                # mais pour une démo simple, on peut juste logguer.
        except Exception as e:
            canva_server.logger.error(f"Error reading from Canva MCP: {e}")
            break

@canva_server.set_startup_hook
async def on_startup(server: MCPServer):
    """Au démarrage, lance le serveur MCP de Canva en sous-processus."""
    global canva_process, canva_reader, canva_writer
    command = "npx"
    args = ["-y", "@canva/cli@latest", "mcp"]
    
    try:
        server.logger.info(f"Starting Canva MCP server with command: {command} {' '.join(args)}")
        canva_process = await asyncio.create_subprocess_exec(
            command,
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        canva_reader = canva_process.stdout
        canva_writer = canva_process.stdin
        server.logger.info(f"Canva MCP server started with PID: {canva_process.pid}")
        
        # Lancer une tâche pour écouter les logs de Canva
        asyncio.create_task(read_from_canva_mcp())

    except Exception as e:
        server.logger.critical(f"Failed to start Canva MCP server: {e}", exc_info=True)
        canva_process = None

@canva_server.set_shutdown_hook
async def on_shutdown(server: MCPServer):
    """À l'arrêt, termine proprement le sous-processus Canva."""
    if canva_process and canva_process.returncode is None:
        server.logger.info(f"Stopping Canva MCP server (PID: {canva_process.pid})...")
        canva_process.terminate()
        await canva_process.wait()
        server.logger.info("Canva MCP server stopped.")

@canva_server.register_method("mcp.canva.ask")
async def handle_canva_ask(server: MCPServer, request_id, params: list):
    if not canva_writer:
        raise RuntimeError("Canva MCP server is not running or not ready.")
        
    query = params[0]
    
    # Le protocole MCP est basé sur JSON-RPC. On doit construire une requête valide.
    # Pour une simple question, on peut l'encapsuler dans un format que le serveur Canva comprendra.
    # Souvent, c'est une requête `textDocument/didChange` ou similaire.
    # Pour une démo, on envoie une requête JSON-RPC simple.
    mcp_request = {
        "jsonrpc": "2.0",
        "method": "$/invokeTool", # Méthode hypothétique pour invoquer un outil
        "params": {
            "toolName": "canva-dev", # Nom de l'outil
            "prompt": query
        },
        "id": f"canva-bridge-{uuid.uuid4().hex}"
    }

    request_str = json.dumps(mcp_request) + "\n"
    server.logger.info(f"CANVA_MCP_SEND: {request_str.strip()}")
    canva_writer.write(request_str.encode())
    await canva_writer.drain()
    
    # ATTENTION: La réponse est asynchrone. Ce handler ne peut pas attendre
    # directement la réponse. Il faudrait un système de callback ou de future.
    # Pour un MVP, on retourne juste un statut d'envoi.
    return {"status": "Query sent to Canva MCP server. Check logs for response."}


if __name__ == "__main__":
    asyncio.run(canva_server.start())