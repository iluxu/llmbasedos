# llmbasedos/gateway/main.py
import asyncio
import json
import logging
from pathlib import Path
import os
import signal # For graceful shutdown handling

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

# Use centralized JSON-RPC error helper for consistency if needed here
from llmbasedos.mcp_server_framework import create_mcp_error, JSONRPC_PARSE_ERROR, JSONRPC_INVALID_REQUEST

from .config import (
    GATEWAY_UNIX_SOCKET_PATH, GATEWAY_HOST, GATEWAY_WEB_PORT
)
from . import registry
from . import dispatch # For handling MCP requests & shutting down its executor
from .auth import authenticate_and_authorize_request, LicenceDetails # For auth and type hint
from . import setup_gateway_logging # Import the setup function

# Logger will be configured by setup_gateway_logging()
logger: Optional[logging.Logger] = None # Placeholder, will be set after setup

app = FastAPI(
    title="llmbasedos MCP Gateway",
    description="Central router for Model Context Protocol requests.",
    version="0.1.1" # Incremented version
)

# --- Mock WebSocket for UNIX Socket Context ---
class MockUnixClientContext:
    """Provides a consistent client context for auth, similar to WebSocket's client."""
    def __init__(self, peername: Any): # peername can be complex for UNIX sockets
        # For UNIX sockets, 'host' and 'port' are less relevant for client ID.
        # We might use a unique ID per connection or a fixed ID for all local.
        # For now, use a fixed identifier for simplicity.
        self.host = "unix_socket_client"
        self.port = 0 # peername from asyncio unix server is often path or (host,port) for abstract
        self.peername_str = str(peername) # Store actual peername for logging if needed
    
    def __repr__(self):
        return f"<MockUnixClientContext peer='{self.peername_str}'>"


# --- WebSocket Endpoint ---
@app.websocket("/ws")
async def websocket_mcp_endpoint(websocket: WebSocket):
    await websocket.accept()
    client_addr = f"{websocket.client.host}:{websocket.client.port}"
    logger.info(f"WebSocket client connected: {client_addr}")
    
    try:
        while True: # Loop for messages on this connection
            raw_data = await websocket.receive_text()
            logger.debug(f"WS RCV from {client_addr}: {raw_data[:250]}...")
            
            try: request_data = json.loads(raw_data)
            except json.JSONDecodeError:
                err_resp = create_mcp_error(None, JSONRPC_PARSE_ERROR, "Invalid JSON payload.")
                await websocket.send_text(json.dumps(err_resp)); continue

            request_id = request_data.get("id") # Get ID early for error responses
            method_name = request_data.get("method")
            if not isinstance(method_name, str):
                err_resp = create_mcp_error(request_id, JSONRPC_INVALID_REQUEST, "Method name missing or not a string.")
                await websocket.send_text(json.dumps(err_resp)); continue

            # Auth layer: llm_model_requested and llm_tokens_to_request are estimates for pre-auth.
            # For LLM chat, actual model might be default, tokens unknown until response.
            # For now, these are None/0 for non-LLM calls.
            # TODO: Extract these from mcp.llm.chat params if method_name matches.
            # This is a simplification; full parsing of params might be needed for accurate pre-auth.
            licence_ctx, auth_error_obj = authenticate_and_authorize_request(
                websocket, method_name, 
                llm_model_requested=None, # TODO: Extract from params if method is mcp.llm.chat
                llm_tokens_to_request=0   # TODO: Estimate from params if method is mcp.llm.chat
            )
            
            if auth_error_obj:
                logger.warning(f"Auth failed for {client_addr}, method '{method_name}': {auth_error_obj['message']}")
                err_resp = create_mcp_error(request_id, auth_error_obj["code"], auth_error_obj["message"], auth_error_obj.get("data"))
                await websocket.send_text(json.dumps(err_resp))
                if auth_error_obj["code"] == config.JSONRPC_AUTH_ERROR: # type: ignore # config defined in other module
                    await websocket.close(code=1008); break # Policy violation
                continue
            
            if not licence_ctx: # Should not happen if auth_error_obj is None
                logger.error(f"Auth internal error for {client_addr} (no licence, no error). Denying.")
                # This implies an issue in `authenticate_and_authorize_request` logic.
                err_resp = create_mcp_error(request_id, JSONRPC_INTERNAL_ERROR, "Internal authentication error.") # type: ignore
                await websocket.send_text(json.dumps(err_resp)); continue

            # Dispatch the authorized request
            response_or_generator = await dispatch.handle_mcp_request(request_data, licence_ctx, websocket)

            if isinstance(response_or_generator, AsyncGenerator):
                logger.info(f"Streaming response to WS {client_addr} for '{method_name}' (ID {request_id})")
                try:
                    async for stream_chunk_resp in response_or_generator:
                        await websocket.send_text(json.dumps(stream_chunk_resp))
                    logger.debug(f"Finished streaming to WS {client_addr} (ID {request_id})")
                except WebSocketDisconnect:
                    logger.info(f"WS {client_addr} disconnected during stream (ID {request_id})."); break
                except Exception as stream_exc:
                    logger.error(f"Error streaming to WS {client_addr} (ID {request_id}): {stream_exc}", exc_info=True)
                    # Attempt to send error if still connected (response_or_generator might have yielded error already)
                    if websocket.application_state == WebSocketState.CONNECTED: # type: ignore # WebSocketState not directly in fastapi.WebSocket
                         try: await websocket.send_text(json.dumps(create_mcp_error(request_id, JSONRPC_INTERNAL_ERROR, "Streaming error."))) # type: ignore
                         except: pass
                    break 
            else: # Single JSON-RPC response dict
                logger.debug(f"WS SEND to {client_addr} (ID {request_id}): {str(response_or_generator)[:250]}...")
                await websocket.send_text(json.dumps(response_or_generator))

    except WebSocketDisconnect:
        logger.info(f"WebSocket client {client_addr} disconnected.")
    except Exception as e: # Catch-all for the connection handler
        logger.error(f"Error in WebSocket handler for {client_addr}: {e}", exc_info=True)
        # Try to close gracefully if possible
        if websocket.application_state == WebSocketState.CONNECTED: # type: ignore
            await websocket.close(code=1011) # Internal error
    finally:
        logger.info(f"WebSocket connection cleanup for {client_addr}")


