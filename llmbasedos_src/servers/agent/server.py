# llmbasedos_src/servers/agent/server.py
import asyncio
import logging # Assurez-vous que cet import est bien là
import os
from pathlib import Path
import uuid
import yaml
import docker 
import requests 
import threading 
import time 
from typing import Any, Dict, List, Optional, Union, Callable
from datetime import datetime, timezone
import subprocess 
import json 
import re  
import socket # Ajouté pour _call_mcp_service_blocking

# --- Import Framework ---
from llmbasedos.mcp_server_framework import MCPServer 

# --- Server Specific Configuration ---
SERVER_NAME = "agent"
CAPS_FILE_PATH_STR = str(Path(__file__).parent / "caps.json")
AGENT_CUSTOM_ERROR_BASE = -32040

WORKFLOWS_DIR_CONF = Path(os.getenv("LLMBDO_AGENT_WORKFLOWS_DIR", "/etc/llmbasedos/workflows"))
WORKFLOWS_DIR_CONF.mkdir(parents=True, exist_ok=True)

AGENT_EXEC_LOG_DIR_CONF = Path(os.getenv("LLMBDO_AGENT_EXEC_LOG_DIR", f"/var/log/llmbasedos/{SERVER_NAME}_executions"))
AGENT_EXEC_LOG_DIR_CONF.mkdir(parents=True, exist_ok=True)

N8N_LITE_URL_CONF = os.getenv("LLMBDO_N8N_LITE_URL", "http://localhost:5678")

agent_server = MCPServer(
    server_name=SERVER_NAME,
    caps_file_path_str=CAPS_FILE_PATH_STR,
    custom_error_code_base=AGENT_CUSTOM_ERROR_BASE
)

# --- Helper pour appeler un autre service MCP via son socket UNIX (bloquant) ---
# Dans llmbasedos_src/servers/agent/server.py

