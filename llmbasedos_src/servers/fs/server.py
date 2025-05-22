# llmbasedos_pkg/servers/fs/server.py
import asyncio
# logging sera géré par MCPServer, pas besoin d'importer directement ici.
import os
import shutil
from pathlib import Path
from datetime import datetime, timezone
import base64
import magic # For MIME types
from typing import Any, Dict, List, Optional, Tuple, Union
import json # For FAISS metadata

# Imports du projet
from llmbasedos.mcp_server_framework import MCPServer 
from llmbasedos.common_utils import validate_mcp_path_param, DEFAULT_VIRTUAL_ROOT_STR as COMMON_DEFAULT_VIRTUAL_ROOT_STR

# Embedding and Search related imports
try:
    from sentence_transformers import SentenceTransformer
    import faiss
    import numpy as np
    EMBEDDING_SYSTEM_AVAILABLE = True
except ImportError:
    EMBEDDING_SYSTEM_AVAILABLE = False
    SentenceTransformer = type(None) # type: ignore
    faiss = type(None) # type: ignore
    np = type(None) # type: ignore

# --- Server Specific Configuration ---
SERVER_NAME = "fs"
CAPS_FILE_PATH_STR = str(Path(__file__).parent / "caps.json")
FS_CUSTOM_ERROR_BASE = -32010 # Base for FS specific errors

# Embedding config: Read from ENV, provide defaults.
EMBEDDING_MODEL_NAME_CONF = os.getenv("LLMBDO_FS_EMBEDDING_MODEL", 'all-MiniLM-L6-v2' if EMBEDDING_SYSTEM_AVAILABLE else "disabled")
_faiss_dir_default_str = "/var/lib/llmbasedos/faiss_indices_fs" # Nom plus spécifique pour FS
FAISS_INDEX_DIR_STR = os.getenv("LLMBDO_FS_FAISS_DIR", _faiss_dir_default_str)
FAISS_INDEX_DIR_PATH_CONF = Path(FAISS_INDEX_DIR_STR).resolve()

FAISS_INDEX_FILE_PATH_CONF = FAISS_INDEX_DIR_PATH_CONF / "fs_index.faiss"
FAISS_METADATA_FILE_PATH_CONF = FAISS_INDEX_DIR_PATH_CONF / "fs_metadata.json"

# Virtual root for this FS server.
# Utilise LLMBDO_FS_VIRTUAL_ROOT si défini, sinon le DEFAULT_VIRTUAL_ROOT_STR de common_utils.
# LLMBDO_FS_DATA_ROOT est utilisé dans le Dockerfile/Compose pour le point de montage.
# Idéalement, LLMBDO_FS_VIRTUAL_ROOT devrait correspondre à LLMBDO_FS_DATA_ROOT.
FS_VIRTUAL_ROOT_STR = os.getenv(f"LLMBDO_{SERVER_NAME.upper()}_VIRTUAL_ROOT", os.getenv("LLMBDO_FS_DATA_ROOT", COMMON_DEFAULT_VIRTUAL_ROOT_STR))

# Initialize server instance
fs_server = MCPServer(SERVER_NAME, CAPS_FILE_PATH_STR, custom_error_code_base=FS_CUSTOM_ERROR_BASE)

# Attach embedding-specific state to the server instance (will be initialized in on_startup)
fs_server.embedding_enabled: bool = False # type: ignore
fs_server.embedding_model: Optional[SentenceTransformer] = None # type: ignore
fs_server.faiss_index: Optional[faiss.Index] = None # type: ignore
fs_server.faiss_index_metadata: List[Dict[str, Any]] = [] # type: ignore
fs_server.faiss_next_id: int = 0 # type: ignore


