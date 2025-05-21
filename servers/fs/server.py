# llmbasedos/servers/fs/server.py
import asyncio
import logging # Logger will be obtained from MCPServer instance
import os
import shutil
from pathlib import Path
from datetime import datetime, timezone
import base64
import magic # For MIME types
from typing import Any, Dict, List, Optional, Tuple, Union # For type hints
import json # For FAISS metadata

# Embedding and Search related imports
try:
    from sentence_transformers import SentenceTransformer
    import faiss
    import numpy as np
    EMBEDDING_SYSTEM_AVAILABLE = True
except ImportError:
    EMBEDDING_SYSTEM_AVAILABLE = False
    # Create dummy types for type hinting if imports fail
    SentenceTransformer = faiss = np = type(None) # type: ignore

# --- Import Framework and Common Utils ---
# Supposons que ces modules sont accessibles dans le PYTHONPATH
# from llmbasedos.mcp_server_framework import MCPServer
# from llmbasedos.common_utils import validate_mcp_path_param, DEFAULT_VIRTUAL_ROOT_STR

# --- Simulation de MCPServer et common_utils pour l'exécution standalone ---
if __name__ == '__main__':
    DEFAULT_VIRTUAL_ROOT_STR = os.path.expanduser("~/llmbasedos_virtual_root_default")
    Path(DEFAULT_VIRTUAL_ROOT_STR).mkdir(parents=True, exist_ok=True)
    
    class MCPServer:
        ERROR_INVALID_PARAMS = -32602
        ERROR_INTERNAL = -32603
        ERROR_RESOURCE_NOT_FOUND = -32004 # Example custom error code

        def __init__(self, server_name, caps_file_path_str, custom_error_code_base=0):
            self.server_name = server_name
            self.caps_file_path = Path(caps_file_path_str)
            self.custom_error_code_base = custom_error_code_base
            self.logger = logging.getLogger(f"llmbasedos.servers.{server_name}")
            
            # Basic logger setup if not already configured by a higher level
            if not self.logger.hasHandlers():
                log_level_env = os.getenv(f"LLMBDO_{server_name.upper()}_LOG_LEVEL", os.getenv("LLMBDO_LOG_LEVEL", "INFO")).upper()
                _lvl = logging.getLevelName(log_level_env)
                if not isinstance(_lvl, int): _lvl = logging.INFO
                self.logger.setLevel(_lvl)
                ch = logging.StreamHandler()
                log_format_env = os.getenv(f"LLMBDO_{server_name.upper()}_LOG_FORMAT", os.getenv("LLMBDO_LOG_FORMAT", "simple"))
                if log_format_env == "json":
                    # Basic JSON-like formatter for mock, real one would use python-json-logger
                    formatter = logging.Formatter('{"time": "%(asctime)s", "name": "%(name)s", "level": "%(levelname)s", "message": "%(message)s"}')
                else:
                    formatter = logging.Formatter(f"%(asctime)s - %(name)s - %(levelname)s - %(message)s")
                ch.setFormatter(formatter)
                self.logger.addHandler(ch)
                self.logger.propagate = False


            self.executor = None # Placeholder for ThreadPoolExecutor
            self.on_startup_handler = None # Changed attribute name
            self.on_shutdown_handler = None # Changed attribute name
            self._handlers_registry = {} # Changed attribute name

        def register(self, method_name):
            def decorator(func):
                self._handlers_registry[method_name] = func
                return func
            return decorator

        async def run_in_executor(self, func, *args):
            # This would use self.executor in a real framework
            loop = asyncio.get_running_loop()
            # Python's default executor is fine for many cases if self.executor is None
            return await loop.run_in_executor(None, func, *args)

        async def start(self): # Mocked start method
            if hasattr(self, 'executor') and self.executor is None: # Initialize if not done by framework
                 from concurrent.futures import ThreadPoolExecutor
                 self.executor = ThreadPoolExecutor(max_workers=os.cpu_count())

            if self.on_startup_handler: await self.on_startup_handler(self)
            self.logger.info(f"Mock MCPServer '{self.server_name}' started. Listening on (simulated) socket.")
            try:
                # Simulate running and handling requests (not actually done in mock)
                while True: await asyncio.sleep(3600) 
            except asyncio.CancelledError: self.logger.info(f"Mock MCPServer '{self.server_name}' run cancelled.")
            finally:
                if self.on_shutdown_handler: await self.on_shutdown_handler(self)
                if hasattr(self, 'executor') and self.executor: self.executor.shutdown(wait=True)
                self.logger.info(f"Mock MCPServer '{self.server_name}' shut down.")
        
        def create_custom_error(self, code: int, message: str, data: Optional[Any] = None) -> Dict[str, Any]:
            # Mocked error creation
            err_obj = {"code": code, "message": message}
            if data: err_obj["data"] = data
            # This would normally be returned and the framework creates the full JSON-RPC error
            # For handlers raising exceptions, this structure implies the handler creates the error payload for the exception
            # For this example, ValueError will be converted by the framework
            raise ValueError(message) # Simple way to raise for the mock

    def validate_mcp_path_param(
        path_param: Any,
        virtual_root_str: Optional[str] = None,
        relative_to_cwd_str: Optional[str] = None,
        check_exists: bool = False,
        must_be_dir: Optional[bool] = None,
        must_be_file: Optional[bool] = None,
        allow_outside_virtual_root: bool = False # New param for flexibility
    ) -> Tuple[Optional[Path], Optional[str]]:
        
        _actual_virtual_root_str = virtual_root_str or DEFAULT_VIRTUAL_ROOT_STR
        virtual_root = Path(_actual_virtual_root_str).resolve()

        if not isinstance(path_param, str):
            return None, "Path parameter must be a string."

        try:
            # Determine base for resolution
            if os.path.isabs(path_param) and not virtual_root_str : # Absolute path given, no virtual root constraint
                # This case is dangerous if not careful. For FS server, we usually want to confine to virtual_root.
                # If allow_outside_virtual_root is True (use with extreme caution)
                if allow_outside_virtual_root:
                    p = Path(path_param).resolve()
                else:
                    return None, "Absolute paths are only allowed if a virtual_root is defined or allow_outside_virtual_root is true."

            elif os.path.isabs(path_param) and virtual_root_str: # Absolute path, but needs to be under virtual_root
                # Treat it as /foo meaning virtual_root/foo
                p_relative_to_vroot = Path(path_param.lstrip('/\\'))
                p = (virtual_root / p_relative_to_vroot).resolve()

            elif relative_to_cwd_str: # Relative path to a CWD
                base_dir = Path(relative_to_cwd_str).resolve()
                p = (base_dir / path_param).resolve()
            
            else: # Relative path, assume relative to virtual_root
                p = (virtual_root / path_param).resolve()
            
            # Security Check: Confine to virtual_root unless explicitly allowed
            if not allow_outside_virtual_root:
                if virtual_root != p and not str(p).startswith(str(virtual_root) + os.sep):
                    return None, f"Path '{path_param}' (resolved to '{p}') is outside designated virtual root '{virtual_root}'."

            if check_exists and not p.exists():
                return None, f"Path '{path_param}' (resolved to '{p}') does not exist."
            if must_be_dir and p.exists() and not p.is_dir():
                return None, f"Path '{path_param}' (resolved to '{p}') is not a directory."
            if must_be_file and p.exists() and not p.is_file():
                return None, f"Path '{path_param}' (resolved to '{p}') is not a file."
            return p, None
        except Exception as e:
            return None, f"Error processing path '{path_param}': {e}"
    # --- Fin de la simulation ---
