# llmbasedos_pkg/gateway/main.py
import asyncio
import json
import logging
import logging.config # Pour dictConfig si setup_gateway_logging l'utilise
from pathlib import Path
import os
import signal 
from typing import Any, Dict, List, Optional, AsyncGenerator, Set # Ajout de Set

import uvicorn # type: ignore
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request as FastAPIRequest
from starlette.websockets import WebSocketState # Pour vérifier l'état de la connexion WebSocket
from contextlib import asynccontextmanager

# Imports depuis le package llmbasedos
from llmbasedos.mcp_server_framework import create_mcp_error, JSONRPC_PARSE_ERROR, JSONRPC_INVALID_REQUEST, JSONRPC_INTERNAL_ERROR
# Supposons que config.py exporte ces constantes pour les erreurs personnalisées
from .config import (
    GATEWAY_UNIX_SOCKET_PATH, GATEWAY_HOST, GATEWAY_WEB_PORT,
    LOGGING_CONFIG, # Supposons que LOGGING_CONFIG est bien défini dans config.py
    JSONRPC_AUTH_ERROR # Pour la fermeture du WebSocket sur erreur d'auth
)
from . import registry
from . import dispatch
from .auth import authenticate_and_authorize_request, LicenceDetails # Simulé si non défini

# --- Configuration du Logging ---
# Il est préférable d'appeler dictConfig une seule fois au démarrage.
# Si __init__.py du package gateway le fait, c'est bon. Sinon, ici.
# Pour cet exemple, je vais supposer que LOGGING_CONFIG est chargé depuis .config
# et que setup_gateway_logging est une fonction qui applique cette config.

def setup_gateway_logging(): # Fonction placeholder
    """Applies the logging configuration."""
    try:
        logging.config.dictConfig(LOGGING_CONFIG)
        logging.getLogger("llmbasedos.gateway.main").info("Gateway logging configured.")
    except Exception as e:
        # Fallback basic logging si dictConfig échoue
        logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        logging.getLogger("llmbasedos.gateway.main").error(f"Failed to apply dictConfig for logging: {e}. Using basicConfig.", exc_info=True)

# Logger global pour ce module, sera initialisé dans le lifespan
logger: logging.Logger = logging.getLogger("llmbasedos.gateway.main")


# --- Variables Globales pour les Serveurs/Tâches en Arrière-plan ---
_unix_socket_server: Optional[asyncio.AbstractServer] = None
_active_unix_client_handler_tasks: Set[asyncio.Task] = set()
_capability_watcher_task: Optional[asyncio.Task] = None
_shutdown_event = asyncio.Event() # Pour signaler un arrêt gracieux

# --- Mock Client Context pour Sockets UNIX (pour l'authentification) ---
class MockUnixClientContext:
    def __init__(self, peername: Any):
        self.host = "unix_socket"
        self.port = 0
        self.peername_str = str(peername)
    def __repr__(self): return f"<MockUnixClientContext peer='{self.peername_str}'>"


# --- Gestionnaire de Lifespan FastAPI ---
@asynccontextmanager
async def lifespan_manager(app: FastAPI):
    global logger, _capability_watcher_task, _unix_socket_server

    # 1. Actions au Démarrage
    setup_gateway_logging() # Configurer le logging en premier
    logger = logging.getLogger("llmbasedos.gateway.main") # Réassigner après config
    logger.info("Gateway Lifespan: Startup sequence initiated...")

    # Ajouter des gestionnaires de signaux pour SIGINT et SIGTERM
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(initiate_shutdown(s)))

    # Démarrer le watcher de capabilities
    if registry.start_capability_watcher_task: # Vérifier si la fonction existe
        _capability_watcher_task = asyncio.create_task(registry.start_capability_watcher_task(), name="CapabilityWatcher")
        logger.info("Capability watcher task created.")
    
    # Démarrer le serveur de socket UNIX
    await _start_unix_socket_server_logic()
    
    logger.info("Gateway Lifespan: Startup complete. Application is ready.")
    
    try:
        yield # L'application tourne ici
    finally:
        # 2. Actions à l'Arrêt (déclenchées par _shutdown_event ou fin normale)
        logger.info("Gateway Lifespan: Shutdown sequence initiated...")
        
        if _capability_watcher_task and not _capability_watcher_task.done():
            logger.info("Cancelling capability watcher task...")
            _capability_watcher_task.cancel()
            try: await _capability_watcher_task
            except asyncio.CancelledError: logger.info("Capability watcher task successfully cancelled.")
            except Exception as e_watch_stop: logger.error(f"Error stopping watcher task: {e_watch_stop}", exc_info=True)
        
        await _stop_unix_socket_server_logic()
        
        if hasattr(dispatch, 'shutdown_dispatch_executor') and callable(dispatch.shutdown_dispatch_executor):
            dispatch.shutdown_dispatch_executor() # Pour le ThreadPoolExecutor dans dispatch.py
            logger.info("Dispatch executor shutdown requested.")
        
        logger.info("Gateway Lifespan: Shutdown complete.")