# --- Path Validation Helper for FS Server (using common_utils) ---
def _validate_fs_path(
        path_param_from_client: Any, # Ce que le client envoie (ex: "/documents/file.txt" ou "file.txt")
        check_exists: bool = False,
        must_be_dir: Optional[bool] = None,
        must_be_file: Optional[bool] = None
    ) -> Path:
    """
    Wrapper for validate_mcp_path_param using this server's FS_VIRTUAL_ROOT_STR.
    The path_param_from_client is interpreted as relative to the FS_VIRTUAL_ROOT_STR.
    Example: if FS_VIRTUAL_ROOT_STR is /mnt/user_data, and client sends "/docs/file.txt",
    it's resolved against /mnt/user_data, effectively becoming /mnt/user_data/docs/file.txt.
    If client sends "docs/file.txt", it also becomes /mnt/user_data/docs/file.txt.
    Raises ValueError on failure.
    """
    # `validate_mcp_path_param` expects path_param to be relative to virtual_root if not absolute.
    # If client sends "/foo.txt", it means "foo.txt" inside virtual_root.
    path_to_validate = str(path_param_from_client).lstrip('/\\') # Remove leading slashes to make it relative to virtual_root

    resolved_path, err_msg = validate_mcp_path_param(
        path_param=path_to_validate, # path_param is now relative to virtual_root
        virtual_root_str=FS_VIRTUAL_ROOT_STR, # This server's specific root
        check_exists=check_exists,
        must_be_dir=must_be_dir,
        must_be_file=must_be_file,
        allow_outside_virtual_root=False # FS server MUST confine to its virtual root
    )
    if err_msg:
        fs_server.logger.warning(f"Path validation failed for '{path_param_from_client}' (resolved against '{FS_VIRTUAL_ROOT_STR}'): {err_msg}")
        raise ValueError(f"Invalid path or permissions for '{path_param_from_client}'. Details: {err_msg}")
    if resolved_path is None:
        fs_server.logger.error(f"Path validation for '{path_param_from_client}' returned no error but no path.")
        raise ValueError("Path validation failed unexpectedly.")
    return resolved_path

def _get_client_facing_path(abs_disk_path: Path) -> str:
    """Converts an absolute disk path back to a client-facing path (relative to virtual root, starts with /)."""
    virtual_root_path = Path(FS_VIRTUAL_ROOT_STR).resolve()
    try:
        relative_path = abs_disk_path.relative_to(virtual_root_path)
        return "/" + str(relative_path)
    except ValueError: # Path is not under virtual_root (should not happen if validation is correct)
        fs_server.logger.error(f"Cannot make client-facing path for {abs_disk_path}, not under {virtual_root_path}")
        return str(abs_disk_path) # Fallback, but indicates an issue


# --- Embedding and Search (Sync blocking functions for executor) ---
def _load_embedding_model_sync(server: MCPServer):
    if not server.embedding_enabled: return # type: ignore
    if server.embedding_model is None: # type: ignore
        server.logger.info(f"Loading sentence transformer model: {EMBEDDING_MODEL_NAME_CONF}")
        if not EMBEDDING_MODEL_NAME_CONF or EMBEDDING_MODEL_NAME_CONF == "disabled":
            server.logger.warning("Embedding model name not configured or disabled. Cannot load.")
            server.embedding_enabled = False; return # type: ignore
        try:
            server.embedding_model = SentenceTransformer(EMBEDDING_MODEL_NAME_CONF) # type: ignore
        except Exception as e:
            server.logger.error(f"Failed to load ST model '{EMBEDDING_MODEL_NAME_CONF}': {e}", exc_info=True)
            server.embedding_enabled = False; raise # type: ignore

def _load_faiss_index_sync(server: MCPServer):
    if not server.embedding_enabled: return # type: ignore
    if server.faiss_index is not None: return # type: ignore
    
    server.logger.info(f"Initializing/Loading FAISS index from: {FAISS_INDEX_FILE_PATH_CONF}")
    if FAISS_INDEX_FILE_PATH_CONF.exists() and FAISS_METADATA_FILE_PATH_CONF.exists():
        try:
            server.faiss_index = faiss.read_index(str(FAISS_INDEX_FILE_PATH_CONF)) # type: ignore
            with FAISS_METADATA_FILE_PATH_CONF.open('r') as f: server.faiss_index_metadata = json.load(f) # type: ignore
            if server.faiss_index_metadata: # type: ignore
                server.faiss_next_id = max(item['id'] for item in server.faiss_index_metadata) + 1 if server.faiss_index_metadata else 0 # type: ignore
            server.logger.info(f"FAISS index loaded with {server.faiss_index.ntotal if server.faiss_index else 0} vectors.") # type: ignore
            return
        except Exception as e:
            server.logger.error(f"Failed to load FAISS index/metadata from {FAISS_INDEX_DIR_PATH_CONF}: {e}. Will create new.", exc_info=True)
            # Reset to ensure clean state for new index creation
            server.faiss_index = None; server.faiss_index_metadata = []; server.faiss_next_id = 0 # type: ignore
    
    # Create new index if loading failed or files don't exist
    if server.embedding_model is None: _load_embedding_model_sync(server) # Ensure model is loaded to get dim
    if not server.embedding_enabled: return # type: ignore

    embedding_dim = server.embedding_model.get_sentence_embedding_dimension() # type: ignore
    server.logger.info(f"Creating new FAISS index (dim: {embedding_dim}) at {FAISS_INDEX_DIR_PATH_CONF}.")
    server.faiss_index = faiss.IndexIDMap(faiss.IndexFlatL2(embedding_dim)) # type: ignore
    server.faiss_index_metadata = []; server.faiss_next_id = 0 # type: ignore
    _save_faiss_index_sync(server) # Save empty index and metadata

