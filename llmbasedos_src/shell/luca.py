# llmbasedos_src/shell/luca.py
import asyncio
import json
import logging
import logging.config # For dictConfig
import os
import sys
from pathlib import Path
import uuid
import shlex # For parsing command line string
from typing import Any, Dict, Optional, List, Callable, Awaitable, Set # For type hints
import signal # Import du module signal

import websockets # Main library for WebSocket client
from websockets.client import WebSocketClientProtocol # For precise type hinting
from websockets.exceptions import ConnectionClosed, ConnectionClosedOK, WebSocketException 

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.styles import Style as PromptStyle
from rich.console import Console
from rich.text import Text
from rich.syntax import Syntax

# --- Import des modules locaux ---
from . import builtin_cmds 
from .shell_utils import stream_llm_chat_to_console 

# --- Configuration du Shell, Logging, Console Rich ---
SHELL_HISTORY_FILE = Path(os.path.expanduser("~/.llmbasedos_shell_history"))
GATEWAY_WS_URL_CONF = os.getenv("LLMBDO_GATEWAY_WS_URL", "ws://localhost:8000/ws")

LOG_LEVEL_STR_CONF = os.getenv("LLMBDO_SHELL_LOG_LEVEL", "INFO").upper()
LOG_FORMAT_CONF = os.getenv("LLMBDO_SHELL_LOG_FORMAT", "simple")

