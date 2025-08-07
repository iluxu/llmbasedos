# llmbasedos_src/servers/orchestrator/server.py
import asyncio
import json
import os
import logging # Sera géré par MCPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from llmbasedos_src.mcp_server_framework import MCPServer

# --- Configuration du Serveur ---
SERVER_NAME = "orchestrator"
CAPS_FILE_PATH_STR = str(Path(__file__).parent / "caps.json")
ORCHESTRATOR_CUSTOM_ERROR_BASE = -32050 # Base pour les erreurs spécifiques

# Initialiser l'instance du serveur
orchestrator_server = MCPServer(
    SERVER_NAME,
    CAPS_FILE_PATH_STR,
    custom_error_code_base=ORCHESTRATOR_CUSTOM_ERROR_BASE
)

# Variable pour stocker la liste des capacités MCP (pourra être mise à jour)
orchestrator_server.available_mcp_capabilities: List[str] = [] # type: ignore
orchestrator_server.detailed_mcp_capabilities: List[Dict[str, Any]] = [] # type: ignore

# --- Helper pour appeler le Gateway (LLM ou autres services) ---
# Cette fonction est similaire à celle de prospecting_app.py mais adaptée pour un usage interne au serveur
async def _internal_mcp_call(server: MCPServer, method: str, params: list = []) -> Dict[str, Any]:
    """
    Fonction helper pour que l'orchestrator_server appelle d'autres services MCP
    via leurs sockets UNIX (principalement le gateway pour mcp.llm.chat ou mcp.listCapabilities).
    """
    # Déterminer le nom du service à partir de la méthode MCP
    # Le Gateway gère mcp.llm.* et les méthodes MCP globales comme mcp.listCapabilities
    target_service_name = "gateway"
    if method.startswith("mcp.") and not method.startswith("mcp.llm.") and not method.startswith("mcp.gateway.") and method != "mcp.listCapabilities" and method != "mcp.hello":
        try:
            target_service_name = method.split('.')[1]
        except IndexError:
            server.logger.error(f"_internal_mcp_call: Could not determine service for method '{method}'. Assuming gateway.")
            # Fallback to gateway if service name cannot be parsed, though this indicates an issue.

    socket_path_str = f"/run/mcp/{target_service_name}.sock"
    # server.logger.debug(f"_internal_mcp_call: Calling {method} on {target_service_name} via {socket_path_str} with params {params}")

    # Utiliser run_in_executor pour l'opération de socket bloquante
    def blocking_socket_call():
        if not os.path.exists(socket_path_str):
            raise FileNotFoundError(f"Socket for service '{target_service_name}' not found at {socket_path_str}")

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(120.0) # Timeout pour la connexion et la réponse
            sock.connect(socket_path_str)
            payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": f"orchestrator-call-{os.urandom(4).hex()}"}
            sock.sendall(json.dumps(payload).encode('utf-8') + b'\0')
            
            buffer = bytearray()
            while b'\0' not in buffer:
                chunk = sock.recv(8192)
                if not chunk: raise ConnectionError("Connection closed by target service.")
                buffer.extend(chunk)
            
            response_bytes, _ = buffer.split(b'\0', 1)
            response_data = json.loads(response_bytes.decode('utf-8'))
            
            if "error" in response_data and response_data["error"]:
                err_details = response_data["error"]
                raise RuntimeError(f"MCP call to '{method}' failed: {err_details.get('message', 'Unknown error')} (Code: {err_details.get('code')})")
            if "result" in response_data:
                return response_data["result"]
            raise ValueError("Invalid MCP response from target service (missing result/error).")

    # Exécuter l'appel bloquant dans le pool de threads du serveur MCPServer
    # Note: MCPServer doit avoir un executor initialisé.
    # S'il n'en a pas, il faudra gérer les threads ici ou utiliser une lib async pour les sockets UNIX.
    # Mais MCPServer framework a un self.executor
    return await server.run_in_executor(blocking_socket_call)


