# llmbasedos_src/gateway/dispatch.py
import asyncio
import json
import logging
from typing import Any, Dict, Optional, Union, List, AsyncGenerator
from concurrent.futures import ThreadPoolExecutor
import httpx
import socket

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

def _send_request_to_backend_server_blocking(socket_path: str, request_payload: Dict[str, Any]) -> Dict[str, Any]:
    sock = None
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(10.0)
        sock.connect(socket_path)
        request_bytes = json.dumps(request_payload).encode('utf-8') + b'\0'
        sock.sendall(request_bytes)
        response_buffer = bytearray()
        sock.settimeout(120.0)
        while True:
            chunk = sock.recv(8192)
            if not chunk: break
            if b'\0' in chunk:
                response_buffer.extend(chunk.split(b'\0', 1)[0])
                break
            response_buffer.extend(chunk)
        if not response_buffer:
            return create_mcp_error(request_payload.get("id"), JSONRPC_INTERNAL_ERROR, "No response from backend.")
        return json.loads(response_buffer.decode('utf-8'))
    except Exception as e:
        logger.error(f"Error with local socket {socket_path}: {e}", exc_info=True)
        return create_mcp_error(request_payload.get("id"), JSONRPC_INTERNAL_ERROR, f"Comm error with local backend: {type(e).__name__}.")
    finally:
        if sock: sock.close()

async def _send_request_to_external_tcp_server(address: str, request_payload: Dict[str, Any]) -> Dict[str, Any]:
    request_id = request_payload.get("id")
    host, port_str = address.split(":", 1)
    port = int(port_str)
    writer = None
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=10.0)
        writer.write(json.dumps(request_payload).encode('utf-8') + b'\n')
        await writer.drain()
        response_bytes = await asyncio.wait_for(reader.read(65536), timeout=120.0)
        if not response_bytes:
            raise ConnectionError("External TCP server closed connection without sending data.")
        return json.loads(response_bytes)
    except Exception as e:
        logger.error(f"Error calling external TCP server {address}: {e}", exc_info=True)
        return create_mcp_error(request_id, JSONRPC_INTERNAL_ERROR, f"Failed to communicate with external TCP server: {type(e).__name__}")
    finally:
        if writer:
            writer.close()
            await writer.wait_closed()

async def handle_mcp_request(
    request: Dict[str, Any],
    licence_details: LicenceDetails, 
    client_websocket_for_context: Any 
) -> Union[Dict[str, Any], AsyncGenerator[Dict[str, Any], None]]:
    
    request_id = request.get("id")
    method_name = request.get("method", "").strip()
    params = request.get("params", []) 

    if not method_name:
        return create_mcp_error(request_id, JSONRPC_INVALID_REQUEST, "Method name must be non-empty.")

    # --- DÉBALLAGE DE LA REQUÊTE DE L'UI ---
    if method_name in ["call-tool", "execute_llmbasedos_command"]:
        logger.info(f"Gateway: Received a wrapped call '{method_name}'. Unwrapping...")
        try:
            # Gérer les deux formats possibles pour les paramètres
            wrapped_params = params[0] if isinstance(params, list) and len(params) > 0 and isinstance(params[0], dict) else params
            
            real_method = wrapped_params.get("method")
            real_params = wrapped_params.get("params")

            if not isinstance(real_method, str):
                raise ValueError("Wrapped call is missing a 'method' string.")

            logger.info(f"Gateway: Unwrapped to -> Method: '{real_method}', Params: {str(real_params)[:100]}...")
            
            # Reconstruire la requête pour que le reste du système l'utilise
            request = {
                "jsonrpc": "2.0",
                "method": real_method,
                "params": real_params,
                "id": request_id
            }
            # Mettre à jour les variables locales pour la suite de la fonction
            method_name = real_method

        except (ValueError, TypeError, IndexError) as e:
            logger.error(f"Failed to unwrap call: {e}. Original params: {params}", exc_info=True)
            return create_mcp_error(request_id, JSONRPC_INVALID_PARAMS, f"Invalid wrapped call format: {e}")

    # Le reste de la logique utilise maintenant la requête (potentiellement modifiée)
    
    # Gestion des méthodes internes au Gateway
    if method_name == "mcp.hello":
        return create_mcp_response(request_id, result=registry.get_all_registered_method_names())
    if method_name == "mcp.listCapabilities":
        return create_mcp_response(request_id, result=registry.get_detailed_capabilities_list())
    if method_name == "mcp.licence.check":
        return create_mcp_response(request_id, result=get_licence_info_for_mcp_call(client_websocket_for_context))

    # Gestion spécifique de mcp.llm.chat
    if method_name == "mcp.llm.chat":
        try:
            llm_params = request.get("params", [])
            messages = llm_params[0]
            options = llm_params[1] if len(llm_params) > 1 else {}
            stream = options.get("stream", False)
            return await upstream.call_llm_chat_completion(messages, licence_details, options.get("model"), stream, **options)
        except Exception as e:
            logger.error(f"Error in llm.chat dispatch: {e}", exc_info=True)
            return create_mcp_error(request_id, JSONRPC_INTERNAL_ERROR, "Failed to process llm.chat request.")

    # Routage vers les services backend
    routing_info = registry.get_capability_routing_info(method_name)
    if routing_info:
        if routing_info.get("socket_path") == "external":
            address = routing_info["config"]["address"]
            logger.info(f"Dispatching '{method_name}' to external TCP server at {address}")
            return await _send_request_to_external_tcp_server(address, request)
        else: # Service local
            socket_path = routing_info["socket_path"]
            logger.info(f"Dispatching '{method_name}' to local service at {socket_path}")
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                _dispatch_executor, 
                _send_request_to_backend_server_blocking, 
                socket_path, request
            )
    
    # Si aucune route n'est trouvée
    logger.warning(f"Method '{method_name}' (ID {request_id}) NOT FOUND in any registry.")
    return create_mcp_error(request_id, JSONRPC_METHOD_NOT_FOUND, f"Method '{method_name}' not found.")

def shutdown_dispatch_executor():
    logger.info("Shutting down dispatch thread pool executor...")
    _dispatch_executor.shutdown(wait=True)
    logger.info("Dispatch executor shut down.")