def setup_shell_logging():
    log_level_int = logging.getLevelName(LOG_LEVEL_STR_CONF)
    if not isinstance(log_level_int, int):
        log_level_int = logging.INFO
        logging.warning(f"Invalid shell log level '{LOG_LEVEL_STR_CONF}', defaulting to INFO.")

    formatter_to_use = LOG_FORMAT_CONF
    formatter_class = "logging.Formatter"
    formatter_details = {"format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s"}

    if formatter_to_use == "json":
        try:
            from python_json_logger import jsonlogger # type: ignore
            formatter_class = "python_json_logger.jsonlogger.JsonFormatter"
            formatter_details = {"format": "%(asctime)s %(levelname)s %(name)s %(module)s %(funcName)s %(lineno)d %(message)s"}
        except ImportError:
            logging.warning("python-json-logger not found. Defaulting to 'simple' log format for shell.")
            formatter_to_use = "simple"
    
    if formatter_to_use != "simple" and formatter_to_use != "json":
        logging.warning(f"Invalid shell log format '{LOG_FORMAT_CONF}', defaulting to 'simple'.")
        formatter_to_use = "simple"
        formatter_class = "logging.Formatter"
        formatter_details = {"format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s"}

    LOGGING_CONFIG_DICT = {
        "version": 1, "disable_existing_loggers": False,
        "formatters": {formatter_to_use: {"()": formatter_class, **formatter_details}},
        "handlers": {"console_stderr": {"class": "logging.StreamHandler", "formatter": formatter_to_use, "stream": "ext://sys.stderr"}},
        "root": {"handlers": ["console_stderr"], "level": "WARNING"}, 
        "loggers": {
            "llmbasedos.shell": {"handlers": ["console_stderr"], "level": log_level_int, "propagate": False},
            "websockets.client": {"handlers": ["console_stderr"], "level": "WARNING", "propagate": False}, # Moins verbeux
            "websockets.protocol": {"handlers": ["console_stderr"], "level": "WARNING", "propagate": False},
        }
    }
    try:
        logging.config.dictConfig(LOGGING_CONFIG_DICT)
    except Exception as e_log_conf: 
        logging.basicConfig(level=log_level_int, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s (fallback)")
        logging.error(f"Failed to apply dictConfig for shell logging: {e_log_conf}. Using basicConfig.", exc_info=True)

setup_shell_logging()
logger = logging.getLogger("llmbasedos.shell.luca")
console = Console(stderr=True, force_terminal=True if sys.stderr.isatty() else False)

class ShellApp:
    def __init__(self, gateway_url: str, console_instance: Console):
        self.gateway_url: str = gateway_url
        self.console: Console = console_instance
        self.mcp_websocket: Optional[WebSocketClientProtocol] = None
        self.pending_responses: Dict[str, asyncio.Future] = {}
        self.available_mcp_commands: List[str] = []
        self.response_listener_task: Optional[asyncio.Task] = None
        self.cwd_state: Path = Path(os.path.expanduser("~")).resolve()
        self.is_shutting_down: bool = False
        self.prompt_style = PromptStyle.from_dict({
            'prompt': 'fg:ansibrightblue bold', 
            'path': 'fg:ansigreen bold', 
            'disconnected': 'fg:ansired bold',
            'error': 'fg:ansired bold'
        })

    def get_cwd(self) -> Path: return self.cwd_state
    def set_cwd(self, new_path: Path):
        try: self.cwd_state = new_path.resolve()
        except Exception as e: self.console.print(f"[[error]Error setting CWD to '{new_path}': {e}[/]]")

    def _is_websocket_open(self) -> bool:
        """Helper to check if websocket is connected and open."""
        return bool(self.mcp_websocket and self.mcp_websocket.open)

    async def _cancel_existing_listener(self):
        if self.response_listener_task and not self.response_listener_task.done():
            logger.debug("Cancelling previous response listener task.")
            self.response_listener_task.cancel()
            try: await self.response_listener_task
            except asyncio.CancelledError: logger.debug("Previous listener task successfully cancelled.")
            except Exception as e_cancel: logger.error(f"Error awaiting previous listener cancellation: {e_cancel}")
        self.response_listener_task = None

    async def _start_response_listener(self):
        await self._cancel_existing_listener()
        if self._is_websocket_open():
            self.response_listener_task = asyncio.create_task(self._response_listener_logic(), name="ShellResponseListener")
            logger.info("Response listener task started for new connection.")
        else:
            logger.error("Cannot start response listener: WebSocket is not connected or not open.")

    async def _response_listener_logic(self):
        active_ws = self.mcp_websocket
        if not active_ws: logger.error("Listener logic: No active WebSocket at start."); return
        logger.debug(f"Listener logic running for WebSocket: id={id(active_ws)}")
        
        try:
            async for message_str in active_ws:
                if self.is_shutting_down or self.mcp_websocket != active_ws or not self.mcp_websocket.open:
                    logger.info(f"Listener (ws_id={id(active_ws)}): Conditions changed. Exiting loop."); break
                try:
                    response = json.loads(message_str)
                    logger.debug(f"Gateway RCV (ShellApp): {str(response)[:200]}...")
                    response_id = response.get("id")
                    future = self.pending_responses.pop(response_id, None)
                    if future:
                        if not future.done(): future.set_result(response)
                        else: logger.warning(f"Listener: Future for ID {response_id} already done. Ignored.")
                    # ... (logique pour stream chunks) ...
                except json.JSONDecodeError: logger.error(f"Listener: Invalid JSON from gateway: {message_str}")
                except Exception as e_inner: logger.error(f"Listener: Error processing message: {e_inner}", exc_info=True)
        
        except (ConnectionClosed, ConnectionClosedOK) as e_ws_closed:
            logger.warning(f"Listener: WebSocket connection (id={id(active_ws)}) closed: {e_ws_closed}")
        except WebSocketException as e_ws_generic:
             logger.error(f"Listener: WebSocketException (id={id(active_ws)}): {e_ws_generic}", exc_info=True)
        except asyncio.CancelledError: logger.info(f"Listener: Task (id={id(active_ws)}) explicitly cancelled.")
        except Exception as e_outer: logger.error(f"Listener: Task (id={id(active_ws)}) ended with critical error: {e_outer}", exc_info=True)
        finally:
            logger.info(f"Listener: Task stopped for WebSocket id={id(active_ws)}.")
            if self.mcp_websocket == active_ws and (not self.mcp_websocket or not self.mcp_websocket.open):
                self.mcp_websocket = None
            for req_id, fut in list(self.pending_responses.items()):
                if not fut.done(): fut.set_exception(RuntimeError(f"Gateway conn lost. Req ID: {req_id}"))
                self.pending_responses.pop(req_id, None)

    async def ensure_connection(self, force_reconnect: bool = False) -> bool:
        if self.is_shutting_down: return False
        if not force_reconnect and self._is_websocket_open():
            return True
        
        action_str = "Reconnecting" if force_reconnect or self.mcp_websocket else "Connecting"
        logger.info(f"{action_str} to MCP Gateway: {self.gateway_url}")
        
        await self._cancel_existing_listener()
        if self.mcp_websocket and self.mcp_websocket.open:
            try: 
                logger.debug(f"Closing existing open WebSocket (id={id(self.mcp_websocket)}) before {action_str.lower()}.")
                await self.mcp_websocket.close(code=1000, reason="Client initiated reconnect")
            except WebSocketException as e_close_old: logger.debug(f"Error closing old websocket: {e_close_old}")
        self.mcp_websocket = None
        
        try:
            new_ws: WebSocketClientProtocol = await websockets.connect(
                self.gateway_url, open_timeout=10, close_timeout=5,
                ping_interval=20, ping_timeout=20
            )
            self.mcp_websocket = new_ws
            logger.info(f"Successfully established new WebSocket connection (id={id(self.mcp_websocket)}).")
            await self._start_response_listener()
            
            try:
                hello_resp = await self.send_mcp_request(None, "mcp.hello", [])
                if hello_resp and "result" in hello_resp and isinstance(hello_resp["result"], list):
                    self.available_mcp_commands = sorted(list(set(hello_resp["result"])))
                    logger.debug(f"Fetched {len(self.available_mcp_commands)} MCP commands.")
                else:
                    logger.warning(f"Failed to get/parse command list from mcp.hello: {str(hello_resp)[:200]}")
                    self.available_mcp_commands = []
            except Exception as e_hello:
                logger.error(f"Error calling mcp.hello on connect: {e_hello}", exc_info=True)
                self.available_mcp_commands = []
            return True
        except ConnectionRefusedError: logger.error(f"Connection refused by Gateway at {self.gateway_url}.")
        except asyncio.TimeoutError: logger.error(f"Timeout connecting to Gateway at {self.gateway_url}.")
        except WebSocketException as e_ws_conn_main: logger.error(f"WebSocket connection failure to {self.gateway_url}: {e_ws_conn_main}")
        except Exception as e_conn_main_other: logger.error(f"Failed to connect to Gateway at {self.gateway_url}: {e_conn_main_other}", exc_info=True)
        
        if self.mcp_websocket and self.mcp_websocket.open:
            try: await self.mcp_websocket.close()
            except: pass
        self.mcp_websocket = None
        await self._cancel_existing_listener()
        return False

    async def send_mcp_request(
        self, request_id_override: Optional[str], method: str, params: List[Any], timeout: float = 20.0
    ) -> Optional[Dict[str, Any]]:
        if self.is_shutting_down: 
            return {"jsonrpc": "2.0", "id": request_id_override, "error": {"code": -32000, "message": "Shell is shutting down."}}
        
        if not self._is_websocket_open():
            logger.warning(f"send_mcp_request: No active connection for '{method}'. Attempting to connect...")
            if not await self.ensure_connection():
                 return {"jsonrpc": "2.0", "id": request_id_override, "error": {"code": -32003, "message": "Gateway connection failed."}}
            if not self._is_websocket_open():
                 return {"jsonrpc": "2.0", "id": request_id_override, "error": {"code": -32003, "message": "Gateway connection still unavailable."}}

        req_id = request_id_override if request_id_override is not None else str(uuid.uuid4())
        payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": req_id}
        
        current_loop = asyncio.get_running_loop()
        future: asyncio.Future = current_loop.create_future()
        self.pending_responses[req_id] = future

        try:
            logger.debug(f"ShellApp SEND (ID {req_id}): {method} {str(params)[:100]}...")
            if not self.mcp_websocket: raise ConnectionError("WebSocket is None before send.")
            await self.mcp_websocket.send(json.dumps(payload))
            response = await asyncio.wait_for(future, timeout=timeout)
            return response
        except asyncio.TimeoutError:
            logger.error(f"Timeout (ID {req_id}) for {method}.")
            popped_future = self.pending_responses.pop(req_id, None)
            if popped_future and not popped_future.done(): popped_future.cancel()
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32000, "message": "Request timed out."}}
        except (ConnectionClosed, ConnectionClosedOK, WebSocketException) as e_ws_send_err: 
            logger.error(f"Connection error during send/wait for {method} (ID {req_id}): {e_ws_send_err}")
            self.pending_responses.pop(req_id, None)
            # Vérifier si l'objet websocket est l'instance qui a causé l'erreur si l'exception le fournit
            ws_instance_from_exc = getattr(e_ws_send_err, 'ws_client', getattr(e_ws_send_err, 'protocol', None))
            if self.mcp_websocket and self.mcp_websocket == ws_instance_from_exc :
                 try: await self.mcp_websocket.close()
                 except: pass
                 self.mcp_websocket = None 
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32001, "message": f"Gateway connection error: {e_ws_send_err}"}}
        except Exception as e_send_req:
            logger.error(f"Error sending MCP request {method} (ID {req_id}): {e_send_req}", exc_info=True)
            self.pending_responses.pop(req_id, None)
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32002, "message": f"Shell client error sending request: {e_send_req}"}}

    async def handle_command_line(self, command_line_str: str):
        if not command_line_str.strip(): return
        try: parts = shlex.split(command_line_str)
        except ValueError as e_shlex: self.console.print(f"[[error]Parsing error[/]]: {e_shlex}"); return
        
        cmd_name = parts[0]; cmd_args_list = parts[1:]

        if hasattr(builtin_cmds, f"cmd_{cmd_name}"):
            handler = getattr(builtin_cmds, f"cmd_{cmd_name}")
            try: await handler(cmd_args_list, self)
            except Exception as e_builtin: logger.error(f"Error in builtin '{cmd_name}': {e_builtin}", exc_info=True); self.console.print(f"[[error]Error in '{cmd_name}'[/]]: {e_builtin}")
            return

        mcp_full_method = cmd_name; parsed_params: List[Any] = []
        for arg_s in cmd_args_list:
            try: parsed_params.append(json.loads(arg_s))
            except json.JSONDecodeError:
                if mcp_full_method.startswith("mcp.fs.") and not arg_s.startswith("/"):
                    abs_path = (self.get_cwd() / os.path.expanduser(arg_s)).resolve()
                    parsed_params.append(str(abs_path))
                else: parsed_params.append(arg_s)
        
        if mcp_full_method == "mcp.llm.chat": 
            self.console.print(Text("Use the 'llm' command for interactive chat streaming. Ex: llm \"Your prompt\"", style="yellow")); return

        response = await self.send_mcp_request(None, mcp_full_method, parsed_params)
        if response: 
            # Assumer que _rich_format_mcp_response est défini dans builtin_cmds
            # ou le déplacer dans ShellApp comme méthode _format_and_print_mcp_response
            if hasattr(builtin_cmds, '_rich_format_mcp_response'):
                await builtin_cmds._rich_format_mcp_response(self.console, mcp_full_method, response)
            else: # Fallback simple si la fonction de formatage n'est pas trouvée là
                self.console.print(response)


    async def run_repl(self):
        if not await self.ensure_connection(force_reconnect=True):
            self.console.print("[[error]Failed to connect[/]] to gateway on startup. Try 'connect' or check gateway.")

        class AppCompleter(Completer):
            def __init__(self, shell_app_instance: 'ShellApp'):
                self.shell_app = shell_app_instance
            def get_completions(self, document, complete_event):
                text_before = document.text_before_cursor.lstrip(); words = text_before.split()
                if not words or (len(words) == 1 and not text_before.endswith(' ')):
                    current_w = words[0] if words else ""
                    all_cmds = sorted(list(set(builtin_cmds.BUILTIN_COMMAND_LIST + self.shell_app.available_mcp_commands)))
                    for cmd_s in all_cmds:
                        if cmd_s.startswith(current_w): yield Completion(cmd_s, start_position=-len(current_w))

        pt_session = PromptSession(history=FileHistory(str(SHELL_HISTORY_FILE)),
                                   auto_suggest=AutoSuggestFromHistory(),
                                   completer=AppCompleter(self), style=self.prompt_style, enable_suspend=True)
        
        while not self.is_shutting_down:
            try:
                path_disp = str(self.get_cwd()); home_str = str(Path.home())
                if path_disp.startswith(home_str) and path_disp != home_str : path_disp = "~" + path_disp[len(home_str):]
                
                prompt_list = [('class:path', f"{path_disp} "), ('class:prompt', 'luca> ')]
                if not self._is_websocket_open():
                     prompt_list.insert(0, ('class:disconnected', "[Disconnected] "))
                
                cmd_line_str = await pt_session.prompt_async(prompt_list)
                await self.handle_command_line(cmd_line_str)
            except KeyboardInterrupt: self.console.print() ; continue
            except EOFError: self.console.print("Exiting luca-shell (EOF)..."); break
            except Exception as e_repl_loop:
                logger.critical(f"Critical error in REPL loop: {e_repl_loop}", exc_info=True)
                self.console.print(f"[[error]REPL Error[/]]: {e_repl_loop}.")
        
        await self.shutdown()

    async def shutdown(self):
        if self.is_shutting_down: return
        self.is_shutting_down = True
        logger.info("ShellApp shutting down...")
        await self._cancel_existing_listener()
        
        if self.mcp_websocket and self.mcp_websocket.open:
            logger.info("Closing WebSocket connection to gateway...");
            try: await self.mcp_websocket.close(code=1000, reason="Client shutdown")
            except Exception as e_ws_close: logger.debug(f"Exception closing websocket on shutdown: {e_ws_close}")
        self.mcp_websocket = None
        logger.info("ShellApp shutdown complete.")

