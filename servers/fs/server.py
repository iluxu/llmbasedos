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
from llmbasedos.mcp_server_framework import MCPServer
from llmbasedos.common_utils import validate_mcp_path_param

# --- Server Specific Configuration ---
SERVER_NAME = "fs"
CAPS_FILE_PATH_STR = str(Path(__file__).parent / "caps.json") # MCPServer expects string
FS_CUSTOM_ERROR_BASE = -32010 # Base for FS specific errors

# Embedding config
EMBEDDING_MODEL_NAME_CONF = os.getenv("LLMBDO_FS_EMBED_MODEL", 'all-MiniLM-L6-v2' if EMBEDDING_SYSTEM_AVAILABLE else None)
FAISS_INDEX_DIR_CONF = Path(os.getenv("LLMBDO_FS_FAISS_DIR", f"/var/lib/llmbasedos/faiss_indices/{SERVER_NAME}"))
FAISS_INDEX_FILE_CONF = FAISS_INDEX_DIR_CONF / "index.faiss"
FAISS_METADATA_FILE_CONF = FAISS_INDEX_DIR_CONF / "metadata.json"

# Virtual root for this FS server. All paths from MCP calls will be validated against this.
# Can be configured via environment variable.
FS_VIRTUAL_ROOT_STR = os.getenv(f"LLMBDO_{SERVER_NAME.upper()}_VIRTUAL_ROOT") # If None, common_utils.DEFAULT_VIRTUAL_ROOT will be used

# Initialize server instance
fs_server = MCPServer(SERVER_NAME, CAPS_FILE_PATH_STR, custom_error_code_base=FS_CUSTOM_ERROR_BASE)

# Attach embedding-specific state to the server instance if system is available
if EMBEDDING_SYSTEM_AVAILABLE:
    fs_server.embedding_model = None # type: ignore # Will be SentenceTransformer instance
    fs_server.faiss_index = None     # type: ignore # Will be faiss.Index instance
    fs_server.faiss_index_metadata: List[Dict[str, Any]] = [] # List of {"id": int, "path": str}
    fs_server.faiss_next_id: int = 0
    FAISS_INDEX_DIR_CONF.mkdir(parents=True, exist_ok=True) # Ensure directory exists
else:
    fs_server.logger.warning("Embedding system not available due to missing libraries (sentence-transformers, faiss, numpy). Embed/search capabilities will be disabled.")


# --- Path Validation Helper for FS Server ---
def _validate_fs_path(path_param: Any, check_exists: bool = False,
                      must_be_dir: Optional[bool] = None, must_be_file: Optional[bool] = None) -> Path:
    """
    Validates a path parameter specifically for this FS server.
    Uses the server's configured virtual root (FS_VIRTUAL_ROOT_STR) or the default.
    Raises ValueError with a user-friendly message on failure.
    """
    # Shell/client should resolve relative paths to CWD before sending to server.
    # Server always expects paths to be "absolute-like" from its perspective.
    # validate_mcp_path_param handles this by resolving against virtual_root if path is not os.path.isabs().
    # However, for clarity, FS server should ideally receive paths that are already conceptually absolute
    # within its operational scope. `relative_to_cwd_str` is None here.
    resolved_path, err_msg = validate_mcp_path_param(
        path_param,
        virtual_root_str=FS_VIRTUAL_ROOT_STR, # Pass server-specific virtual root if defined
        relative_to_cwd_str=None, # FS server methods typically operate on "absolute" given paths
        check_exists=check_exists,
        must_be_dir=must_be_dir,
        must_be_file=must_be_file
    )
    if err_msg:
        raise ValueError(err_msg) # MCPServer framework will catch this
    if resolved_path is None: # Should not happen if err_msg is None, but defensive
        raise ValueError("Path validation failed unexpectedly.")
    return resolved_path