def _call_mcp_service_blocking(
    server_instance: MCPServer,
    method: str,
    params: Union[List[Any], Dict[str, Any]]
) -> Dict[str, Any]: # Le type de retour sera la réponse MCP "finale" simulée
    import socket
    import json # Assurez-vous que json est importé
    import uuid   # Assurez-vous que uuid est importé

    socket_path_str: str
    target_service_name_for_log: str
    is_llm_chat_call = False # Drapeau pour gérer le streaming

    gateway_handled_method_prefixes = ("mcp.llm.", "mcp.licence.")
    gateway_handled_exact_methods = ("mcp.hello", "mcp.listCapabilities")

    if method.startswith(gateway_handled_method_prefixes) or \
       method in gateway_handled_exact_methods:
        target_service_name_for_log = f"gateway (for method '{method}')"
        gateway_socket_env_var = "LLMBDO_GATEWAY_UNIX_SOCKET_PATH"
        default_gateway_socket = "/run/mcp/gateway.sock"
        socket_path_str = os.getenv(gateway_socket_env_var, default_gateway_socket)
        if not Path(socket_path_str).name:
             server_instance.logger.warning(f"Env var {gateway_socket_env_var} might be empty, using default {default_gateway_socket} for gateway socket.")
             socket_path_str = default_gateway_socket
        
        if method == "mcp.llm.chat":
            is_llm_chat_call = True
            # S'assurer que l'agent demande explicitement NON-STREAM au gateway
            # si les options de llm sont passées en params[1]
            if isinstance(params, list) and len(params) > 1 and isinstance(params[1], dict):
                params[1]["stream"] = False # L'agent ne veut pas gérer le stream du gateway
            elif isinstance(params, list) and len(params) == 1: # Seulement la liste de messages
                params.append({"stream": False}) # Ajouter les options avec stream: false
            else: # Format de params inattendu pour mcp.llm.chat
                server_instance.logger.warning(f"Agent MCP Call: Unexpected params format for mcp.llm.chat: {params}. Forcing non-streamed request if possible.")
                # On essaie de continuer, le gateway devrait valider.
                # Si params est un dict, on ne peut pas facilement injecter stream:false.
                # Pour la démo, on suppose que params est une liste.

    else:
        try:
            target_service_name_for_log = method.split('.')[1] 
            socket_path_str = f"/run/mcp/{target_service_name_for_log}.sock"
        except IndexError:
            err_msg = f"Invalid MCP method format for service dispatch: '{method}'. Expected 'mcp.service.action'."
            server_instance.logger.error(err_msg); raise ValueError(err_msg)

    request_id = f"agent_mcp_call_{uuid.uuid4().hex[:8]}"
    request_payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": request_id}

    server_instance.logger.debug(
        f"Agent MCP Call: To {socket_path_str} (Service: {target_service_name_for_log}), "
        f"Method: {method}, Payload: {str(request_payload)[:200]}..., ReqID: {request_id}"
    )
    
    sock = None
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        call_timeout = float(os.getenv("LLMBDO_AGENT_MCP_CALL_TIMEOUT_SEC", "180.0")) 
        sock.settimeout(call_timeout) 
        sock.connect(socket_path_str)
        request_bytes = json.dumps(request_payload).encode('utf-8') + b'\0'
        sock.sendall(request_bytes)

        # --- Logique de lecture de réponse modifiée ---
        full_assembled_content = [] # Pour assembler les chunks de mcp.llm.chat
        final_llm_api_response_structure = None # Pour stocker la structure complète de l'API LLM
        last_received_id = None

        while True: # Lire tous les messages streamés jusqu'à la fin
            response_buffer = bytearray()
            message_bytes = b""
            while True: # Lire un message JSON complet délimité par \0
                chunk = sock.recv(8192) 
                if not chunk: # EOF
                    if not response_buffer and not message_bytes: # Rien du tout et EOF
                        raise RuntimeError(f"No response (connection closed by peer prematurely) from {target_service_name_for_log} for method {method}")
                    message_bytes = response_buffer # Traiter ce qui a été bufferisé avant EOF
                    response_buffer = bytearray() # Vider le buffer
                    break # Sortir de la boucle de lecture de chunk
                
                response_buffer.extend(chunk)
                if b'\0' in response_buffer:
                    message_bytes, rest = response_buffer.split(b'\0', 1)
                    response_buffer = rest 
                    break
            
            if not message_bytes: # Si EOF a été atteint et qu'il n'y avait rien dans le buffer
                if is_llm_chat_call and full_assembled_content: # Fin normale du stream LLM par EOF
                    server_instance.logger.info(f"Agent MCP Call: LLM stream for {method} (ReqID {request_id}) ended by EOF after receiving content.")
                    # Construire la réponse finale simulée que l'agent attend
                    # La structure doit ressembler à une réponse OpenAI non-streamée
                    final_simulated_result = {
                        "choices": [{"message": {"content": "".join(full_assembled_content)}}],
                        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0} # Placeholder
                    }
                    # Si on a capturé la structure de la dernière réponse de l'API LLM (le chunk [DONE])
                    if final_llm_api_response_structure and isinstance(final_llm_api_response_structure.get("data"), dict):
                        final_simulated_result["id"] = final_llm_api_response_structure["data"].get("id", "simulated_id")
                        final_simulated_result["model"] = final_llm_api_response_structure["data"].get("model", "simulated_model")
                        # On pourrait essayer d'extraire "usage" si disponible dans le dernier chunk (rare)

                    return {"jsonrpc": "2.0", "id": last_received_id or request_id, "result": final_simulated_result}
                elif not is_llm_chat_call: # Pour les appels non-LLM, EOF sans message est une erreur
                     raise RuntimeError(f"Connection closed by {target_service_name_for_log} for {method} without sending a complete response.")
                else: # Stream LLM, mais aucun contenu reçu et EOF
                     raise RuntimeError(f"LLM stream for {method} ended by EOF without any content chunks.")


            response_str = message_bytes.decode('utf-8')
            response_data = json.loads(response_str)
            last_received_id = response_data.get("id")

            server_instance.logger.debug(
                f"Agent MCP Call: RCV_MSG from {target_service_name_for_log} (OrigReqID {request_id}, MsgID {last_received_id}): {response_str[:200]}..."
            )

            if "error" in response_data:
                err_obj = response_data["error"]
                err_msg_detail = (f"MCP error from {target_service_name_for_log} (method {method}): "
                                  f"Code {err_obj.get('code')} - {err_obj.get('message')}")
                error_data_log = f" Error Data: {json.dumps(err_obj.get('data'))}" if err_obj.get('data') else ""
                server_instance.logger.warning(err_msg_detail + error_data_log)
                raise RuntimeError(err_msg_detail)

            if not is_llm_chat_call: # Si ce n'est pas un appel llm.chat, c'est la réponse finale
                return response_data 
            
            # C'est un appel mcp.llm.chat, on s'attend à des chunks
            mcp_result = response_data.get("result", {})
            if not isinstance(mcp_result, dict):
                raise RuntimeError(f"LLM chat response 'result' field is not a dictionary: {mcp_result}")

            event_type = mcp_result.get("type")
            event_content = mcp_result.get("content") # C'est le payload de l'API LLM (ex: le chunk OpenAI)

            if event_type == "llm_chunk" and isinstance(event_content, dict):
                # Structure OpenAI: event_content = {"choices": [{"delta": {"content": "..."}}]}
                delta_content = event_content.get("choices", [{}])[0].get("delta", {}).get("content")
                if delta_content:
                    full_assembled_content.append(delta_content)
                # On pourrait aussi stocker le dernier 'event_content' si on veut l'ID, le modèle, etc.
                final_llm_api_response_structure = {"event": "chunk", "data": event_content}


            elif event_type == "llm_stream_end":
                server_instance.logger.info(f"Agent MCP Call: LLM stream for {method} (ReqID {request_id}) ended via 'llm_stream_end'.")
                final_simulated_result = {
                    "choices": [{"message": {"content": "".join(full_assembled_content)}}],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0} # Placeholder
                }
                if event_content and isinstance(event_content, dict): # le content de llm_stream_end peut avoir des infos
                     final_llm_api_response_structure = {"event": "done", "data": event_content}
                     final_simulated_result["id"] = event_content.get("id", "simulated_id_done")
                     final_simulated_result["model"] = event_content.get("model", "simulated_model_done")

                return {"jsonrpc": "2.0", "id": last_received_id or request_id, "result": final_simulated_result}
            
            elif event_type == "error": # Si le gateway a encapsulé une erreur dans un event
                 err_data = event_content or {}
                 err_msg_detail = (f"LLM Error event from gateway (method {method}): "
                                   f"{err_data.get('message', 'Unknown LLM error event')}")
                 server_instance.logger.warning(err_msg_detail)
                 raise RuntimeError(err_msg_detail)
            
            # Si ce n'est pas la fin du stream, on continue de lire le socket pour le prochain message
            if not response_buffer: # Si on a lu exactement un message et vidé le buffer
               pass # La boucle while externe va continuer

    # ... (catch des exceptions FileNotFoundError, ConnectionRefusedError, socket.timeout etc. comme avant) ...
    except FileNotFoundError:
        err = f"Socket for service '{target_service_name_for_log}' not found at {socket_path_str}."
        server_instance.logger.error(err); raise RuntimeError(err)
    except ConnectionRefusedError:
        err = f"Connection refused by service '{target_service_name_for_log}' at {socket_path_str}."
        server_instance.logger.error(err); raise RuntimeError(err)
    except socket.timeout:
        err = f"Timeout ({call_timeout}s) calling service '{target_service_name_for_log}'.{method} at {socket_path_str}."
        server_instance.logger.error(err); raise RuntimeError(err)
    except json.JSONDecodeError as e_json_dec:
        response_preview = message_bytes.decode('utf-8', errors='replace')[:300] if 'message_bytes' in locals() and message_bytes else "N/A (no bytes for preview)"
        err = f"Failed to decode JSON response from '{target_service_name_for_log}'.{method}. Error: {e_json_dec}. Response preview: {response_preview}"
        server_instance.logger.error(err)
        raise RuntimeError(err) from e_json_dec
    except Exception as e: 
        server_instance.logger.error(
            f"Agent MCP Call to {target_service_name_for_log}.{method} failed unexpectedly: {e}", 
            exc_info=True
        )
        raise RuntimeError(f"Unexpected failure calling {target_service_name_for_log}.{method}: {type(e).__name__} - {str(e)[:100]}") from e
    finally:
        if sock:
            try: sock.shutdown(socket.SHUT_RDWR)
            except OSError: pass 
            sock.close()