async def initiate_shutdown(sig: Optional[signal.Signals] = None):
    if sig: logger.info(f"Received signal {sig.name}. Initiating graceful shutdown...")
    else: logger.info("Graceful shutdown initiated programmatically...")
    _shutdown_event.set()

# Initialisation de l'application FastAPI
app = FastAPI(
    title="llmbasedos MCP Gateway",
    description="Central router for Model Context Protocol requests.",
    version="0.1.1",
    lifespan=lifespan_manager # Utilisation du nouveau gestionnaire de lifespan
)


# --- WebSocket Endpoint ---
@app.websocket("/ws")
async def websocket_mcp_endpoint(websocket: WebSocket):
    await websocket.accept()
    client_addr = f"{websocket.client.host}:{websocket.client.port}"
    logger.info(f"WebSocket client connected: {client_addr}")
    
    try:
        while True:
            raw_data = await websocket.receive_text()
            logger.debug(f"WS RCV from {client_addr}: {raw_data[:250]}...")
            
            try: request_data = json.loads(raw_data)
            except json.JSONDecodeError:
                err_resp = create_mcp_error(None, JSONRPC_PARSE_ERROR, "Invalid JSON payload.")
                await websocket.send_text(json.dumps(err_resp)); continue

            request_id = request_data.get("id"); method_name = request_data.get("method")
            if not isinstance(method_name, str):
                err_resp = create_mcp_error(request_id, JSONRPC_INVALID_REQUEST, "Method name missing or not a string.")
                await websocket.send_text(json.dumps(err_resp)); continue

            # Simplification: llm_model et tokens ne sont pas extraits ici pour pré-auth pour l'instant
            licence_ctx, auth_error_obj = authenticate_and_authorize_request(websocket, method_name)
            
            if auth_error_obj:
                logger.warning(f"Auth failed for WS {client_addr}, method '{method_name}': {auth_error_obj['message']}")
                err_resp = create_mcp_error(request_id, auth_error_obj["code"], auth_error_obj["message"], auth_error_obj.get("data"))
                await websocket.send_text(json.dumps(err_resp))
                if auth_error_obj["code"] == JSONRPC_AUTH_ERROR: # Erreur d'auth fatale
                    await websocket.close(code=1008); break
                continue
            if not licence_ctx:
                logger.error(f"Internal auth error for WS {client_addr}. Denying.");
                err_resp = create_mcp_error(request_id, JSONRPC_INTERNAL_ERROR, "Internal authentication error.")
                await websocket.send_text(json.dumps(err_resp)); continue

            response_or_generator = await dispatch.handle_mcp_request(request_data, licence_ctx, websocket)

            if isinstance(response_or_generator, AsyncGenerator):
                # ... (Logique de streaming comme avant) ...
                async for chunk in response_or_generator: await websocket.send_text(json.dumps(chunk))
            else:
                await websocket.send_text(json.dumps(response_or_generator))

    except WebSocketDisconnect: logger.info(f"WebSocket client {client_addr} disconnected.")
    except Exception as e:
        logger.error(f"Error in WebSocket handler for {client_addr}: {e}", exc_info=True)
        if websocket.client_state == WebSocketState.CONNECTED:
            await websocket.close(code=1011) # Erreur interne serveur
    finally: logger.info(f"WebSocket connection cleanup for {client_addr}")


