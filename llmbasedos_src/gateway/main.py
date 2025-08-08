# llmbasedos_src/gateway/main.py
import asyncio
import json
import logging
import logging.config
from pathlib import Path
import os
import signal 
import uuid # <-- AJOUTÉ
from typing import Any, Dict, List, Optional, AsyncGenerator, Set

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request # <-- MODIFIÉ
from fastapi.responses import JSONResponse # <-- AJOUTÉ
from starlette.websockets import WebSocketState
from contextlib import asynccontextmanager

from llmbasedos_src.mcp_server_framework import create_mcp_error, JSONRPC_PARSE_ERROR, JSONRPC_INVALID_REQUEST, JSONRPC_INTERNAL_ERROR
from .config import (
    GATEWAY_UNIX_SOCKET_PATH, GATEWAY_HOST, GATEWAY_WEB_PORT,
    LOGGING_CONFIG,
    JSONRPC_AUTH_ERROR,
)
from . import registry
from . import dispatch
from .auth import authenticate_and_authorize_request, LicenceDetails 

def setup_gateway_logging():
    try:
        logging.config.dictConfig(LOGGING_CONFIG)
        logging.getLogger("llmbasedos.gateway.main").info("Gateway logging configured via dictConfig.")
    except Exception as e_log:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s (fallback)")
        logging.getLogger("llmbasedos.gateway.main").error(f"Failed to apply dictConfig for logging: {e_log}. Using basicConfig.", exc_info=True)

logger: logging.Logger = logging.getLogger("llmbasedos.gateway.main")

_unix_socket_server_instance: Optional[asyncio.AbstractServer] = None
_active_unix_client_tasks: Set[asyncio.Task] = set()
_capability_watcher_task_instance: Optional[asyncio.Task] = None
_shutdown_event_flag = asyncio.Event()
_tcp_socket_server_instance: Optional[asyncio.AbstractServer] = None

class MockUnixClientContext:
    class _ClientInfo:
        def __init__(self, peername_str: str):
            self.host = "unix_socket_client" # Type générique
            self.port = peername_str # Utiliser peername comme "port" pour unicité
    
    def __init__(self, peername: Any):
        self.peername_str = str(peername)
        self.client = self._ClientInfo(self.peername_str)

    def __repr__(self): 
        return f"<MockUnixClientContext peer='{self.peername_str}'>"

async def _start_unix_socket_server_logic():
    global _unix_socket_server_instance
    socket_path_obj = Path(GATEWAY_UNIX_SOCKET_PATH)
    try:
        socket_path_obj.parent.mkdir(parents=True, exist_ok=True)
        if socket_path_obj.exists(): socket_path_obj.unlink()
    except OSError as e:
        logger.error(f"Error preparing UNIX socket path {socket_path_obj}: {e}. UNIX server may fail.")
        return
    try:
        _unix_socket_server_instance = await asyncio.start_unix_server(
            _run_unix_socket_client_handler_managed, path=str(socket_path_obj)
        )
        addr = _unix_socket_server_instance.sockets[0].getsockname() if _unix_socket_server_instance.sockets else str(socket_path_obj)
        logger.info(f"MCP Gateway listening on UNIX socket: {addr}")
        try:
            os.chmod(str(socket_path_obj), 0o660) # rw pour user et group
            logger.info(f"Set permissions for {socket_path_obj} to 0660.")
        except OSError as e_perm:
            logger.warning(f"Could not set optimal permissions for UNIX socket {socket_path_obj}: {e_perm}.")
    except Exception as e_start_unix:
        logger.error(f"Failed to start UNIX socket server on {socket_path_obj}: {e_start_unix}", exc_info=True)
        _unix_socket_server_instance = None

async def _stop_tcp_socket_server_logic():
    global _tcp_socket_server_instance
    if _tcp_socket_server_instance:
        logger.info("Stopping TCP socket server...")
        _tcp_socket_server_instance.close()
        try:
            await _tcp_socket_server_instance.wait_closed()
        except Exception as e:
            logger.error(f"Error closing TCP server: {e}")
        _tcp_socket_server_instance = None
        logger.info("TCP server socket now closed.")