else:
    from llmbasedos.mcp_server_framework import MCPServer
    from llmbasedos.common_utils import validate_mcp_path_param, DEFAULT_VIRTUAL_ROOT_STR


# --- Server Specific Configuration ---
SERVER_NAME = "fs"
CAPS_FILE_PATH_STR = str(Path(__file__).parent / "caps.json")
FS_CUSTOM_ERROR_BASE = -32010 # Base for FS specific errors

# Embedding config: Read from ENV, provide defaults.
EMBEDDING_MODEL_NAME_CONF = os.getenv("LLMBDO_FS_EMBEDDING_MODEL", 'all-MiniLM-L6-v2' if EMBEDDING_SYSTEM_AVAILABLE else "disabled")
_faiss_dir_default = "/var/lib/llmbasedos/faiss_indices_default" # Default if ENV not set
FAISS_INDEX_DIR_STR = os.getenv("LLMBDO_FS_FAISS_DIR", _faiss_dir_default)
FAISS_INDEX_DIR_CONF = Path(FAISS_INDEX_DIR_STR).resolve() # Resolve to absolute path

FAISS_INDEX_FILE_CONF = FAISS_INDEX_DIR_CONF / "index.faiss"
FAISS_METADATA_FILE_CONF = FAISS_INDEX_DIR_CONF / "metadata.json"

