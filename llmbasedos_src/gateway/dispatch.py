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
    
    request_id = request.get("id") # Garder l'ID de la requête MCP originale
    method_name = request.get("method", "").strip()
    params = request.get("params", []) 
    
    # ... (logique de déballage de la requête UI, mcp.hello, mcp.listCapabilities, mcp.licence.check) ...

    # Gestion spécifique de mcp.llm.chat
    if method_name == "mcp.llm.chat":
        try:
            if not isinstance(params, list):
                 return create_mcp_error(request_id, JSONRPC_INVALID_PARAMS, "Params for mcp.llm.chat must be an array.")

            messages = params[0]
            options = params[1] if len(params) > 1 and isinstance(params[1], dict) else {}
            
            stream_flag = options.pop("stream", False)
            model_alias = options.pop("model", None)

            # Appel à upstream.call_llm_chat_completion
            llm_api_response_or_generator = await upstream.call_llm_chat_completion(
                request_id=request_id, # Passer l'ID de la requête MCP pour la gestion des erreurs dans upstream
                messages=messages, 
                licence=licence_details, 
                requested_model_alias=model_alias, 
                stream=stream_flag, 
                **options
            )

            # --- MODIFICATION IMPORTANTE ICI ---
            if isinstance(llm_api_response_or_generator, AsyncGenerator):
                # Si c'est un stream, upstream.call_llm_chat_completion doit déjà
                # générer des réponses JSON-RPC valides (avec "result" ou "error").
                # Donc, on le retourne directement.
                logger.debug(f"Dispatching LLM stream for request ID {request_id}")
                return llm_api_response_or_generator
            elif isinstance(llm_api_response_or_generator, dict):
                # Si c'est une réponse unique (non-stream)
                if "error" in llm_api_response_or_generator: # Si upstream a déjà formaté une erreur JSON-RPC
                    logger.warning(f"LLM call for ID {request_id} resulted in upstream error: {llm_api_response_or_generator['error']}")
                    return llm_api_response_or_generator # C'est déjà une erreur MCP valide
                else:
                    # Encapsuler la réponse de l'API LLM dans un "result" JSON-RPC
                    logger.debug(f"LLM call for ID {request_id} successful, wrapping result.")
                    return create_mcp_response(request_id, result=llm_api_response_or_generator)
            else:
                # Cas inattendu si upstream ne retourne ni dict ni AsyncGenerator
                logger.error(f"Unexpected response type from upstream.call_llm_chat_completion for ID {request_id}: {type(llm_api_response_or_generator)}")
                return create_mcp_error(request_id, JSONRPC_INTERNAL_ERROR, "Internal error: Unexpected response type from LLM upstream handler.")

        except Exception as e:
            logger.error(f"Error in mcp.llm.chat dispatch for ID {request_id}: {e}", exc_info=True)
            return create_mcp_error(request_id, JSONRPC_INTERNAL_ERROR, f"Failed to process mcp.llm.chat request: {type(e).__name__}")


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