# --- Prompt de Base pour la Génération de Script ---
# Ce prompt est crucial et nécessitera beaucoup d'itération.
# Il doit être suffisamment précis pour guider le LLM.
SCRIPT_GENERATION_SYSTEM_PROMPT_TEMPLATE = """
You are an expert Python script generator for the 'llmbasedos' operating system.
Your task is to translate a user's natural language intention into a functional Python script.
This script will use a predefined `mcp_call(method: str, params: list = [])` function to interact with llmbasedos capabilities.

The `mcp_call` function works as follows:
- It takes the full MCP method name (e.g., "mcp.fs.read") and a list of parameters.
- It connects to the appropriate llmbasedos service via its UNIX socket.
- It sends a JSON-RPC request.
- It returns the "result" field of the JSON-RPC response if successful.
- It raises an MCPError exception if the JSON-RPC response contains an "error" field or if a communication error occurs.
- You should assume `mcp_call` and standard Python libraries like `json`, `os`, `re`, `time` are available.

Available MCP Capabilities:
{mcp_capabilities_description}

User's Intention:
"{user_intention}"

Your generated Python script should:
1. Be a complete, runnable Python script.
2. Only use the `mcp_call` function to interact with llmbasedos. Do NOT attempt to use `socket` or other low-level networking directly.
3. Import `json` if you need to parse or generate JSON within the script.
4. If the user's intention involves file paths, assume they are absolute virtual paths within llmbasedos (e.g., "/notes/file.txt", "/data/images/pic.png").
5. If the intention requires a result from an `mcp_call` to be used in a subsequent `mcp_call`, store it in a variable.
6. If the `mcp.llm.chat` capability is used, its `params` should be a list: `[[{"role": "user", "content": "..."}], {"model": "model_name_optional"}]`. The result of `mcp_call("mcp.llm.chat", ...)` will be the direct API response from the LLM provider (e.g., a dictionary with a "choices" key).
7. For `mcp.fs.read`, the result of `mcp_call` is `{"path": "...", "content": "...", "encoding": "...", ...}`. The actual file content is in `result.get("content")`.
8. For `mcp.fs.write`, `params` are `["/path/to/file", "content_to_write", "encoding_text_or_base64_optional"]`.
9. Print informative messages using `print()` to indicate progress or results.
10. Handle potential errors from `mcp_call` gracefully if possible (e.g., using try-except for file not found on read), or let them propagate if they are critical.
11. The script should be self-contained and not define the `mcp_call` function itself.
12. The final output of the script should achieve the user's intention.
13. IMPORTANT: Do NOT include the markdown backticks (```python ... ```) around the code. Just provide the raw Python code.

Generate the Python script now:
"""

@orchestrator_server.register_method("mcp.orchestrator.generate_script_from_intent")
async def handle_generate_script(
    server: MCPServer,
    request_id: Optional[Union[str, int]],
    params: List[Any]
):
    user_intention = params[0]
    options = params[1] if len(params) > 1 else {}
    preferred_llm = options.get("preferred_llm_model", "gemini-1.5-pro") # ou le modèle par défaut du gateway

    server.logger.info(f"Received intent for script generation: '{user_intention}'")

    # Préparer la description des capacités MCP pour le prompt
    # Utiliser les capacités détaillées pour donner plus d'infos au LLM
    if not server.detailed_mcp_capabilities: # type: ignore
        server.logger.warning("No detailed MCP capabilities loaded yet for prompt generation. Attempting to fetch.")
        try:
            # L'appel à mcp.listCapabilities est géré par le Gateway
            server.detailed_mcp_capabilities = await _internal_mcp_call(server, "mcp.listCapabilities", []) # type: ignore
            server.logger.info(f"Fetched {len(server.detailed_mcp_capabilities)} detailed capabilities.") # type: ignore
        except Exception as e_caps:
            server.logger.error(f"Failed to fetch detailed MCP capabilities: {e_caps}")
            raise server.create_custom_error(
                request_id, 1, "Failed to retrieve system capabilities for script generation.",
                data={"details": str(e_caps)}
            )

    # Formater la description des capacités pour le prompt
    # On veut une description concise mais utile.
    # Lister uniquement les noms de méthodes et leurs descriptions.
    # Les schémas de paramètres pourraient être trop verbeux pour un prompt initial.
    # On pourrait avoir une version "résumée" de `detailed_mcp_capabilities`
    
    caps_description_parts = []
    for service_info in server.detailed_mcp_capabilities: # type: ignore
        service_name = service_info.get("service_name", "unknown_service")
        for cap in service_info.get("capabilities", []):
            method_name = cap.get("method")
            description = cap.get("description", "No description.")
            # Pour le MVP, on ne met pas les params_schema pour garder le prompt plus court
            # params_info = cap.get("params_schema", {}) 
            # caps_description_parts.append(f"- {method_name}: {description} (Params schema: {json.dumps(params_info)})")
            caps_description_parts.append(f"- {method_name}: {description}")
    
    mcp_capabilities_str = "\n".join(caps_description_parts)
    if not mcp_capabilities_str:
        mcp_capabilities_str = "No capabilities currently available or described."

    # Construire le prompt final pour le LLM
    final_prompt_for_llm = SCRIPT_GENERATION_SYSTEM_PROMPT_TEMPLATE.format(
        mcp_capabilities_description=mcp_capabilities_str,
        user_intention=user_intention
    )
    server.logger.debug(f"Prompt for script generation LLM:\n{final_prompt_for_llm}")

    # Appeler le LLM (via le Gateway) pour générer le script
    llm_params = [
        [{"role": "user", "content": final_prompt_for_llm}], # Messages
        {"model": preferred_llm, "temperature": 0.2} # Options (température basse pour plus de déterminisme)
    ]

    try:
        # _internal_mcp_call retourne directement le "result" de l'appel JSON-RPC au Gateway
        # qui, pour mcp.llm.chat, est la réponse directe de l'API LLM
        llm_response = await _internal_mcp_call(server, "mcp.llm.chat", llm_params)
        server.logger.debug(f"LLM response for script generation: {llm_response}")

        if not llm_response or "choices" not in llm_response or not llm_response["choices"]:
            raise ValueError(f"LLM response for script generation is malformed or empty. Full response: {llm_response}")

        generated_script_content = llm_response['choices'][0].get('message', {}).get('content', "")
        
        # Nettoyage additionnel si le LLM ajoute quand même les backticks
        generated_script_content = generated_script_content.strip()
        if generated_script_content.startswith("```python"):
            generated_script_content = generated_script_content[len("```python"):].strip()
        if generated_script_content.startswith("```"):
            generated_script_content = generated_script_content[len("```"):].strip()
        if generated_script_content.endswith("```"):
            generated_script_content = generated_script_content[:-len("```")].strip()

        server.logger.info(f"Successfully generated script for intent '{user_intention}'")
        server.logger.debug(f"Generated script:\n{generated_script_content}")
        
        return {
            "script_content": generated_script_content,
            "estimated_cost": llm_response.get("usage"), # Si le LLM le fournit
            "warnings": [] # Pour l'instant
        }

    except FileNotFoundError as fnfe: # Pour _internal_mcp_call si un socket est manquant
        server.logger.error(f"Script Generation: Socket not found during internal MCP call: {fnfe}")
        raise server.create_custom_error(request_id, 2, f"Internal communication error: {fnfe}", data={"reason": "socket_not_found"})
    except ConnectionError as ce:
        server.logger.error(f"Script Generation: Connection error during internal MCP call: {ce}")
        raise server.create_custom_error(request_id, 3, f"Internal communication error: {ce}", data={"reason": "connection_error"})
    except RuntimeError as rte: # Erreurs levées par _internal_mcp_call pour les échecs MCP
        server.logger.error(f"Script Generation: MCP error during LLM call: {rte}")
        raise server.create_custom_error(request_id, 4, f"Failed to use LLM for script generation: {rte}", data={"reason": "llm_call_failed"})
    except ValueError as ve: # Erreurs de parsing de la réponse LLM
        server.logger.error(f"Script Generation: Error processing LLM response: {ve}")
        raise server.create_custom_error(request_id, 5, f"Invalid response from LLM during script generation: {ve}", data={"reason": "llm_response_invalid"})
    except Exception as e:
        server.logger.error(f"Unexpected error during script generation for intent '{user_intention}': {e}", exc_info=True)
        raise server.create_custom_error(request_id, ORCHESTRATOR_CUSTOM_ERROR_BASE - 99, f"Unexpected internal server error during script generation: {type(e).__name__}", data={"details": str(e)})