def _save_faiss_index_sync(server: MCPServer):
    if server.embedding_enabled and server.faiss_index is not None: # type: ignore
        try:
            FAISS_INDEX_DIR_PATH_CONF.mkdir(parents=True, exist_ok=True)
            server.logger.info(f"Saving FAISS index ({server.faiss_index.ntotal} vectors) & metadata to {FAISS_INDEX_DIR_PATH_CONF}...") # type: ignore
            faiss.write_index(server.faiss_index, str(FAISS_INDEX_FILE_PATH_CONF)) # type: ignore
            with FAISS_METADATA_FILE_PATH_CONF.open('w') as f: json.dump(server.faiss_index_metadata, f) # type: ignore
            server.logger.info("FAISS index and metadata saved.")
        except Exception as e: server.logger.error(f"Failed to save FAISS index/metadata: {e}", exc_info=True)


# --- Server Lifecycle Hooks ---
async def on_fs_server_startup(server: MCPServer):
    server.logger.info(f"FS Server '{server.server_name}' on_startup hook running...")
    
    # Initialize embedding_enabled state based on config and available libraries
    server.embedding_enabled = EMBEDDING_SYSTEM_AVAILABLE and (EMBEDDING_MODEL_NAME_CONF != "disabled") # type: ignore
    if not server.embedding_enabled: # type: ignore
        if not EMBEDDING_SYSTEM_AVAILABLE:
            server.logger.warning("Embedding system dependencies (sentence-transformers, faiss, numpy) missing. Embed/search capabilities disabled.")
        elif EMBEDDING_MODEL_NAME_CONF == "disabled":
            server.logger.info("Embedding model name configured as 'disabled'. Embed/search capabilities disabled.")
        return

    # Create FAISS directory if it doesn't exist
    try:
        FAISS_INDEX_DIR_PATH_CONF.mkdir(parents=True, exist_ok=True)
        server.logger.info(f"FAISS index directory ensured at: {FAISS_INDEX_DIR_PATH_CONF}")
    except OSError as e:
        server.logger.error(f"Could not create FAISS directory {FAISS_INDEX_DIR_PATH_CONF}: {e}. Embedding will be disabled.")
        server.embedding_enabled = False; return # type: ignore
        
    server.logger.info("Pre-loading embedding model and FAISS index in executor...")
    try:
        await server.run_in_executor(_load_embedding_model_sync, server)
        await server.run_in_executor(_load_faiss_index_sync, server)
        server.logger.info("Embedding model and FAISS index initialized successfully.")
    except Exception as e:
        server.logger.error(f"Error during startup initialization of embedding system: {e}", exc_info=True)
        server.embedding_enabled = False # type: ignore
        server.logger.warning("Embedding system has been disabled due to startup error.")

async def on_fs_server_shutdown(server: MCPServer):
    server.logger.info(f"FS Server '{server.server_name}' on_shutdown hook running...")
    if server.embedding_enabled and server.faiss_index is not None: # type: ignore
        server.logger.info("Attempting to save FAISS index on shutdown...")
        try:
            await server.run_in_executor(_save_faiss_index_sync, server)
        except Exception as e_save:
            server.logger.error(f"Failed to save FAISS index via executor during shutdown: {e_save}. Attempting sync save.", exc_info=True)
            try: _save_faiss_index_sync(server)
            except Exception as e_sync_save: server.logger.error(f"Synchronous FAISS save also failed: {e_sync_save}", exc_info=True)