# --- UNIX Socket Server (for local MCP communication) ---
_UNIX_SOCKET_SERVER: Optional[asyncio.AbstractServer] = None
_ACTIVE_UNIX_CLIENT_TASKS: set[asyncio.Task] = set()

async def _handle_unix_socket_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    peername = writer.get_extra_info('peername', 'unknown_unix_peer') # Get peer info
    client_desc = f"unix_client_{peername}"
    logger.info(f"UNIX socket client connected: {client_desc}")
    
    mock_client_ctx = MockUnixClientContext(peername) # For auth context

    message_buffer = bytearray()
    try:
        while True: # Loop for messages on this connection
            chunk = await reader.read(4096)
            if not chunk: logger.info(f"UNIX client {client_desc} disconnected (EOF)."); break
            message_buffer.extend(chunk)

            while b'\0' in message_buffer:
                message_bytes, rest_of_buffer = message_buffer.split(b'\0', 1)
                message_buffer = rest_of_buffer
                message_str = message_bytes.decode('utf-8')
                logger.debug(f"UNIX RCV from {client_desc}: {message_str[:250]}...")

                try: request_data = json.loads(message_str)
                except json.JSONDecodeError:
                    err_resp = create_mcp_error(None, JSONRPC_PARSE_ERROR, "Invalid JSON.")
                    writer.write(json.dumps(err_resp).encode('utf-8') + b'\0'); await writer.drain()
                    continue
                
                request_id = request_data.get("id"); method_name = request_data.get("method")
                if not isinstance(method_name, str):
                    err_resp = create_mcp_error(request_id, JSONRPC_INVALID_REQUEST, "Method name missing/invalid.")
                    writer.write(json.dumps(err_resp).encode('utf-8') + b'\0'); await writer.drain()
                    continue

                licence_ctx, auth_error_obj = authenticate_and_authorize_request(
                    mock_client_ctx, method_name # type: ignore # Pass mock context
                ) # TODO: LLM pre-auth params extraction for UNIX sockets too
                
                if auth_error_obj:
                    logger.warning(f"Auth failed for UNIX {client_desc}, method '{method_name}': {auth_error_obj['message']}")
                    err_resp = create_mcp_error(request_id, auth_error_obj["code"], auth_error_obj["message"], auth_error_obj.get("data"))
                    writer.write(json.dumps(err_resp).encode('utf-8') + b'\0'); await writer.drain()
                    if auth_error_obj["code"] == config.JSONRPC_AUTH_ERROR: break # type: ignore
                    continue
                if not licence_ctx:
                    err_resp = create_mcp_error(request_id, JSONRPC_INTERNAL_ERROR, "Internal auth error."); # type: ignore
                    writer.write(json.dumps(err_resp).encode('utf-8') + b'\0'); await writer.drain(); continue

                response_or_generator = await dispatch.handle_mcp_request(request_data, licence_ctx, mock_client_ctx) # type: ignore

                if isinstance(response_or_generator, AsyncGenerator):
                    logger.info(f"Streaming response to UNIX {client_desc} for '{method_name}' (ID {request_id})")
                    try:
                        async for stream_chunk_resp in response_or_generator:
                            writer.write(json.dumps(stream_chunk_resp).encode('utf-8') + b'\0')
                            await writer.drain()
                        logger.debug(f"Finished streaming to UNIX {client_desc} (ID {request_id})")
                    except (ConnectionResetError, BrokenPipeError): # Client disconnected during stream
                        logger.info(f"UNIX {client_desc} disconnected during stream (ID {request_id})."); break
                    except Exception as stream_exc:
                        logger.error(f"Error streaming to UNIX {client_desc} (ID {request_id}): {stream_exc}", exc_info=True)
                        # Cannot reliably send error if stream/pipe broke.
                        break
                else: # Single response
                    logger.debug(f"UNIX SEND to {client_desc} (ID {request_id}): {str(response_or_generator)[:250]}...")
                    writer.write(json.dumps(response_or_generator).encode('utf-8') + b'\0')
                    await writer.drain()
    
    except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
        logger.info(f"UNIX client {client_desc} connection lost/reset.")
    except UnicodeDecodeError:
        logger.error(f"UNIX client {client_desc} sent invalid UTF-8. Closing connection.")
        # Cannot reliably send error if encoding is broken.
    except asyncio.CancelledError:
        logger.info(f"UNIX client task for {client_desc} cancelled.") # Expected on shutdown
    except Exception as e:
        logger.error(f"Error in UNIX client handler for {client_desc}: {e}", exc_info=True)
    finally:
        logger.info(f"Closing UNIX socket connection for {client_desc}")
        if not writer.is_closing(): writer.close(); await writer.wait_closed()