# --- Point d'Entrée Principal ---
if __name__ == "__main__":
    app = ShellApp(GATEWAY_WS_URL_CONF, console)
    main_event_loop = asyncio.get_event_loop()
    
    should_exit_event = asyncio.Event()
    def signal_handler_main(sig, frame): # Renommé pour éviter conflit si un autre module définit signal_handler
        logger.info(f"Signal {signal.Signals(sig).name} received by main, setting shutdown event...")
        if not main_event_loop.is_closed(): # S'assurer que la boucle n'est pas déjà fermée
            main_event_loop.call_soon_threadsafe(should_exit_event.set)

    signal.signal(signal.SIGINT, signal_handler_main)
    signal.signal(signal.SIGTERM, signal_handler_main)

    async def main_with_shutdown_wrapper():
        repl_task = main_event_loop.create_task(app.run_repl())
        shutdown_task = main_event_loop.create_task(should_exit_event.wait())
        
        done, pending = await asyncio.wait([repl_task, shutdown_task], return_when=asyncio.FIRST_COMPLETED)
        
        if shutdown_task in done:
            logger.info("Shutdown event was set, cancelling REPL task.")
            if not repl_task.done():
                repl_task.cancel()
                try: await repl_task
                except asyncio.CancelledError: logger.info("REPL task cancelled due to shutdown signal.")
        
        if not app.is_shutting_down: # Assurer que shutdown est appelé si REPL finit autrement
            await app.shutdown()

    try:
        main_event_loop.run_until_complete(main_with_shutdown_wrapper())
    except Exception as e_shell_main_exc:
        logger.critical(f"Luca Shell (main) crashed unexpectedly: {e_shell_main_exc}", exc_info=True)
        console.print(f"[[error]Shell crashed fatally[/]]: {e_shell_main_exc}")
    finally:
        logger.info("Luca Shell (main) final cleanup starting...")
        # S'assurer que app.shutdown est appelé si ce n'est pas déjà fait.
        # C'est un peu redondant avec la logique dans main_with_shutdown_wrapper, mais pour être sûr.
        if hasattr(app, 'is_shutting_down') and not app.is_shutting_down:
            logger.info("Running app.shutdown() in final finally block.")
            if not main_event_loop.is_closed():
                try: main_event_loop.run_until_complete(app.shutdown())
                except RuntimeError as e_loop_issue: # Peut arriver si la boucle est en train de se fermer
                     logger.warning(f"Could not run app.shutdown in main_event_loop: {e_loop_issue}. Trying asyncio.run.")
                     try: asyncio.run(app.shutdown()) # Nouvelle boucle temporaire pour shutdown
                     except Exception as e_final_shutdown_run: logger.error(f"asyncio.run(app.shutdown) also failed: {e_final_shutdown_run}")
            else:
                 logger.warning("Main event loop was closed before final shutdown could complete.")

        logger.info("Luca Shell (main) process finished.")