fs_server.set_startup_hook(on_fs_server_startup)
fs_server.set_shutdown_hook(on_fs_server_shutdown)


# --- File System Capability Handlers ---
@fs_server.register_method("mcp.fs.list")
async def handle_fs_list(server: MCPServer, request_id: Optional[Union[str, int]], params: List[Any]):
    client_path_str = params[0]
    target_path_abs = _validate_fs_path(client_path_str, check_exists=True, must_be_dir=True)

    def list_dir_sync():
        items = []
        for item_abs_path in target_path_abs.iterdir():
            try:
                stat_info = item_abs_path.stat()
                item_type = "other"
                if item_abs_path.is_file(): item_type = "file"
                elif item_abs_path.is_dir(): item_type = "directory"
                elif item_abs_path.is_symlink(): item_type = "symlink"
                
                items.append({
                    "name": item_abs_path.name,
                    "path": _get_client_facing_path(item_abs_path),
                    "type": item_type,
                    "size": stat_info.st_size if item_type != "directory" else -1,
                    "modified_at": datetime.fromtimestamp(stat_info.st_mtime, tz=timezone.utc).isoformat()
                })
            except OSError as stat_err:
                server.logger.warning(f"Could not stat {item_abs_path.name} in {target_path_abs}: {stat_err}")
                items.append({"name": item_abs_path.name, "path": _get_client_facing_path(item_abs_path), 
                              "type": "inaccessible", "size": -1, "modified_at": None})
        return items
    
    try: return await server.run_in_executor(list_dir_sync)
    except ValueError as ve: raise # From _validate_fs_path
    except PermissionError as pe: # From iterdir or stat
        raise server.create_custom_error(request_id, 1, f"Permission denied for path '{client_path_str}'.", {"path": client_path_str}) from pe

@fs_server.register_method("mcp.fs.read")
async def handle_fs_read(server: MCPServer, request_id: Optional[Union[str, int]], params: List[Any]):
    client_path_str = params[0]
    encoding_type = params[1] if len(params) > 1 else "text"
    target_file_abs = _validate_fs_path(client_path_str, check_exists=True, must_be_file=True)

    def read_file_sync():
        mime_type_str = "application/octet-stream"
        try: mime_type_str = magic.from_file(str(target_file_abs), mime=True)
        except Exception as e_magic: server.logger.warning(f"Magic lib error for {target_file_abs}: {e_magic}")

        content_data: str
        if encoding_type == "text":
            try: content_data = target_file_abs.read_text(encoding="utf-8")
            except UnicodeDecodeError: raise ValueError(f"File '{client_path_str}' is not valid UTF-8. Try 'base64' encoding.")
        elif encoding_type == "base64":
            content_data = base64.b64encode(target_file_abs.read_bytes()).decode('ascii')
        else: raise ValueError(f"Unsupported encoding '{encoding_type}'.") # Should be caught by schema

        return {"path": _get_client_facing_path(target_file_abs), "content": content_data, 
                "encoding": encoding_type, "mime_type": mime_type_str}

    try: return await server.run_in_executor(read_file_sync)
    except ValueError as ve: raise
    except PermissionError as pe:
        raise server.create_custom_error(request_id, 1, f"Permission denied reading '{client_path_str}'.", {"path": client_path_str}) from pe


@fs_server.register_method("mcp.fs.write")
async def handle_fs_write(server: MCPServer, request_id: Optional[Union[str, int]], params: List[Any]):
    client_path_str = params[0]
    content_to_write = params[1]
    encoding_type = params[2] if len(params) > 2 else "text"
    append_mode = params[3] if len(params) > 3 else False
    
    target_file_abs = _validate_fs_path(client_path_str, check_exists=False) # File may not exist

    if not target_file_abs.parent.is_dir():
         raise ValueError(f"Parent directory for '{client_path_str}' does not exist or is not a directory.")
    if target_file_abs.exists() and target_file_abs.is_dir():
        raise ValueError(f"Cannot write to '{client_path_str}', it is an existing directory.")

    def write_file_sync():
        bytes_to_write: bytes
        if encoding_type == "text": bytes_to_write = content_to_write.encode('utf-8')
        elif encoding_type == "base64":
            try: bytes_to_write = base64.b64decode(content_to_write)
            except Exception: raise ValueError("Invalid base64 content for writing.")
        else: raise ValueError(f"Unsupported encoding type '{encoding_type}'.")

        mode = 'ab' if append_mode else 'wb'
        with target_file_abs.open(mode) as f: num_bytes_written = f.write(bytes_to_write)
        return {"path": _get_client_facing_path(target_file_abs), "bytes_written": num_bytes_written, "status": "success"}

    try: return await server.run_in_executor(write_file_sync)
    except ValueError as ve: raise
    except PermissionError as pe:
        raise server.create_custom_error(request_id, 1, f"Permission denied writing to '{client_path_str}'.", {"path": client_path_str}) from pe