async def _run_unix_socket_client_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """Wrapper to manage the task in _ACTIVE_UNIX_CLIENT_TASKS."""
    task = asyncio.current_task()
    _ACTIVE_UNIX_CLIENT_TASKS.add(task) # type: ignore
    try:
        await _handle_unix_socket_client(reader, writer)
    finally:
        _ACTIVE_UNIX_CLIENT_TASKS.remove(task) # type: ignore

async def _start_unix_socket_server():
    global _UNIX_SOCKET_SERVER
    socket_path_obj = GATEWAY_UNIX_SOCKET_PATH
    try:
        socket_path_obj.parent.mkdir(parents=True, exist_ok=True)
        if socket_path_obj.exists(): socket_path_obj.unlink()
    except OSError as e: logger.error(f"Error preparing UNIX socket path {socket_path_obj}: {e}. Server may fail."); return

    try:
        _UNIX_SOCKET_SERVER = await asyncio.start_unix_server(_run_unix_socket_client_handler, path=str(socket_path_obj))
        # Group permissions for socket (postinstall.sh should create 'llmgroup' and add 'llmuser')
        # Socket owned by llmuser:llmgroup, permissions rw-rw---- (0660)
        # Or, if llmuser runs everything, user llmuser, perms rw------- (0600)
        # For dev/simplicity, if llmuser runs all, 0700 or 0600 is fine.
        # If different users in a group need access, 0660 or 0770.
        os.chown(str(socket_path_obj), os.geteuid(), os.environ.get('LLMBDO_MCP_SOCKET_GID', -1)) # GID from env or llmuser's primary
        os.chmod(str(socket_path_obj), 0o660) # User/Group RW
        logger.info(f"MCP Gateway listening on UNIX socket: {socket_path_obj} (perms 0660)")
    except Exception as e:
        logger.error(f"Failed to start UNIX socket server on {socket_path_obj}: {e}", exc_info=True)
        _UNIX_SOCKET_SERVER = None