# --- Hooks de Cycle de Vie du Serveur ---
async def on_orchestrator_startup(server: MCPServer):
    server.logger.info(f"Orchestrator Server '{server.server_name}' on_startup: Fetching initial MCP capabilities...")
    try:
        # Utiliser _internal_mcp_call pour obtenir les capacités du Gateway
        # Le Gateway expose "mcp.listCapabilities"
        list_caps_result = await _internal_mcp_call(server, "mcp.listCapabilities", [])
        if isinstance(list_caps_result, list):
            server.detailed_mcp_capabilities = list_caps_result # type: ignore
            # Extraire juste les noms de méthode pour une liste simple si besoin
            simple_caps = []
            for service_info in list_caps_result:
                for cap_detail in service_info.get("capabilities", []):
                    if cap_detail.get("method"):
                        simple_caps.append(cap_detail["method"])
            server.available_mcp_capabilities = sorted(list(set(simple_caps))) # type: ignore
            server.logger.info(f"Successfully fetched {len(server.available_mcp_capabilities)} MCP capability names and {len(server.detailed_mcp_capabilities)} detailed entries.") # type: ignore
        else:
            server.logger.error(f"mcp.listCapabilities returned unexpected type: {type(list_caps_result)}. Expected list.")
            
    except Exception as e:
        server.logger.error(f"Failed to fetch MCP capabilities during orchestrator startup: {e}", exc_info=True)
        # Le serveur démarrera quand même, mais la génération de script pourrait échouer ou être limitée.

orchestrator_server.set_startup_hook(on_orchestrator_startup)

# --- Point d'Entrée Principal ---
if __name__ == "__main__":
    # Configuration du logging basique si MCPServer ne le fait pas assez tôt
    if not orchestrator_server.logger.hasHandlers():
        _sh = logging.StreamHandler()
        _sh.setFormatter(logging.Formatter(f"%(asctime)s - {orchestrator_server.server_name} (main) - %(levelname)s - %(message)s"))
        orchestrator_server.logger.addHandler(_sh)
        orchestrator_server.logger.setLevel(os.getenv(f"LLMBDO_{SERVER_NAME.upper()}_LOG_LEVEL", "INFO").upper())
    
    orchestrator_server.logger.info(f"Starting Orchestrator Server '{SERVER_NAME}' via __main__...")
    try:
        asyncio.run(orchestrator_server.start())
    except KeyboardInterrupt:
        orchestrator_server.logger.info(f"Orchestrator Server '{SERVER_NAME}' (main) stopped by user.")
    except Exception as e_main:
        orchestrator_server.logger.critical(f"Orchestrator Server '{SERVER_NAME}' (main) crashed: {e_main}", exc_info=True)
    finally:
        orchestrator_server.logger.info(f"Orchestrator Server '{SERVER_NAME}' (main) exiting.")