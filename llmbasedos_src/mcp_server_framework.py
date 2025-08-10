# llmbasedos/mcp_server_framework.py
import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Callable, Awaitable, Union, Tuple # Added 'Tuple'
from concurrent.futures import ThreadPoolExecutor
import jsonschema # For input validation
import shutil # <<< AJOUTER CET IMPORT

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
                 caps_file_path_str: str, 
                 custom_error_code_base: int = -32000,
                 socket_dir_str: str = "/run/mcp",
                 load_caps_on_init: bool = True):
        self.server_name = server_name
        self.socket_path = Path(socket_dir_str) / f"{self.server_name}.sock"
        self.caps_file_path = Path(caps_file_path_str)
        self.custom_error_code_base = custom_error_code_base

        log_level_str = os.getenv(f"LLMBDO_{self.server_name.upper()}_LOG_LEVEL", "INFO").upper()
        log_level_int = logging.getLevelName(log_level_str)
        self.logger = logging.getLogger(f"llmbasedos.servers.{self.server_name}")
        
        if not self.logger.hasHandlers():
            handler = logging.StreamHandler()
            formatter = logging.Formatter(f"%(asctime)s - {self.server_name} - %(levelname)s - %(message)s")
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)
        self.logger.setLevel(log_level_int)

        self._method_handlers: Dict[str, Callable[..., Awaitable[Any]]] = {}
        self._method_schemas: Dict[str, Dict[str, Any]] = {}
        
        num_workers = int(os.getenv(f"LLMBDO_{self.server_name.upper()}_WORKERS", 
                                    os.getenv("LLMBDO_DEFAULT_SERVER_WORKERS", "2")))
        self.executor = ThreadPoolExecutor(max_workers=num_workers, thread_name_prefix=f"{self.server_name}_worker")
        self.logger.info(f"Initialized with {num_workers} worker threads.")

        if load_caps_on_init:
            self._load_capabilities_and_schemas()

        # Initialize hooks with default (no-op) implementations
        # User can override these by assigning a new callable to self.on_startup / self.on_shutdown
        # The type hint 'MCPServer' needs to be in quotes for forward reference if MCPServer is not fully defined yet.
        self._on_startup_hook: Optional[Callable[['MCPServer'], Awaitable[None]]] = self._default_on_startup
        self._on_shutdown_hook: Optional[Callable[['MCPServer'], Awaitable[None]]] = self._default_on_shutdown
    def _publish_capability_descriptor(self):
        """
        Copies the server's caps.json file to the discovery directory /run/mcp/
        so the gateway can find it.
        """
        if not self.caps_file_path.exists():
            self.logger.error(f"Cannot publish capabilities: Source file {self.caps_file_path} does not exist.")
            return

        discovery_dir = Path("/run/mcp")
        discovery_dir.mkdir(parents=True, exist_ok=True)
        
        destination_cap_file = discovery_dir / f"{self.server_name}.cap.json"
        
        # ====================================================================
        # == CORRECTION : Ne rien faire si la source et la dest sont identiques ==
        # ====================================================================
        if self.caps_file_path.resolve() == destination_cap_file.resolve():
            self.logger.debug(f"Capability file is already in the discovery directory. No copy needed.")
            # On s'assure juste que les permissions sont bonnes
            try:
                os.chmod(destination_cap_file, 0o664)
            except OSError as e:
                self.logger.warning(f"Could not set permissions on existing capability file {destination_cap_file}: {e}")
            return
        # ====================================================================
        # == FIN DE LA CORRECTION ==
        # ====================================================================

        try:
            shutil.copyfile(self.caps_file_path, destination_cap_file)
            os.chmod(destination_cap_file, 0o664)
            self.logger.info(f"Successfully published capability descriptor to {destination_cap_file}")
        except Exception as e:
            self.logger.error(f"Failed to publish capability descriptor from {self.caps_file_path} to {destination_cap_file}: {e}", exc_info=True)

    def _unpublish_capability_descriptor(self):
        """
        Removes the server's caps.json file from the discovery directory /run/mcp/
        during shutdown.
        """
        discovery_dir = Path("/run/mcp")
        destination_cap_file = discovery_dir / f"{self.server_name}.cap.json"
        if destination_cap_file.exists():
            try:
                destination_cap_file.unlink()
                self.logger.info(f"Successfully unpublished capability descriptor from {destination_cap_file}")
            except Exception as e:
                self.logger.error(f"Failed to unpublish capability descriptor {destination_cap_file}: {e}", exc_info=True)

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
                if method_name and isinstance(params_schema, dict):
                    self._method_schemas[method_name] = params_schema
                elif method_name and params_schema is None:
                     self._method_schemas[method_name] = {"type": "array", "maxItems": 0}
                elif method_name:
                    self.logger.warning(f"Method '{method_name}' in {self.caps_file_path.name} has invalid 'params_schema'.")
            self.logger.info(f"Loaded {len(self._method_schemas)} param schemas from {self.caps_file_path.name}")
        except Exception as e:
            self.logger.error(f"Error loading schemas from {self.caps_file_path}: {e}", exc_info=True)

    def register_method(self, method_name: str): # Renamed from 'register' for clarity
        def decorator(func: Callable[..., Awaitable[Any]]):
            if method_name in self._method_handlers:
                self.logger.warning(f"Method '{method_name}' re-registered. Overwriting.")
            self.logger.debug(f"Registering method: {method_name} -> {func.__name__}")
            self._method_handlers[method_name] = func
            if method_name not in self._method_schemas:
                 self.logger.warning(f"Method '{method_name}' registered but no params_schema in {self.caps_file_path.name}.")
                 self._method_schemas.setdefault(method_name, {"type": "array", "maxItems": 0})
            return func
        return decorator