async def _stop_unix_socket_server_logic():
    global _unix_socket_server_instance
    if _unix_socket_server_instance:
        logger.info("Stopping UNIX socket server...")
        _unix_socket_server_instance.close()
        try: await _unix_socket_server_instance.wait_closed()
        except Exception as e_wait: logger.error(f"Error during wait_closed for UNIX server: {e_wait}")
        _unix_socket_server_instance = None
        logger.info("UNIX server socket now closed.")
    if _active_unix_client_tasks:
        logger.info(f"Cancelling {len(_active_unix_client_tasks)} active UNIX client tasks...")
        for task in list(_active_unix_client_tasks): task.cancel()
        await asyncio.gather(*_active_unix_client_tasks, return_exceptions=True)
        _active_unix_client_tasks.clear()
        logger.info("Active UNIX client tasks finished processing.")
    if GATEWAY_UNIX_SOCKET_PATH.exists():
        try: GATEWAY_UNIX_SOCKET_PATH.unlink()
        except OSError as e_unlink: logger.error(f"Error removing UNIX socket file on stop: {e_unlink}")
    logger.info("UNIX socket server fully stopped.")

async def _handle_single_unix_client_task(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    peername = writer.get_extra_info('peername', 'unknown_unix_peer')
    client_desc = f"unix_client_{str(peername).replace('/', '_').replace(':', '_')}"
    logger.info(f"UNIX socket client connected: {client_desc}")
    mock_client_ctx = MockUnixClientContext(peername)
    try:
        while not _shutdown_event_flag.is_set():
            message_buffer = bytearray()
            while b'\0' not in message_buffer:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=1.0)
                if not chunk: 
                    logger.info(f"UNIX client {client_desc} disconnected (EOF).")
                    return
                message_buffer.extend(chunk)
            
            message_bytes, _ = message_buffer.split(b'\0', 1)
            message_str = message_bytes.decode('utf-8')
            logger.debug(f"UNIX RCV from {client_desc}: {message_str[:250]}...")
            
            try: request_data = json.loads(message_str)
            except json.JSONDecodeError:
                err_resp = create_mcp_error(None, JSONRPC_PARSE_ERROR, "Invalid JSON payload.")
                writer.write(json.dumps(err_resp).encode('utf-8') + b'\0'); await writer.drain(); continue
            
            request_id = request_data.get("id"); method_name = request_data.get("method", "").strip()
            if not method_name:
                err_resp = create_mcp_error(request_id, JSONRPC_INVALID_REQUEST, "Method name missing or empty.")
                writer.write(json.dumps(err_resp).encode('utf-8') + b'\0'); await writer.drain(); continue
            
            llm_model_requested = None
            if method_name == "mcp.llm.chat" and isinstance(request_data.get("params"), list) and len(request_data["params"]) > 1 and isinstance(request_data["params"][1], dict):
                llm_model_requested = request_data["params"][1].get("model")

            licence_ctx, auth_error_obj = authenticate_and_authorize_request(mock_client_ctx, method_name, llm_model_requested)
            if auth_error_obj:
                err_resp = create_mcp_error(request_id, auth_error_obj.get("code", -32000), auth_error_obj.get("message"), auth_error_obj.get("data"))
                writer.write(json.dumps(err_resp).encode('utf-8') + b'\0'); await writer.drain()
                if auth_error_obj.get("code") == JSONRPC_AUTH_ERROR: break 
                continue
            if not licence_ctx:
                err_resp = create_mcp_error(request_id, JSONRPC_INTERNAL_ERROR, "Internal authentication error.")
                writer.write(json.dumps(err_resp).encode('utf-8') + b'\0'); await writer.drain(); continue
            
            response_or_generator = await dispatch.handle_mcp_request(request_data, licence_ctx, mock_client_ctx)
            if isinstance(response_or_generator, AsyncGenerator):
                async for stream_chunk_resp in response_or_generator:
                    if _shutdown_event_flag.is_set(): break
                    writer.write(json.dumps(stream_chunk_resp).encode('utf-8') + b'\0')
                    await writer.drain()
                if _shutdown_event_flag.is_set(): break
            else:
                writer.write(json.dumps(response_or_generator).encode('utf-8') + b'\0')
                await writer.drain()

    except asyncio.TimeoutError:
        logger.debug(f"UNIX client {client_desc} timed out waiting for data, closing connection.")
    except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
        logger.info(f"UNIX client {client_desc} connection issue.");
    except UnicodeDecodeError: 
        logger.error(f"UNIX client {client_desc} sent invalid UTF-8.");
    except asyncio.CancelledError: 
        logger.info(f"UNIX client task for {client_desc} cancelled.")
    except Exception as e_client: 
        logger.error(f"Error in UNIX client handler for {client_desc}: {e_client}", exc_info=True)
    finally:
        logger.info(f"Closing UNIX connection for {client_desc}")
        if not writer.is_closing(): 
            try: writer.close(); await writer.wait_closed()
            except Exception as e_close: logger.debug(f"Error closing writer for {client_desc}: {e_close}")