@fs_server.register_method("mcp.fs.delete")
async def handle_fs_delete(server: MCPServer, request_id: Optional[Union[str, int]], params: List[Any]):
    client_path_str = params[0]
    recursive = params[1] if len(params) > 1 else False
    target_path_abs = _validate_fs_path(client_path_str, check_exists=True)

    def delete_path_sync():
        if target_path_abs.is_file() or target_path_abs.is_symlink(): target_path_abs.unlink()
        elif target_path_abs.is_dir():
            if recursive: shutil.rmtree(target_path_abs)
            else:
                try: target_path_abs.rmdir()
                except OSError: raise ValueError(f"Directory '{client_path_str}' not empty. Use recursive=true to delete.")
        else: raise ValueError(f"Path '{client_path_str}' is an unknown type for deletion.")
        return {"path": _get_client_facing_path(target_path_abs), "status": "success"}

    try: return await server.run_in_executor(delete_path_sync)
    except ValueError as ve: raise
    except PermissionError as pe:
        raise server.create_custom_error(request_id, 1, f"Permission denied deleting '{client_path_str}'.", {"path": client_path_str}) from pe


@fs_server.register_method("mcp.fs.embed")
async def handle_fs_embed(server: MCPServer, request_id: Optional[Union[str, int]], params: List[Any]):
    if not server.embedding_enabled: # type: ignore
        raise RuntimeError("Embedding system is disabled for this server instance.")
    
    client_path_str = params[0]
    recursive = params[1] if len(params) > 1 else False
    target_path_abs = _validate_fs_path(client_path_str, check_exists=True)

    def embed_path_sync():
        embedding_model = _get_embedding_model_sync(server)
        faiss_idx = _get_faiss_index_sync(server)

        files_to_embed_abs: List[Path] = []
        if target_path_abs.is_file(): files_to_embed_abs.append(target_path_abs)
        elif target_path_abs.is_dir():
            glob_pattern = "**/*" if recursive else "*"
            for item_path_abs in target_path_abs.glob(glob_pattern):
                if item_path_abs.is_file(): files_to_embed_abs.append(item_path_abs)
        
        processed_count = 0
        MAX_FILE_SIZE_BYTES = int(os.getenv("LLMBDO_FS_EMBED_MAX_SIZE_KB", "1024")) * 1024
        new_embeddings_data = []
        new_metadata_entries = []
        
        existing_client_paths = {item['path'] for item in server.faiss_index_metadata} # type: ignore

        for file_abs_path in files_to_embed_abs:
            client_facing_file_path = _get_client_facing_path(file_abs_path)
            if client_facing_file_path in existing_client_paths:
                server.logger.debug(f"Skipping already embedded: {client_facing_file_path}")
                continue
            try:
                if file_abs_path.stat().st_size > MAX_FILE_SIZE_BYTES:
                    server.logger.warning(f"Skipping large file {file_abs_path} for embedding.")
                    continue
                content = file_abs_path.read_text(encoding='utf-8', errors='ignore')
                if not content.strip(): continue
                
                server.logger.debug(f"Embedding: {client_facing_file_path}")
                embedding_vec = embedding_model.encode([content])[0]
                new_embeddings_data.append(embedding_vec.astype('float32'))
                new_metadata_entries.append({"id": server.faiss_next_id, "path": client_facing_file_path}) # type: ignore
                server.faiss_next_id += 1 # type: ignore
                processed_count += 1
            except Exception as e_single_embed:
                server.logger.error(f"Error embedding file {file_abs_path}: {e_single_embed}", exc_info=True)
        
        if new_embeddings_data:
            try:
                embeddings_np = np.array(new_embeddings_data) # type: ignore
                ids_np = np.array([m['id'] for m in new_metadata_entries], dtype='int64') # type: ignore
                faiss_idx.add_with_ids(embeddings_np, ids_np) # type: ignore
                server.faiss_index_metadata.extend(new_metadata_entries) # type: ignore
                _save_faiss_index_sync(server)
                server.logger.info(f"Added {len(new_embeddings_data)} new embeddings. Total: {faiss_idx.ntotal}.") # type: ignore
            except Exception as e_faiss:
                server.logger.error(f"Error adding embeddings to FAISS: {e_faiss}", exc_info=True)
                raise RuntimeError(f"Failed to update search index: {e_faiss}")
        
        return {"path_processed": client_path_str, "files_embedded_this_run": processed_count,
                "total_embeddings_in_index": faiss_idx.ntotal, "status": "success"} # type: ignore

    try: return await server.run_in_executor(embed_path_sync)
    except ValueError as ve: raise
    except RuntimeError as rte:
        raise server.create_custom_error(request_id, 2, str(rte), {"path": client_path_str}) from rte