# Dans llmbasedos_pkg/mcp_server_framework.py
    async def _validate_params(self, method_name: str, params: Union[List[Any], Dict[str, Any]]) -> Optional[str]:
        schema = self._method_schemas.get(method_name)
        if not schema:
            self.logger.debug(f"No schema found for method '{method_name}', skipping validation.")
            return None

        self.logger.debug(f"Validating params for '{method_name}'. Schema: {schema}, Instance: {params}")
        try:
            jsonschema.validate(instance=params, schema=schema)
            self.logger.debug(f"Params for '{method_name}' are valid.")
            return None
        except jsonschema.exceptions.ValidationError as e_val_error:
            self.logger.warning(f"jsonschema.exceptions.ValidationError for '{method_name}': {e_val_error.message}. Path: {e_val_error.path}, Validator: {e_val_error.validator}, Schema: {e_val_error.schema}")
            error_path_str = " -> ".join(map(str, e_val_error.path)) if e_val_error.path else "params"
            return f"Invalid parameter '{error_path_str}': {e_val_error.message}"
        except Exception as e_other_val: # Capturer toute autre exception
            self.logger.error(f"UNEXPECTED validation error for '{method_name}': {type(e_other_val).__name__} - {e_other_val}", exc_info=True)
            return f"Internal error during parameter validation: {type(e_other_val).__name__}"

    async def _handle_single_request(self, request_data: Dict[str, Any]) -> Dict[str, Any]:
        request_id = request_data.get("id")
        method_name = request_data.get("method")
        params = request_data.get("params", [])

        if not isinstance(method_name, str):
            return create_mcp_error(request_id, JSONRPC_INVALID_REQUEST, "Method name must be a string.")
        
        # Basic type check for params if no schema is available or schema allows list/dict
        if not self._method_schemas.get(method_name) and not isinstance(params, list):
             return create_mcp_error(request_id, JSONRPC_INVALID_PARAMS, "Params must be an array if no schema defines object type.")
        elif self._method_schemas.get(method_name) and not isinstance(params, (list, dict)): # If schema exists, it will enforce type
             pass # jsonschema will handle this type check based on schema.type

        handler = self._method_handlers.get(method_name)
        if not handler:
            return create_mcp_error(request_id, JSONRPC_METHOD_NOT_FOUND, f"Method '{method_name}' not found on server '{self.server_name}'.")

        validation_error_msg = await self._validate_params(method_name, params)
        if validation_error_msg:
            return create_mcp_error(request_id, JSONRPC_INVALID_PARAMS, validation_error_msg)
        
        try:
            result_payload = await handler(self, request_id, params) # Pass self, request_id, params
            return create_mcp_response(request_id, result_payload)
        except ValueError as ve:
            self.logger.warning(f"Handler for '{method_name}' raised ValueError: {ve}")
            return create_mcp_error(request_id, self.custom_error_code_base - 1, str(ve))
        except PermissionError as pe:
            self.logger.warning(f"Handler for '{method_name}' raised PermissionError: {pe}")
            return create_mcp_error(request_id, self.custom_error_code_base - 2, str(pe))
        except FileNotFoundError as fnfe:
            self.logger.warning(f"Handler for '{method_name}' raised FileNotFoundError: {fnfe}")
            return create_mcp_error(request_id, self.custom_error_code_base - 3, str(fnfe))
        except NotImplementedError as nie:
            self.logger.warning(f"Handler for '{method_name}' raised NotImplementedError: {nie}")
            return create_mcp_error(request_id, JSONRPC_METHOD_NOT_FOUND, f"Method '{method_name}' action not fully implemented: {nie}")
        except Exception as e:
            self.logger.error(f"Error executing method '{method_name}' (ID {request_id}): {e}", exc_info=True)
            return create_mcp_error(request_id, JSONRPC_INTERNAL_ERROR, f"Internal server error during '{method_name}': {type(e).__name__}")

    async def _client_connection_handler(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
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
                            request_id_for_error = request_data.get("id")
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
                
                except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
                    self.logger.info(f"Client {client_desc} connection lost/reset/broken."); break
                except UnicodeDecodeError:
                    err_resp = create_mcp_error(None, JSONRPC_PARSE_ERROR, "Invalid UTF-8 sequence.")
                    try: writer.write(json.dumps(err_resp).encode('utf-8') + b'\0'); await writer.drain()
                    except: pass
                    break
        
        except asyncio.CancelledError: self.logger.info(f"Client handler for {client_desc} cancelled.")
        except Exception as e_outer: self.logger.error(f"Unexpected error in client handler for {client_desc}: {e_outer}", exc_info=True)
        finally:
            self.logger.info(f"Closing connection for {client_desc}")
            if not writer.is_closing():
                try: writer.close(); await writer.wait_closed()
                except: pass

    async def start(self):
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            try: self.socket_path.unlink()
            except OSError as e: self.logger.error(f"Error removing old socket {self.socket_path}: {e}"); return

        asyncio_server_obj = await asyncio.start_unix_server(self._client_connection_handler, path=str(self.socket_path))
        addr = asyncio_server_obj.sockets[0].getsockname() if asyncio_server_obj.sockets else str(self.socket_path)
        self.logger.info(f"MCP Server '{self.server_name}' listening on UNIX socket: {addr}")
        
        try:
            os.chmod(str(self.socket_path), 0o666) 
            self.logger.info(f"Set permissions for {self.socket_path} to 0660.")
        except OSError as e:
            self.logger.warning(f"Could not set permissions/owner for socket {self.socket_path}: {e}")

        # Publier le descripteur de capacité
        self._publish_capability_descriptor() # <<< APPEL ICI

        if not self.caps_file_path.exists(): # Redondant si _publish a déjà vérifié, mais ok
            self.logger.error(f"Reminder: Caps file {self.caps_file_path} is missing for '{self.server_name}'.")
        # else: # Plus besoin de ce log car _publish_capability_descriptor logue déjà
            # self.logger.info(f"Service capabilities defined in: {self.caps_file_path.name}")

        if self._on_startup_hook:
            self.logger.info(f"Running on_startup() for {self.server_name}...")
            await self._on_startup_hook(self)

        try:
            async with asyncio_server_obj: await asyncio_server_obj.serve_forever()
        except asyncio.CancelledError: self.logger.info(f"Server '{self.server_name}' main loop cancelled.")
        except Exception as e: self.logger.error(f"Server '{self.server_name}' exited with error: {e}", exc_info=True)
        finally:
            self.logger.info(f"Server '{self.server_name}' shutting down...")
            if self._on_shutdown_hook:
                self.logger.info(f"Running on_shutdown() for {self.server_name}...")
                try: await self._on_shutdown_hook(self)
                except Exception as e_shutdown_hook: self.logger.error(f"Error in on_shutdown() for {self.server_name}: {e_shutdown_hook}", exc_info=True)
            
            self._unpublish_capability_descriptor() # <<< APPEL ICI POUR NETTOYER

            self.logger.info(f"Shutting down executor for {self.server_name}...")
            self.executor.shutdown(wait=True)
            self.logger.info(f"Executor for {self.server_name} shut down.")
            
            if self.socket_path.exists():
                try: self.socket_path.unlink()
                except OSError as e: self.logger.error(f"Error removing socket {self.socket_path} on shutdown: {e}")
            self.logger.info(f"Server '{self.server_name}' fully stopped.")

    async def run_in_executor(self, func: Callable[..., Any], *args: Any) -> Any:
        if self.executor._shutdown: # type: ignore
             self.logger.warning(f"Executor for {self.server_name} is shutdown. Cannot run task.")
             raise RuntimeError(f"Executor for {self.server_name} is already shut down.")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.executor, func, *args)

    # --- Hook Management ---
    # Default hook implementations (private)
    async def _default_on_startup(self, server_instance: 'MCPServer'): # Renamed param for clarity
        self.logger.debug(f"{self.server_name} default on_startup called (instance: {id(server_instance)}).")
        pass

    async def _default_on_shutdown(self, server_instance: 'MCPServer'):
        self.logger.debug(f"{self.server_name} default on_shutdown called (instance: {id(server_instance)}).")
        pass

    # Public methods to set hooks
    def set_startup_hook(self, hook: Callable[['MCPServer'], Awaitable[None]]):
        """Assigns a coroutine to be called on server startup. Hook signature: async def my_hook(server: MCPServer)."""
        self._on_startup_hook = hook
        self.logger.info(f"Custom startup hook set for {self.server_name}.")

    def set_shutdown_hook(self, hook: Callable[['MCPServer'], Awaitable[None]]):
        """Assigns a coroutine to be called on server shutdown. Hook signature: async def my_hook(server: MCPServer)."""
        self._on_shutdown_hook = hook
        self.logger.info(f"Custom shutdown hook set for {self.server_name}.")

    # For direct attribute assignment (less formal, but used in your server files)
    # This ensures the type hint is correct if user does `server.on_startup = my_func`
    @property
    def on_startup(self) -> Optional[Callable[['MCPServer'], Awaitable[None]]]:
        return self._on_startup_hook

    @on_startup.setter
    def on_startup(self, hook: Callable[['MCPServer'], Awaitable[None]]):
        self.set_startup_hook(hook)

    @property
    def on_shutdown(self) -> Optional[Callable[['MCPServer'], Awaitable[None]]]:
        return self._on_shutdown_hook

    @on_shutdown.setter
    def on_shutdown(self, hook: Callable[['MCPServer'], Awaitable[None]]):
        self.set_shutdown_hook(hook)

    # Method to create custom error responses consistently
    def create_custom_error(self, request_id: Union[str, int, None], error_sub_code: int, message: str, data: Optional[Any] = None) -> Dict[str, Any]:
        """Creates a JSON-RPC error object using the server's custom error base."""
        # Ensure sub_code is negative if base is negative, or positive if base is positive
        # Here, base is -32000, so sub_codes like -1, -2 become -32001, -32002.
        # If you pass sub_code as 1, 2, it would be -31999, -31998.
        # Let's assume sub_code is positive (1, 2, 3...) and we subtract it from base.
        final_code = self.custom_error_code_base - abs(error_sub_code)
        return create_mcp_error(request_id, final_code, message, data)