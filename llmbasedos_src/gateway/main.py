# llmbasedos_pkg/gateway/main.py
import asyncio
import json
import logging
import logging.config
from pathlib import Path
import os
import signal 
from typing import Any, Dict, List, Optional, AsyncGenerator, Set

import uvicorn # type: ignore
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState # Nécessaire pour WebSocketState.CONNECTED
from contextlib import asynccontextmanager

# Imports depuis le package llmbasedos
from llmbasedos.mcp_server_framework import create_mcp_error, JSONRPC_PARSE_ERROR, JSONRPC_INVALID_REQUEST, JSONRPC_INTERNAL_ERROR
from .config import (
    GATEWAY_UNIX_SOCKET_PATH, GATEWAY_HOST, GATEWAY_WEB_PORT,
    LOGGING_CONFIG, # S'assurer que c'est bien défini et importable
    JSONRPC_AUTH_ERROR, # Pour l'erreur d'auth
    # D'autres codes d'erreur de config.py pourraient être nécessaires ici
)
from . import registry
from . import dispatch
from .auth import authenticate_and_authorize_request, LicenceDetails 

# --- Configuration du Logging ---
def setup_gateway_logging():
    try:
        logging.config.dictConfig(LOGGING_CONFIG)
        logging.getLogger("llmbasedos.gateway.main").info("Gateway logging configured via dictConfig.")
    except ValueError as e_log_val: # Peut arriver si python-json-logger n'est pas là et que le formatter 'json' est demandé
        logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s (fallback)")
        logging.getLogger("llmbasedos.gateway.main").error(f"Failed to apply dictConfig for logging: {e_log_val}. Using basicConfig.", exc_info=True)
    except Exception as e_log_other:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s (fallback)")
        logging.getLogger("llmbasedos.gateway.main").error(f"Unexpected error applying dictConfig: {e_log_other}. Using basicConfig.", exc_info=True)

# Logger global, sera correctement initialisé dans le lifespan_manager
logger: logging.Logger = logging.getLogger("llmbasedos.gateway.main")

# --- Variables Globales pour les Serveurs/Tâches en Arrière-plan ---
_unix_socket_server_instance: Optional[asyncio.AbstractServer] = None # Renommé pour clarté
_active_unix_client_tasks: Set[asyncio.Task] = set()
_capability_watcher_task_instance: Optional[asyncio.Task] = None # Renommé pour clarté
_shutdown_event_flag = asyncio.Event() # Renommé pour clarté

# --- Mock Client Context pour Sockets UNIX (pour l'authentification) ---
class MockUnixClientContext:
    def __init__(self, peername: Any):
        self.host = "unix_socket_client" # Identifiant générique
        self.port = 0 
        self.peername_str = str(peername) # Pour le logging
    def __repr__(self): return f"<MockUnixClientContext peer='{self.peername_str}'>"


# --- Fonctions de Gestion des Tâches en Arrière-plan ---
async def _start_unix_socket_server_logic():
    global _unix_socket_server_instance
    socket_path_obj = Path(GATEWAY_UNIX_SOCKET_PATH) # GATEWAY_UNIX_SOCKET_PATH vient de .config et est un Path

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
            uid = os.geteuid(); gid = os.getgid()
            os.chown(str(socket_path_obj), uid, gid)
            os.chmod(str(socket_path_obj), 0o660)
            logger.info(f"Set permissions for {socket_path_obj} to 0660 (uid:{uid}, gid:{gid}).")
        except OSError as e_perm:
            logger.warning(f"Could not set optimal permissions for UNIX socket {socket_path_obj}: {e_perm}.")
    except Exception as e_start_unix:
        logger.error(f"Failed to start UNIX socket server on {socket_path_obj}: {e_start_unix}", exc_info=True) # CORRIGÉ ICI
        _unix_socket_server_instance = None

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
        results = await asyncio.gather(*_active_unix_client_tasks, return_exceptions=True)
        for i, res in enumerate(results):
            if isinstance(res, Exception) and not isinstance(res, asyncio.CancelledError):
                logger.error(f"Error in cancelled UNIX client task {i}: {res}")
        _active_unix_client_tasks.clear()
        logger.info("Active UNIX client tasks finished processing.")
    
    if GATEWAY_UNIX_SOCKET_PATH.exists():
        try: GATEWAY_UNIX_SOCKET_PATH.unlink()
        except OSError as e_unlink: logger.error(f"Error removing UNIX socket file on stop: {e_unlink}")
    logger.info("UNIX socket server fully stopped.")

