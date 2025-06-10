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
from typing import Any, Dict, Optional, List, Callable, Awaitable, Set, Tuple # For type hints
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
from datetime import datetime # Pour le formatage dans _rich_format_mcp_fs_list
from rich.markup import escape # Pour échapper les messages d'erreur

# --- Import des modules locaux ---
from . import builtin_cmds 
# stream_llm_chat_to_console est utilisé par builtin_cmds.cmd_llm, pas directement ici.

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
            "websockets.client": {"handlers": ["console_stderr"], "level": "WARNING", "propagate": False},
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
        self.active_streams: Dict[str, asyncio.Queue] = {} # Initialisé ici
        self.available_mcp_commands: List[str] = []
        self.response_listener_task: Optional[asyncio.Task] = None
        self.cwd_state: Path = Path("/") # Représente la racine virtuelle du FS
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

                    if response_id in self.active_streams:
                        queue = self.active_streams[response_id]
                        try: await queue.put(response)
                        except Exception as e_put_q: logger.error(f"Error putting message for stream {response_id} into queue: {e_put_q}")
                        continue 

                    future = self.pending_responses.pop(response_id, None)
                    if future:
                        if not future.done(): future.set_result(response)
                        else: logger.warning(f"Listener: Future for ID {response_id} already done. Ignored.")
                    else:
                        logger.warning(f"Listener: Rcvd response for unknown/non-stream ID: {response_id}. Data: {str(response)[:100]}")
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
            for req_id, q in list(self.active_streams.items()):
                try: await q.put(RuntimeError("Gateway connection lost (listener ended)."))
                except Exception: pass
            self.active_streams.clear()

    async def start_mcp_stream_request(
        self, method: str, params: List[Any]
    ) -> Tuple[Optional[str], Optional[asyncio.Queue]]:
        if self.is_shutting_down or not self._is_websocket_open():
            if not await self.ensure_connection():
                self.console.print("[[error]Cannot start stream[/]]: Gateway connection failed.")
                return None, None
            if not self._is_websocket_open():
                self.console.print("[[error]Cannot start stream[/]]: Gateway connection still unavailable.")
                return None, None
        
        request_id = str(uuid.uuid4())
        payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": request_id}
        
        stream_queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self.active_streams[request_id] = stream_queue
        logger.info(f"Stream queue created for request ID {request_id}")

        try:
            if not self.mcp_websocket: raise ConnectionError("WebSocket is None before send.")
            await self.mcp_websocket.send(json.dumps(payload))
            logger.debug(f"Stream request {request_id} ({method}) sent.")
            return request_id, stream_queue
        except Exception as e:
            logger.error(f"Failed to send stream request {request_id} ({method}): {e}", exc_info=True)
            self.active_streams.pop(request_id, None)
            return None, None

    async def ensure_connection(self, force_reconnect: bool = False) -> bool:
        if self.is_shutting_down: return False
        if not force_reconnect and self._is_websocket_open(): return True
        
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
        self.mcp_websocket = None; await self._cancel_existing_listener()
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
        
        current_loop = asyncio.get_running_loop(); future: asyncio.Future = current_loop.create_future()
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
            ws_instance_from_exc = getattr(e_ws_send_err, 'ws_client', getattr(e_ws_send_err, 'protocol', self.mcp_websocket))
            if self.mcp_websocket and self.mcp_websocket == ws_instance_from_exc :
                 try: await self.mcp_websocket.close()
                 except: pass
                 self.mcp_websocket = None 
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32001, "message": f"Gateway connection error: {e_ws_send_err}"}}
        except Exception as e_send_req:
            logger.error(f"Error sending MCP request {method} (ID {req_id}): {e_send_req}", exc_info=True)
            self.pending_responses.pop(req_id, None)
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32002, "message": f"Shell client error sending request: {e_send_req}"}}

    async def _rich_format_mcp_fs_list(self, result: List[Dict[str,Any]], request_path: str):
        # S'assurer que Table est importé ici ou est un attribut de self.console si elle vient de Rich
        from rich.table import Table # Import local pour être sûr
        table = Table(show_header=True, header_style="bold cyan", title=f"Contents of {escape(request_path) if request_path else 'directory'}")
        table.add_column("Type", width=10); table.add_column("Size", justify="right", width=10)
        table.add_column("Modified", width=20); table.add_column("Name")
        for item in sorted(result, key=lambda x: (x.get('type') != 'directory', str(x.get('name','')).lower())):
            item_type = item.get("type", "other")
            color = "blue" if item_type == "directory" else "green" if item_type == "file" else "magenta" if item_type == "symlink" else "bright_black"
            size_val = item.get("size", -1); size_str = ""
            if item_type == "directory": size_str = "[DIR]"
            elif isinstance(size_val, int) and size_val >= 0:
                num = float(size_val); units = ["B", "KB", "MB", "GB", "TB"]; i = 0
                while num >= 1024 and i < len(units) - 1: num /= 1024.0; i += 1
                size_str = f"{num:.1f}{units[i]}" if i > 0 else f"{int(num)}{units[i]}"
            else: size_str = "N/A"
            mod_iso = item.get("modified_at", "")
            mod_display = datetime.fromisoformat(mod_iso.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M:%S") if mod_iso else "N/A"
            table.add_row(Text(item_type, style=color), size_str, mod_display, Text(escape(item.get("name", "?")), style=color))
        self.console.print(table)

    async def _rich_format_mcp_fs_read(self, result: Dict[str,Any]):
        content = result.get("content", ""); encoding = result.get("encoding"); mime_type = result.get("mime_type", "application/octet-stream")
        if encoding == "text":
            lexer = "text"; simple_mime = mime_type.split('/')[-1].split('+')[0].lower()
            known_lexers = ["json", "xml", "python", "markdown", "html", "css", "javascript", "yaml", "c", "cpp", "java", "go", "rust", "php", "ruby", "perl", "sql", "ini", "toml", "diff", "dockerfile"]
            shell_lexers = ["bash", "sh", "zsh", "fish", "powershell", "batch"]
            if simple_mime in known_lexers: lexer = simple_mime
            elif simple_mime in ["x-yaml", "vnd.yaml"]: lexer = "yaml"
            elif simple_mime in ["x-python", "x-python3"]: lexer = "python"
            elif simple_mime in ["x-shellscript", "x-sh", "x-bash"]: lexer = "bash"
            elif simple_mime in shell_lexers : lexer = simple_mime
            self.console.print(Syntax(content, lexer, theme="native", line_numbers=True, word_wrap=True, background_color="default"))
        elif encoding == "base64": self.console.print(f"[yellow]Base64 (MIME: {mime_type}):[/yellow]\n{escape(content[:500])}{'...' if len(content)>500 else ''}")
        else: self.console.print(escape(str(result)))

    async def _format_and_print_mcp_response(self, mcp_method: str, response: Optional[Dict[str,Any]], request_path_for_ls: Optional[str] = None):
        if not response: self.console.print("[[error]No response or connection failed.[/]]"); return
        if "error" in response:
            err = response["error"]
            self.console.print(f"[[error]MCP Error (Code {err.get('code')})[/]]: {escape(str(err.get('message')))}")
            if "data" in err: self.console.print(Syntax(json.dumps(err['data'],indent=2),"json",theme="native",background_color="default"))
        elif "result" in response:
            result = response["result"]
            if mcp_method == "mcp.fs.list" and isinstance(result, list):
                await self._rich_format_mcp_fs_list(result, request_path_for_ls or "current directory")
            elif mcp_method == "mcp.fs.read" and isinstance(result, dict):
                await self._rich_format_mcp_fs_read(result)
            elif isinstance(result, (dict, list)):
                self.console.print(Syntax(json.dumps(result,indent=2),"json",theme="native",line_numbers=True,background_color="default"))
            else: self.console.print(escape(str(result)))
        else: self.console.print(Syntax(json.dumps(response,indent=2),"json",theme="native",background_color="default"))

    async def handle_command_line(self, command_line_str_raw: str):
        logger.info(f"SHELL RCV RAW: '{command_line_str_raw}'")
        path_disp = str(self.get_cwd()); home_str = str(Path.home())
        if path_disp.startswith(home_str) and path_disp != home_str : path_disp = "~" + path_disp[len(home_str):]
        prompt_connected_str = f"{path_disp} luca> "; prompt_disconnected_str = f"[Disconnected] {path_disp} luca> "
        command_line_str = command_line_str_raw; stripped_prompt_prefix = "" # Pour le log
        
        if command_line_str_raw.startswith(prompt_disconnected_str):
            stripped_prompt_prefix = prompt_disconnected_str
            command_line_str = command_line_str_raw[len(prompt_disconnected_str):].lstrip()
        elif command_line_str_raw.startswith(prompt_connected_str):
            stripped_prompt_prefix = prompt_connected_str
            command_line_str = command_line_str_raw[len(prompt_connected_str):].lstrip()
        
        if stripped_prompt_prefix: logger.info(f"SHELL STRIPPED CMD: '{command_line_str}' (using prefix '{stripped_prompt_prefix}')")
        else: # Si aucun prompt standard n'est trouvé, on lstrip juste
             command_line_str = command_line_str_raw.lstrip()
             if command_line_str_raw != command_line_str: logger.info(f"SHELL STRIPPED (no specific prompt): '{command_line_str}'")

        if not command_line_str.strip(): logger.debug("Command line empty after strip."); return
        try: parts = shlex.split(command_line_str)
        except ValueError as e_shlex: self.console.print(f"[[error]Parsing error[/]]: {escape(str(e_shlex))}"); return
        if not parts: logger.debug("Empty command after shlex.split."); return

        cmd_name = parts[0]; cmd_args_list = parts[1:]
        logger.info(f"SHELL FINAL CMD_NAME: '{cmd_name}', ARGS: {cmd_args_list}")

        if hasattr(builtin_cmds, f"cmd_{cmd_name}"):
            handler = getattr(builtin_cmds, f"cmd_{cmd_name}")
            try: await handler(cmd_args_list, self)
            except Exception as e_builtin: logger.error(f"Error in builtin '{cmd_name}': {e_builtin}", exc_info=True); self.console.print(f"[[error]Error in '{cmd_name}'[/]]: {escape(str(e_builtin))}")
            return

        # Dans llmbasedos_src/shell/luca.py, méthode ShellApp.handle_command_line

        # ... (après la section `if hasattr(builtin_cmds, f"cmd_{cmd_name}"): ... return`) ...

        mcp_full_method = cmd_name
        # parsed_params peut être une liste ou un dictionnaire pour JSON-RPC.
        # Les schémas de vos capacités attendent principalement des listes.
        parsed_params: Union[List[Any], Dict[str, Any]]
        request_path_for_ls: Optional[str] = None # Spécifiquement pour l'affichage de mcp.fs.list

        if not cmd_args_list:
            # Cas 1: Commande MCP directe sans arguments (ex: 'mcp.hello')
            # Ou comportement par défaut pour certaines commandes si aucun argument n'est donné.
            if mcp_full_method == "mcp.fs.list":
                # Pour 'mcp.fs.list' sans args, utiliser le CWD virtuel du shell
                current_virtual_cwd = str(self.get_cwd())
                # Normaliser le CWD virtuel pour qu'il soit toujours un chemin absolu virtuel
                path_to_send_str = current_virtual_cwd
                if path_to_send_str == ".": path_to_send_str = "/" # Si CWD est / et on fait "ls ."
                if not path_to_send_str.startswith("/"): path_to_send_str = "/" + path_to_send_str
                
                parsed_params = [path_to_send_str]
                request_path_for_ls = path_to_send_str # Pour l'affichage du titre de la table Rich
                logger.info(f"SHELL: MCP method '{mcp_full_method}' called with no args, defaulting params to CWD: {parsed_params}")
            else:
                # Pour les autres méthodes MCP sans args (comme mcp.hello), envoyer une liste vide
                parsed_params = []
                logger.info(f"SHELL: MCP method '{mcp_full_method}' called with no args, sending empty params: {parsed_params}")

        elif len(cmd_args_list) == 1:
            # Cas 2: Commande MCP directe avec UN seul argument.
            # Cet argument DOIT être une chaîne JSON valide représentant TOUS les paramètres.
            # Ex: mcp.fs.list '["/path", {"option": true}]'
            param_json_string = cmd_args_list[0]
            try:
                loaded_json_params = json.loads(param_json_string)
                
                if not isinstance(loaded_json_params, (list, dict)):
                    self.console.print(
                        Text(f"MCP Error: Parameters for '{escape(mcp_full_method)}' must be a JSON array or object. ", style="error") +
                        Text(f"You provided: '{escape(param_json_string)}', which parsed to type: {type(loaded_json_params).__name__}", style="yellow")
                    )
                    return
                parsed_params = loaded_json_params
                
                # Si c'est mcp.fs.list, et que le premier paramètre est une chaîne (le chemin)
                if mcp_full_method == "mcp.fs.list" and isinstance(parsed_params, list) and parsed_params and isinstance(parsed_params[0], str):
                    request_path_for_ls = parsed_params[0]
                    # On pourrait ajouter une validation ici pour s'assurer que parsed_params[0] commence par "/"
                    # if not request_path_for_ls.startswith("/"):
                    #    self.console.print(Text(f"Warning: Path '{request_path_for_ls}' for mcp.fs.list should be a virtual absolute path (start with '/').", style="yellow"))
                    
            except json.JSONDecodeError:
                self.console.print(
                    Text(f"MCP Error: Invalid JSON for parameters of '{escape(mcp_full_method)}'.\n", style="error") +
                    Text(f"Could not parse: '{escape(param_json_string)}'\n", style="yellow") +
                    Text(f"Ensure parameters are a single, valid JSON string (e.g., '[\"/some/path\"]' or '{{\"key\": \"value\"}}').", style="italic")
                )
                return
        else: # len(cmd_args_list) > 1
            # Cas 3: Commande MCP directe avec PLUSIEURS arguments séparés par des espaces.
            # Ce n'est pas la syntaxe attendue pour les paramètres JSON-RPC.
            self.console.print(
                Text(f"MCP Syntax Error: For method '{escape(mcp_full_method)}', provide all parameters as a single JSON string argument.\n", style="error") +
                Text(f"Example: {escape(mcp_full_method)} '[param1, param2, {{\"option\": true}}]'", style="italic")
            )
            return

        # Log final des paramètres parsés avant envoi
        logger.info(f"SHELL: Sending MCP method '{mcp_full_method}' with parsed_params: {parsed_params}")

        # Exclure la commande 'llm' des appels MCP directs car elle a un traitement spécial de streaming
        if mcp_full_method == "mcp.llm.chat": 
            self.console.print(Text("Please use the 'llm' built-in command for interactive chat streaming.", style="yellow"))
            self.console.print(Text("Example: llm \"Your prompt here\"", style="italic"))
            return

        # Envoyer la requête MCP
        response = await self.send_mcp_request(None, mcp_full_method, parsed_params)
        await self._format_and_print_mcp_response(mcp_full_method, response, request_path_for_ls=request_path_for_ls)

    async def run_repl(self):
        if not await self.ensure_connection(force_reconnect=True):
            self.console.print("[[error]Failed to connect[/]] to gateway on startup. Try 'connect' or check gateway.")

        class AppCompleter(Completer):
            def __init__(self, shell_app_instance: 'ShellApp'): self.shell_app = shell_app_instance
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
                
                prompt_list_parts = [('class:path', f"{path_disp} "), ('class:prompt', 'luca> ')]
                if not self._is_websocket_open(): prompt_list_parts.insert(0, ('class:disconnected', "[Disconnected] "))
                
                cmd_line_str = await pt_session.prompt_async(prompt_list_parts)
                await self.handle_command_line(cmd_line_str)
            except KeyboardInterrupt: self.console.print() ; continue
            except EOFError: self.console.print("Exiting luca-shell (EOF)..."); break
            except Exception as e_repl_loop:
                logger.critical(f"Critical error in REPL loop: {e_repl_loop}", exc_info=True)
                self.console.print(f"[[error]REPL Error[/]]: {escape(str(e_repl_loop))}.")
        
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
    
    _should_exit_main_event = asyncio.Event()
    def _main_signal_handler(sig, frame):
        logger.info(f"Signal {signal.Signals(sig).name} received by main, setting shutdown event...")
        if not main_event_loop.is_closed():
            main_event_loop.call_soon_threadsafe(_should_exit_main_event.set)

    if os.name == 'posix':
        signal.signal(signal.SIGINT, _main_signal_handler)
        signal.signal(signal.SIGTERM, _main_signal_handler)
    else: 
        logger.info("Signal handlers for SIGINT/SIGTERM not set (non-POSIX OS). Relying on KeyboardInterrupt/EOFError.")

    async def main_with_shutdown_wrapper():
        repl_task = main_event_loop.create_task(app.run_repl())
        shutdown_signal_task = main_event_loop.create_task(_should_exit_main_event.wait())
        done, pending = await asyncio.wait([repl_task, shutdown_signal_task], return_when=asyncio.FIRST_COMPLETED)
        if shutdown_signal_task in done:
            logger.info("Shutdown event set, cancelling REPL task.")
            if not repl_task.done(): repl_task.cancel(); await asyncio.gather(repl_task, return_exceptions=True)
        if not app.is_shutting_down: await app.shutdown()

    try:
        main_event_loop.run_until_complete(main_with_shutdown_wrapper())
    except Exception as e_shell_main_exc:
        logger.critical(f"Luca Shell (main) crashed OUTSIDE REPL: {e_shell_main_exc}", exc_info=True)
        console.print(f"[[error]Shell crashed fatally[/]]: {escape(str(e_shell_main_exc))}")
    finally:
        logger.info("Luca Shell (main) final cleanup starting...")
        if hasattr(app, 'is_shutting_down') and not app.is_shutting_down:
            logger.info("Running app.shutdown() in final finally block.")
            # ... (logique de fermeture de boucle affinée) ...
        logger.info("Luca Shell (main) process finished.")