# Virtual root for this FS server. All paths from MCP calls will be validated against this.
# If LLMBDO_FS_VIRTUAL_ROOT is not set, uses DEFAULT_VIRTUAL_ROOT_STR from common_utils (or local mock).
FS_VIRTUAL_ROOT_ENV_STR = os.getenv(f"LLMBDO_{SERVER_NAME.upper()}_VIRTUAL_ROOT")
# The actual FS_VIRTUAL_ROOT_PATH used by _validate_fs_path will be FS_VIRTUAL_ROOT_ENV_STR if set,
# otherwise the default from common_utils.validate_mcp_path_param.

# Initialize server instance
fs_server = MCPServer(SERVER_NAME, CAPS_FILE_PATH_STR, custom_error_code_base=FS_CUSTOM_ERROR_BASE)

# Attach embedding-specific state to the server instance
fs_server.embedding_enabled: bool = EMBEDDING_SYSTEM_AVAILABLE and (EMBEDDING_MODEL_NAME_CONF != "disabled") # type: ignore
fs_server.embedding_model: Optional[SentenceTransformer] = None # type: ignore
fs_server.faiss_index: Optional[faiss.Index] = None # type: ignore
fs_server.faiss_index_metadata: List[Dict[str, Any]] = [] # type: ignore
fs_server.faiss_next_id: int = 0 # type: ignore

if fs_server.embedding_enabled: # type: ignore
    try:
        FAISS_INDEX_DIR_CONF.mkdir(parents=True, exist_ok=True)
        fs_server.logger.info(f"FAISS index directory configured at: {FAISS_INDEX_DIR_CONF}")
    except OSError as e:
        fs_server.logger.error(f"Could not create FAISS directory {FAISS_INDEX_DIR_CONF}: {e}. Embedding will be disabled.")
        fs_server.embedding_enabled = False # type: ignore
else:
    if not EMBEDDING_SYSTEM_AVAILABLE:
        fs_server.logger.warning("Embedding system dependencies (sentence-transformers, faiss, numpy) missing. Embed/search capabilities disabled.")
    elif EMBEDDING_MODEL_NAME_CONF == "disabled":
        fs_server.logger.info("Embedding model name configured as 'disabled'. Embed/search capabilities disabled.")


# --- Path Validation Helper for FS Server (using common_utils) ---
def _validate_fs_path_against_server_root(
        path_param: Any,
        check_exists: bool = False,
        must_be_dir: Optional[bool] = None,
        must_be_file: Optional[bool] = None
    ) -> Path:
    """
    Wrapper for validate_mcp_path_param using this server's specific FS_VIRTUAL_ROOT_ENV_STR.
    Raises ValueError on failure, which MCPServer should map to an MCP error.
    """
    # MCP paths are typically "absolute" from the client's perspective of the exposed root.
    # `validate_mcp_path_param` will treat `/foo` as `FS_VIRTUAL_ROOT_ENV_STR/foo`.
    # If `path_param` is `foo/bar`, it means `FS_VIRTUAL_ROOT_ENV_STR/foo/bar`.
    # We don't use relative_to_cwd_str here as server calls are self-contained.
    resolved_path, err_msg = validate_mcp_path_param(
        path_param,
        virtual_root_str=FS_VIRTUAL_ROOT_ENV_STR, # This server's specific root from ENV
        relative_to_cwd_str=None, # Paths from MCP are absolute relative to the conceptual root
        check_exists=check_exists,
        must_be_dir=must_be_dir,
        must_be_file=must_be_file,
        allow_outside_virtual_root=False # Critical: FS server MUST confine to its virtual root
    )
    if err_msg:
        # Log the detailed error on server, return a user-friendly message
        fs_server.logger.warning(f"Path validation failed for '{path_param}': {err_msg}")
        # Raise a ValueError that the framework should convert to an MCP error (e.g., INVALID_PARAMS or a custom one)
        raise ValueError(f"Invalid path or permissions for '{path_param}'. Details: {err_msg}")
    if resolved_path is None: # Defensive check, should be caught by err_msg
        fs_server.logger.error(f"Path validation for '{path_param}' returned no error but no path.")
        raise ValueError("Path validation failed unexpectedly.") # Generic error
    return resolved_path