# --- Fonction de Templating Simplifiée ---
def _apply_template(template_value: Any, context: Dict[str, Any], inputs: Dict[str, Any]) -> Any:
    # ... (La fonction _apply_template que je vous ai fournie précédemment est correcte et peut être insérée ici)
    # ... (Elle est un peu longue, donc je la omets pour la concision de cette réponse, mais vous l'avez)
    if isinstance(template_value, str):
        final_str = template_value
        def replace_var_with_value(match_obj: re.Match) -> str:
            full_placeholder = match_obj.group(0) 
            expression = match_obj.group(1).strip() 
            path_str = expression
            default_value_provided = None
            has_default_filter = False
            if '| default(' in expression:
                parts = expression.split('| default(')
                path_str = parts[0].strip()
                default_value_str = parts[1].rstrip(')').strip()
                has_default_filter = True
                try: default_value_provided = eval(default_value_str)
                except:
                    agent_server.logger.warning(f"Template: Could not eval default value '{default_value_str}', using as string.")
                    default_value_provided = default_value_str
            scope_name, *key_parts = path_str.split('.')
            current_data_source = None
            if scope_name == 'inputs': current_data_source = inputs
            elif scope_name == 'context': current_data_source = context
            else:
                agent_server.logger.warning(f"Template: Unknown scope '{scope_name}' in '{full_placeholder}'")
                return full_placeholder
            resolved_value = current_data_source
            try:
                for key_part in key_parts:
                    if isinstance(resolved_value, dict): resolved_value = resolved_value[key_part]
                    elif isinstance(resolved_value, list) and key_part.isdigit(): resolved_value = resolved_value[int(key_part)]
                    else: raise KeyError(f"Invalid path part '{key_part}'")
            except (KeyError, IndexError, TypeError):
                if has_default_filter: return str(default_value_provided)
                agent_server.logger.warning(f"Template: Key path '{path_str}' not found in {scope_name}, no default. Placeholder: {full_placeholder}")
                return full_placeholder
            return str(resolved_value)
        final_str = re.sub(r"\{\{\s*(.*?)\s*\}\}", replace_var_with_value, final_str)