async def _run_unix_socket_client_handler_managed(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    task = asyncio.current_task()
    _active_unix_client_tasks.add(task)
    try: await _handle_single_unix_client_task(reader, writer)
    finally: _active_unix_client_tasks.discard(task)

@asynccontextmanager
async def lifespan_manager(app_fastapi: FastAPI):
    global logger, _capability_watcher_task_instance
    setup_gateway_logging()
    logger = logging.getLogger("llmbasedos.gateway.main")
    logger.info("Gateway Lifespan: Startup sequence initiated...")
    loop = asyncio.get_running_loop()
    active_signals = []
    def _shutdown_signal_handler(sig: signal.Signals):
        logger.info(f"Signal {sig.name} received, setting shutdown event...")
        if not _shutdown_event_flag.is_set(): loop.call_soon_threadsafe(_shutdown_event_flag.set)
    for sig_val in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig_val, lambda s=sig_val: _shutdown_signal_handler(s))
            active_signals.append(sig_val)
        except (ValueError, RuntimeError) as e_signal:
             logger.warning(f"Could not set signal handler for {sig_val}: {e_signal}. Relying on Uvicorn for shutdown.")
    
    _capability_watcher_task_instance = asyncio.create_task(registry.start_capability_watcher_task(), name="CapabilityWatcher")
    logger.info("Capability watcher task created.")
    await _start_unix_socket_server_logic()
    logger.info("Gateway Lifespan: Startup complete. Application is ready.")
    try:
        yield
    finally:
        logger.info("Gateway Lifespan: Shutdown sequence initiated...")
        if not _shutdown_event_flag.is_set(): _shutdown_event_flag.set()
        if _capability_watcher_task_instance and not _capability_watcher_task_instance.done():
            _capability_watcher_task_instance.cancel()
            try: await _capability_watcher_task_instance
            except asyncio.CancelledError: logger.info("Capability watcher task successfully cancelled.")
        await _stop_unix_socket_server_logic()
        await _stop_tcp_socket_server_logic()
        dispatch.shutdown_dispatch_executor()
        for sig_val in active_signals:
            try: loop.remove_signal_handler(sig_val)
            except Exception: pass
        logger.info("Gateway Lifespan: Shutdown complete.")

app = FastAPI(
    title="llmbasedos MCP Gateway",
    description="Central router for Model Context Protocol requests.",
    version="0.1.7",
    lifespan=lifespan_manager
)

# ====================================================================
# == NOUVELLE ROUTE HTTP POUR COMPATIBILITÉ OPENAI                ==
# ====================================================================
@app.post("/v1/chat/completions")
async def handle_openai_compatible_request(request: Request):
    logger.info("Received OpenAI-compatible HTTP request.")
    try:
        openai_payload = await request.json()
        
        messages = openai_payload.get("messages", [])
        options = {
            "model": openai_payload.get("model"),
            "temperature": openai_payload.get("temperature"),
            "max_tokens": openai_payload.get("max_tokens"),
            "stream": openai_payload.get("stream", False)
        }
        
        mcp_params = [{
            "messages": messages,
            "options": {k: v for k, v in options.items() if v is not None}
        }]
        
        class MockHttpContext:
            class Client:
                host = request.client.host if request.client else "unknown_http_host"
            client = Client()

        licence_ctx, auth_error_obj = authenticate_and_authorize_request(MockHttpContext(), "mcp.llm.chat")
        if auth_error_obj:
            return JSONResponse(status_code=401, content={"error": auth_error_obj})
        if not licence_ctx:
             return JSONResponse(status_code=500, content={"error": {"message": "Internal licence context error."}})

        mcp_request_payload = {
            "jsonrpc": "2.0",
            "method": "mcp.llm.chat",
            "params": mcp_params,
            "id": f"http-req-{uuid.uuid4().hex[:8]}"
        }
        
        mcp_response = await dispatch.handle_mcp_request(mcp_request_payload, licence_ctx, MockHttpContext())
        
        if "result" in mcp_response:
            return JSONResponse(content=mcp_response["result"])
        else:
            return JSONResponse(status_code=500, content={"error": mcp_response.get("error")})

    except Exception as e:
        logger.error(f"Error in OpenAI-compatible endpoint: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": {"message": "Internal Server Error"}})
# ====================================================================
# == FIN DE L'AJOUT                                               ==
# ====================================================================