# --- File System Capability Handlers (decorated) ---
@fs_server.register("mcp.fs.list")
async def handle_fs_list(server: MCPServer, request_id: str, params: List[Any]):
    target_path = _validate_fs_path(params[0], check_exists=True, must_be_dir=True)

    def list_dir_sync(): # Runs in executor
        listed_items = []
        for item in target_path.iterdir():
            try:
                stat_info = item.stat() # Could fail for broken symlinks or permissions
                item_type = "other"
                if item.is_file(): item_type = "file"
                elif item.is_dir(): item_type = "directory"
                elif item.is_symlink(): item_type = "symlink"
                
                listed_items.append({
                    "name": item.name, "type": item_type,
                    "size": stat_info.st_size if item_type != "directory" else -1, # Consistent size for dirs
                    "modified_at": datetime.fromtimestamp(stat_info.st_mtime, tz=timezone.utc).isoformat()
                })
            except OSError as stat_err:
                server.logger.warning(f"Could not stat item {item.name} in {target_path}: {stat_err}")
                listed_items.append({"name": item.name, "type": "inaccessible", "size": -1, "modified_at": None})
        return listed_items
    
    try:
        return await server.run_in_executor(list_dir_sync)
    except PermissionError as pe: # From iterdir itself if target_path is inaccessible
        raise PermissionError(f"Permission denied listing '{target_path}': {pe}")


@fs_server.register("mcp.fs.read")
async def handle_fs_read(server: MCPServer, request_id: str, params: List[Any]):
    target_file = _validate_fs_path(params[0], check_exists=True, must_be_file=True)
    encoding_type = params[1] if len(params) > 1 else "text" # Validated by schema

    def read_file_sync(): # Runs in executor
        _mime_type = "application/octet-stream"
        try: _mime_type = magic.from_file(str(target_file), mime=True)
        except Exception as magic_e: server.logger.warning(f"python-magic error for {target_file}: {magic_e}")

        if encoding_type == "text":
            try: _content = target_file.read_text(encoding="utf-8")
            except UnicodeDecodeError: raise ValueError(f"File '{target_file}' not valid UTF-8. Try 'base64' encoding.")
        else: # base64
            _content_bytes = target_file.read_bytes()
            _content = base64.b64encode(_content_bytes).decode('ascii')
        return {"path": str(target_file), "content": _content, "encoding": encoding_type, "mime_type": _mime_type}

    try:
        return await server.run_in_executor(read_file_sync)
    except PermissionError as pe: raise PermissionError(f"Permission denied reading '{target_file}': {pe}")


@fs_server.register("mcp.fs.write")
async def handle_fs_write(server: MCPServer, request_id: str, params: List[Any]):
    target_file = _validate_fs_path(params[0], check_exists=False) # File doesn't need to exist
    content_str = params[1]
    encoding_type = params[2] if len(params) > 2 else "text"
    append_mode = params[3] if len(params) > 3 else False

    if not target_file.parent.is_dir(): # Ensure parent dir exists and is a dir
         raise ValueError(f"Parent directory for '{target_file}' does not exist or is not a directory.")
    if target_file.exists() and target_file.is_dir():
        raise ValueError(f"Cannot write to '{target_file}', it is an existing directory.")

    def write_file_sync(): # Runs in executor
        _content_bytes: bytes
        if encoding_type == "text": _content_bytes = content_str.encode('utf-8')
        else: # base64
            try: _content_bytes = base64.b64decode(content_str)
            except Exception: raise ValueError("Invalid base64 content for writing.")

        _mode = 'ab' if append_mode else 'wb'
        with target_file.open(_mode) as f: bytes_written = f.write(_content_bytes)
        return {"path": str(target_file), "bytes_written": bytes_written, "status": "success"}

    try:
        return await server.run_in_executor(write_file_sync)
    except PermissionError as pe: raise PermissionError(f"Permission denied writing to '{target_file}': {pe}")
    # ValueError for bad base64 or parent dir issues will be caught by framework