async def _handle_single_unix_client_task(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    peername = writer.get_extra_info('peername', 'unknown_unix_peer')
    client_desc = f"unix_client_{str(peername).replace('/', '_').replace(':', '_')}" # Make it more filename friendly
    logger.info(f"UNIX socket client connected: {client_desc}")
    mock_client_ctx = MockUnixClientContext(peername)
    message_buffer = bytearray()

    try:
        while not _shutdown_event_flag.is_set():
            try:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=1.0)
                if not chunk: logger.info(f"UNIX client {client_desc} disconnected (EOF)."); break
                message_buffer.extend(chunk)

                while b'\0' in message_buffer:
                    if _shutdown_event_flag.is_set(): break
                    message_bytes, rest_of_buffer = message_buffer.split(b'\0', 1)
                    message_buffer = rest_of_buffer
                    message_str = message_bytes.decode('utf-8')
                    logger.debug(f"UNIX RCV from {client_desc}: {message_str[:250]}...")
                    
                    try: request_data = json.loads(message_str)
                    except json.JSONDecodeError:
                        err_resp = create_mcp_error(None, JSONRPC_PARSE_ERROR, "Invalid JSON payload.")
                        writer.write(json.dumps(err_resp).encode('utf-8') + b'\0'); await writer.drain()
                        continue

                    req_id = request_data.get("id"); method_name = request_data.get("method")
                    if not isinstance(method_name, str):
                        err_resp = create_mcp_error(req_id, JSONRPC_INVALID_REQUEST, "Method name missing or not a string.")
                        writer.write(json.dumps(err_resp).encode('utf-8') + b'\0'); await writer.drain()
                        continue
                    
                    # Note: authenticate_and_authorize_request is a placeholder from previous versions.
                    # Ensure its signature matches and it handles MockUnixClientContext correctly.
                    licence_ctx, auth_error_obj = authenticate_and_authorize_request(mock_client_ctx, method_name) # type: ignore
                    
                    if auth_error_obj:
                        logger.warning(f"Auth failed for UNIX {client_desc}, method '{method_name}': {auth_error_obj['message']}")
                        err_resp = create_mcp_error(req_id, auth_error_obj["code"], auth_error_obj["message"], auth_error_obj.get("data"))
                        writer.write(json.dumps(err_resp).encode('utf-8') + b'\0'); await writer.drain()
                        if auth_error_obj["code"] == JSONRPC_AUTH_ERROR: break 
                        continue
                    if not licence_ctx:
                        logger.error(f"Internal auth error for UNIX {client_desc}. Denying.")
                        err_resp = create_mcp_error(req_id, JSONRPC_INTERNAL_ERROR, "Internal authentication error.")
                        writer.write(json.dumps(err_resp).encode('utf-8') + b'\0'); await writer.drain(); continue

                    response_or_generator = await dispatch.handle_mcp_request(request_data, licence_ctx, mock_client_ctx) # type: ignore

                    if isinstance(response_or_generator, AsyncGenerator):
                        async for stream_chunk_resp in response_or_generator:
                            if _shutdown_event_flag.is_set(): break
                            writer.write(json.dumps(stream_chunk_resp).encode('utf-8') + b'\0')
                            await writer.drain()
                        if _shutdown_event_flag.is_set(): break
                        logger.debug(f"Finished streaming to UNIX {client_desc} (ID {req_id})")
                    else:
                        writer.write(json.dumps(response_or_generator).encode('utf-8') + b'\0')
                        await writer.drain()
            
            except asyncio.TimeoutError: continue # Timeout on read, allows checking _shutdown_event_flag
            except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
                logger.info(f"UNIX client {client_desc} connection issue."); break
            except UnicodeDecodeError: logger.error(f"UNIX client {client_desc} sent invalid UTF-8."); break
        if _shutdown_event_flag.is_set(): logger.info(f"UNIX client {client_desc} handler exiting due to shutdown signal.")
    except asyncio.CancelledError: logger.info(f"UNIX client task for {client_desc} cancelled.")
    except Exception as e_client: logger.error(f"Error in UNIX client handler for {client_desc}: {e_client}", exc_info=True)
    finally:
        logger.info(f"Closing UNIX connection for {client_desc}")
        if not writer.is_closing(): 
            try: writer.close(); await writer.wait_closed()
            except Exception as e_close: logger.debug(f"Error closing writer for {client_desc}: {e_close}")

