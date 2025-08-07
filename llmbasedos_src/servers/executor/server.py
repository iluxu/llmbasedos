import asyncio
import json
import os
import logging
import socket  # <-- Import crucial manquant
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

# Importe le framework MCP depuis la racine du projet
from llmbasedos_src.mcp_server_framework import MCPServer

# --- Configuration du Serveur ---
SERVER_NAME = "orchestrator"
CAPS_FILE_PATH_STR = str(Path(__file__).parent / "caps.json")
ORCHESTRATOR_CUSTOM_ERROR_BASE = -32050

# Initialisation de l'instance du serveur
orchestrator_server = MCPServer(
    SERVER_NAME,
    CAPS_FILE_PATH_STR,
    custom_error_code_base=ORCHESTRATOR_CUSTOM_ERROR_BASE
)

# Stockage en mémoire des capacités découvertes
orchestrator_server.detailed_mcp_capabilities: List[Dict[str, Any]] = []


# --- Helper pour la communication interne via MCP ---
async def _internal_mcp_call(server: MCPServer, method: str, params: list = []) -> Any:
    """
    Fonction helper pour que l'orchestrateur appelle D'AUTRES services MCP,
    principalement le Gateway pour `mcp.llm.chat` et `mcp.listCapabilities`.
    """
    # Pour l'orchestrateur, tous les appels essentiels passent par le Gateway.
    socket_path_str = "/run/mcp/gateway.sock"
    
    def blocking_socket_call():
        if not os.path.exists(socket_path_str):
            raise FileNotFoundError(f"Gateway socket not found at {socket_path_str}")

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(300.0)  # Timeout généreux pour les appels LLM
            sock.connect(socket_path_str)
            
            payload_id = f"orchestrator-call-{uuid.uuid4().hex[:8]}"
            payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": payload_id}
            sock.sendall(json.dumps(payload).encode('utf-8') + b'\0')
            
            buffer = bytearray()
            while b'\0' not in buffer:
                chunk = sock.recv(16384)
                if not chunk: raise ConnectionError("Gateway closed connection unexpectedly.")
                buffer.extend(chunk)
            
            response_bytes, _ = buffer.split(b'\0', 1)
            response_data = json.loads(response_bytes.decode('utf-8'))
            
            if "error" in response_data and response_data["error"]:
                err = response_data["error"]
                raise RuntimeError(f"Internal MCP call to '{method}' failed: {err.get('message', 'Unknown error')} (Code: {err.get('code')})")
            
            if "result" in response_data:
                return response_data["result"]
            
            raise ValueError("Invalid MCP response from Gateway (missing result/error key).")

    return await server.run_in_executor(blocking_socket_call)


# --- Prompt Système de Génération ---
# C'est le "cerveau" de l'orchestrateur. Il doit être excellent.
SCRIPT_GENERATION_SYSTEM_PROMPT = """
You are an expert-level Python script generator for a platform called 'llmbasedos'. Your sole purpose is to translate a user's high-level intent into a functional, standalone Python script.

This script will run in an environment where a function `mcp_call(method: str, params: list)` is available.

### Rules for `mcp_call`:
1.  It sends a JSON-RPC request to the llmbasedos Gateway.
2.  It handles all networking and authentication.
3.  On success, it returns the `result` field of the JSON-RPC response.
4.  On failure, it raises an exception.
5.  All interactions with the outside world (files, LLMs, APIs) MUT be done through `mcp_call`.

### Available MCP Capabilities:
{mcp_capabilities_description}

### User's Intent:
"{user_intention}"

### Your Task:
Generate a Python script that accomplishes the user's intent.

### CRITICAL INSTRUCTIONS:
- The script MUST be complete and runnable.
- Assume `json`, `os`, and `time` are already imported. Do not import them again.
- The script must NOT define the `mcp_call` function.
- `mcp.fs.read` returns `{"content": "...", ...}`. Access the content with `.get("content")`.
- `mcp.llm.chat` takes `params=[{ "messages": [...], "options": {...} }]`. The result of this call is the direct, raw JSON response from the LLM provider (e.g., `{"choices": [...]}`).
- Use `print()` to provide informative feedback to the user about the script's progress.
- Your entire response MUST be ONLY the raw Python code. Do NOT wrap it in markdown backticks (```python ... ```) or add any explanation.
"""