# --- File System Capability Handlers (modified to use new path validation wrapper) ---
@fs_server.register("mcp.fs.list")
async def handle_fs_list(server: MCPServer, request_id: str, params: List[Any]):
    # params[0] is the path string, validated by schema by MCPServer
    target_path_abs = _validate_fs_path_against_server_root(params[0], check_exists=True, must_be_dir=True)

    def list_dir_sync_op():
        listed_items = []
        for item_abs in target_path_abs.iterdir():
            try:
                # Path to return to client: relative to the server's virtual root, starting with /
                path_for_client = Path("/") / item_abs.relative_to(Path(FS_VIRTUAL_ROOT_ENV_STR or DEFAULT_VIRTUAL_ROOT_STR).resolve())
                
                stat_info = item_abs.stat()
                item_type = "other"
                if item_abs.is_file(): item_type = "file"
                elif item_abs.is_dir(): item_type = "directory"
                elif item_abs.is_symlink(): item_type = "symlink"
                
                listed_items.append({
                    "name": item_abs.name,
                    "path": str(path_for_client), # Client-facing path
                    "type": item_type,
                    "size": stat_info.st_size if item_type != "directory" else -1,
                    "modified_at": datetime.fromtimestamp(stat_info.st_mtime, tz=timezone.utc).isoformat()
                })
            except OSError as stat_err:
                server.logger.warning(f"Could not stat item {item_abs.name} in {target_path_abs}: {stat_err}")
                path_for_client_err = Path("/") / item_abs.relative_to(Path(FS_VIRTUAL_ROOT_ENV_STR or DEFAULT_VIRTUAL_ROOT_STR).resolve())
                listed_items.append({"name": item_abs.name, "path": str(path_for_client_err), "type": "inaccessible", "size": -1, "modified_at": None})
        return listed_items
    
    try: return await server.run_in_executor(list_dir_sync_op)
    except PermissionError as pe: # iterdir or stat failed due to permissions
        server.logger.error(f"Permission error listing '{params[0]}' (resolved: {target_path_abs}): {pe}", exc_info=True)
        raise server.create_custom_error(FS_CUSTOM_ERROR_BASE -1, f"Permission denied for path '{params[0]}'.", {"path": params[0]}) from pe
    except ValueError as ve: # From _validate_fs_path_against_server_root
        raise ve # Let framework handle it (should be INVALID_PARAMS or similar)

# Handlers for read, write, delete need similar adjustments:
# 1. Use `_validate_fs_path_against_server_root` for path validation.
# 2. Return paths relative to the virtual root (FS_VIRTUAL_ROOT_ENV_STR) in responses if applicable.
# 3. Raise specific exceptions (ValueError for bad params, PermissionError, RuntimeError for server issues)
#    that MCPServer framework can map to appropriate JSON-RPC errors.

@fs_server.register("mcp.fs.read")
async def handle_fs_read(server: MCPServer, request_id: str, params: List[Any]):
    target_file_abs = _validate_fs_path_against_server_root(params[0], check_exists=True, must_be_file=True)
    encoding_type = params[1] if len(params) > 1 else "text"

    def read_file_sync_op():
        _mime_type = "application/octet-stream"
        try: _mime_type = magic.from_file(str(target_file_abs), mime=True)
        except Exception as magic_e: server.logger.warning(f"python-magic error for {target_file_abs}: {magic_e}")

        _content: str
        if encoding_type == "text":
            try: _content = target_file_abs.read_text(encoding="utf-8")
            except UnicodeDecodeError: raise ValueError(f"File '{params[0]}' not valid UTF-8. Try 'base64' encoding.")
        elif encoding_type == "base64":
            _content_bytes = target_file_abs.read_bytes()
            _content = base64.b64encode(_content_bytes).decode('ascii')
        else: # Should be caught by schema validation in MCPServer
            raise ValueError(f"Unsupported encoding type '{encoding_type}'.")

        path_for_client = Path("/") / target_file_abs.relative_to(Path(FS_VIRTUAL_ROOT_ENV_STR or DEFAULT_VIRTUAL_ROOT_STR).resolve())
        return {"path": str(path_for_client), "content": _content, "encoding": encoding_type, "mime_type": _mime_type}

    try: return await server.run_in_executor(read_file_sync_op)
    except PermissionError as pe:
        raise server.create_custom_error(FS_CUSTOM_ERROR_BASE -1, f"Permission denied reading '{params[0]}'.", {"path": params[0]}) from pe
    except ValueError as ve: raise ve