@app.websocket("/ws")
async def websocket_mcp_endpoint(websocket: WebSocket):
    await websocket.accept()
    client_addr = f"{websocket.client.host}:{websocket.client.port}" if websocket.client else "unknown_client"
    logger.info(f"WebSocket client connected: {client_addr}")
    
    try:
        while not _shutdown_event_flag.is_set():
            request_data = None
            try:
                raw_data = await asyncio.wait_for(websocket.receive_text(), timeout=1.0)
                
                logger.debug(f"WS RCV from {client_addr}: {raw_data[:250]}...")
                
                try:
                    request_data = json.loads(raw_data)
                except json.JSONDecodeError:
                    err_resp = create_mcp_error(None, JSONRPC_PARSE_ERROR, "Invalid JSON payload.")
                    await websocket.send_text(json.dumps(err_resp))
                    continue

                request_id = request_data.get("id")
                method_name = request_data.get("method", "").strip()

                if not method_name:
                    err_resp = create_mcp_error(request_id, JSONRPC_INVALID_REQUEST, "Method name missing or empty.")
                    await websocket.send_text(json.dumps(err_resp))
                    continue

                logger.debug(f"Extracted method_name from request: '{method_name}'")
                
                llm_model_requested = None
                if method_name == "mcp.llm.chat" and isinstance(request_data.get("params"), list) and len(request_data["params"]) > 1 and isinstance(request_data["params"][1], dict):
                    llm_model_requested = request_data["params"][1].get("model")

                licence_ctx, auth_error_obj = authenticate_and_authorize_request(websocket, method_name, llm_model_requested)
                
                if auth_error_obj:
                    logger.warning(f"Auth failed for WS {client_addr}, method '{method_name}': {auth_error_obj.get('message')}")
                    err_resp = create_mcp_error(request_id, auth_error_obj.get("code", -32000), auth_error_obj.get("message"), auth_error_obj.get("data"))
                    await websocket.send_text(json.dumps(err_resp))
                    if auth_error_obj.get("code") == JSONRPC_AUTH_ERROR:
                        await websocket.close(code=1008)
                        break 
                    continue
                
                if not licence_ctx:
                    logger.error(f"Internal auth error for WS {client_addr}. Denying.")
                    err_resp = create_mcp_error(request_id, JSONRPC_INTERNAL_ERROR, "Internal authentication error.")
                    await websocket.send_text(json.dumps(err_resp))
                    continue

                response_or_generator = await dispatch.handle_mcp_request(request_data, licence_ctx, websocket)

                if isinstance(response_or_generator, AsyncGenerator):
                    logger.info(f"Streaming response to WS {client_addr} for '{method_name}' (ID {request_id})")
                    async for stream_chunk_resp in response_or_generator:
                        if _shutdown_event_flag.is_set(): break
                        await websocket.send_text(json.dumps(stream_chunk_resp))
                    if _shutdown_event_flag.is_set(): break
                    logger.debug(f"Finished streaming to WS {client_addr} (ID {request_id})")
                else: 
                    logger.debug(f"Sending single response to WS {client_addr} (ID {request_id}): {str(response_or_generator)[:250]}...")
                    await websocket.send_text(json.dumps(response_or_generator))
            
            except asyncio.TimeoutError:
                if websocket.client_state != WebSocketState.CONNECTED:
                    logger.info(f"WS client {client_addr} appears disconnected after read timeout.")
                    break
                continue
            except WebSocketDisconnect:
                logger.info(f"WebSocket client {client_addr} disconnected gracefully.")
                break
            except Exception as e_inner_loop:
                logger.error(f"Error in WebSocket handler for {client_addr} while processing a request: {e_inner_loop}", exc_info=True)
                if websocket.client_state == WebSocketState.CONNECTED:
                    try: 
                        err_id = request_data.get("id") if request_data else None
                        err_resp = create_mcp_error(err_id, JSONRPC_INTERNAL_ERROR, f"Server error processing request: {type(e_inner_loop).__name__}")
                        await websocket.send_text(json.dumps(err_resp))
                        await websocket.close(code=1011)
                    except: pass 
                break 

    except asyncio.CancelledError:
        logger.info(f"WebSocket task for {client_addr} cancelled.")
    except Exception as e_outer:
        logger.error(f"Outer error in WebSocket endpoint for {client_addr}: {e_outer}", exc_info=True)
    finally:
        logger.info(f"WebSocket connection cleanup for {client_addr}")

def run_gateway_service():
    uvicorn.run(
        "llmbasedos_src.gateway.main:app",
        host=GATEWAY_HOST, port=GATEWAY_WEB_PORT,
        log_config=None, 
    )

if __name__ == "__main__":
    run_gateway_service()