@fs_server.register_method("mcp.fs.search")
async def handle_fs_search(server: MCPServer, request_id: Optional[Union[str, int]], params: List[Any]):
    if not server.embedding_enabled: # type: ignore
        raise RuntimeError("Search system is disabled.")

    def _ensure_search_ready_sync_local(): # Renamed to avoid conflict
        _load_embedding_model_sync(server)
        _load_faiss_index_sync(server)
        if server.faiss_index is None or server.faiss_index.ntotal == 0: # type: ignore
            raise RuntimeError("Search index not ready or empty.")
    await server.run_in_executor(_ensure_search_ready_sync_local)

    query_text = params[0]
    top_k = int(params[1]) if len(params) > 1 else 5
    scope_client_path_str = params[2] if len(params) > 2 else None
    
    # Resolve virtual root once for path operations
    _fs_virtual_root_resolved = Path(FS_VIRTUAL_ROOT_STR).resolve()
    
    scope_filter_prefix: Optional[str] = None
    if scope_client_path_str:
        # Validate the scope path and convert to a client-facing prefix
        scope_abs_path = _validate_fs_path(scope_client_path_str, check_exists=True, must_be_dir=True)
        scope_filter_prefix = _get_client_facing_path(scope_abs_path)
        if not scope_filter_prefix.endswith('/'): scope_filter_prefix += '/' # Ensure it's a dir prefix

    def search_sync():
        embedding_model = server.embedding_model # type: ignore
        faiss_idx: faiss.Index = server.faiss_index # type: ignore

        query_embedding = embedding_model.encode([query_text])[0].astype('float32').reshape(1, -1)
        
        # Fetch more results if filtering by scope, then narrow down
        k_to_fetch_faiss = max(top_k * 5, 20) if scope_filter_prefix else top_k
        k_to_fetch_faiss = min(k_to_fetch_faiss, faiss_idx.ntotal)
        if k_to_fetch_faiss == 0: return []

        distances, faiss_ids_array = faiss_idx.search(query_embedding, k=k_to_fetch_faiss)
        
        search_results = []
        for i in range(len(faiss_ids_array[0])):
            faiss_id_val = faiss_ids_array[0][i]
            if faiss_id_val == -1: continue
            
            meta = next((m for m in server.faiss_index_metadata if m['id'] == faiss_id_val), None) # type: ignore
            if not meta: continue
            
            client_path_found = meta['path']
            if scope_filter_prefix and not client_path_found.startswith(scope_filter_prefix):
                continue

            similarity_score = float(1.0 / (1.0 + distances[0][i]))
            preview = ""
            try:
                # Convert client path back to absolute disk path for preview reading
                abs_path_for_preview = (_fs_virtual_root_resolved / client_path_found.lstrip('/')).resolve()
                # Security check: ensure preview path is still within the virtual root (after resolving symlinks etc.)
                if abs_path_for_preview.is_file() and validate_mcp_path_param(str(abs_path_for_preview), virtual_root_str=FS_VIRTUAL_ROOT_STR)[1] is None:
                    with open(abs_path_for_preview, 'r', encoding='utf-8', errors='ignore') as pf:
                        preview_content = pf.read(250)
                        preview = preview_content.strip() + ("..." if len(preview_content) == 250 else "")
            except Exception as e_prev: server.logger.debug(f"Could not get preview for {client_path_found}: {e_prev}")

            search_results.append({"path": client_path_found, "score": round(similarity_score, 4), "preview": preview})
            if len(search_results) >= top_k: break 
        
        search_results.sort(key=lambda x: x['score'], reverse=True)
        return search_results[:top_k]

    try: return await server.run_in_executor(search_sync)
    except ValueError as ve: raise # From _validate_fs_path on scope_path
    except RuntimeError as rte:
        raise server.create_custom_error(request_id, 3, str(rte), {"query": query_text}) from rte