@fs_server.register("mcp.fs.write")
async def handle_fs_write(server: MCPServer, request_id: str, params: List[Any]):
    target_file_abs = _validate_fs_path_against_server_root(params[0], check_exists=False) # File doesn't need to exist
    content_str = params[1]
    encoding_type = params[2] if len(params) > 2 else "text"
    append_mode = params[3] if len(params) > 3 else False

    if not target_file_abs.parent.is_dir():
         raise ValueError(f"Parent directory for '{params[0]}' does not exist or is not a directory.")
    if target_file_abs.exists() and target_file_abs.is_dir():
        raise ValueError(f"Cannot write to '{params[0]}', it is an existing directory.")

    def write_file_sync_op():
        _content_bytes: bytes
        if encoding_type == "text": _content_bytes = content_str.encode('utf-8')
        elif encoding_type == "base64":
            try: _content_bytes = base64.b64decode(content_str)
            except Exception: raise ValueError("Invalid base64 content for writing.")
        else: raise ValueError(f"Unsupported encoding type '{encoding_type}'.")

        _mode = 'ab' if append_mode else 'wb'
        with target_file_abs.open(_mode) as f: bytes_written = f.write(_content_bytes)
        
        path_for_client = Path("/") / target_file_abs.relative_to(Path(FS_VIRTUAL_ROOT_ENV_STR or DEFAULT_VIRTUAL_ROOT_STR).resolve())
        return {"path": str(path_for_client), "bytes_written": bytes_written, "status": "success"}

    try: return await server.run_in_executor(write_file_sync_op)
    except PermissionError as pe:
        raise server.create_custom_error(FS_CUSTOM_ERROR_BASE -1, f"Permission denied writing to '{params[0]}'.", {"path": params[0]}) from pe
    except ValueError as ve: raise ve


@fs_server.register("mcp.fs.delete")
async def handle_fs_delete(server: MCPServer, request_id: str, params: List[Any]):
    target_path_abs = _validate_fs_path_against_server_root(params[0], check_exists=True)
    recursive = params[1] if len(params) > 1 else False

    def delete_path_sync_op():
        if target_path_abs.is_file() or target_path_abs.is_symlink(): target_path_abs.unlink()
        elif target_path_abs.is_dir():
            if recursive: shutil.rmtree(target_path_abs)
            else:
                try: target_path_abs.rmdir()
                except OSError: raise ValueError(f"Directory '{params[0]}' not empty. Use recursive=true to delete.")
        else: raise ValueError(f"Path '{params[0]}' is an unknown type for deletion.")
        
        path_for_client = Path("/") / target_path_abs.relative_to(Path(FS_VIRTUAL_ROOT_ENV_STR or DEFAULT_VIRTUAL_ROOT_STR).resolve())
        return {"path": str(path_for_client), "status": "success"}

    try: return await server.run_in_executor(delete_path_sync_op)
    except PermissionError as pe:
        raise server.create_custom_error(FS_CUSTOM_ERROR_BASE -1, f"Permission denied deleting '{params[0]}'.", {"path": params[0]}) from pe
    except ValueError as ve: raise ve


# --- Embedding and Search (Refactored) ---
def _get_embedding_model_sync(server_ref: MCPServer) -> SentenceTransformer:
    if not server_ref.embedding_enabled: raise RuntimeError("Embedding system disabled.") # type: ignore
    if server_ref.embedding_model is None: # type: ignore
        server_ref.logger.info(f"Loading sentence transformer model: {EMBEDDING_MODEL_NAME_CONF}")
        if not EMBEDDING_MODEL_NAME_CONF: raise RuntimeError("Embedding model name not configured.")
        try: server_ref.embedding_model = SentenceTransformer(EMBEDDING_MODEL_NAME_CONF) # type: ignore
        except Exception as e:
            server_ref.logger.error(f"Failed to load ST model '{EMBEDDING_MODEL_NAME_CONF}': {e}", exc_info=True)
            raise RuntimeError(f"Could not load embedding model: {e}")
    return server_ref.embedding_model # type: ignore

def _get_faiss_index_sync(server_ref: MCPServer, ensure_init: bool = True) -> faiss.Index: # type: ignore
    if not server_ref.embedding_enabled: raise RuntimeError("Embedding system disabled.") # type: ignore
    if server_ref.faiss_index is None and ensure_init: # type: ignore
        server_ref.logger.info(f"Initializing/Loading FAISS index from dir: {FAISS_INDEX_DIR_CONF}")
        if FAISS_INDEX_FILE_CONF.exists() and FAISS_METADATA_FILE_CONF.exists():
            try:
                server_ref.faiss_index = faiss.read_index(str(FAISS_INDEX_FILE_CONF)) # type: ignore
                with FAISS_METADATA_FILE_CONF.open('r') as f: server_ref.faiss_index_metadata = json.load(f) # type: ignore
                if server_ref.faiss_index_metadata: # type: ignore
                    server_ref.faiss_next_id = max(item['id'] for item in server_ref.faiss_index_metadata) + 1 # type: ignore
                else: server_ref.faiss_next_id = 0 # type: ignore
                server_ref.logger.info(f"FAISS index loaded with {server_ref.faiss_index.ntotal if server_ref.faiss_index else 0} vectors.") # type: ignore
            except Exception as e:
                server_ref.logger.error(f"Failed to load FAISS index/metadata: {e}. Creating new.", exc_info=True)
                server_ref.faiss_index = None; server_ref.faiss_index_metadata = []; server_ref.faiss_next_id = 0 # type: ignore
        
        if server_ref.faiss_index is None: # type: ignore
            model_for_dim = _get_embedding_model_sync(server_ref)
            embedding_dim = model_for_dim.get_sentence_embedding_dimension()
            server_ref.logger.info(f"Creating new FAISS index (dim: {embedding_dim}) at {FAISS_INDEX_DIR_CONF}.")
            server_ref.faiss_index = faiss.IndexIDMap(faiss.IndexFlatL2(embedding_dim)) # type: ignore
            server_ref.faiss_index_metadata = []; server_ref.faiss_next_id = 0 # type: ignore
            _save_faiss_index_sync(server_ref) # Save empty index and metadata immediately

    if server_ref.faiss_index is None and ensure_init: # type: ignore
        raise RuntimeError("FAISS index could not be initialized.")
    return server_ref.faiss_index # type: ignore