async def _run_unix_socket_client_handler_managed(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    task = asyncio.current_task()
    _active_unix_client_tasks.add(task) # type: ignore
    try:
        await _handle_single_unix_client_task(reader, writer)
    finally:
        _active_unix_client_tasks.discard(task) # type: ignore

# --- Gestionnaire de Lifespan FastAPI ---
@asynccontextmanager
async def lifespan_manager(app_fastapi: FastAPI): # Renommé 'app' en 'app_fastapi' pour éviter conflit
    global logger, _capability_watcher_task_instance, _unix_socket_server_instance

    setup_gateway_logging()
    logger = logging.getLogger("llmbasedos.gateway.main") # S'assurer que logger est bien celui configuré
    logger.info("Gateway Lifespan: Startup sequence initiated...")

    loop = asyncio.get_running_loop()
    active_signals = []
    def _shutdown_signal_handler(sig: signal.Signals): # Wrapper non-async
        logger.info(f"Signal {sig.name} received by lifespan, setting shutdown event...")
        if not _shutdown_event_flag.is_set():
            loop.call_soon_threadsafe(_shutdown_event_flag.set) # Thread-safe way to set event from signal handler
    
    for sig_val in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig_val, lambda s=sig_val: _shutdown_signal_handler(s))
            active_signals.append(sig_val)
        except (ValueError, RuntimeError) as e_signal: # ex: not in main thread
             logger.warning(f"Could not set signal handler for {sig_val}: {e_signal}. Relying on Uvicorn for shutdown.")


    if hasattr(registry, 'start_capability_watcher_task') and callable(registry.start_capability_watcher_task):
        _capability_watcher_task_instance = asyncio.create_task(registry.start_capability_watcher_task(), name="CapabilityWatcher")
        logger.info("Capability watcher task created.")
    else:
        logger.warning("registry.start_capability_watcher_task not found or not callable.")
    
    await _start_unix_socket_server_logic()
    logger.info("Gateway Lifespan: Startup complete. Application is ready.")
    
    try:
        yield # L'application tourne
    finally:
        logger.info("Gateway Lifespan: Shutdown sequence initiated (from finally block)...")
        if not _shutdown_event_flag.is_set(): # Si shutdown n'a pas été initié par signal
            _shutdown_event_flag.set() # Déclencher l'arrêt des tâches en background

        if _capability_watcher_task_instance and not _capability_watcher_task_instance.done():
            logger.info("Cancelling capability watcher task...")
            _capability_watcher_task_instance.cancel()
            try: await _capability_watcher_task_instance
            except asyncio.CancelledError: logger.info("Capability watcher task successfully cancelled.")
            except Exception as e_watch_stop: logger.error(f"Error stopping watcher task: {e_watch_stop}", exc_info=True)
        
        await _stop_unix_socket_server_logic()
        
        if hasattr(dispatch, 'shutdown_dispatch_executor') and callable(dispatch.shutdown_dispatch_executor):
            try: dispatch.shutdown_dispatch_executor()
            except Exception as e_disp_shutdown : logger.error(f"Error shutting down dispatch executor: {e_disp_shutdown}")
            logger.info("Dispatch executor shutdown requested.")
        
        # Retirer les gestionnaires de signaux
        for sig_val in active_signals:
            try: loop.remove_signal_handler(sig_val)
            except Exception as e_rem_sig: logger.debug(f"Error removing signal handler for {sig_val}: {e_rem_sig}")
        
        logger.info("Gateway Lifespan: Shutdown complete.")

# Initialisation de l'application FastAPI
app = FastAPI(
    title="llmbasedos MCP Gateway",
    description="Central router for Model Context Protocol requests.",
    version="0.1.2", # Version incrémentée
    lifespan=lifespan_manager
)

