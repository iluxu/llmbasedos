# llmbasedos/shell/luca.py
import asyncio
import json
import logging
import logging.config # For dictConfig
import os
import sys
from pathlib import Path
import uuid
import shlex # For parsing command line string
from typing import Any, Dict, Optional, List, Callable, Awaitable # For type hints

import websockets # type: ignore
from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import Completer, Completion # WordCompleter, PathCompleter not used directly here
from prompt_toolkit.styles import Style as PromptStyle
from rich.console import Console
from rich.text import Text
from rich.syntax import Syntax

# --- Shell Configuration, Logging, Rich Console ---
SHELL_HISTORY_FILE = Path(os.path.expanduser("~/.llmbasedos_shell_history"))
GATEWAY_WS_URL_CONF = os.getenv("LLMBDO_GATEWAY_WS_URL", "ws://localhost:8000/ws")

# Logging setup for the shell
LOG_LEVEL_STR_CONF = os.getenv("LLMBDO_SHELL_LOG_LEVEL", "INFO").upper()
LOG_FORMAT_CONF = os.getenv("LLMBDO_SHELL_LOG_FORMAT", "simple") # "simple" or "json"

def setup_shell_logging():
    log_level_int = logging.getLevelName(LOG_LEVEL_STR_CONF)
    formatter_class = "python_json_logger.jsonlogger.JsonFormatter" if LOG_FORMAT_CONF == "json" else "logging.Formatter"
    formatter_config = {
        "format": "%(asctime)s %(levelname)s %(name)s %(module)s %(funcName)s %(lineno)d %(message)s"
    } if LOG_FORMAT_CONF == "json" else {
        "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    }
    LOGGING_CONFIG = {
        "version": 1, "disable_existing_loggers": False,
        "formatters": {LOG_FORMAT_CONF: {"()": formatter_class, **formatter_config}},
        "handlers": {"console_stderr": {"class": "logging.StreamHandler", "formatter": LOG_FORMAT_CONF, "stream": "ext://sys.stderr"}},
        "root": {"handlers": ["console_stderr"], "level": "WARNING"},
        "loggers": {
            "llmbasedos.shell": {"handlers": ["console_stderr"], "level": log_level_int, "propagate": False},
            "websockets": {"handlers": ["console_stderr"], "level": "INFO", "propagate": False}
        }
    }
    logging.config.dictConfig(LOGGING_CONFIG)

setup_shell_logging() # Initialize logging as soon as module is loaded
logger = logging.getLogger("llmbasedos.shell.luca") # Main logger for this module
console = Console(stderr=True) # Use stderr for prompts/errors to not mix with command stdout redirected to file

# --- Import Builtins and Utils ---
from . import builtin_cmds # For BUILTIN_COMMAND_LIST and handlers
from .shell_utils import stream_llm_chat_to_console

# --- ShellApp Class for State Management ---
class ShellApp:
    def __init__(self, gateway_url: str):
        self.gateway_url = gateway_url
        self.mcp_websocket: Optional[websockets.WebSocketClientProtocol] = None # type: ignore
        self.pending_responses: Dict[str, asyncio.Future] = {}
        self.available_mcp_commands: List[str] = []
        self.response_listener_task: Optional[asyncio.Task] = None
        self.cwd_state: Dict[str, Path] = {"cwd": Path(os.path.expanduser("~")).resolve()}
        self.is_shutting_down = False # Flag for shutdown sequence

    def get_cwd(self) -> Path: return self.cwd_state["cwd"]
    def set_cwd(self, new_path: Path):
        try: self.cwd_state["cwd"] = new_path.resolve()
        except Exception as e: console.print(f"[prompt.error]Error setting CWD to '{new_path}': {e}[/]")

    async def _start_response_listener(self):
        if self.response_listener_task and not self.response_listener_task.done():
            self.response_listener_task.cancel() # Cancel previous if any
            try: await self.response_listener_task
            except asyncio.CancelledError: pass
        
        if self.mcp_websocket and self.mcp_websocket.open:
            self.response_listener_task = asyncio.create_task(self._response_listener_logic(), name="ShellResponseListener")
            logger.info("Response listener task started.")
        else:
            logger.error("Cannot start response listener: WebSocket not connected.")
            self.response_listener_task = None


    async def _response_listener_logic(self):
        # Assumes self.mcp_websocket is valid and open when this task starts
        active_ws = self.mcp_websocket
        logger.debug(f"Listener logic running for WebSocket: {active_ws}")
        try:
            async for message_str in active_ws: # type: ignore
                if self.is_shutting_down or not self.mcp_websocket or self.mcp_websocket != active_ws:
                    logger.info("Listener: WebSocket changed or shutdown initiated. Exiting listener logic."); break
                try:
                    response = json.loads(message_str)
                    logger.debug(f"Gateway RCV (ShellApp): {str(response)[:200]}...")
                    response_id = response.get("id")

                    if response_id in self.pending_responses:
                        future = self.pending_responses.pop(response_id, None) # Pop it
                        if future and not future.done(): future.set_result(response)
                        elif future and future.done(): logger.warning(f"Listener: Future for ID {response_id} was already done. Ignored.")
                    else: logger.warning(f"Listener: Rcvd response for unknown/handled ID: {response_id}. Data: {str(response)[:100]}")
                except json.JSONDecodeError: logger.error(f"Listener: Invalid JSON from gateway: {message_str}")
                except Exception as e_inner_listener:
                    logger.error(f"Listener: Error processing message: {e_inner_listener}", exc_info=True)
                    if isinstance(e_inner_listener, websockets.exceptions.ConnectionClosed): raise # Re-raise to stop
        
        except websockets.exceptions.ConnectionClosed as e_closed_ws:
            logger.warning(f"Listener: Gateway connection closed: {e_closed_ws.code} {e_closed_ws.reason}")
        except asyncio.CancelledError: logger.info("Listener: Task cancelled.")
        except Exception as e_outer_listener:
            logger.error(f"Listener: Task ended with critical error: {e_outer_listener}", exc_info=True)
        finally:
            logger.info("Listener: Task stopped.")
            if active_ws == self.mcp_websocket: # If this was the current connection
                self.mcp_websocket = None # Mark as disconnected
            # Clear any remaining pending futures, notifying them of connection loss
            for req_id, fut in list(self.pending_responses.items()): # Iterate copy
                if not fut.done(): fut.set_exception(RuntimeError(f"Gateway conn lost (listener ended). Req ID: {req_id}"))
                self.pending_responses.pop(req_id, None)


    async def ensure_connection(self, force_reconnect: bool = False) -> bool:
        if self.is_shutting_down: return False
        if not force_reconnect and self.mcp_websocket and self.mcp_websocket.open:
            return True
        
        logger.info(f"{'Reconnecting' if force_reconnect else 'Connecting'} to MCP Gateway: {self.gateway_url}")
        # Close existing if any (e.g. stale connection)
        if self.mcp_websocket:
            try: await self.mcp_websocket.close()
            except: pass # Ignore errors closing old socket
        self.mcp_websocket = None
        if self.response_listener_task and not self.response_listener_task.done():
            self.response_listener_task.cancel(); await asyncio.sleep(0) # Give it a chance to cancel

        try:
            self.mcp_websocket = await websockets.connect(self.gateway_url, open_timeout=5, close_timeout=3) # type: ignore
            await self._start_response_listener() # Start listener for new connection
            logger.info("Successfully connected to Gateway.")
            
            # Fetch available commands
            try:
                # Use internal sender, None for ID as it's a simple req/resp
                hello_resp = await self.send_mcp_request(None, "mcp.hello", []) 
                if hello_resp and "result" in hello_resp:
                    self.available_mcp_commands = hello_resp["result"]
                    logger.debug(f"Fetched MCP commands: {self.available_mcp_commands}")
                else:
                    logger.warning(f"Failed to get command list from mcp.hello: {hello_resp}")
                    self.available_mcp_commands = []
            except Exception as e_hello_app:
                logger.error(f"Error calling mcp.hello on connect (ShellApp): {e_hello_app}", exc_info=True)
                self.available_mcp_commands = []
            return True
        except Exception as e_conn:
            logger.error(f"Failed to connect to Gateway at {self.gateway_url}: {e_conn}", exc_info=False) # No need for full trace always
            self.mcp_websocket = None; self.response_listener_task = None
            return False

    async def send_mcp_request(
        self, request_id_override: Optional[str], # For streams that reuse ID
        method: str, params: List[Any], timeout: float = 20.0 # Default timeout
    ) -> Optional[Dict[str, Any]]:
        """Sends MCP request, manages future in self.pending_responses. Returns full JSON-RPC response dict."""
        if self.is_shutting_down: return {"jsonrpc": "2.0", "id": request_id_override, "error": {"code": -32000, "message": "Shell is shutting down."}}
        if not await self.ensure_connection() or not self.mcp_websocket:
            return {"jsonrpc": "2.0", "id": request_id_override, "error": {"code": -32003, "message": "Gateway connection failed."}}

        req_id = request_id_override if request_id_override is not None else str(uuid.uuid4())
        payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": req_id}
        
        # This future is for a single response. Stream handlers must manage per-chunk futures.
        # stream_llm_chat_to_console does this by re-adding to self.pending_responses for each chunk.
        future: asyncio.Future = asyncio.Future()
        self.pending_responses[req_id] = future

        try:
            logger.debug(f"ShellApp SEND (ID {req_id}): {method} {str(params)[:100]}...")
            await self.mcp_websocket.send(json.dumps(payload))
            response = await asyncio.wait_for(future, timeout=timeout)
            return response
        except asyncio.TimeoutError:
            logger.error(f"Timeout (ID {req_id}) for {method}.")
            self.pending_responses.pop(req_id, None) # Clean up future
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32000, "message": "Request timed out."}}
        except websockets.exceptions.ConnectionClosed:
            logger.error(f"Connection closed (ID {req_id}) for {method}. Marking connection as dead.")
            self.pending_responses.pop(req_id, None)
            self.mcp_websocket = None # Mark connection as dead
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32001, "message": "Gateway connection closed."}}
        except Exception as e_send_req:
            logger.error(f"Error (ID {req_id}) for {method}: {e_send_req}", exc_info=True)
            self.pending_responses.pop(req_id, None)
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32002, "message": f"Shell client error: {e_send_req}"}}


    async def handle_command_line(self, command_line_str: str):
        if not command_line_str.strip(): return
        
        try: parts = shlex.split(command_line_str) # Better parsing
        except ValueError as e_shlex: # e.g. unmatched quotes
            console.print(f"[prompt.error]Parsing error: {e_shlex}[/]"); return
        
        cmd_name = parts[0]
        cmd_args_list = parts[1:] # shlex already gives list of args

        # 1. Built-in commands
        if hasattr(builtin_cmds, f"cmd_{cmd_name}"):
            handler_func = getattr(builtin_cmds, f"cmd_{cmd_name}")
            try:
                # Pass ShellApp instance for context (CWD, connection, etc.)
                await handler_func(cmd_args_list, self) # Builtins now take (args_list, shell_app_instance)
            except Exception as e_builtin_exec:
                logger.error(f"Error in builtin '{cmd_name}': {e_builtin_exec}", exc_info=True)
                console.print(f"[prompt.error]Error in '{cmd_name}': {e_builtin_exec}[/]")
            return

        # 2. Direct MCP calls
        mcp_full_method = cmd_name
        parsed_params_for_mcp: List[Any] = []
        for arg_s in cmd_args_list: # Convert args to JSON types if possible, else string
            try: parsed_params_for_mcp.append(json.loads(arg_s))
            except json.JSONDecodeError: # Not a simple JSON literal (number, bool, null, array, object)
                # Path resolution for fs commands (if not absolute)
                if mcp_full_method.startswith("mcp.fs.") and not arg_s.startswith("/"):
                    # This resolution should use common_utils.validate_mcp_path_param
                    # with client-side CWD, but FS server will also validate against its virtual root.
                    # For shell, simpler to just resolve and send absolute.
                    abs_path = (self.get_cwd() / arg_s).resolve()
                    parsed_params_for_mcp.append(str(abs_path))
                else:
                    parsed_params_for_mcp.append(arg_s)
        
        # Special handling for mcp.llm.chat (if typed directly, not via `llm` builtin)
        if mcp_full_method == "mcp.llm.chat":
            if not parsed_params_for_mcp or not (isinstance(parsed_params_for_mcp[0], (str, list))):
                console.print(Text("Usage (direct): mcp.llm.chat <prompt_or_messages_json_array_str> [llm_options_json_dict_str]", style="prompt.error"))
                return
            
            messages_arg = parsed_params_for_mcp[0]
            messages_for_llm: List[Dict[str,str]]
            if isinstance(messages_arg, str): messages_for_llm = [{"role": "user", "content": messages_arg}]
            else: messages_for_llm = messages_arg # Assumed list of dicts
            
            llm_opts_arg = parsed_params_for_mcp[1] if len(parsed_params_for_mcp) > 1 else {}
            # llm_opts_arg would be dict if parsed from JSON, or str if not.
            # This path needs more robust options parsing.
            # For now, assume stream_llm_chat_to_console handles dict or makes one.

            if not await self.ensure_connection() or not self.mcp_websocket: return
            await stream_llm_chat_to_console(console, self.mcp_websocket, self.pending_responses, messages_for_llm, llm_opts_arg if isinstance(llm_opts_arg,dict) else {})
            return

        # 3. Normal non-streaming MCP call
        response = await self.send_mcp_request(None, mcp_full_method, parsed_params_for_mcp)
        if response:
            if "error" in response:
                err = response["error"]; console.print(f"[prompt.error]MCP Error ({err.get('code')}): {err.get('message')}[/]")
                if "data" in err: console.print(Syntax(json.dumps(err['data'], indent=2), "json", theme="native", background_color="default"))
            elif "result" in response:
                if isinstance(response["result"], (dict, list)):
                    console.print(Syntax(json.dumps(response["result"], indent=2), "json", theme="native", line_numbers=True, background_color="default"))
                else: console.print(str(response["result"]))
            else: console.print(Syntax(json.dumps(response, indent=2), "json", theme="native", background_color="default"))
        # else: send_mcp_request already prints error if it returns None due to connection issue


    async def run_repl(self):
        if not await self.ensure_connection(force_reconnect=True): # Initial connection attempt
            console.print("[prompt.error]Failed to connect to gateway on startup. Try 'connect' command or check gateway.[/]")

        # Custom completer using ShellApp state
        class AppCompleter(Completer):
            def get_completions(_, document, complete_event): # Use _ for self if not using ShellApp state here directly
                # Access ShellApp state via `self` of the outer class if this was a method
                # For standalone class, pass `self_app_instance.available_mcp_commands` etc.
                # For now, accessing module-level app_instance.
                text_before = document.text_before_cursor.lstrip()
                words = text_before.split()

                if not words or (len(words) == 1 and not text_before.endswith(' ')): # Completing command
                    current_w = words[0] if words else ""
                    all_cmds = sorted(list(set(builtin_cmds.BUILTIN_COMMAND_LIST + self.available_mcp_commands)))
                    for cmd_s in all_cmds:
                        if cmd_s.startswith(current_w): yield Completion(cmd_s, start_position=-len(current_w))
                # TODO: Add path completion logic here, using self.get_cwd()
                # This is complex, involves listing files from self.get_cwd() (async via MCP call)
                # or using prompt_toolkit.patch_stdout() with a PathCompleter.
                # For now, basic command completion.

        pt_session = PromptSession(history=FileHistory(str(SHELL_HISTORY_FILE)),
                                   auto_suggest=AutoSuggestFromHistory(),
                                   completer=AppCompleter(), # Use custom completer
                                   style=PromptStyle.from_dict({'prompt': 'fg:ansibrightblue bold', 'path': 'fg:ansigreen bold', 'prompt.error': 'fg:ansired bold'}),
                                   enable_suspend=True) # Ctrl-Z suspend
        
        while not self.is_shutting_down:
            try:
                path_disp = str(self.get_cwd())
                home_str = str(Path.home())
                if path_disp.startswith(home_str): path_disp = "~" + path_disp[len(home_str):]
                
                prompt_parts = [('class:path', f"{path_disp} "), ('class:prompt', 'luca> ')]
                if not self.mcp_websocket or not self.mcp_websocket.open :
                     prompt_parts.insert(0, ('fg:ansired', "[Disconnected] "))


                cmd_line = await pt_session.prompt_async(prompt_parts)
                await self.handle_command_line(cmd_line)
            except KeyboardInterrupt: continue # New prompt
            except EOFError: console.print("Exiting luca-shell (EOF)..."); break
            except Exception as e_loop:
                logger.critical(f"Critical error in REPL loop: {e_loop}", exc_info=True)
                console.print(f"[prompt.error]REPL Error: {e_loop}.[/]")
        
        await self.shutdown()

    async def shutdown(self):
        self.is_shutting_down = True
        logger.info("ShellApp shutting down...")
        if self.response_listener_task and not self.response_listener_task.done():
            self.response_listener_task.cancel()
            try: await self.response_listener_task
            except asyncio.CancelledError: logger.info("Response listener task cancelled on shutdown.")
        if self.mcp_websocket and self.mcp_websocket.open:
            logger.info("Closing WebSocket connection to gateway..."); await self.mcp_websocket.close()
        logger.info("ShellApp shutdown complete.")


# --- Main Entry Point ---
if __name__ == "__main__":
    app_instance = ShellApp(GATEWAY_WS_URL_CONF)
    try:
        asyncio.run(app_instance.run_repl())
    except Exception as e_main_shell:
        logger.critical(f"Luca Shell (main) crashed: {e_main_shell}", exc_info=True)
        print(f"Shell crashed: {e_main_shell}", file=sys.stderr) # Ensure some output if logging fails
        # Attempt graceful shutdown of app_instance if loop was running
        if hasattr(app_instance, 'is_shutting_down') and not app_instance.is_shutting_down:
             if asyncio.get_event_loop().is_running():
                asyncio.get_event_loop().run_until_complete(app_instance.shutdown())
             else: # Fallback for sync cleanup if loop already dead
                pass # Websocket closing is async.
    finally:
        logger.info("Luca Shell (main) process finished.")
