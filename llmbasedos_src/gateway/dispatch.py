# llmbasedos/gateway/dispatch.py
import asyncio
import json
import logging
from typing import Any, Dict, Optional, Union, List, AsyncGenerator
from concurrent.futures import ThreadPoolExecutor # For blocking UNIX socket calls

# Use centralized JSON-RPC helpers
from llmbasedos.mcp_server_framework import (
    create_mcp_response, create_mcp_error,
    JSONRPC_PARSE_ERROR, JSONRPC_INVALID_REQUEST, JSONRPC_METHOD_NOT_FOUND,
    JSONRPC_INVALID_PARAMS, JSONRPC_INTERNAL_ERROR
)
from . import registry
from . import upstream
from .auth import LicenceDetails, get_licence_info_for_mcp_call, record_llm_token_usage # Added record_llm_token_usage
from .config import GATEWAY_EXECUTOR_MAX_WORKERS # For thread pool size
from .upstream import LLMError # For catching specific LLM errors from upstream module

logger = logging.getLogger("llmbasedos.gateway.dispatch")

# Thread pool for blocking UNIX socket operations
# This should be initialized once, e.g. in main.py and passed around, or as a global here.
# For simplicity, global here.
_dispatch_executor = ThreadPoolExecutor(max_workers=GATEWAY_EXECUTOR_MAX_WORKERS, thread_name_prefix="gateway_dispatch_worker")

# --- UNIX Socket Client (Blocking version for executor) ---
def _send_request_to_backend_server_blocking(
    socket_path: str, request_payload: Dict[str, Any]
) -> Dict[str, Any]:
    """Sends JSON-RPC to backend via UNIX socket (BLOCKING). For use with run_in_executor."""
    # This function is synchronous and will run in a separate thread.
    # It uses standard Python sockets.
    import socket # Standard library socket

    request_id = request_payload.get("id")
    logger.debug(f"SYNC: Connecting to UNIX socket: {socket_path} for req ID {request_id}")
    
    sock = None # Initialize sock to None
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(5.0) # Connection timeout
        sock.connect(socket_path)
        
        request_bytes = json.dumps(request_payload).encode('utf-8') + b'\0'
        logger.debug(f"SYNC: Sending to {socket_path} (ID {request_id}): {request_payload['method']}")
        sock.sendall(request_bytes)

        response_buffer = bytearray()
        sock.settimeout(15.0) # Response timeout (can be longer for some methods)
        while True:
            chunk = sock.recv(4096)
            if not chunk: break # Server closed connection
            response_buffer.extend(chunk)
            if b'\0' in chunk: # Found delimiter
                response_buffer = response_buffer.split(b'\0', 1)[0]
                break
        
        if not response_buffer:
            logger.error(f"SYNC: No response from {socket_path} for ID {request_id}")
            return create_mcp_error(request_id, JSONRPC_INTERNAL_ERROR, "No response from backend.")

        response_str = response_buffer.decode('utf-8')
        logger.debug(f"SYNC: Received from {socket_path} (ID {request_id}): {response_str[:200]}...")
        return json.loads(response_str)

    except socket.timeout:
        logger.error(f"SYNC: Timeout with {socket_path} for ID {request_id}")
        return create_mcp_error(request_id, JSONRPC_INTERNAL_ERROR, f"Timeout with backend server {socket_path}.")
    except ConnectionRefusedError:
        logger.error(f"SYNC: Connection refused by {socket_path} for ID {request_id}")
        return create_mcp_error(request_id, JSONRPC_INTERNAL_ERROR, f"Backend {socket_path} unavailable (conn refused).")
    except FileNotFoundError:
        logger.error(f"SYNC: UNIX socket not found: {socket_path} for ID {request_id}")
        return create_mcp_error(request_id, JSONRPC_INTERNAL_ERROR, f"Backend {socket_path} unavailable (socket not found).")
    except json.JSONDecodeError:
        logger.error(f"SYNC: Failed to decode JSON from {socket_path} for ID {request_id}. Resp: {response_buffer.decode(errors='ignore')}")
        return create_mcp_error(request_id, JSONRPC_INTERNAL_ERROR, "Invalid JSON response from backend.")
    except Exception as e:
        logger.error(f"SYNC: Error with {socket_path} for ID {request_id}: {e}", exc_info=True)
        return create_mcp_error(request_id, JSONRPC_INTERNAL_ERROR, f"Comm error with backend: {type(e).__name__}.")
    finally:
        if sock: sock.close()