# Dans _apply_template, la partie pour le filtre replace :
# ...
        match_replace = re.search(r"\{\{\s*(inputs\.[a-zA-Z0-9_]+)\s*\|\s*replace\s*\(\s*'(.*?)'\s*,\s*(context\.[a-zA-Z0-9_]+)\s*\)\s*\}\}", template_value)
        if match_replace:
            template_string_var_path = match_replace.group(1) # Ex: "inputs.llm_prompt_template"
            string_to_replace_literal = match_replace.group(2) # Ex: "{file_content}"
            replacement_var_path = match_replace.group(3)     # Ex: "context.file_content_result"

            # Obtenir la valeur de la chaîne de template (ex: le prompt brut)
            # On appelle replace_var_with_value qui gère les scopes 'inputs.' et 'context.'
            # et les defaults simples.
            # On doit lui passer un "match object" simulé pour qu'il extraie le chemin correctement.
            def get_templated_value(path_expression_for_value):
                # Simule un appel à replace_var_with_value pour juste obtenir la valeur
                # d'un placeholder simple comme {{ inputs.llm_prompt_template }}
                mock_match = re.match(r"\{\{\s*(.*?)\s*\}\}", f"{{{{ {path_expression_for_value} }}}}")
                if mock_match:
                    return replace_var_with_value(mock_match)
                return path_expression_for_value # Fallback

            template_string_value = get_templated_value(template_string_var_path)
            replacement_value = get_templated_value(replacement_var_path)
            
            agent_server.logger.debug(f"Template replace: Template string base: '{template_string_value}' (from '{template_string_var_path}')")
            agent_server.logger.debug(f"Template replace: Replacement value: '{replacement_value}' (from '{replacement_var_path}')")

            if isinstance(template_string_value, str) and not template_string_value.startswith("{{") and \
               isinstance(replacement_value, str) and not replacement_value.startswith("{{"):
                final_str = template_string_value.replace(string_to_replace_literal, replacement_value)
                agent_server.logger.debug(f"Template replace: Applied: '{string_to_replace_literal}' with '{replacement_value}' -> result: '{final_str[:100]}...'")
            else:
                agent_server.logger.warning(f"Template replace: Could not apply. Base template string was '{template_string_value}', replacement was '{replacement_value}'. Placeholder returned.")
                final_str = template_value # Retourne le template original non résolu
            return final_str
        # Si pas de filtre replace, continuer avec le templating normal des {{ var }}
        final_str = re.sub(r"\{\{\s*(.*?)\s*\}\}", replace_var_with_value, final_str)
        return final_str
    elif isinstance(template_value, list): return [_apply_template(item, context, inputs) for item in template_value]
    elif isinstance(template_value, dict): return {key: _apply_template(value, context, inputs) for key, value in template_value.items()}
    return template_value

# --- Définition des Workflows ---
def _load_workflow_definitions_blocking(server_instance: MCPServer):
    # ... (votre fonction _load_workflow_definitions_blocking existante)
    # ... (elle semble correcte)
    server_instance.workflow_definitions = {} # type: ignore
    if not WORKFLOWS_DIR_CONF.exists() or not WORKFLOWS_DIR_CONF.is_dir():
        server_instance.logger.warning(f"Workflows directory {WORKFLOWS_DIR_CONF} not found. No workflows loaded.")
        return

    for filepath in WORKFLOWS_DIR_CONF.glob("*.yaml"): # Or .yml
        try:
            with filepath.open('r', encoding='utf-8') as f: # Spécifier encoding
                workflow_yaml = yaml.safe_load(f)
            
            if not isinstance(workflow_yaml, dict): 
                server_instance.logger.warning(f"Workflow file {filepath.name} does not contain a valid YAML dictionary. Skipping.")
                continue

            wf_id = str(workflow_yaml.get("id", filepath.stem)) 
            wf_name = str(workflow_yaml.get("name", wf_id))   
            
            server_instance.workflow_definitions[wf_id] = { # type: ignore
                "workflow_id": wf_id,
                "name": wf_name,
                "description": workflow_yaml.get("description"),
                "path": str(filepath),
                "parsed_yaml": workflow_yaml, 
                "input_schema": workflow_yaml.get("input_schema")
            }
            server_instance.logger.info(f"Loaded workflow definition: '{wf_name}' (ID: {wf_id}) from {filepath.name}")
        except yaml.YAMLError as ye:
            server_instance.logger.error(f"Error parsing YAML for workflow {filepath.name}: {ye}")
        except Exception as e:
            server_instance.logger.error(f"Error loading workflow {filepath.name}: {e}", exc_info=True)
    server_instance.logger.info(f"Loaded {len(getattr(server_instance, 'workflow_definitions', {}))} workflow definitions.")