# --- WebSocket Endpoint ---
@app.websocket("/ws")
async def websocket_mcp_endpoint(websocket: WebSocket):
    await websocket.accept()
    client_addr = f"{websocket.client.host}:{websocket.client.port}" # type: ignore # client peut être None en théorie
    logger.info(f"WebSocket client connected: {client_addr}")
    
    try:
        while not _shutdown_event_flag.is_set(): # Vérifier l'arrêt
            try:
                # Utiliser un timeout pour permettre de vérifier _shutdown_event_flag
                raw_data = await asyncio.wait_for(websocket.receive_text(), timeout=1.0)
            except asyncio.TimeoutError:
                # Vérifier si le client est toujours connecté (ping/pong implicite de websockets)
                if websocket.client_state != WebSocketState.CONNECTED:
                    logger.info(f"WS client {client_addr} appears disconnected after read timeout.")
                    break
                continue # Timeout, on revérifie _shutdown_event_flag et on réessaie de lire

            logger.debug(f"WS RCV from {client_addr}: {raw_data[:250]}...")
            
            try: request_data = json.loads(raw_data)
            except json.JSONDecodeError:
                err_resp = create_mcp_error(None, JSONRPC_PARSE_ERROR, "Invalid JSON payload.")
                await websocket.send_text(json.dumps(err_resp)); continue

            request_id = request_data.get("id"); method_name = request_data.get("method")
            if not isinstance(method_name, str):
                err_resp = create_mcp_error(request_id, JSONRPC_INVALID_REQUEST, "Method name missing or not a string.")
                await websocket.send_text(json.dumps(err_resp)); continue
            logger.debug(f"Extracted method_name from request: '{method_name}'")
            licence_ctx, auth_error_obj = authenticate_and_authorize_request(websocket, method_name)
            
            if auth_error_obj:
                logger.warning(f"Auth failed for WS {client_addr}, method '{method_name}': {auth_error_obj['message']}")
                err_resp = create_mcp_error(request_id, auth_error_obj["code"], auth_error_obj["message"], auth_error_obj.get("data"))
                await websocket.send_text(json.dumps(err_resp))
                if auth_error_obj["code"] == JSONRPC_AUTH_ERROR: # type: ignore # JSONRPC_AUTH_ERROR est bien dans config
                    await websocket.close(code=1008); break
                continue
            if not licence_ctx:
                logger.error(f"Internal auth error for WS {client_addr}. Denying.");
                err_resp = create_mcp_error(request_id, JSONRPC_INTERNAL_ERROR, "Internal authentication error.")
                await websocket.send_text(json.dumps(err_resp)); continue

            response_or_generator = await dispatch.handle_mcp_request(request_data, licence_ctx, websocket)

            if isinstance(response_or_generator, AsyncGenerator):
                logger.info(f"Streaming response to WS {client_addr} for '{method_name}' (ID {request_id})")
                try:
                    async for stream_chunk_resp in response_or_generator:
                        if _shutdown_event_flag.is_set(): break # Arrêter le stream si shutdown
                        await websocket.send_text(json.dumps(stream_chunk_resp))
                    if _shutdown_event_flag.is_set(): break
                    logger.debug(f"Finished streaming to WS {client_addr} (ID {request_id})")
                except WebSocketDisconnect: logger.info(f"WS {client_addr} disconnected during stream (ID {request_id})."); break
                except Exception as stream_exc:
                    logger.error(f"Error streaming to WS {client_addr} (ID {request_id}): {stream_exc}", exc_info=True)
                    if websocket.client_state == WebSocketState.CONNECTED:
                         try: await websocket.send_text(json.dumps(create_mcp_error(request_id, JSONRPC_INTERNAL_ERROR, "Streaming error.")))
                         except: pass # Ignorer si on ne peut plus envoyer
                    break 
            else: # Single JSON-RPC response dict
                logger.debug(f"WS SEND to {client_addr} (ID {request_id}): {str(response_or_generator)[:250]}...")
                await websocket.send_text(json.dumps(response_or_generator))
        if _shutdown_event_flag.is_set(): logger.info(f"WebSocket handler for {client_addr} exiting due to shutdown signal.")

    except WebSocketDisconnect: logger.info(f"WebSocket client {client_addr} disconnected.")
    except asyncio.CancelledError: logger.info(f"WebSocket task for {client_addr} cancelled.") # Peut arriver sur shutdown rapide
    except Exception as e:
        logger.error(f"Error in WebSocket handler for {client_addr}: {e}", exc_info=True)
        if hasattr(websocket, 'client_state') and websocket.client_state == WebSocketState.CONNECTED:
            try: await websocket.close(code=1011)
            except: pass
    finally:
        logger.info(f"WebSocket connection cleanup for {client_addr}")
        # La fermeture explicite est déjà gérée dans les blocs except ou par le client.
        # S'assurer que le client sait que la connexion est finie.

# --- Point d'Entrée Principal (pour Uvicorn) ---
def run_gateway_service():
    uvicorn.run(
        "llmbasedos.gateway.main:app",
        host=GATEWAY_HOST, port=GATEWAY_WEB_PORT,
        log_config=None, # Laisser le lifespan manager configurer le logging
        # workers=1 # Important pour la gestion de l'état global (_shutdown_event_flag, etc.)
                  # Si workers > 1, il faut une communication inter-processus pour les signaux.
    )

if __name__ == "__main__":
    run_gateway_service()