# --- Logique du Serveur Socket UNIX (déplacée pour clarté) ---
async def _handle_single_unix_client_task(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    peername = writer.get_extra_info('peername', 'unknown_unix_peer')
    client_desc = f"unix_client_{peername}"
    logger.info(f"UNIX socket client connected: {client_desc}")
    mock_client_ctx = MockUnixClientContext(peername)
    message_buffer = bytearray()

    try:
        while not _shutdown_event.is_set(): # Check shutdown event
            try:
                # Utiliser un timeout pour permettre de vérifier _shutdown_event périodiquement
                chunk = await asyncio.wait_for(reader.read(4096), timeout=1.0)
                if not chunk: logger.info(f"UNIX client {client_desc} disconnected (EOF)."); break
                message_buffer.extend(chunk)

                while b'\0' in message_buffer:
                    if _shutdown_event.is_set(): break # Check again before processing
                    message_bytes, rest = message_buffer.split(b'\0', 1)
                    message_buffer = rest
                    message_str = message_bytes.decode('utf-8')
                    logger.debug(f"UNIX RCV from {client_desc}: {message_str[:250]}...")

                    # ... (Logique de parsing JSON, auth, dispatch comme pour WebSocket) ...
                    try: request_data = json.loads(message_str)
                    except json.JSONDecodeError: # ... (send error) ...
                        err_resp = create_mcp_error(None, JSONRPC_PARSE_ERROR, "Invalid JSON.")
                        writer.write(json.dumps(err_resp).encode('utf-8') + b'\0'); await writer.drain()
                        continue
                    
                    request_id = request_data.get("id"); method_name = request_data.get("method")
                    if not isinstance(method_name, str): # ... (send error) ...
                        err_resp = create_mcp_error(request_id, JSONRPC_INVALID_REQUEST, "Method name missing/invalid.")
                        writer.write(json.dumps(err_resp).encode('utf-8') + b'\0'); await writer.drain()
                        continue

                    licence_ctx, auth_error_obj = authenticate_and_authorize_request(mock_client_ctx, method_name) # type: ignore
                    if auth_error_obj: # ... (send error, break on fatal) ...
                        err_resp = create_mcp_error(request_id, auth_error_obj["code"], auth_error_obj["message"], auth_error_obj.get("data"))
                        writer.write(json.dumps(err_resp).encode('utf-8') + b'\0'); await writer.drain()
                        if auth_error_obj["code"] == JSONRPC_AUTH_ERROR: break
                        continue
                    if not licence_ctx: # ... (send error) ...
                        err_resp = create_mcp_error(request_id, JSONRPC_INTERNAL_ERROR, "Internal auth error.");
                        writer.write(json.dumps(err_resp).encode('utf-8') + b'\0'); await writer.drain(); continue

                    response_or_generator = await dispatch.handle_mcp_request(request_data, licence_ctx, mock_client_ctx) # type: ignore
                    if isinstance(response_or_generator, AsyncGenerator):
                        async for chunk_resp in response_or_generator:
                            writer.write(json.dumps(chunk_resp).encode('utf-8') + b'\0')
                            await writer.drain()
                    else:
                        writer.write(json.dumps(response_or_generator).encode('utf-8') + b'\0')
                        await writer.drain()

            except asyncio.TimeoutError: continue # Timeout sur read, permet de vérifier _shutdown_event
            except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
                logger.info(f"UNIX client {client_desc} connection issue."); break
            except UnicodeDecodeError: logger.error(f"UNIX client {client_desc} sent invalid UTF-8."); break
    except asyncio.CancelledError: logger.info(f"UNIX client task for {client_desc} cancelled.") # Attendu lors du shutdown
    except Exception as e_client: logger.error(f"Error in UNIX client handler for {client_desc}: {e_client}", exc_info=True)
    finally:
        logger.info(f"Closing UNIX connection for {client_desc}")
        if not writer.is_closing(): writer.close(); await writer.wait_closed()

async def _run_unix_socket_client_handler_managed(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """Wrapper to manage the task in _active_unix_client_handler_tasks."""
    task = asyncio.current_task()
    _active_unix_client_handler_tasks.add(task) # type: ignore
    try:
        await _handle_single_unix_client_task(reader, writer)
    finally:
        _active_unix_client_handler_tasks.remove(task) # type: ignore

async def _start_unix_socket_server_logic():
    global _unix_socket_server # Assurez-vous que _unix_socket_server est bien déclaré globalement si modifié ici
    
    # GATEWAY_UNIX_SOCKET_PATH est déjà un objet Path grâce à config.py
    # Pour éviter toute ambiguïté et s'assurer que c'est un Path, on peut le ré-envelopper
    # mais si config.py est correct, ce n'est pas strictement nécessaire ici.
    # Pour la robustesse, on peut le faire :
    socket_path_obj = Path(GATEWAY_UNIX_SOCKET_PATH) # Assure que c'est un Path

    try:
        socket_path_obj.parent.mkdir(parents=True, exist_ok=True)
        if socket_path_obj.exists():
            socket_path_obj.unlink()
    except OSError as e:
        logger.error(f"Error preparing UNIX socket path {socket_path_obj}: {e}. Server may fail.") # Utilisé socket_path_obj
        return

    try:
        _unix_socket_server = await asyncio.start_unix_server(
            _run_unix_socket_client_handler_managed, # Assurez-vous que cette fonction est définie
            path=str(socket_path_obj) # start_unix_server attend une chaîne pour 'path'
        )
        
        addr = _unix_socket_server.sockets[0].getsockname() if _unix_socket_server.sockets else str(socket_path_obj)
        logger.info(f"MCP Gateway listening on UNIX socket: {addr}") # Log avec addr

        try:
            uid = os.geteuid()
            # Tenter d'obtenir un GID 'llmgroup' ou similaire, sinon GID de l'utilisateur.
            # Pour la simplicité, utilisons le GID actuel de l'utilisateur (llmuser dans Docker)
            gid = os.getgid() 
            # Si vous avez un groupe spécifique, vous devrez le rechercher avec grp.getgrnam('nom_groupe').gr_gid
            
            os.chown(str(socket_path_obj), uid, gid) # Utilisé socket_path_obj
            os.chmod(str(socket_path_obj), 0o660)    # Utilisé socket_path_obj
            logger.info(f"Set permissions for {socket_path_obj} to 0660 (uid:{uid}, gid:{gid}).")
        except OSError as e_perm:
            logger.warning(f"Could not set optimal permissions/owner for UNIX socket {socket_path_obj}: {e_perm}. Using defaults.") # Utilisé socket_path_obj
            # Pas de logger.info ici car le précédent logger.info(f"MCP Gateway listening...") suffit.

    except Exception as e_start_unix:
        logger.error(f"Failed to start UNIX socket server on {socket_path_obj}: {e_start_unix}", exc_info=True) # Utilisé socket_path_obj
        _unix_socket_server = None

async def _stop_unix_socket_server_logic():
    global _unix_socket_server
    if _unix_socket_server:
        logger.info("Stopping UNIX socket server...")
        _unix_socket_server.close()
        await _unix_socket_server.wait_closed()
        _unix_socket_server = None
        logger.info("UNIX server socket now closed.")

    if _active_unix_client_handler_tasks:
        logger.info(f"Cancelling {len(_active_unix_client_handler_tasks)} active UNIX client tasks...")
        for task in list(_active_unix_client_handler_tasks): task.cancel()
        # Attendre que toutes les tâches se terminent (elles devraient lever CancelledError)
        await asyncio.gather(*_active_unix_client_handler_tasks, return_exceptions=True)
        _active_unix_client_handler_tasks.clear()
        logger.info("Active UNIX client tasks finished processing.")
    
    if GATEWAY_UNIX_SOCKET_PATH.exists():
        try: GATEWAY_UNIX_SOCKET_PATH.unlink()
        except OSError as e_unlink: logger.error(f"Error removing UNIX socket file on stop: {e_unlink}")
    logger.info("UNIX socket server fully stopped.")


# --- Point d'Entrée Principal (pour Uvicorn) ---
def run_gateway_service():
    """ Main function to run Uvicorn server for the gateway. """
    uvicorn.run(
        "llmbasedos.gateway.main:app", # <<< CORRIGER ICI: utiliser 'llmbasedos'
        host=GATEWAY_HOST,
        port=GATEWAY_WEB_PORT,
        log_config=None, 
    )

if __name__ == "__main__":
    # Ce bloc est exécuté lorsque vous faites : python -m llmbasedos_pkg.gateway.main
    # Il est important que setup_gateway_logging() soit appelé avant que run_gateway_service()
    # ne lance Uvicorn, si Uvicorn lui-même n'active pas le lifespan assez tôt pour le logging.
    # Cependant, avec FastAPI lifespan, le logging est configuré DANS le lifespan.
    # Uvicorn utilisera le logger configuré par le lifespan.
    run_gateway_service()