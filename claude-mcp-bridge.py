# claude-mcp-bridge.py
import asyncio
import json
import websockets
import uuid
import logging
from typing import Any, Dict, List, Optional
from mcp.server.fastmcp import FastMCP

# --- Configuration & Logging ---
LLMBASEDO_GATEWAY_WS_URL = "ws://localhost:8000/ws"
LOG_FILE = "/tmp/llmbasedos_claude_bridge.log"
logging.basicConfig(filename=LOG_FILE, level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
log = logging.getLogger("claude_bridge")

# --- Instance du Serveur Pont ---
mcp_bridge = FastMCP("llmbasedos-bridge")

async def relay_to_gateway(method: str, params: Any) -> str:
    """Fonction générique pour relayer n'importe quel appel MCP au gateway."""
    log.info(f"Relaying call for method '{method}' with params: {params}")
    try:
        async with websockets.connect(LLMBASEDO_GATEWAY_WS_URL, open_timeout=10) as ws:
            request_id = f"bridge-call-{uuid.uuid4().hex[:8]}"
            mcp_request = {"jsonrpc": "2.0", "method": method, "params": params, "id": request_id}
            
            await ws.send(json.dumps(mcp_request))
            response_str = await asyncio.wait_for(ws.recv(), timeout=60.0) # Timeout plus long
            response = json.loads(response_str)
            
            if "result" in response:
                # Retourner une chaîne formatée pour aider Claude
                result_data = response["result"]
                if isinstance(result_data, (dict, list)):
                    return f"Success. Result:\n{json.dumps(result_data, indent=2)}"
                else:
                    return f"Success. Result: {result_data}"
            elif "error" in response:
                return f"Error from llmbasedos: {json.dumps(response['error'])}"
            else:
                return "Unknown response from llmbasedos."
    except Exception as e:
        log.error(f"Error relaying call for '{method}': {e}", exc_info=True)
        return f"Bridge error: Failed to execute '{method}'. Reason: {type(e).__name__}"

async def create_and_register_tools():
    """Récupère les capacités du gateway et crée dynamiquement les outils pour FastMCP."""
    log.info("Fetching capabilities from llmbasedos gateway to create tools...")
    try:
        async with websockets.connect(LLMBASEDO_GATEWAY_WS_URL, open_timeout=10) as ws:
            req_id = f"bridge-init-{uuid.uuid4().hex[:8]}"
            await ws.send(json.dumps({"jsonrpc": "2.0", "method": "mcp.listCapabilities", "id": req_id}))
            response_str = await asyncio.wait_for(ws.recv(), timeout=10.0)
            response = json.loads(response_str)
            
            if not response or "result" not in response:
                log.error("Failed to get capabilities from gateway.")
                return

            for service in response["result"]:
                for cap in service.get("capabilities", []):
                    method_name = cap["method"]
                    description = cap.get("description", f"Executes {method_name}")
                    params_schema = cap.get("params_schema", {})
                    
                    # FastMCP utilise les annotations de type Python pour créer le input_schema.
                    # Nous devons créer une fonction avec la bonne signature dynamiquement.
                    # C'est complexe.
                    
                    # *** PIVOT STRATÉGIQUE POUR LA DÉMO ***
                    # On garde UN SEUL outil, mais on corrige le type du paramètre `params`.
                    # La boucle de Claude vient du fait qu'il essaie de passer une LISTE à un paramètre de type STRING.
                    # On va dire à FastMCP que le paramètre `params` est de type `Any`.
                    pass # Implémenté dans la version finale ci-dessous

    except Exception as e:
        log.error(f"Could not create tools. Bridge will have no capabilities. Error: {e}", exc_info=True)


# --- Version Finale du Pont avec UN SEUL Outil, mais avec le bon Type ---

@mcp_bridge.tool()
async def execute_mcp_command(method: str, params: Any) -> str:
    """
    Executes a raw MCP command on the llmbasedos gateway.
    
    Args:
        method: The full MCP method name (e.g., 'mcp.fs.list').
        params: The parameters for the command, can be a list or an object/dictionary.
    """
    log.info(f"Executing tool 'execute_mcp_command' with method: '{method}', params: {params} (type: {type(params)})")
    try:
        # 'params' est maintenant directement un objet Python (list ou dict), pas une chaîne JSON.
        # FastMCP et Pydantic s'en sont chargés.
        
        mcp_request = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params, # On passe l'objet Python directement
            "id": f"bridge-call-{uuid.uuid4().hex[:8]}"
        }
        
        async with websockets.connect(LLMBASEDO_GATEWAY_WS_URL, open_timeout=10) as ws:
            await ws.send(json.dumps(mcp_request))
            response_str = await asyncio.wait_for(ws.recv(), timeout=60.0)
            response = json.loads(response_str)
            
            if "result" in response:
                result_data = response["result"]
                if method == "mcp.fs.list" and isinstance(result_data, list):
                    if not result_data: return "The directory is empty."
                    formatted_list = "\n".join([f"- {item.get('name')} ({item.get('type')})" for item in result_data])
                    return f"Successfully listed files:\n{formatted_list}"
                else:
                    return f"Command successful. Result:\n{json.dumps(result_data, indent=2)}"
            elif "error" in response:
                return f"Error from llmbasedos: {json.dumps(response['error'])}"
            else:
                return "Unknown response from llmbasedos."

    except Exception as e:
        log.error(f"Error in execute_mcp_command: {e}", exc_info=True)
        return f"Bridge error: Failed to execute command. Reason: {type(e).__name__}"


if __name__ == "__main__":
    # La méthode .run() de FastMCP gère toute la boucle stdio et l'initialisation.
    log.info("Starting FastMCP bridge server for llmbasedos...")
    mcp_bridge.run(transport='stdio')