# --- Exécution du Workflow ---
def _execute_workflow_in_thread(
    server_instance: MCPServer,
    execution_id: str,
    workflow_id: str,
    inputs: Optional[Dict[str, Any]]
):
    # Définition de _log_exec DANS _execute_workflow_in_thread pour capturer exec_data
    exec_data_ref = server_instance.executions_state.get(execution_id) if hasattr(server_instance, 'executions_state') else None # type: ignore

    def _log_exec(message: str, level: str = "info", exc_info: bool = False):
        # Utiliser les variables execution_id et workflow_id de la portée de _execute_workflow_in_thread
        current_execution_id_for_log = execution_id
        current_workflow_id_for_log = workflow_id
        
        # Récupérer exec_data pour le chemin du log si possible
        # Cela permet de logguer même si exec_data est mis à jour plus tard.
        # On utilise exec_data_ref qui est une "snapshot" au début du thread.
        log_file_to_use = AGENT_EXEC_LOG_DIR_CONF / f"{current_execution_id_for_log}.log"
        if exec_data_ref and exec_data_ref.get("log_file"):
            log_file_to_use = Path(exec_data_ref["log_file"])

        timestamp = datetime.now(timezone.utc).isoformat()
        log_line = f"{timestamp} [{level.upper()}] {message}\n"
        try:
            with open(log_file_to_use, 'a', encoding='utf-8') as lf: 
                lf.write(log_line)
                if exc_info: 
                    import traceback
                    traceback.print_exc(file=lf)
        except Exception as e_log_write:
            server_instance.logger.error(f"Exec {current_execution_id_for_log}: Failed to write to log file '{log_file_to_use}': {e_log_write}")
        
        log_fn = getattr(server_instance.logger, level, server_instance.logger.info)
        log_fn(f"Exec {current_execution_id_for_log} (WF {current_workflow_id_for_log}): {message}", exc_info=exc_info)

    _log_exec(f"Execution thread started. Inputs: {json.dumps(inputs) if inputs else '{}'}")
    
    # S'assurer que exec_data est bien celui de CETTE exécution
    # C'est redondant si exec_data_ref est utilisé, mais bon pour la clarté que exec_data est la référence principale.
    if execution_id not in server_instance.executions_state: # type: ignore
        _log_exec(f"Critical error: Execution ID {execution_id} not found in state at thread start.", "error")
        return
    exec_data = server_instance.executions_state[execution_id] # type: ignore
        
    exec_data["status"] = "running"
    exec_data["start_time"] = datetime.now(timezone.utc)
    
    workflow_context: Dict[str, Any] = {} 
    docker_container_obj: Optional[Any] = None

    try:
        if workflow_id not in server_instance.workflow_definitions: # type: ignore
            raise ValueError(f"Workflow ID '{workflow_id}' not found.")
        
        wf_config = server_instance.workflow_definitions[workflow_id]["parsed_yaml"] # type: ignore
        wf_type = wf_config.get("type", "simple_sequential")

        if wf_type == "mcp_sequential_agent":
            _log_exec("Executing MCP sequential agent steps...")
            for i, step_config in enumerate(wf_config.get("steps", [])):
                step_name = step_config.get("name", f"Step_{i+1}")
                step_action = step_config.get("action")
                _log_exec(f"Running step '{step_name}': Action='{step_action}'")

                if step_action == "mcp_call":
                    mcp_method_name = step_config.get("method")
                    params_template = step_config.get("params_template")
                    outputs_to_ctx_map = step_config.get("outputs_to_context")

                    if not mcp_method_name or params_template is None:
                        raise ValueError(f"Step '{step_name}': 'method' and 'params_template' are required for mcp_call.")
                    
                    actual_params = _apply_template(params_template, workflow_context, inputs or {})
                    _log_exec(f"Step '{step_name}': Calling {mcp_method_name} with params: {json.dumps(actual_params)}")
                    
                    # L'argument target_service_name a été enlevé de _call_mcp_service_blocking
                    mcp_response_full = _call_mcp_service_blocking(server_instance, mcp_method_name, actual_params)
                    mcp_result = mcp_response_full.get("result")

                    if outputs_to_ctx_map and isinstance(outputs_to_ctx_map, dict) and mcp_result is not None:
                        for ctx_key, result_path_template_str in outputs_to_ctx_map.items():
                            if not isinstance(result_path_template_str, str) or \
                               not result_path_template_str.startswith("{{") or \
                               not result_path_template_str.endswith("}}"):
                                _log_exec(f"Step '{step_name}': Invalid result_path_template '{result_path_template_str}' for context key '{ctx_key}'. Must be like '{{{{ result.some_key }}}}'.", "warning")
                                continue
                            key_path_str = result_path_template_str[2:-2].strip()
                            if not key_path_str.startswith("result."):
                                _log_exec(f"Step '{step_name}': Result path template '{result_path_template_str}' must start with 'result.'", "warning")
                                continue
                            actual_key_path_parts = key_path_str.split('.')[1:]
                            current_val_from_result = mcp_result
                            valid_path = True
                            for k_part in actual_key_path_parts:
                                try:
                                    if isinstance(current_val_from_result, dict): current_val_from_result = current_val_from_result[k_part]
                                    elif isinstance(current_val_from_result, list) and k_part.isdigit(): current_val_from_result = current_val_from_result[int(k_part)]
                                    else: valid_path = False; break
                                except (KeyError, IndexError, TypeError): valid_path = False; break
                            if valid_path:
                                workflow_context[ctx_key] = current_val_from_result
                                _log_exec(f"Step '{step_name}': Stored result of '{key_path_str}' as context key '{ctx_key}'. Value preview: {str(current_val_from_result)[:100]}")
                            else: _log_exec(f"Step '{step_name}': Error extracting path '{key_path_str}' from MCP result for '{ctx_key}'. Result: {str(mcp_result)[:200]}", "warning")
                    _log_exec(f"Step '{step_name}' ({mcp_method_name}) completed.")
                else: _log_exec(f"Unknown action '{step_action}' in step '{step_name}'. Skipping.", "warning")
            exec_data["status"] = "completed"; exec_data["output"] = workflow_context
            _log_exec(f"MCP sequential agent workflow completed. Final context: {json.dumps(workflow_context)}")
        elif wf_type == "docker":
            if not server_instance.docker_client: raise RuntimeError("Docker client not available.") # type: ignore
            docker_image = wf_config.get("docker_image"); command_list = wf_config.get("command")
            if not docker_image: raise ValueError("Docker image not specified.")
            environment_dict = {str(k).upper(): str(v) for k,v in (inputs or {}).items()}
            environment_dict.update(wf_config.get("environment", {}))
            _log_exec(f"Starting Docker: Image='{docker_image}', Cmd='{command_list}'")
            container = server_instance.docker_client.containers.run(image=docker_image, command=command_list, environment=environment_dict, detach=True, remove=False) # type: ignore
            docker_container_obj = container; exec_data["docker_container_id"] = container.id
            _log_exec(f"Docker container {container.id} started.")
            for log_entry in container.logs(stream=True, follow=True, timestamps=True, stdout=True, stderr=True):
                _log_exec(f"DOCKER: {log_entry.decode('utf-8').strip()}")
            container.reload(); container_state = container.attrs['State']; exit_code = container_state.get('ExitCode', -1)
            if exit_code == 0:
                exec_data["status"] = "completed"; exec_data["output"] = {"message": "Docker task completed.", "exit_code": 0, "id": container.id}
            else: raise RuntimeError(f"Docker task {container.id} failed. ExitCode: {exit_code}. Error: {container_state.get('Error', 'N/A')}")
        elif wf_type == "n8n_webhook": 
            webhook_url = wf_config.get("webhook_url", f"{N8N_LITE_URL_CONF.rstrip('/')}/webhook/{workflow_id}")
            _log_exec(f"Calling HTTP webhook: {webhook_url}"); http_timeout = wf_config.get("timeout_seconds", 300)
            response = requests.post(webhook_url, json=inputs, timeout=http_timeout); response.raise_for_status()
            exec_data["status"] = "completed"
            try: exec_data["output"] = response.json()
            except: exec_data["output"] = {"raw_response": response.text}
            _log_exec(f"HTTP webhook call successful. Status: {response.status_code}.")
        elif wf_type == "simple_sequential": 
            _log_exec("Executing simple sequential Python steps..."); current_output = inputs or {}
            for i, step_config in enumerate(wf_config.get("steps", [])):
                step_name = step_config.get("name", f"Step_{i+1}"); step_action = step_config.get("action", "log_message")
                _log_exec(f"Running step '{step_name}': Action='{step_action}'")
                if step_action == "log_message": _log_exec(f"STEP LOG ({step_name}): {step_config.get('message', 'Default log.')}")
                elif step_action == "sleep": time.sleep(float(step_config.get("duration_seconds", 1.0)))
                else: _log_exec(f"Unknown action '{step_action}' in step '{step_name}'.", "warning")
            exec_data["status"] = "completed"; exec_data["output"] = current_output
        else: raise NotImplementedError(f"Workflow type '{wf_type}' not implemented.")
    except Exception as e_wf:
        # Utilisation correcte de _log_exec avec exc_info
        _log_exec(f"Workflow execution critically failed: {e_wf}", level="error", exc_info=True)
        if execution_id in server_instance.executions_state: # type: ignore
            exec_data["status"] = "failed"; exec_data["error_message"] = f"{type(e_wf).__name__}: {str(e_wf)[:500]}"
    finally:
        if execution_id in server_instance.executions_state: # type: ignore
             exec_data["end_time"] = datetime.now(timezone.utc)
        if docker_container_obj: # docker_container_obj peut être None
            try:
                # S'assurer que wf_config est défini avant de l'utiliser ici (si l'erreur est avant sa définition)
                wf_config_for_cleanup = server_instance.workflow_definitions[workflow_id]["parsed_yaml"] if workflow_id in server_instance.workflow_definitions else {} # type: ignore
                auto_remove = wf_config_for_cleanup.get("auto_remove_container", True)
                docker_container_obj.reload() # Recharger l'état du conteneur
                if auto_remove and docker_container_obj.status in ['exited', 'dead', 'created']:
                    _log_exec(f"Removing Docker container {docker_container_obj.id}")
                    docker_container_obj.remove(v=True)
            except Exception as e_docker_clean: _log_exec(f"Error cleaning Docker container {docker_container_obj.id}: {e_docker_clean}", "error")
        _log_exec(f"Execution thread finished.")

