# llmbasedos_src/gateway/dispatch.py
import asyncio
import json
import logging
from typing import Any, Dict, Optional, Union, List, AsyncGenerator
from concurrent.futures import ThreadPoolExecutor

from llmbasedos.mcp_server_framework import (
    create_mcp_response, create_mcp_error,
    JSONRPC_INVALID_REQUEST, JSONRPC_METHOD_NOT_FOUND,
    JSONRPC_INVALID_PARAMS, JSONRPC_INTERNAL_ERROR
)
from . import registry
from . import upstream
from .auth import LicenceDetails, get_licence_info_for_mcp_call, record_llm_token_usage
from .config import GATEWAY_EXECUTOR_MAX_WORKERS

logger = logging.getLogger("llmbasedos.gateway.dispatch")

_dispatch_executor = ThreadPoolExecutor(
    max_workers=GATEWAY_EXECUTOR_MAX_WORKERS, 
    thread_name_prefix="gateway_dispatch_worker"
)

# ... (La fonction _send_request_to_backend_server_blocking reste la même) ...
def _send_request_to_backend_server_blocking(
    socket_path: str, request_payload: Dict[str, Any]
) -> Dict[str, Any]:
    import socket
    request_id = request_payload.get("id")
    method_name_for_log = request_payload.get("method", "unknown_method")
    logger.debug(f"DISPATCH_SYNC: Connecting to UNIX socket: {socket_path} for ReqID {request_id} (Method: {method_name_for_log})")
    sock = None 
    final_payload_bytes: bytes = b""
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(10.0) 
        sock.connect(socket_path)
        request_bytes = json.dumps(request_payload).encode('utf-8') + b'\0'
        logger.debug(f"DISPATCH_SYNC: Sending to {socket_path} (ReqID {request_id}, Method: {method_name_for_log}): {str(request_payload)[:200]}...")
        sock.sendall(request_bytes)
        response_buffer = bytearray()
        sock.settimeout(120.0) 
        while True:
            chunk = sock.recv(8192) 
            if not chunk: 
                logger.warning(f"DISPATCH_SYNC: Connection closed by {socket_path} (ReqID {request_id}) before receiving full response or delimiter.")
                break 
            response_buffer.extend(chunk)
            if b'\0' in response_buffer: 
                break
        if b'\0' in response_buffer:
            final_payload_bytes, _ = response_buffer.split(b'\0', 1)
        else:
            final_payload_bytes = response_buffer
        if not final_payload_bytes:
            logger.error(f"DISPATCH_SYNC: No response data from {socket_path} for ReqID {request_id} (Method: {method_name_for_log})")
            return create_mcp_error(request_id, JSONRPC_INTERNAL_ERROR, f"No response from backend server: {socket_path.split('/')[-1].split('.')[0]}")
        response_str = final_payload_bytes.decode('utf-8')
        logger.debug(f"DISPATCH_SYNC: Received from {socket_path} (ReqID {request_id}, Method: {method_name_for_log}): {response_str[:300]}...")
        return json.loads(response_str)
    except socket.timeout:
        logger.error(f"DISPATCH_SYNC: Timeout with {socket_path} for ReqID {request_id} (Method: {method_name_for_log})")
        return create_mcp_error(request_id, JSONRPC_INTERNAL_ERROR, f"Timeout communicating with backend server: {socket_path.split('/')[-1].split('.')[0]}")
    except ConnectionRefusedError:
        logger.error(f"DISPATCH_SYNC: Connection refused by {socket_path} for ReqID {request_id} (Method: {method_name_for_log})")
        return create_mcp_error(request_id, JSONRPC_INTERNAL_ERROR, f"Backend server {socket_path.split('/')[-1].split('.')[0]} unavailable (conn refused).")
    except FileNotFoundError:
        logger.error(f"DISPATCH_SYNC: UNIX socket not found: {socket_path} for ReqID {request_id} (Method: {method_name_for_log})")
        return create_mcp_error(request_id, JSONRPC_INTERNAL_ERROR, f"Backend server {socket_path.split('/')[-1].split('.')[0]} unavailable (socket not found).")
    except json.JSONDecodeError:
        decoded_buffer_for_log = final_payload_bytes.decode('utf-8', errors='replace')
        logger.error(f"DISPATCH_SYNC: Failed to decode JSON from {socket_path} for ReqID {request_id}. Resp buffer: {decoded_buffer_for_log[:300]}...")
        return create_mcp_error(request_id, JSONRPC_INTERNAL_ERROR, "Invalid JSON response from backend server.")
    except Exception as e:
        logger.error(f"DISPATCH_SYNC: Generic error with {socket_path} for ReqID {request_id}: {e}", exc_info=True)
        return create_mcp_error(request_id, JSONRPC_INTERNAL_ERROR, f"Comm error with backend: {type(e).__name__}.")
    finally:
        if sock: 
            try: sock.shutdown(socket.SHUT_RDWR)
            except OSError: pass 
            sock.close()