async def _stop_unix_socket_server():
    global _UNIX_SOCKET_SERVER
    if _UNIX_SOCKET_SERVER:
        logger.info("Stopping UNIX socket server...")
        _UNIX_SOCKET_SERVER.close()
        await _UNIX_SOCKET_SERVER.wait_closed()
        _UNIX_SOCKET_SERVER = None
        logger.info("UNIX server socket closed.")

        # Cancel and await active client handler tasks
        if _ACTIVE_UNIX_CLIENT_TASKS:
            logger.info(f"Cancelling {_ACTIVE_UNIX_CLIENT_TASKS} active UNIX client tasks...")
            for task in list(_ACTIVE_UNIX_CLIENT_TASKS): task.cancel()
            await asyncio.gather(*_ACTIVE_UNIX_CLIENT_TASKS, return_exceptions=True)
            logger.info("Active UNIX client tasks finished.")
        
        if GATEWAY_UNIX_SOCKET_PATH.exists():
            try: GATEWAY_UNIX_SOCKET_PATH.unlink()
            except OSError as e: logger.error(f"Error removing UNIX socket file: {e}")
    logger.info("UNIX socket server fully stopped.")


_graceful_shutdown_event = asyncio.Event()

async def _signal_handler(sig, frame):
    logger.warning(f"Received signal {sig.name}. Initiating graceful shutdown...")
    _graceful_shutdown_event.set()
    # Stop Uvicorn server (if running via uvicorn.run directly in future)
    # For now, rely on main task cancellation or FastAPI shutdown event.

async def lifespan(app: FastAPI):
    """FastAPI lifespan context manager."""
    global logger # Make sure logger is accessible
    # Setup logging (moved from __init__.py to be explicit on startup)
    setup_gateway_logging()
    logger = logging.getLogger("llmbasedos.gateway.main") # Now logger is set

    logger.info("Gateway lifespan: Startup sequence starting...")
    # Start background tasks
    registry_task = asyncio.create_task(registry.start_capability_watcher_task(), name="CapabilityWatcher")
    await _start_unix_socket_server()
    logger.info("Gateway lifespan: Startup sequence complete.")
    
    # Wait for shutdown signal
    await _graceful_shutdown_event.wait() # Blocks here until event is set
    
    # Shutdown sequence
    logger.info("Gateway lifespan: Shutdown sequence starting...")
    if registry_task and not registry_task.done():
        registry_task.cancel()
        try: await registry_task
        except asyncio.CancelledError: logger.info("Capability watcher task cancelled successfully.")
    
    await _stop_unix_socket_server()
    dispatch.shutdown_dispatch_executor() # Shutdown thread pool from dispatch
    logger.info("Gateway lifespan: Shutdown sequence complete.")

# Assign lifespan to app (FastAPI >= 0.90.0 way)
app.router.lifespan_context = lifespan


def run_gateway_service():
    """ Main function to run Uvicorn server for the gateway. """
    # Set up signal handlers for graceful shutdown BEFORE starting Uvicorn
    # This is important if Uvicorn doesn't handle them in a way that triggers our lifespan.
    # However, Uvicorn/FastAPI's standard way is to handle SIGINT/SIGTERM and trigger lifespan's shutdown.
    # Let's rely on FastAPI's lifespan for now. Adding manual signal handlers can conflict.
    # loop = asyncio.get_event_loop()
    # signals = (signal.SIGHUP, signal.SIGTERM, signal.SIGINT)
    # for s in signals:
    #    loop.add_signal_handler(s, lambda s=s: asyncio.create_task(_signal_handler(s, None)))

    # Uvicorn will use the lifespan context manager defined above.
    uvicorn.run(
        "llmbasedos.gateway.main:app", # app instance is now correctly found
        host=GATEWAY_HOST, port=GATEWAY_WEB_PORT,
        log_config=None, # We use our own logging via setup_gateway_logging() in lifespan
        # workers= (set via env or config if > 1, but stateful parts like _PENDING_RESPONSES need care)
    )

if __name__ == "__main__":
    # This allows `python -m llmbasedos.gateway.main`
    run_gateway_service()
