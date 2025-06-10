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


# llmbasedos_pkg/common_utils.py

# llmbasedos_pkg/common_utils.py
# ... (logger, DEFAULT_VIRTUAL_ROOT_STR, _is_path_within_virtual_root) ...

def validate_mcp_path_param(
    path_param_relative_to_root: str, # Ex: "docs/file.txt" ou "" pour la racine virtuelle elle-même
    virtual_root_str: str,            # Ex: "/mnt/user_data" (doit être fourni et exister)
    check_exists: bool = False,
    must_be_dir: Optional[bool] = None,
    must_be_file: Optional[bool] = None
) -> Tuple[Optional[Path], Optional[str]]:
    
    logger.debug(f"validate_mcp_path_param: Validating '{path_param_relative_to_root}' against virtual_root '{virtual_root_str}'")
    
    try:
        # La racine virtuelle doit exister et être un répertoire
        effective_virtual_root = Path(virtual_root_str).resolve()
        if not effective_virtual_root.is_dir():
            # Cet échec devrait être attrapé au démarrage du serveur fs, mais vérification ici aussi.
            msg = f"Virtual root '{effective_virtual_root}' is not an existing directory."
            logger.error(msg)
            return None, msg

        # path_param_relative_to_root est déjà nettoyé de son '/' initial.
        # Il représente un chemin relatif à la racine virtuelle.
        # Ex: path_param_relative_to_root = "docs/notes.txt", effective_virtual_root = Path("/mnt/user_data")
        # disk_path deviendra Path("/mnt/user_data/docs/notes.txt")
        # Si path_param_relative_to_root est "", disk_path deviendra Path("/mnt/user_data")
        disk_path = (effective_virtual_root / path_param_relative_to_root).resolve()

        # Sécurité : Vérifier que le chemin résolu `disk_path` est bien DANS ou ÉGAL à `effective_virtual_root`.
        if not _is_path_within_virtual_root(disk_path, effective_virtual_root):
            unconfined_msg = f"Access violation: Path '{path_param_relative_to_root}' (resolves to '{disk_path}') is outside virtual root '{effective_virtual_root}'."
            logger.warning(unconfined_msg)
            return None, f"Path '{path_param_relative_to_root}' is outside allowed access boundaries."

        if check_exists and not disk_path.exists():
            return None, f"Path '{path_param_relative_to_root}' (resolved to '{disk_path.relative_to(effective_virtual_root)}' within root) does not exist."
        
        if disk_path.exists(): # Vérifier le type seulement si le chemin existe
            if must_be_dir is True and not disk_path.is_dir():
                return None, f"Path '{path_param_relative_to_root}' (resolved to '{disk_path.relative_to(effective_virtual_root)}') is not a directory."
            if must_be_file is True and not disk_path.is_file():
                return None, f"Path '{path_param_relative_to_root}' (resolved to '{disk_path.relative_to(effective_virtual_root)}') is not a file."
            
        return disk_path, None # Retourne le chemin disque absolu et validé
    
    except ValueError as ve: 
        logger.warning(f"Path '{path_param_relative_to_root}' is malformed: {ve}")
        return None, f"Path '{path_param_relative_to_root}' is malformed."
    except Exception as e: 
        logger.error(f"Unexpected error validating path '{path_param_relative_to_root}' against '{virtual_root_str}': {e}", exc_info=True)
        return None, f"Error processing path '{path_param_relative_to_root}': {type(e).__name__}"