# --- Handlers MCP ---
# ... (vos handlers MCP handle_agent_list_workflows, runWorkflow, getStatus, stopWorkflow)
# ... (Assurez-vous qu'ils utilisent getattr(server, 'attribute_name', defaultValue) pour plus de robustesse)
@agent_server.register_method("mcp.agent.listWorkflows")
async def handle_agent_list_workflows(server: MCPServer, request_id: Optional[Union[str, int]], params: List[Any]):
    if not hasattr(server, 'workflow_definitions') or not server.workflow_definitions: # type: ignore
        await server.run_in_executor(_load_workflow_definitions_blocking, server)
    return [{"workflow_id": wf["workflow_id"], "name": wf["name"],
             "description": wf.get("description"), "input_schema": wf.get("input_schema")}
            for wf_id, wf in getattr(server, 'workflow_definitions', {}).items()]

@agent_server.register_method("mcp.agent.runWorkflow")
async def handle_agent_run_workflow(server: MCPServer, request_id: Optional[Union[str, int]], params: List[Any]):
    if not params or not isinstance(params[0], str):
        raise server.create_custom_error(request_id, AGENT_CUSTOM_ERROR_BASE - 1, "Invalid params: workflow_id (string) is required.")
    workflow_id = params[0]
    inputs = params[1] if len(params) > 1 and isinstance(params[1], dict) else {}

    if not hasattr(server, 'workflow_definitions') or workflow_id not in server.workflow_definitions: # type: ignore
        # Charger les workflows si non présents (peut arriver si le hook de démarrage a un souci)
        await server.run_in_executor(_load_workflow_definitions_blocking, server)
        if not hasattr(server, 'workflow_definitions') or workflow_id not in server.workflow_definitions: # type: ignore
            raise server.create_custom_error(request_id, AGENT_CUSTOM_ERROR_BASE - 2, f"Workflow ID '{workflow_id}' not found.")

    execution_id = f"exec_{uuid.uuid4().hex[:12]}"
    if not hasattr(server, 'executions_state'): server.executions_state = {} # type: ignore

    server.executions_state[execution_id] = { # type: ignore
        "execution_id": execution_id, "workflow_id": workflow_id, "status": "pending",
        "inputs": inputs, "output": None, "error_message": None,
        "log_file": str(AGENT_EXEC_LOG_DIR_CONF / f"{execution_id}.log"), "thread_obj": None
    }
    thread = threading.Thread(target=_execute_workflow_in_thread, args=(server, execution_id, workflow_id, inputs), daemon=True)
    server.executions_state[execution_id]["thread_obj"] = thread # type: ignore
    thread.start()
    return {"execution_id": execution_id, "workflow_id": workflow_id, "status": "started"}