async def handle_mcp_request(
    request: Dict[str, Any],
    licence_details: LicenceDetails, 
    client_websocket_for_context: Any 
) -> Union[Dict[str, Any], AsyncGenerator[Dict[str, Any], None]]:
    request_id = request.get("id")
    method_name = request.get("method", "").strip()
    params = request.get("params", []) 

    logger.debug(f"DISPATCH_HANDLE: Request ID {request_id}, Method: '{method_name}', Params type: {type(params).__name__}, Params preview: {str(params)[:100]}")

    if not method_name:
        return create_mcp_error(request_id, JSONRPC_INVALID_REQUEST, "Method name must be a non-empty string.")
    if not isinstance(params, (list, dict)): 
        return create_mcp_error(request_id, JSONRPC_INVALID_PARAMS, "Params field must be an array or an object.")

    # --- 1. Gateway's own MCP methods ---
    if method_name == "mcp.hello":
        all_methods = registry.get_all_registered_method_names()
        # ... (logique de permission)
        return create_mcp_response(request_id, result=sorted(list(set(all_methods)))) # Simplifié pour la démo

    elif method_name == "mcp.listCapabilities":
        return create_mcp_response(request_id, result=registry.get_detailed_capabilities_list())

    elif method_name == "mcp.licence.check":
        return create_mcp_response(request_id, result=get_licence_info_for_mcp_call(client_websocket_for_context))

    # --- CORRECTION POUR mcp.llm.chat ---
    elif method_name == "mcp.llm.chat":
        if not (isinstance(params, list) and len(params) >= 1 and isinstance(params[0], list)):
            return create_mcp_error(request_id, JSONRPC_INVALID_PARAMS, "Invalid params for mcp.llm.chat. Expected: [[messages_list], ?llm_options_dict]")
        
        messages_list: List[Dict[str,str]] = params[0]
        llm_options_from_client = params[1] if len(params) > 1 and isinstance(params[1], dict) else {}
        
        client_requests_stream = llm_options_from_client.pop("stream", False) 
        requested_model_alias = llm_options_from_client.get("model")

        logger.info(f"DISPATCH_HANDLE: mcp.llm.chat called (ID {request_id}), Client wants stream: {client_requests_stream}, Model: {requested_model_alias}")
        
        # On appelle directement upstream.py et on retourne ce qu'il nous donne.
        # Soit un dict (pour non-stream/erreur), soit un AsyncGenerator (pour stream).
        # C'est à main.py de gérer ces deux types de retour.
        upstream_response = await upstream.call_llm_chat_completion(
            messages=messages_list, 
            licence=licence_details,
            requested_model_alias=requested_model_alias, 
            stream=client_requests_stream, # On passe directement la volonté du client
            **llm_options_from_client
        )

        # Si le client voulait un stream, upstream retourne un générateur.
        # On le transforme en un nouveau générateur qui produit des réponses MCP complètes.
        if isinstance(upstream_response, AsyncGenerator):
            async def mcp_stream_wrapper():
                async for llm_event in upstream_response:
                    if llm_event.get("event") == "error":
                        err_data = llm_event.get("data", {})
                        yield create_mcp_error(request_id, JSONRPC_INTERNAL_ERROR, f"LLM Error: {err_data.get('message')}", data=err_data.get('details'))
                        return
                    elif llm_event.get("event") == "chunk":
                        yield create_mcp_response(request_id, result={"type": "llm_chunk", "content": llm_event.get("data")})
                    elif llm_event.get("event") == "done":
                        yield create_mcp_response(request_id, result={"type": "llm_stream_end"})
                        return
            return mcp_stream_wrapper() # Retourne le nouveau générateur
        
        # Si le client ne voulait pas de stream, upstream retourne un dict.
        elif isinstance(upstream_response, dict):
            if upstream_response.get("error"): # Erreur retournée par upstream
                err_data = upstream_response
                return create_mcp_error(request_id, JSONRPC_INTERNAL_ERROR, f"LLM Error: {err_data.get('message')}", data=err_data.get('details'))
            else: # Réponse complète
                # Enregistrer les tokens
                tokens_used = upstream_response.get("usage", {}).get("total_tokens", 0)
                if tokens_used > 0:
                    # ... (logique pour construire client_id_for_quota_tracking) ...
                    client_id_for_quota_tracking = "unknown"
                    if hasattr(client_websocket_for_context, 'client'):
                        client_id_for_quota_tracking = f"ip:{client_websocket_for_context.client.host}"
                    elif hasattr(client_websocket_for_context, 'peername_str'):
                        client_id_for_quota_tracking = f"unix:{client_websocket_for_context.peername_str}"
                    record_llm_token_usage(licence_details, client_websocket_for_context, tokens_used, client_id_for_quota_tracking)
                # Retourner la réponse MCP
                return create_mcp_response(request_id, result=upstream_response)
        
        # Fallback si upstream retourne un type inattendu
        logger.error(f"DISPATCH_HANDLE: Upstream returned unexpected type {type(upstream_response)} for mcp.llm.chat.")
        return create_mcp_error(request_id, JSONRPC_INTERNAL_ERROR, "Internal server error: unexpected LLM service response type.")


    # --- 2. Dispatch to a registered backend MCP server ---
    logger.debug(f"DISPATCH_HANDLE: Method '{method_name}' not gateway-specific. Looking in registry.")
    routing_info = registry.get_capability_routing_info(str(method_name))
    logger.debug(f"DISPATCH_HANDLE: Routing info for '{method_name}' (ID {request_id}): {routing_info}")

    if routing_info:
        # ... (votre logique de dispatch existante, qui est correcte) ...
        socket_path = routing_info["socket_path"]
        logger.info(f"Dispatching '{method_name}' (ID {request_id}) to backend '{routing_info.get('service_name')}' at {socket_path}")
        request_to_forward = request.copy() 
        request_to_forward["jsonrpc"] = "2.0" 
        loop = asyncio.get_running_loop()
        try:
            response_from_server = await loop.run_in_executor(
                _dispatch_executor, _send_request_to_backend_server_blocking, 
                socket_path, request_to_forward
            )
            if response_from_server and ("id" not in response_from_server or response_from_server.get("id") != request_id):
                logger.warning(f"DISPATCH_HANDLE: Backend {socket_path} ID mismatch. Orig: {request_id}, Backend: {response_from_server.get('id') if response_from_server else 'N/A'}. Correcting.")
                if isinstance(response_from_server, dict): response_from_server["id"] = request_id
            return response_from_server
        except Exception as e_exec: 
            logger.error(f"DISPATCH_HANDLE: Executor error for '{method_name}' (ID {request_id}) to {socket_path}: {e_exec}", exc_info=True)
            return create_mcp_error(request_id, JSONRPC_INTERNAL_ERROR, f"Failed to dispatch to backend: {type(e_exec).__name__}")
    
    logger.warning(f"DISPATCH_HANDLE: Method '{method_name}' (ID {request_id}) NOT FOUND by registry and not gateway method. Returning METHOD_NOT_FOUND.")
    return create_mcp_error(request_id, JSONRPC_METHOD_NOT_FOUND, f"Method '{method_name}' not found or not accessible.")

def shutdown_dispatch_executor():
    logger.info("Shutting down dispatch thread pool executor...")
    _dispatch_executor.shutdown(wait=True)
    logger.info("Dispatch executor shut down.")