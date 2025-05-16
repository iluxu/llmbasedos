# llmbasedos/mcp_server_framework.py
import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Callable, Awaitable, Union, Tuple
from concurrent.futures import ThreadPoolExecutor
import jsonschema # For input validation

# --- JSON-RPC Constants (centralized) ---
JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603

def create_mcp_response(id: Union[str, int, None], result: Optional[Any] = None) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id, "result": result}

def create_mcp_error(id: Union[str, int, None], code: int, message: str, data: Optional[Any] = None) -> Dict[str, Any]:
    error_obj: Dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error_obj["data"] = data
    return {"jsonrpc": "2.0", "id": id, "error": error_obj}

# --- Base MCP Server Class ---
class MCPServer:
    def __init__(self, 
                 server_name: str, 
                 caps_file_path_str: str, # Pass as string, convert to Path internally
                 custom_error_code_base: int = -32000,
                 socket_dir_str: str = "/run/mcp"):
        self.server_name = server_name
        self.socket_path = Path(socket_dir_str) / f"{self.server_name}.sock"
        self.caps_file_path = Path(caps_file_path_str)
        self.custom_error_code_base = custom_error_code_base

        # Logger setup for this specific server instance
        log_level_str = os.getenv(f"LLMBDO_{self.server_name.upper()}_LOG_LEVEL", "INFO").upper()
        log_level_int = logging.getLevelName(log_level_str)
        self.logger = logging.getLogger(f"llmbasedos.servers.{self.server_name}")
        
        # Configure logger if not already configured (e.g. by a central setup)
        if not self.logger.hasHandlers():
            handler = logging.StreamHandler()
            # Basic formatter, can be made configurable (simple/json) like gateway
            formatter = logging.Formatter(f"%(asctime)s - {self.server_name} - %(levelname)s - %(message)s")
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)
        self.logger.setLevel(log_level_int)

        self._method_handlers: Dict[str, Callable[..., Awaitable[Any]]] = {}
        self._method_schemas: Dict[str, Dict[str, Any]] = {}
        
        num_workers = int(os.getenv(f"LLMBDO_{self.server_name.upper()}_WORKERS", 
                                    os.getenv("LLMBDO_DEFAULT_SERVER_WORKERS", "2"))) # Default workers
        self.executor = ThreadPoolExecutor(max_workers=num_workers, thread_name_prefix=f"{self.server_name}_worker")
        self.logger.info(f"Initialized with {num_workers} worker threads.")

        self._load_capabilities_and_schemas()

    def _load_capabilities_and_schemas(self):
        if not self.caps_file_path.exists():
            self.logger.error(f"CRITICAL: Capability file {self.caps_file_path} missing for '{self.server_name}'.")
            return
        try:
            with self.caps_file_path.open('r') as f:
                caps_data = json.load(f)
            for cap_item in caps_data.get("capabilities", []):
                method_name = cap_item.get("method")
                params_schema = cap_item.get("params_schema")
                if method_name and isinstance(params_schema, dict): # Ensure schema is a dict
                    self._method_schemas[method_name] = params_schema
                elif method_name and params_schema is None: # Method with no params defined in schema
                     self._method_schemas[method_name] = {"type": "array", "maxItems": 0} # Expect empty array
                elif method_name:
                    self.logger.warning(f"Method '{method_name}' in {self.caps_file_path.name} has invalid 'params_schema'. Validation may fail.")
            self.logger.info(f"Loaded {len(self._method_schemas)} param schemas from {self.caps_file_path.name}")
        except Exception as e:
            self.logger.error(f"Error loading schemas from {self.caps_file_path}: {e}", exc_info=True)

    def register(self, method_name: str):
        def decorator(func: Callable[..., Awaitable[Any]]):
            if method_name in self._method_handlers:
                self.logger.warning(f"Method '{method_name}' re-registered. Overwriting.")
            self.logger.debug(f"Registering method: {method_name} -> {func.__name__}")
            self._method_handlers[method_name] = func
            if method_name not in self._method_schemas:
                 self.logger.warning(f"Method '{method_name}' registered but no params_schema found in {self.caps_file_path.name}. Validation will be skipped or use default (empty array).")
                 self._method_schemas.setdefault(method_name, {"type": "array", "maxItems": 0}) # Default if missing
            return func
        return decorator

    async def _validate_params(self, method_name: str, params: Union[List[Any], Dict[str, Any]]) -> Optional[str]:
        schema = self._method_schemas.get(method_name)
        if not schema: return None # No schema, skip validation (or default to empty array if strict)

        try:
            jsonschema.validate(instance=params, schema=schema)
            return None
        except jsonschema.exceptions.ValidationError as e:
            self.logger.warning(f"Invalid params for '{method_name}': {e.message}. Params: {str(params)[:100]}...")
            error_path = " -> ".join(map(str, e.path)) if e.path else "params"
            return f"Invalid parameter '{error_path}': {e.message}"
        except Exception as e_val:
            self.logger.error(f"Schema validation error for '{method_name}': {e_val}", exc_info=True)
            return "Internal error during parameter validation."

    async def _handle_single_request(self, request_data: Dict[str, Any]) -> Dict[str, Any]:
        request_id = request_data.get("id")
        method_name = request_data.get("method")
        params = request_data.get("params", [])

        if not isinstance(method_name, str):
            return create_mcp_error(request_id, JSONRPC_INVALID_REQUEST, "Method name must be a string.")
        # Params validation for type (array/object) is handled by jsonschema if schema is present
        # If no schema, jsonschema won't run. Default is list params.
        if not isinstance(params, (list, dict)): # Basic check if no schema or schema allows both
             if not self._method_schemas.get(method_name): # If no schema, default to expecting list
                if not isinstance(params, list):
                    return create_mcp_error(request_id, JSONRPC_INVALID_PARAMS, "Params must be an array if no schema defines object type.")


        handler = self._method_handlers.get(method_name)
        if not handler:
            return create_mcp_error(request_id, JSONRPC_METHOD_NOT_FOUND, f"Method '{method_name}' not found on server '{self.server_name}'.")

        validation_error_msg = await self._validate_params(method_name, params)
        if validation_error_msg:
            return create_mcp_error(request_id, JSONRPC_INVALID_PARAMS, validation_error_msg)
        
        try:
            # Handler signature: async def my_handler(mcp_server: MCPServer, request_id: str, params: ListOrDict)
            result_payload = await handler(self, request_id, params)
            return create_mcp_response(request_id, result_payload)
        except ValueError as ve: # Specific app logic errors raised by handlers
            self.logger.warning(f"Handler for '{method_name}' raised ValueError: {ve}")
            return create_mcp_error(request_id, self.custom_error_code_base - 1, str(ve)) # Example custom error
        except PermissionError as pe:
            self.logger.warning(f"Handler for '{method_name}' raised PermissionError: {pe}")
            return create_mcp_error(request_id, self.custom_error_code_base - 2, str(pe)) # Example custom error
        except FileNotFoundError as fnfe: # Common for FS operations
            self.logger.warning(f"Handler for '{method_name}' raised FileNotFoundError: {fnfe}")
            return create_mcp_error(request_id, self.custom_error_code_base - 3, str(fnfe))
        except NotImplementedError as nie: # For features not yet implemented
            self.logger.warning(f"Handler for '{method_name}' raised NotImplementedError: {nie}")
            return create_mcp_error(request_id, JSONRPC_METHOD_NOT_FOUND, f"Method '{method_name}' action not fully implemented: {nie}")
        except Exception as e:
            self.logger.error(f"Error executing method '{method_name}' (ID {request_id}): {e}", exc_info=True)
            return create_mcp_error(request_id, JSONRPC_INTERNAL_ERROR, f"Internal server error during '{method_name}': {type(e).__name__}")

    async def _client_connection_handler(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        # (Identical to the one previously generated for individual servers, uses self.logger etc.)
        client_addr_obj = writer.get_extra_info('socket').getsockname()
        client_desc = f"client_at_{client_addr_obj}" if isinstance(client_addr_obj, str) else f"client_pid_{client_addr_obj}"
        self.logger.info(f"Client connected: {client_desc}")
        message_buffer = bytearray()
        try:
            while True:
                try:
                    chunk = await reader.read(4096)
                    if not chunk: self.logger.info(f"Client {client_desc} disconnected (EOF)."); break
                    message_buffer.extend(chunk)
                    
                    while b'\0' in message_buffer:
                        message_bytes, rest_of_buffer = message_buffer.split(b'\0', 1)
                        message_buffer = rest_of_buffer
                        message_str = message_bytes.decode('utf-8')
                        self.logger.debug(f"RCV from {client_desc}: {message_str[:200]}...")
                        
                        request_id_for_error = None
                        try:
                            request_data = json.loads(message_str)
                            request_id_for_error = request_data.get("id") # Get ID early
                            response = await self._handle_single_request(request_data)
                        except json.JSONDecodeError:
                            response = create_mcp_error(None, JSONRPC_PARSE_ERROR, "Failed to parse JSON.")
                        except Exception as e_handler:
                            self.logger.error(f"Critical error in _handle_single_request from {client_desc}: {e_handler}", exc_info=True)
                            response = create_mcp_error(request_id_for_error, JSONRPC_INTERNAL_ERROR, "Critical internal server error.")
                        
                        response_bytes = json.dumps(response).encode('utf-8') + b'\0'
                        self.logger.debug(f"SND to {client_desc}: {response_bytes.decode()[:200]}...")
                        writer.write(response_bytes)
                        await writer.drain()
                
                except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError): # Added BrokenPipeError
                    self.logger.info(f"Client {client_desc} connection lost/reset/broken."); break
                except UnicodeDecodeError:
                    err_resp = create_mcp_error(None, JSONRPC_PARSE_ERROR, "Invalid UTF-8 sequence.")
                    try: writer.write(json.dumps(err_resp).encode('utf-8') + b'\0'); await writer.drain()
                    except: pass # Ignore write error if pipe already broken
                    break # Stop handling this client
        
        except asyncio.CancelledError: self.logger.info(f"Client handler for {client_desc} cancelled.")
        except Exception as e_outer: self.logger.error(f"Unexpected error in client handler for {client_desc}: {e_outer}", exc_info=True)
        finally:
            self.logger.info(f"Closing connection for {client_desc}")
            if not writer.is_closing():
                try: writer.close(); await writer.wait_closed()
                except: pass # Ignore errors during close if already problematic

    async def start(self):
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            try: self.socket_path.unlink()
            except OSError as e: self.logger.error(f"Error removing old socket {self.socket_path}: {e}"); return

        server = await asyncio.start_unix_server(self._client_connection_handler, path=str(self.socket_path))
        addr = server.sockets[0].getsockname() if server.sockets else str(self.socket_path)
        self.logger.info(f"MCP Server '{self.server_name}' listening on UNIX socket: {addr}")
        
        try:
            # Set permissions for the socket file.
            # Owner: current user (llmuser). Group: from env or llmuser's primary.
            # Perms: 0660 (rw-rw----) assuming gateway is in same group or runs as llmuser.
            # Requires postinstall.sh to set up /run/mcp with llmuser:llmgroup and g+s (setgid)
            # so that socket inherits group llmgroup, then chmod 0660 works for group access.
            # Simpler for now: if all run as llmuser, 0600 is fine.
            # Using 0660 as a compromise assuming group setup will happen.
            
            # Get current user/group to ensure socket ownership if needed (usually not required if server runs as target user)
            # uid = os.geteuid()
            # gid = int(os.getenv(f"LLMBDO_{self.server_name.upper()}_SOCKET_GID", os.getgid())) # GID for llmgroup
            # os.chown(str(self.socket_path), uid, gid) # May fail if not root and changing group
            
            os.chmod(str(self.socket_path), 0o660) # User RW, Group RW, Other None
            self.logger.info(f"Set permissions for {self.socket_path} to 0660.")
        except OSError as e:
            self.logger.warning(f"Could not set permissions/owner for socket {self.socket_path}: {e}")

        if not self.caps_file_path.exists():
            self.logger.error(f"Reminder: Caps file {self.caps_file_path} is missing for '{self.server_name}'.")
        else:
            self.logger.info(f"Service capabilities defined in: {self.caps_file_path.name}")

        # Call user-defined startup hook if it exists
        if hasattr(self, 'on_startup') and callable(self.on_startup):
            self.logger.info(f"Running on_startup() for {self.server_name}...")
            await self.on_startup()

        try:
            async with server: await server.serve_forever()
        except asyncio.CancelledError: self.logger.info(f"Server '{self.server_name}' main loop cancelled.")
        except Exception as e: self.logger.error(f"Server '{self.server_name}' exited with error: {e}", exc_info=True)
        finally:
            self.logger.info(f"Server '{self.server_name}' shutting down...")
            if hasattr(self, 'on_shutdown') and callable(self.on_shutdown):
                self.logger.info(f"Running on_shutdown() for {self.server_name}...")
                try: await self.on_shutdown()
                except Exception as e_shutdown_hook: self.logger.error(f"Error in on_shutdown() for {self.server_name}: {e_shutdown_hook}", exc_info=True)
            
            self.logger.info(f"Shutting down executor for {self.server_name}...")
            self.executor.shutdown(wait=True) # Ensure all worker threads complete
            self.logger.info(f"Executor for {self.server_name} shut down.")
            
            if self.socket_path.exists():
                try: self.socket_path.unlink()
                except OSError as e: self.logger.error(f"Error removing socket {self.socket_path} on shutdown: {e}")
            self.logger.info(f"Server '{self.server_name}' fully stopped.")

    async def run_in_executor(self, func: Callable[..., Any], *args: Any) -> Any:
        """Runs a blocking function in the server's thread pool."""
        if self.executor._shutdown: # type: ignore # Accessing protected member for check
             self.logger.warning(f"Executor for {self.server_name} is shutdown. Cannot run task.")
             raise RuntimeError(f"Executor for {self.server_name} is already shut down.")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.executor, func, *args)

    # Default on_startup and on_shutdown hooks (can be overridden by subclasses)
    async def on_startup(self):
        self.logger.debug(f"{self.server_name} default on_startup called.")
        pass

    async def on_shutdown(self):
        self.logger.debug(f"{self.server_name} default on_shutdown called.")
        pass