def _save_faiss_index_sync(server_ref: MCPServer): # Blocking
    if server_ref.embedding_enabled and server_ref.faiss_index is not None: # type: ignore
        try:
            FAISS_INDEX_DIR_CONF.mkdir(parents=True, exist_ok=True)
            server_ref.logger.info(f"Saving FAISS index ({server_ref.faiss_index.ntotal} vectors) & metadata to {FAISS_INDEX_DIR_CONF}...") # type: ignore
            faiss.write_index(server_ref.faiss_index, str(FAISS_INDEX_FILE_CONF)) # type: ignore
            with FAISS_METADATA_FILE_CONF.open('w') as f: json.dump(server_ref.faiss_index_metadata, f) # type: ignore
            server_ref.logger.info("FAISS index and metadata saved.")
        except Exception as e: server_ref.logger.error(f"Failed to save FAISS index/metadata: {e}", exc_info=True)

# This is the startup hook that will be called by MCPServer
async def on_fs_server_startup(server: MCPServer):
    server.logger.info(f"FS Server '{server.server_name}' on_startup hook running...")
    if server.embedding_enabled: # type: ignore
        server.logger.info("Pre-loading embedding model and FAISS index in executor...")
        try:
            # Run these potentially blocking initializations in the executor
            await server.run_in_executor(_get_embedding_model_sync, server)
            await server.run_in_executor(_get_faiss_index_sync, server, True) # ensure_init=True
            server.logger.info("Embedding model and FAISS index initialized successfully via startup hook.")
        except Exception as e:
            server.logger.error(f"Error during startup initialization of embedding system: {e}", exc_info=True)
            server.embedding_enabled = False # Disable if startup fails
            server.logger.warning("Embedding system has been disabled due to startup error.")
    else:
        server.logger.info("Embedding system is disabled by configuration or missing libraries. Skipping pre-load.")

# This is the shutdown hook
async def on_fs_server_shutdown(server: MCPServer):
    server.logger.info(f"FS Server '{server.server_name}' on_shutdown hook running...")
    if server.embedding_enabled and server.faiss_index is not None: # type: ignore
        server.logger.info("Attempting to save FAISS index on shutdown...")
        try:
            await server.run_in_executor(_save_faiss_index_sync, server)
        except Exception as e_save: # Catch errors if executor is already shutting down
            server.logger.error(f"Failed to save FAISS index via executor during shutdown: {e_save}. Attempting synchronous save.")
            try: _save_faiss_index_sync(server) # Last resort sync save
            except Exception as e_sync_save: server.logger.error(f"Synchronous FAISS save also failed: {e_sync_save}")

fs_server.on_startup_handler = on_fs_server_startup # Assign to the attribute MCPServer expects
fs_server.on_shutdown_handler = on_fs_server_shutdown


