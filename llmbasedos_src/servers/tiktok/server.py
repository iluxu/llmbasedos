# llmbasedos_src/servers/tiktok/server.py
import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

# Importer le framework MCP depuis le chemin du projet
from llmbasedos.mcp_server_framework import MCPServer

# --- Configuration du Serveur ---
SERVER_NAME = "tiktok"
CAPS_FILE_PATH_STR = str(Path(__file__).parent / "caps.json")

# Initialiser l'instance du serveur
tiktok_server = MCPServer(SERVER_NAME, CAPS_FILE_PATH_STR)

# --- Handler de la Méthode MCP (Simulé) ---

@tiktok_server.register_method("mcp.tiktok.search")
async def handle_tiktok_search(server: MCPServer, request_id: str, params: List[Any]):
    """
    Simule une recherche de vidéos TikTok et retourne des données en dur.
    """
    # Le schéma attend un tableau avec un objet de paramètres.
    search_params = params[0] if params else {}
    query = search_params.get("query", "No query provided")
    
    server.logger.info(f"[MOCK] Received TikTok search request for query: '{query}'")

    # Créer une réponse simulée (mock)
    mock_response = {
        "videos": [
            {
                "creator": "@ai_innovator",
                "description": "I automated my entire job with this one AI trick!",
                "views": 250000,
                "url": "https://www.tiktok.com/mock/video1"
            },
            {
                "creator": "@productivity_guru",
                "description": "Stop using ChatGPT for this... use a local agent instead.",
                "views": 180000,
                "url": "https://www.tiktok.com/mock/video2"
            }
        ]
    }
    
    # Simuler une petite latence pour que ce soit réaliste
    await asyncio.sleep(1.5)
    
    server.logger.info(f"[MOCK] Returning simulated TikTok search results.")
    return mock_response

# --- Point d'Entrée Principal ---
if __name__ == "__main__":
    import sys
    logging.basicConfig(
        level=os.getenv(f"LLMBDO_{SERVER_NAME.upper()}_LOG_LEVEL", "INFO").upper(),
        format=f"%(asctime)s - TIKTOK_MOCK_MAIN - %(name)s - %(levelname)s - %(message)s"
    )
    try:
        asyncio.run(tiktok_server.start())
    except KeyboardInterrupt:
        print(f"\nTikTok Mock Server '{SERVER_NAME}' stopped by user.")
    except Exception as e:
        print(f"TikTok Mock Server '{SERVER_NAME}' crashed: {e}", file=sys.stderr)