@fs_server.register("mcp.fs.delete")
async def handle_fs_delete(server: MCPServer, request_id: str, params: List[Any]):
    target_path = _validate_fs_path(params[0], check_exists=True) # Must exist to delete
    recursive = params[1] if len(params) > 1 else False

    def delete_path_sync(): # Runs in executor
        if target_path.is_file() or target_path.is_symlink(): target_path.unlink()
        elif target_path.is_dir():
            if recursive: shutil.rmtree(target_path)
            else:
                try: target_path.rmdir() # Fails if not empty
                except OSError: raise ValueError(f"Directory '{target_path}' not empty. Use recursive=true to delete.")
        else: raise ValueError(f"Path '{target_path}' is an unknown type for deletion.")
        return {"path": str(target_path), "status": "success"}

    try:
        return await server.run_in_executor(delete_path_sync)
    except PermissionError as pe: raise PermissionError(f"Permission denied deleting '{target_path}': {pe}")


# --- Embedding and Search (Refactored for MCPServer and executor) ---
def _get_embedding_model(server_ref: MCPServer) -> SentenceTransformer: # type: ignore
    if not EMBEDDING_SYSTEM_AVAILABLE: raise RuntimeError("Embedding system unavailable (libs missing).")
    if server_ref.embedding_model is None: # type: ignore
        server_ref.logger.info(f"Lazy loading sentence transformer model: {EMBEDDING_MODEL_NAME_CONF}")
        if not EMBEDDING_MODEL_NAME_CONF: raise RuntimeError("Embedding model name not configured.")
        try:
            # This is blocking, should be called from an executor context if first time during request
            server_ref.embedding_model = SentenceTransformer(EMBEDDING_MODEL_NAME_CONF) # type: ignore
        except Exception as e:
            server_ref.logger.error(f"Failed to load ST model '{EMBEDDING_MODEL_NAME_CONF}': {e}", exc_info=True)
            raise RuntimeError(f"Could not load embedding model: {e}")
    return server_ref.embedding_model # type: ignore

def _get_faiss_index(server_ref: MCPServer, ensure_init: bool = True) -> faiss.IndexIDMap: # type: ignore
    if not EMBEDDING_SYSTEM_AVAILABLE: raise RuntimeError("FAISS system unavailable (libs missing).")
    if server_ref.faiss_index is None and ensure_init: # type: ignore
        server_ref.logger.info("Lazy initializing FAISS index...")
        if FAISS_INDEX_FILE_CONF.exists() and FAISS_METADATA_FILE_CONF.exists():
            try:
                server_ref.logger.info(f"Loading FAISS index from {FAISS_INDEX_FILE_CONF}")
                server_ref.faiss_index = faiss.read_index(str(FAISS_INDEX_FILE_CONF)) # type: ignore
                with FAISS_METADATA_FILE_CONF.open('r') as f: server_ref.faiss_index_metadata = json.load(f) # type: ignore
                # Recalculate next_id based on loaded metadata
                if server_ref.faiss_index_metadata: # type: ignore
                    server_ref.faiss_next_id = max(item['id'] for item in server_ref.faiss_index_metadata) + 1 # type: ignore
                else: server_ref.faiss_next_id = 0 # type: ignore
                server_ref.logger.info(f"FAISS index loaded with {server_ref.faiss_index.ntotal if server_ref.faiss_index else 0} vectors.") # type: ignore
            except Exception as e:
                server_ref.logger.error(f"Failed to load FAISS index/metadata: {e}. Creating new.", exc_info=True)
                server_ref.faiss_index = None; server_ref.faiss_index_metadata = []; server_ref.faiss_next_id = 0 # type: ignore
        
        if server_ref.faiss_index is None: # type: ignore # If loading failed or index doesn't exist
            model_for_dim = _get_embedding_model(server_ref) # Ensures model is loaded to get dim
            embedding_dim = model_for_dim.get_sentence_embedding_dimension() # type: ignore
            server_ref.logger.info(f"Creating new FAISS index (dim: {embedding_dim}).")
            server_ref.faiss_index = faiss.IndexIDMap(faiss.IndexFlatL2(embedding_dim)) # type: ignore
            server_ref.faiss_index_metadata = []; server_ref.faiss_next_id = 0 # type: ignore
    
    if server_ref.faiss_index is None: # type: ignore # Should not happen if ensure_init is True and no errors
        raise RuntimeError("FAISS index could not be initialized.")
    return server_ref.faiss_index # type: ignore