@fs_server.register("mcp.fs.embed")
async def handle_fs_embed(server: MCPServer, request_id: str, params: List[Any]):
    if not server.embedding_enabled: # type: ignore
        raise RuntimeError("Embedding system is disabled for this server instance.")
    
    target_path_str = params[0] # User provided path string
    recursive = params[1] if len(params) > 1 else False

    # Path validation must happen before passing to executor, as it might raise ValueError
    target_path_abs = _validate_fs_path_against_server_root(target_path_str, check_exists=True)

    def embed_path_sync_blocking_op():
        embedding_model = _get_embedding_model_sync(server) # Assumes model is loaded by startup or first call
        faiss_idx = _get_faiss_index_sync(server) # Assumes index is loaded/created by startup or first call

        _files_to_process_abs: List[Path] = []
        if target_path_abs.is_file(): _files_to_process_abs.append(target_path_abs)
        elif target_path_abs.is_dir():
            glob_pattern = "**/*" if recursive else "*"
            for item_abs in target_path_abs.glob(glob_pattern):
                if item_abs.is_file(): _files_to_process_abs.append(item_abs)
        # No else needed, _validate_fs_path_against_server_root would have raised if not file/dir

        _processed_count = 0
        MAX_FILE_SIZE_BYTES = int(os.getenv("LLMBDO_FS_EMBED_MAX_SIZE_KB", "1024")) * 1024 # 1MB default for example
        _new_embeddings_list = []
        _new_metadata_batch_list = []
        
        # Resolve virtual root once for creating relative paths for metadata
        _virtual_root_path_resolved = Path(FS_VIRTUAL_ROOT_ENV_STR or DEFAULT_VIRTUAL_ROOT_STR).resolve()

        existing_embedded_client_paths = {item['path'] for item in server.faiss_index_metadata} # type: ignore

        for file_p_abs in _files_to_process_abs:
            path_for_client_meta = str(Path("/") / file_p_abs.relative_to(_virtual_root_path_resolved))
            
            if path_for_client_meta in existing_embedded_client_paths:
                server.logger.debug(f"Skipping already embedded file (client path): {path_for_client_meta} (abs: {file_p_abs})")
                continue
            try:
                if file_p_abs.stat().st_size > MAX_FILE_SIZE_BYTES:
                    server.logger.warning(f"Skipping large file {file_p_abs} (>{MAX_FILE_SIZE_BYTES}B) for embedding.")
                    continue
                content = file_p_abs.read_text(encoding='utf-8', errors='ignore')
                if not content.strip():
                    server.logger.debug(f"Skipping empty or whitespace-only file {file_p_abs}")
                    continue
                
                server.logger.debug(f"Embedding: {path_for_client_meta} (abs: {file_p_abs})")
                embedding_vector = embedding_model.encode([content])[0]
                _new_embeddings_list.append(embedding_vector.astype('float32'))
                _new_metadata_batch_list.append({"id": server.faiss_next_id, "path": path_for_client_meta}) # type: ignore
                server.faiss_next_id += 1 # type: ignore
                _processed_count += 1
            except Exception as e_emb:
                server.logger.error(f"Error embedding file {file_p_abs}: {e_emb}", exc_info=True)
        
        if _new_embeddings_list:
            try:
                embeddings_np_arr = np.array(_new_embeddings_list)
                ids_np_arr = np.array([m['id'] for m in _new_metadata_batch_list], dtype='int64')
                faiss_idx.add_with_ids(embeddings_np_arr, ids_np_arr) # type: ignore
                server.faiss_index_metadata.extend(_new_metadata_batch_list) # type: ignore
                _save_faiss_index_sync(server)
                server.logger.info(f"Added {len(_new_embeddings_list)} new embeddings. Total: {faiss_idx.ntotal}.") # type: ignore
            except Exception as e_faiss_add:
                server.logger.error(f"Error adding embeddings to FAISS index: {e_faiss_add}", exc_info=True)
                raise RuntimeError(f"Failed to update search index: {e_faiss_add}")
        
        return {"path_processed": target_path_str, # User's original path
                "files_embedded_this_run": _processed_count,
                "total_embeddings_in_index": faiss_idx.ntotal, "status": "success"} # type: ignore
    
    try: return await server.run_in_executor(embed_path_sync_blocking_op)
    except ValueError as ve: raise ve # Path validation errors
    except RuntimeError as rte: # Errors from embedding/FAISS logic
        raise server.create_custom_error(FS_CUSTOM_ERROR_BASE -2, str(rte), {"path": target_path_str}) from rte