@orchestrator_server.register_method("mcp.orchestrator.generate_script_from_intent")
async def handle_generate_script(server: MCPServer, request_id: str, params: List[Any]):
    user_intention = params[0]
    options = params[1] if len(params) > 1 else {}
    preferred_llm = options.get("preferred_llm_model", "gemini-1.5-pro")

    server.logger.info(f"Received intent for script generation: '{user_intention[:100]}...'")

    # Formater la description des capacités MCP pour le prompt
    caps_description_parts = []
    for service_info in server.detailed_mcp_capabilities:
        for cap in service_info.get("capabilities", []):
            method = cap.get("method")
            description = cap.get("description", "No description.")
            caps_description_parts.append(f"- `{method}`: {description}")
    
    mcp_capabilities_str = "\n".join(caps_description_parts)
    if not mcp_capabilities_str:
        mcp_capabilities_str = "No capabilities currently available."

    # Construire le prompt final
    final_prompt = SCRIPT_GENERATION_SYSTEM_PROMPT.format(
        mcp_capabilities_description=mcp_capabilities_str,
        user_intention=user_intention
    )

    # Paramètres pour l'appel LLM via le Gateway
    llm_params = [{
        "messages": [{"role": "user", "content": final_prompt}],
        "options": {"model": preferred_llm, "temperature": 0.1} # Température basse pour la génération de code
    }]

    try:
        server.logger.info("Calling LLM via Gateway to generate script...")
        llm_response = await _internal_mcp_call(server, "mcp.llm.chat", llm_params)
        
        if not llm_response or "choices" not in llm_response or not llm_response["choices"]:
            raise ValueError(f"LLM response for script generation is malformed: {llm_response}")

        generated_script = llm_response['choices'][0].get('message', {}).get('content', "").strip()
        
        # Nettoyage final
        if generated_script.startswith("```python"):
            generated_script = generated_script[len("```python"):].strip()
        if generated_script.endswith("```"):
            generated_script = generated_script[:-len("```")].strip()

        server.logger.info("Successfully generated script.")
        
        return {
            "script_content": generated_script,
            "usage_info": llm_response.get("usage")
        }

    except Exception as e:
        server.logger.error(f"Failed to generate script: {e}", exc_info=True)
        # On lève une exception que le framework MCPServer va transformer en erreur JSON-RPC
        raise RuntimeError(f"An error occurred during script generation: {e}")

# --- Hook de Démarrage ---
@orchestrator_server.set_startup_hook
async def on_orchestrator_startup(server: MCPServer):
    """Au démarrage, récupère la liste des capacités disponibles pour l'injecter dans les prompts."""
    server.logger.info("Orchestrator startup: Fetching initial MCP capabilities...")
    try:
        # On attend un peu que le gateway soit prêt
        await asyncio.sleep(2) 
        capabilities = await _internal_mcp_call(server, "mcp.listCapabilities")
        if isinstance(capabilities, list):
            server.detailed_mcp_capabilities = capabilities
            server.logger.info(f"Successfully fetched {len(capabilities)} service capability descriptions.")
        else:
            server.logger.error("Failed to fetch capabilities: response was not a list.")
    except Exception as e:
        server.logger.error(f"Could not fetch MCP capabilities on startup: {e}", exc_info=True)
        # Le serveur continue de tourner, mais la génération de script sera moins informée.

# --- Point d'Entrée Principal ---
if __name__ == "__main__":
    # La configuration du logging est gérée par MCPServer
    try:
        asyncio.run(orchestrator_server.start())
    except KeyboardInterrupt:
        orchestrator_server.logger.info("Orchestrator server stopped by user.")