def _save_faiss_index_sync(server_ref: MCPServer): # Blocking
    if EMBEDDING_SYSTEM_AVAILABLE and server_ref.faiss_index is not None: # type: ignore
        try:
            server_ref.logger.info(f"Saving FAISS index ({server_ref.faiss_index.ntotal} vectors) & metadata...") # type: ignore
            FAISS_INDEX_DIR_CONF.mkdir(parents=True, exist_ok=True) # Ensure dir exists
            faiss.write_index(server_ref.faiss_index, str(FAISS_INDEX_FILE_CONF)) # type: ignore
            with FAISS_METADATA_FILE_CONF.open('w') as f: json.dump(server_ref.faiss_index_metadata, f) # type: ignore
            server_ref.logger.info("FAISS index and metadata saved.")
        except Exception as e: server_ref.logger.error(f"Failed to save FAISS index/metadata: {e}", exc_info=True)


@fs_server.register("mcp.fs.embed")
async def handle_fs_embed(server: MCPServer, request_id: str, params: List[Any]):
    if not EMBEDDING_SYSTEM_AVAILABLE: raise RuntimeError("Embedding system disabled (missing libraries).")
    
    target_path_obj = _validate_fs_path(params[0], check_exists=True)
    recursive = params[1] if len(params) > 1 else False

    def embed_path_sync_blocking(): # Runs in executor
        # Ensure model and index are loaded/initialized (this itself might involve IO/CPU)
        # These calls are blocking if model/index not yet loaded.
        embedding_model = _get_embedding_model(server)
        faiss_idx = _get_faiss_index(server) # type: ignore

        _files_to_process: List[Path] = []
        if target_path_obj.is_file(): _files_to_process.append(target_path_obj)
        elif target_path_obj.is_dir():
            glob_pattern = "**/*" if recursive else "*"
            for item in target_path_obj.glob(glob_pattern):
                if item.is_file(): _files_to_process.append(item)
        else: raise ValueError(f"Path '{target_path_obj}' not a file or directory for embedding.")

        _processed_count = 0; MAX_FILE_SIZE = 100 * 1024 # Configurable
        _new_embeddings_list = []; _new_metadata_batch_list = []

        # Create a set of existing paths for faster lookups
        existing_embedded_paths = {item['path'] for item in server.faiss_index_metadata} # type: ignore

        for file_p in _files_to_process:
            if str(file_p) in existing_embedded_paths: # Check if already embedded
                server.logger.debug(f"Skipping already embedded file: {file_p}")
                # TODO: Re-embed if mtime changed or forced.
                continue
            try:
                if file_p.stat().st_size > MAX_FILE_SIZE: server.logger.warning(f"Skipping large file {file_p} for embedding."); continue
                content = file_p.read_text(encoding='utf-8', errors='ignore')
                if not content.strip(): server.logger.debug(f"Skipping empty file {file_p}"); continue
                
                # Actual embedding is CPU bound
                embedding_vector = embedding_model.encode([content])[0] # type: ignore
                _new_embeddings_list.append(embedding_vector.astype('float32'))
                _new_metadata_batch_list.append({"id": server.faiss_next_id, "path": str(file_p)}) # type: ignore
                server.faiss_next_id += 1; _processed_count += 1 # type: ignore
            except Exception as e_emb: server.logger.error(f"Error embedding file {file_p}: {e_emb}")
        
        if _new_embeddings_list:
            try:
                embeddings_np_arr = np.array(_new_embeddings_list) # type: ignore
                ids_np_arr = np.array([m['id'] for m in _new_metadata_batch_list], dtype='int64') # type: ignore
                faiss_idx.add_with_ids(embeddings_np_arr, ids_np_arr) # type: ignore
                server.faiss_index_metadata.extend(_new_metadata_batch_list) # type: ignore
                _save_faiss_index_sync(server) # Save after batch (blocking)
                server.logger.info(f"Added {len(_new_embeddings_list)} new embeddings to FAISS index.")
            except Exception as e_faiss_add:
                server.logger.error(f"Error adding embeddings to FAISS index: {e_faiss_add}", exc_info=True)
                raise RuntimeError(f"Failed to update search index: {e_faiss_add}") # Propagate
        
        return {"path": str(target_path_obj), "files_processed_for_embedding": _processed_count,
                "total_embeddings_in_index": faiss_idx.ntotal, "status": "success"}
    
    return await server.run_in_executor(embed_path_sync_blocking)