@fs_server.register("mcp.fs.search")
async def handle_fs_search(server: MCPServer, request_id: str, params: List[Any]):
    if not server.embedding_enabled: # type: ignore
        raise RuntimeError("Search system is disabled for this server instance.")

    # Ensure embedding system is ready (model/index loaded)
    # This is a sync call within the async handler, so needs to be run in executor
    # if it might block for loading. The startup hook should have handled pre-loading.
    def _ensure_search_ready_sync():
        _get_embedding_model_sync(server) # Ensures model loaded
        _get_faiss_index_sync(server)     # Ensures index loaded
        if server.faiss_index is None or server.faiss_index.ntotal == 0: # type: ignore
            raise RuntimeError("Search index is not ready or is empty.")
    await server.run_in_executor(_ensure_search_ready_sync)


    query_text = params[0]
    top_k = int(params[1]) if len(params) > 1 else 5
    scope_path_user_str = params[2] if len(params) > 2 else None
    
    scope_path_abs: Optional[Path] = None
    # Client path for scope, e.g. "/documents/projectA"
    # This needs to be resolved to an absolute disk path for actual file reading for previews,
    # and to a client-facing path prefix for metadata filtering.
    _virtual_root_path_resolved = Path(FS_VIRTUAL_ROOT_ENV_STR or DEFAULT_VIRTUAL_ROOT_STR).resolve()
    scope_path_client_prefix: Optional[str] = None

    if scope_path_user_str:
        scope_path_abs = _validate_fs_path_against_server_root(scope_path_user_str, check_exists=True, must_be_dir=True)
        scope_path_client_prefix = str(Path("/") / scope_path_abs.relative_to(_virtual_root_path_resolved))
        if not scope_path_client_prefix.endswith("/"): scope_path_client_prefix += "/"


    def search_sync_blocking_op():
        embedding_model = server.embedding_model # Assumed loaded
        faiss_idx: faiss.Index = server.faiss_index # type: ignore # Assumed loaded

        query_embedding = embedding_model.encode([query_text])[0].astype('float32').reshape(1, -1) # type: ignore
        
        search_k_faiss = max(top_k * 5, 20) if scope_path_client_prefix else top_k
        search_k_faiss = min(search_k_faiss, faiss_idx.ntotal)
        if search_k_faiss == 0: return []

        distances, faiss_ids_retrieved = faiss_idx.search(query_embedding, k=search_k_faiss)
        
        _results_list = []
        for i in range(len(faiss_ids_retrieved[0])):
            retrieved_faiss_id = faiss_ids_retrieved[0][i]
            if retrieved_faiss_id == -1: continue
            
            meta_item = next((item for item in server.faiss_index_metadata if item['id'] == retrieved_faiss_id), None) # type: ignore
            if not meta_item: continue
            
            # meta_item['path'] is already the client-facing path (e.g., "/docs/file.txt")
            path_for_client_found = meta_item['path'] 
            
            if scope_path_client_prefix and not path_for_client_found.startswith(scope_path_client_prefix):
                continue

            distance_val = distances[0][i]
            similarity = float(1.0 / (1.0 + distance_val))
            
            preview_text = ""
            try:
                # For preview, convert client path back to absolute disk path
                abs_file_path_for_preview = (_virtual_root_path_resolved / path_for_client_found.lstrip('/')).resolve()
                if abs_file_path_for_preview.is_file() and str(abs_file_path_for_preview).startswith(str(_virtual_root_path_resolved)): # Security check
                    with open(abs_file_path_for_preview, 'r', encoding='utf-8', errors='ignore') as pf:
                        preview_text = pf.read(250).strip() # Slightly longer preview
                        if len(preview_text) == 250 : preview_text += "..."
            except Exception as e_preview:
                server.logger.debug(f"Could not generate preview for {abs_file_path_for_preview}: {e_preview}")

            _results_list.append({"path": path_for_client_found, "score": round(similarity, 4), "preview": preview_text})
            if len(_results_list) >= top_k: break # Stop after collecting top_k valid results
        
        # Re-sort by score if filtering was applied, as FAISS order might not be perfect for the subset
        _results_list.sort(key=lambda x: x['score'], reverse=True) # Ensure highest score first
        
        return _results_list[:top_k] # Return final top_k

    try: return await server.run_in_executor(search_sync_blocking_op)
    except RuntimeError as rte: # Errors from search logic (e.g. index empty)
        raise server.create_custom_error(FS_CUSTOM_ERROR_BASE -3, str(rte), {"query": query_text}) from rte
    except ValueError as ve: raise ve # Path validation errors for scope_path


# --- Main Entry Point (using the framework) ---
if __name__ == "__main__":
    if not (FS_VIRTUAL_ROOT_ENV_STR or DEFAULT_VIRTUAL_ROOT_STR): # Critical for fs server
        fs_server.logger.critical(f"FS Server cannot start: Neither LLMBDO_{SERVER_NAME.upper()}_VIRTUAL_ROOT env var nor DEFAULT_VIRTUAL_ROOT_STR is defined.")
        exit(1)
    fs_server.logger.info(f"FS Server '{SERVER_NAME}' starting with virtual root determined by ENV or default...")
    try:
        asyncio.run(fs_server.start()) # MCPServer should handle its own socket creation and loop
    except KeyboardInterrupt:
        fs_server.logger.info(f"FS Server '{SERVER_NAME}' (main) stopped by KeyboardInterrupt.")
    except Exception as e_main_fs:
        fs_server.logger.critical(f"FS Server '{SERVER_NAME}' (main) crashed: {e_main_fs}", exc_info=True)
    finally:
        fs_server.logger.info(f"FS Server '{SERVER_NAME}' (main) exiting.")