@agent_server.register_method("mcp.agent.getWorkflowStatus")
async def handle_agent_get_workflow_status(server: MCPServer, request_id: Optional[Union[str, int]], params: List[Any]):
    if not params or not isinstance(params[0], str):
        raise server.create_custom_error(request_id, AGENT_CUSTOM_ERROR_BASE -1, "Invalid params: execution_id (string) is required.")
    execution_id = params[0]
    if not hasattr(server, 'executions_state') or execution_id not in server.executions_state: # type: ignore
        raise server.create_custom_error(request_id, AGENT_CUSTOM_ERROR_BASE -3, f"Execution ID '{execution_id}' not found.")
    
    exec_data = server.executions_state[execution_id] # type: ignore
    log_preview_list = []
    if exec_data.get("log_file") and Path(exec_data["log_file"]).exists():
        try:
            with open(exec_data["log_file"], 'r', encoding='utf-8', errors='ignore') as lf_read:
                log_preview_list = [line.strip() for line in lf_read.readlines()[-20:]]
        except Exception as e: server.logger.warning(f"Could not read log for {execution_id}: {e}")
    
    return {
        "execution_id": execution_id, "workflow_id": exec_data.get("workflow_id"),
        "status": exec_data.get("status"),
        "start_time": exec_data.get("start_time").isoformat() if exec_data.get("start_time") else None,
        "end_time": exec_data.get("end_time").isoformat() if exec_data.get("end_time") else None,
        "output": exec_data.get("output"), "error_message": exec_data.get("error_message"),
        "log_preview": log_preview_list
    }

@agent_server.register_method("mcp.agent.stopWorkflow")
async def handle_agent_stop_workflow(server: MCPServer, request_id: Optional[Union[str, int]], params: List[Any]):
    if not params or not isinstance(params[0], str):
        raise server.create_custom_error(request_id, AGENT_CUSTOM_ERROR_BASE -1, "Invalid params: execution_id (string) is required.")
    execution_id = params[0]
    if not hasattr(server, 'executions_state') or execution_id not in server.executions_state: # type: ignore
        raise server.create_custom_error(request_id, AGENT_CUSTOM_ERROR_BASE -3, f"Execution ID '{execution_id}' not found.")
    
    exec_data = server.executions_state[execution_id] # type: ignore
    if exec_data.get("status") not in ["pending", "running"]:
        return {"execution_id": execution_id, "status": "already_completed_or_not_running", "message": f"Workflow {execution_id} not stoppable (status: {exec_data.get('status')})."}

    docker_container_id = exec_data.get("docker_container_id")
    if docker_container_id and hasattr(server, 'docker_client') and server.docker_client: # type: ignore
        def stop_docker_sync():
            try:
                container = server.docker_client.containers.get(docker_container_id) # type: ignore
                server.logger.info(f"Exec {execution_id}: Stopping Docker container {docker_container_id}")
                container.stop(timeout=10); exec_data["status"] = "cancelling"
                return {"execution_id": execution_id, "status": "stop_requested", "message": "Stop signal sent to Docker container."}
            except docker.errors.NotFound: # type: ignore
                return {"execution_id": execution_id, "status": "not_running", "message": "Docker container not found."}
            except Exception as e: raise server.create_custom_error(request_id, AGENT_CUSTOM_ERROR_BASE -4, f"Failed to stop Docker: {e}")
        return await server.run_in_executor(stop_docker_sync)
    else: 
        exec_data["status"] = "cancelling" 
        # Récupérer workflow_id de exec_data pour le log, plus sûr
        wf_id_for_log = exec_data.get("workflow_id", "UNKNOWN_WF_ID_IN_STOP")
        # Accéder à la fonction _log_exec définie dans _execute_workflow_in_thread est impossible directement ici.
        # On logue avec le logger du serveur.
        server.logger.warning(f"Exec {execution_id} (WF {wf_id_for_log}): Stop requested for non-Docker workflow. Thread may continue until completion.")
        return {"execution_id": execution_id, "status": "stop_requested_no_force", "message": "Stop requested for workflow thread. Thread may not stop immediately."}