@fs_server.register("mcp.fs.search")
async def handle_fs_search(server: MCPServer, request_id: str, params: List[Any]):
    if not EMBEDDING_SYSTEM_AVAILABLE: raise RuntimeError("Search system disabled.")
    # Ensure model and index are loaded/initialized (can be blocking first time)
    # This should ideally be done on server startup or explicitly by admin command.
    # For on-demand, run in executor.
    def ensure_search_system_ready_sync():
        _get_embedding_model(server) # Ensures model is loaded
        _get_faiss_index(server)     # Ensures index is loaded/created
        if server.faiss_index is None or server.faiss_index.ntotal == 0: # type: ignore
            raise RuntimeError("Search system not ready or index is empty.")
    await server.run_in_executor(ensure_search_system_ready_sync) # Run sync checks in executor

    query_text = params[0]
    top_k = int(params[1]) if len(params) > 1 else 5
    scope_path_str = params[2] if len(params) > 2 else None
    
    scope_path_obj: Optional[Path] = None
    if scope_path_str:
        scope_path_obj = _validate_fs_path(scope_path_str, check_exists=True, must_be_dir=True)

    def search_sync_blocking(): # Runs in executor
        embedding_model = server.embedding_model # Already loaded by ensure_search_system_ready_sync
        faiss_idx = server.faiss_index # type: ignore # Already loaded
        
        query_embedding = embedding_model.encode([query_text])[0].astype('float32').reshape(1, -1) # type: ignore
        
        # FAISS search for k results (k should be larger if filtering by scope, then slice)
        # For simplicity, search for more and then filter.
        search_k = max(top_k * 5, 20) if scope_path_obj else top_k # Search more if filtering
        search_k = min(search_k, faiss_idx.ntotal) # Don't ask for more than available
        if search_k == 0: return [] # Index is empty

        distances, faiss_ids_retrieved = faiss_idx.search(query_embedding, k=search_k)
        
        _results_list = []
        for i in range(len(faiss_ids_retrieved[0])):
            retrieved_faiss_id = faiss_ids_retrieved[0][i]
            if retrieved_faiss_id == -1: continue # No more results from FAISS
            
            meta_item = next((item for item in server.faiss_index_metadata if item['id'] == retrieved_faiss_id), None) # type: ignore
            if not meta_item: continue
            
            file_path_str_found = meta_item['path']
            if scope_path_obj and not file_path_str_found.startswith(str(scope_path_obj)):
                continue # Filter by scope

            distance_val = distances[0][i]
            similarity = float(1.0 / (1.0 + distance_val)) # Example L2 to similarity
            
            preview = "" # TODO: Add preview fetching (careful with IO in this sync func)
            _results_list.append({"path": file_path_str_found, "score": round(similarity, 4), "preview": preview})
            if len(_results_list) >= top_k and not scope_path_obj: # If no scope filter, FAISS results are sorted
                break 
        
        # If scope was applied, results might not be sorted optimally or be less than top_k
        if scope_path_obj:
            _results_list.sort(key=lambda x: x['score'], reverse=True)
        
        return _results_list[:top_k]

    return await server.run_in_executor(search_sync_blocking)


