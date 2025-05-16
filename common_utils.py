# llmbasedos/common_utils.py
import os
from pathlib import Path
from typing import Optional, Tuple, Any # Added Any
import logging

# Logger for this module - will be configured if a server/app imports it and has logging set up
# Or, can set up a basic one here if run standalone (unlikely for utils)
logger = logging.getLogger("llmbasedos.common_utils")

# --- Path Validation (centralized and enhanced) ---
DEFAULT_VIRTUAL_ROOT_STR = os.getenv("LLMBDO_DEFAULT_VIRTUAL_ROOT", os.path.expanduser("~"))
DEFAULT_VIRTUAL_ROOT = Path(DEFAULT_VIRTUAL_ROOT_STR).resolve()
logger.info(f"Default virtual root for path validation: {DEFAULT_VIRTUAL_ROOT}")

def _is_path_within_virtual_root(path_to_check: Path, virtual_root: Path) -> bool:
    try:
        resolved_check = path_to_check.resolve()
        resolved_root = virtual_root.resolve() # Ensure virtual_root itself is resolved
        # Check if resolved_check is equal to or a subpath of resolved_root
        return resolved_check == resolved_root or resolved_root in resolved_check.parents
    except Exception as e: # Symlink loops, permissions on resolve()
        logger.warning(f"Path safety check failed for {path_to_check} against {virtual_root}: {e}")
        return False


def validate_mcp_path_param(
    path_param: Any, # Should be string, but check type
    virtual_root_str: Optional[str] = None, # Allow overriding default virtual root per call
    relative_to_cwd_str: Optional[str] = None,
    check_exists: bool = False,
    must_be_dir: Optional[bool] = None,
    must_be_file: Optional[bool] = None
) -> Tuple[Optional[Path], Optional[str]]: # (Resolved_Path_or_None, Error_Message_or_None)
    
    if not isinstance(path_param, str):
        return None, "Path parameter must be a string."
    if not path_param.strip(): # Check for empty or whitespace-only string
        return None, "Path parameter cannot be empty."

    current_virtual_root = Path(virtual_root_str).resolve() if virtual_root_str else DEFAULT_VIRTUAL_ROOT
    
    p: Path
    try:
        # Handle absolute paths first
        if os.path.isabs(path_param):
            p = Path(path_param).resolve() # Resolve symlinks, normalize '..'
        elif relative_to_cwd_str:
            # Path is relative, resolve against provided CWD
            base_cwd = Path(relative_to_cwd_str).resolve()
            if not _is_path_within_virtual_root(base_cwd, current_virtual_root):
                # This check is important: CWD itself must be within the virtual root
                logger.warning(f"CWD '{base_cwd}' for relative path resolution is outside virtual root '{current_virtual_root}'.")
                return None, "Cannot resolve relative path: current directory is outside allowed boundaries."
            p = (base_cwd / path_param).resolve()
        else:
            # Path is relative, but no CWD provided. Assume relative to virtual_root.
            # This case should be used carefully by servers.
            p = (current_virtual_root / path_param).resolve()
            logger.debug(f"Relative path '{path_param}' resolved against virtual_root to '{p}'")

        # Crucial security check: Ensure the final resolved path is within the virtual root.
        if not _is_path_within_virtual_root(p, current_virtual_root):
            logger.warning(f"Path access violation: '{p}' (from input '{path_param}') is outside virtual root '{current_virtual_root}'.")
            return None, f"Path '{path_param}' (resolved to '{p}') is outside allowed access boundaries."

        if check_exists and not p.exists():
            return None, f"Path '{p}' does not exist."
        
        if p.exists(): # Only check type if it exists and type check requested
            if must_be_dir is True and not p.is_dir():
                return None, f"Path '{p}' is not a directory."
            if must_be_file is True and not p.is_file():
                return None, f"Path '{p}' is not a file."
            
        return p, None # Return the fully resolved, validated path
    
    except ValueError as ve: # e.g. Path(path_param) fails for certain invalid chars on Windows
        logger.warning(f"Path '{path_param}' is malformed or contains invalid characters: {ve}")
        return None, f"Path '{path_param}' is malformed."
    except OSError as ose: # Catch OSErrors during resolution (e.g. permission denied on a component)
        logger.error(f"OS error validating path '{path_param}': {ose}", exc_info=False) # No need for full stack trace always
        return None, f"Cannot access path '{path_param}': {ose.strerror}"
    except Exception as e: # Catch-all for other unexpected errors during path processing
        logger.error(f"Unexpected error validating path '{path_param}': {e}", exc_info=True)
        return None, f"Invalid path '{path_param}': {type(e).__name__}"