async def handle_mcp_request(
    request: Dict[str, Any],
    licence_details: LicenceDetails, # Auth already done by main WebSocket/UNIX handler
    client_websocket_for_context: Any # Pass WebSocket or mock for context (IP, etc.)
) -> Union[Dict[str, Any], AsyncGenerator[Dict[str, Any], None]]:
    """
    Handles an authorized JSON-RPC request.
    Routes to gateway's own methods or dispatches to backend MCP servers.
    """
    request_id = request.get("id") # Should always be present for valid JSON-RPC reqs
    method_name = request.get("method")
    params = request.get("params", [])

    # Basic validation of method_name and params structure (already done by auth layer potentially)
    if not isinstance(method_name, str):
        return create_mcp_error(request_id, JSONRPC_INVALID_REQUEST, "Method name must be a string.")
    if not isinstance(params, (list, dict)): # JSON-RPC params can be array or object
        return create_mcp_error(request_id, JSONRPC_INVALID_PARAMS, "Params must be an array or object.")

    # --- 1. Gateway's own MCP methods ---
    if method_name == "mcp.hello":
        all_methods = registry.get_all_registered_method_names()
        permitted_methods = [] # Filter based on licence_details.allowed_capabilities
        if "*" in licence_details.allowed_capabilities:
            permitted_methods = all_methods
        else:
            for m_name in all_methods:
                is_allowed = any(
                    (m_name.startswith(p[:-1]) if p.endswith(".*") else m_name == p)
                    for p in licence_details.allowed_capabilities
                )
                if is_allowed: permitted_methods.append(m_name)
        
        # Ensure core gateway methods are always present if allowed by base tier definition
        # This check is implicitly handled by `allowed_capabilities` now.
        return create_mcp_response(request_id, result=sorted(list(set(permitted_methods))))

    elif method_name == "mcp.listCapabilities":
        all_services_meta = registry.get_detailed_capabilities_list()
        # TODO: Finer-grained filtering based on licence (capabilities within each service)
        return create_mcp_response(request_id, result=all_services_meta)

    elif method_name == "mcp.licence.check":
        # Pass client_websocket_for_context for IP-based quota display if needed
        lic_info = get_licence_info_for_mcp_call(client_websocket_for_context)
        return create_mcp_response(request_id, result=lic_info)

    elif method_name == "mcp.llm.chat":
        # Params validation (basic, schema would be better)
        if not isinstance(params, list) or len(params) == 0 or not isinstance(params[0], list):
            return create_mcp_error(request_id, JSONRPC_INVALID_PARAMS, "Invalid params for mcp.llm.chat. Expected: [[messages_list], ?llm_options_dict]")
        
        messages_list: List[Dict[str,str]] = params[0]
        llm_options = params[1] if len(params) > 1 and isinstance(params[1], dict) else {}
        
        stream_response_flag = llm_options.pop("stream", False) # Client requests streaming
        requested_model_alias = llm_options.pop("model", None)
        
        # Actual token counting for output is hard with streaming.
        # For now, record_llm_token_usage might be called with estimated input tokens
        # or after full response if non-streaming, or summed up after streaming.
        # Let's assume for now token quota checks were done by auth layer based on estimates.
        # The actual recording happens after generation.

        async def llm_stream_generator():
            total_tokens_used_in_stream = 0 # For recording usage after stream
            try:
                async for llm_event in upstream.call_llm_chat_completion(
                    messages=messages_list, licence=licence_details, # licence passed for context if upstream needs
                    requested_model_alias=requested_model_alias, stream=True, **llm_options
                ):
                    if llm_event["event"] == "error":
                        # Forward error from upstream as JSON-RPC error
                        err_data = llm_event["data"]
                        yield create_mcp_error(request_id, JSONRPC_INTERNAL_ERROR, # Or more specific LLM error code
                                               f"LLM Error ({err_data.get('provider')}/{err_data.get('model_name')}): {err_data.get('message')}",
                                               data=err_data.get('details'))
                        return # Stop stream on error

                    elif llm_event["event"] == "chunk":
                        # TODO: Estimate tokens from this chunk for more accurate live counting if needed
                        # For OpenAI, chunk format is like:
                        # {"id":"chatcmpl-xxx","object":"chat.completion.chunk","created":1678690,"model":"gpt-3.5-turbo","choices":[{"index":0,"delta":{"content":" World"},"finish_reason":null}]}
                        # We need to count output tokens to record usage.
                        # This is complex with streaming. A simple way: count based on `delta.content`.
                        # For now, this demo won't implement detailed token counting from stream chunks.
                        # Assume a fixed token cost or count after full stream for recording.
                        yield create_mcp_response(request_id, result={"type": "llm_chunk", "content": llm_event["data"]})
                    
                    elif llm_event["event"] == "done":
                        # TODO: If non-streaming, result_payload["usage"]["total_tokens"] gives count for OpenAI
                        # For streaming, we'd need to sum them up.
                        # For now, let's assume a placeholder for tokens_used, or get from final "done" if available.
                        # This is where `record_llm_token_usage` would be called.
                        # For simplicity, not implementing exact token counting from stream here.
                        # Assume a placeholder value or record only for non-streaming where total is known.
                        # record_llm_token_usage(licence_details, client_websocket_for_context, total_tokens_used_in_stream)
                        yield create_mcp_response(request_id, result={"type": "llm_stream_end"})
                        return # End this generator
            
            except Exception as e_stream_handler: # Catch errors from processing the generator
                logger.error(f"Error processing LLM stream for ID {request_id}: {e_stream_handler}", exc_info=True)
                yield create_mcp_error(request_id, JSONRPC_INTERNAL_ERROR, f"LLM stream processing error: {type(e_stream_handler).__name__}")
            # finally:
                # Call record_llm_token_usage here if tokens were accumulated
                # logger.debug(f"LLM stream for {request_id} finished. Total output tokens (estimated/actual): {total_tokens_used_in_stream}")
                # if total_tokens_used_in_stream > 0:
                #    record_llm_token_usage(licence_details, client_websocket_for_context, total_tokens_used_in_stream)


        if stream_response_flag:
            return llm_stream_generator() # Return the async generator
        else:
            # Collect non-streaming result (should be one 'chunk' event with full data, then 'done')
            final_result_payload = None
            async for event_response in llm_stream_generator():
                # The generator yields full JSON-RPC responses. We need the 'result' part from the 'llm_chunk'.
                if event_response.get("result", {}).get("type") == "llm_chunk":
                    # This will be the full LLM provider's response object
                    final_result_payload = event_response["result"]["content"] 
                elif event_response.get("result", {}).get("type") == "llm_stream_end":
                    break # Done collecting
                elif "error" in event_response: # Error occurred
                    return event_response # Forward the error JSON-RPC response
            
            if final_result_payload:
                # Record token usage for non-streaming if data is available
                # OpenAI example: final_result_payload.get("usage", {}).get("total_tokens", 0)
                tokens_actually_used = 0 # Placeholder for actual token counting
                if isinstance(final_result_payload, dict) and "usage" in final_result_payload:
                    tokens_actually_used = final_result_payload["usage"].get("total_tokens", 0)
                if tokens_actually_used > 0:
                    record_llm_token_usage(licence_details, client_websocket_for_context, tokens_actually_used)
                
                return create_mcp_response(request_id, result=final_result_payload)
            else:
                return create_mcp_error(request_id, JSONRPC_INTERNAL_ERROR, "LLM call (non-stream) failed to produce a result.")

    # --- 2. Dispatch to a registered backend MCP server ---
    routing_info = registry.get_capability_routing_info(method_name)
    if routing_info:
        socket_path = routing_info["socket_path"]
        logger.info(f"Dispatching '{method_name}' (ID {request_id}) to backend at {socket_path}")
        
        request_to_forward = request.copy() # Contains id, method, params
        request_to_forward["jsonrpc"] = "2.0" # Ensure protocol version is set

        loop = asyncio.get_running_loop()
        try:
            # Run blocking socket call in thread pool executor
            response_from_server = await loop.run_in_executor(
                _dispatch_executor,
                _send_request_to_backend_server_blocking, 
                socket_path, 
                request_to_forward
            )
            # Ensure the ID from the backend matches our original request_id if it was changed/missing
            if "id" not in response_from_server or response_from_server["id"] != request_id:
                logger.warning(f"Backend server {socket_path} returned mismatched/missing ID. Original: {request_id}, Backend: {response_from_server.get('id')}. Fixing.")
                response_from_server["id"] = request_id
            return response_from_server
        except Exception as e_exec: # Error from run_in_executor itself or if task cancelled
            logger.error(f"Error dispatching to backend {socket_path} via executor: {e_exec}", exc_info=True)
            return create_mcp_error(request_id, JSONRPC_INTERNAL_ERROR, f"Failed to dispatch to backend: {type(e_exec).__name__}")
    
    # --- 3. Method not found ---
    logger.warning(f"Method not found: {method_name} (ID {request_id})")
    return create_mcp_error(request_id, JSONRPC_METHOD_NOT_FOUND, f"Method '{method_name}' not found.")

def shutdown_dispatch_executor():
    """Shuts down the thread pool executor used for dispatching."""
    logger.info("Shutting down dispatch thread pool executor...")
    _dispatch_executor.shutdown(wait=True)
    logger.info("Dispatch executor shut down.")