# --- Server Lifecycle Hooks (Example) ---
async def on_fs_server_startup(server: MCPServer):
    server.logger.info(f"FS Server '{server.server_name}' custom startup actions...")
    # Pre-load embedding model and FAISS index in background if EMBEDDING_SYSTEM_AVAILABLE
    if EMBEDDING_SYSTEM_AVAILABLE:
        server.logger.info("Initiating background load of embedding model and FAISS index...")
        # Don't await here to keep startup fast, let them load in executor.
        # First call to embed/search will await its completion if still loading.
        asyncio.create_task(server.run_in_executor(_get_faiss_index, server)) # type: ignore

async def on_fs_server_shutdown(server: MCPServer):
    server.logger.info(f"FS Server '{server.server_name}' custom shutdown actions...")
    if EMBEDDING_SYSTEM_AVAILABLE and server.faiss_index is not None: # type: ignore
        # Run save in executor to ensure it completes even if main loop is stopping
        server.logger.info("Requesting FAISS index save on shutdown...")
        # This might be tricky if executor is already shutting down.
        # MCPServer framework's executor shutdown should wait.
        try:
            await server.run_in_executor(_save_faiss_index_sync, server)
        except Exception as e_save:
            server.logger.error(f"Failed to save FAISS index during shutdown: {e_save}")

# Assign hooks to server instance if MCPServer framework supports it by convention
# For now, these are called manually in __main__
# fs_server.on_startup = on_fs_server_startup
# fs_server.on_shutdown = on_fs_server_shutdown

# --- Main Entry Point (using the framework) ---
if __name__ == "__main__":
    # Basic logging for the script itself if run directly
    script_logger = logging.getLogger("llmbasedos.servers.fs_script_main") # Unique name
    script_logger.setLevel(os.getenv(f"LLMBDO_{SERVER_NAME.upper()}_LOG_LEVEL", "INFO").upper())
    if not script_logger.hasHandlers(): # Avoid adding handlers if already configured by MCPServer import
        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter(f"%(asctime)s - MAIN SCRIPT - %(levelname)s - %(message)s"))
        script_logger.addHandler(ch)

    main_event_loop = asyncio.get_event_loop()
    try:
        # If MCPServer class itself handles on_startup/on_shutdown hooks in its start method:
        # main_event_loop.run_until_complete(fs_server.start())

        # Manual hook calling for now:
        main_event_loop.run_until_complete(on_fs_server_startup(fs_server))
        main_event_loop.run_until_complete(fs_server.start())

    except KeyboardInterrupt:
        script_logger.info(f"Server '{SERVER_NAME}' stopped by KeyboardInterrupt.")
    except Exception as e_main: # Catch errors during startup itself
        script_logger.error(f"FS Server failed to start or run: {e_main}", exc_info=True)
    finally:
        script_logger.info(f"FS Server shutting down main process...")
        # Ensure shutdown hook is called if server.start() didn't complete or handle it
        # This is complex if server.start() blocks forever.
        # A better MCPServer would have a .stop() method or handle signals to trigger its own finally block.
        # For now, assume server.start()'s finally block runs on CancelledError.
        # If on_fs_server_shutdown is awaitable and does IO, it needs a running loop.
        if main_event_loop.is_running():
             main_event_loop.run_until_complete(on_fs_server_shutdown(fs_server))
        else: # Loop closed, try to run sync parts if any critical non-async cleanup
            script_logger.warning("Event loop closed before async shutdown hook could run for FS server.")
            if EMBEDDING_SYSTEM_AVAILABLE and fs_server.faiss_index is not None: # type: ignore
                _save_faiss_index_sync(fs_server) # Try sync save as last resort

        script_logger.info(f"FS Server main process finished.")