# --- Main Entry Point ---
if __name__ == "__main__":
    # This block is for direct execution of this file, e.g., for testing outside supervisord/Docker.
    # It needs its own logger setup if MCPServer's logger isn't globally configured.
    
    # Determine the FS_VIRTUAL_ROOT_STR for standalone execution
    # This should match how it's determined at the module level for consistency
    _standalone_fs_virtual_root = os.getenv(f"LLMBDO_{SERVER_NAME.upper()}_VIRTUAL_ROOT", 
                                          os.getenv("LLMBDO_FS_DATA_ROOT", 
                                                    os.path.expanduser(f"~/{SERVER_NAME}_standalone_root")))
    
    # Ensure the standalone virtual root exists for testing
    try:
        Path(_standalone_fs_virtual_root).mkdir(parents=True, exist_ok=True)
        print(f"FS Server standalone test: Using virtual root at {_standalone_fs_virtual_root}")
    except Exception as e:
        print(f"FS Server standalone test: Could not create virtual root at {_standalone_fs_virtual_root}: {e}")
        # Decide if to exit or continue with a potentially non-functional virtual root.
        # For critical FS_VIRTUAL_ROOT, might be better to exit if it's based on user home and fails.

    # Update FS_VIRTUAL_ROOT_STR for this standalone run if different from module-level
    # This is tricky because FS_VIRTUAL_ROOT_STR is used globally in this file.
    # It's better if _validate_fs_path always gets its virtual_root_str from FS_VIRTUAL_ROOT_ENV_STR
    # or the common default, and FS_VIRTUAL_ROOT_ENV_STR is set correctly by the environment.
    # The logic defining FS_VIRTUAL_ROOT_STR at module level should be robust.

    if not FS_VIRTUAL_ROOT_STR:
        print(f"CRITICAL for FS Server: FS_VIRTUAL_ROOT_STR is not defined. Set LLMBDO_FS_VIRTUAL_ROOT or LLMBDO_FS_DATA_ROOT or LLMBDO_DEFAULT_VIRTUAL_ROOT.", file=sys.stderr)
        exit(1)
    
    # Ensure the determined FS_VIRTUAL_ROOT_STR actually points to an existing directory
    # This is crucial for the server to operate.
    _final_virtual_root_to_check = Path(FS_VIRTUAL_ROOT_STR).resolve()
    if not _final_virtual_root_to_check.is_dir():
        print(f"CRITICAL for FS Server: The virtual root '{_final_virtual_root_to_check}' is not an existing directory.", file=sys.stderr)
        print(f"Please create it or set LLMBDO_FS_VIRTUAL_ROOT / LLMBDO_FS_DATA_ROOT to a valid directory path.", file=sys.stderr)
        exit(1)

    fs_server.logger.info(f"FS Server '{SERVER_NAME}' starting with effective virtual root: {FS_VIRTUAL_ROOT_STR}")

    try:
        asyncio.run(fs_server.start())
    except KeyboardInterrupt:
        fs_server.logger.info(f"FS Server '{SERVER_NAME}' (main) stopped by KeyboardInterrupt.")
    except Exception as e_main_fs:
        fs_server.logger.critical(f"FS Server '{SERVER_NAME}' (main) crashed: {e_main_fs}", exc_info=True)
    finally:
        fs_server.logger.info(f"FS Server '{SERVER_NAME}' (main) exiting.")