# --- Hooks de Cycle de Vie ---
async def on_agent_server_startup(server: MCPServer):
    server.logger.info(f"Agent Server '{server.server_name}' custom startup...")
    server.workflow_definitions = {} 
    server.executions_state = {}    
    server.docker_client = None     
    try:
        def init_docker_client_sync(): return docker.from_env(timeout=5) 
        
        server.docker_client = await server.run_in_executor(init_docker_client_sync) 
        server.logger.info("Docker client initialized successfully.")
    except docker.errors.DockerException as e_docker_init:
        server.logger.warning(f"Docker client init failed: {e_docker_init}. Docker-based agents will be unavailable.")
    except Exception as e: 
        server.logger.error(f"Unexpected error during Docker client init: {e}. Docker agents unavailable.", exc_info=True)
    await server.run_in_executor(_load_workflow_definitions_blocking, server)

async def on_agent_server_shutdown(server: MCPServer):
    server.logger.info(f"Agent Server '{server.server_name}' custom shutdown...")
    if hasattr(server, 'executions_state'):
        for exec_id, exec_data in list(getattr(server, 'executions_state', {}).items()): 
            if exec_data.get("status") == "running":
                server.logger.info(f"Exec {exec_id}: Attempting cleanup on server shutdown...")
                # Logique de stop Docker (simplifiée, car _log_exec n'est pas accessible ici)
                container_id_shutdown = exec_data.get("docker_container_id")
                if container_id_shutdown and hasattr(server, 'docker_client') and server.docker_client:
                    try:
                        # Cette partie doit être exécutée dans l'executor car elle est bloquante
                        def final_docker_stop_sync():
                            cont = server.docker_client.containers.get(container_id_shutdown) # type: ignore
                            server.logger.info(f"Exec {exec_id}: Final stop for Docker container {container_id_shutdown}.")
                            cont.stop(timeout=3)
                            wf_config = getattr(server, 'workflow_definitions', {}).get(exec_data.get("workflow_id", ""), {}).get("parsed_yaml", {})
                            auto_remove = wf_config.get("auto_remove_container", True)
                            cont.reload()
                            if auto_remove and cont.status in ['exited', 'dead']:
                                server.logger.info(f"Exec {exec_id}: Final remove for container {container_id_shutdown}.")
                                cont.remove(v=True)
                        # On ne peut pas await ici car on_shutdown n'est pas toujours dans une boucle asyncio gérée par l'executor de MCPServer
                        # Le mieux est de laisser les threads existants se terminer ou de compter sur le stop_grace_period de Docker Compose
                        server.logger.warning(f"Exec {exec_id}: Docker container {container_id_shutdown} might need manual cleanup if not stopped by Docker Compose.")
                    except Exception as e_final_stop:
                        server.logger.warning(f"Exec {exec_id}: Error during final Docker cleanup: {e_final_stop}")


agent_server.set_startup_hook(on_agent_server_startup)
agent_server.set_shutdown_hook(on_agent_server_shutdown)

if __name__ == "__main__":
    import sys 
    # Configuration du logging pour l'exécution directe du script
    # Le logger de l'instance agent_server.logger est configuré par MCPServer
    # Ceci configure le logger root si ce script est le point d'entrée.
    logging.basicConfig(
        level=os.getenv(f"LLMBDO_{SERVER_NAME.upper()}_LOG_LEVEL", "INFO").upper(),
        format=f"%(asctime)s - AGENT_MAIN - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)] # Forcer stdout pour voir dans 'docker logs' si pas via supervisord
    )
    # Si on veut que les loggers de MCPServer utilisent aussi ce handler:
    # logging.getLogger("llmbasedos.mcp_server_framework").addHandler(logging.StreamHandler(sys.stdout))
    # logging.getLogger("llmbasedos.servers.agent").addHandler(logging.StreamHandler(sys.stdout))


    main_logger_script = logging.getLogger("AGENT_SCRIPT_MAIN") # Logger spécifique pour ce bloc __main__
    main_logger_script.info(f"Starting Agent Server '{SERVER_NAME}' directly via __main__...")
    
    try:
        asyncio.run(agent_server.start())
    except KeyboardInterrupt: 
        main_logger_script.info(f"\nAgent Server '{SERVER_NAME}' (main) stopped by KeyboardInterrupt.")
    except Exception as e_main_script: 
        main_logger_script.critical(f"Agent Server '{SERVER_NAME}' (main) crashed: {e_main_script}", exc_info=True)
    finally:
        main_logger_script.info(f"Agent Server '{SERVER_NAME}' (main) fully shut down.")