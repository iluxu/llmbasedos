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

def validate_mcp_path_param(
    path_param_from_wrapper: Any, # Ex: "mon_doc.txt" ou "sous_dossier/doc.txt" (après lstrip)
    virtual_root_str: Optional[str] = None, # Ex: "/mnt/user_data" ou "/home/llmuser"
    check_exists: bool = False,
    must_be_dir: Optional[bool] = None,
    must_be_file: Optional[bool] = None
    # relative_to_cwd_str n'est plus utilisé car les serveurs MCP n'ont pas de "CWD" pour les requêtes externes.
    # allow_outside_virtual_root a été enlevé car le serveur FS doit toujours confiner.
) -> Tuple[Optional[Path], Optional[str]]:
    
    if not isinstance(path_param_from_wrapper, str):
        return None, "Path parameter must be a string."
    
    # Utiliser le virtual_root fourni, ou le défaut global.
    # Ce virtual_root EST la base absolue sur le disque du serveur.
    effective_virtual_root = Path(virtual_root_str if virtual_root_str else DEFAULT_VIRTUAL_ROOT_STR).resolve()
    
    # path_param_from_wrapper est déjà "nettoyé" de son '/' initial par _validate_fs_path.
    # Il représente un chemin relatif au virtual_root.
    # Exemple: path_param_from_wrapper = "docs/notes.txt", effective_virtual_root = Path("/mnt/user_data")
    # p deviendra Path("/mnt/user_data/docs/notes.txt")

    try:
        # Construire le chemin absolu sur le disque en combinant la racine virtuelle et le chemin fourni.
        # Path.joinpath() ou l'opérateur / gère correctement les cas où path_param_from_wrapper pourrait être vide.
        # Si path_param_from_wrapper est ".", il pointera vers effective_virtual_root.
        p = (effective_virtual_root / path_param_from_wrapper).resolve()

        # Sécurité : Vérifier que le chemin résolu `p` est bien DANS ou ÉGAL à `effective_virtual_root`.
        # C'est la vérification cruciale contre le directory traversal (ex: si path_param_from_wrapper était "../../../etc/passwd").
        if not _is_path_within_virtual_root(p, effective_virtual_root):
            logger.warning(f"Path access violation: '{p}' (from input '{path_param_from_wrapper}') is outside virtual root '{effective_virtual_root}'.")
            # Retourner un message d'erreur qui n'expose pas la structure interne des chemins résolus.
            return None, f"Path '{path_param_from_wrapper}' is outside allowed access boundaries."

        if check_exists and not p.exists():
            return None, f"Path '{path_param_from_wrapper}' (resolved to '{p.relative_to(effective_virtual_root)}' within root) does not exist."
        
        if p.exists():
            if must_be_dir is True and not p.is_dir():
                return None, f"Path '{path_param_from_wrapper}' (resolved to '{p.relative_to(effective_virtual_root)}') is not a directory."
            if must_be_file is True and not p.is_file():
                return None, f"Path '{path_param_from_wrapper}' (resolved to '{p.relative_to(effective_virtual_root)}') is not a file."
            
        return p, None # Retourne le chemin absolu résolu sur le disque du serveur
    
    except ValueError as ve: 
        logger.warning(f"Path '{path_param_from_wrapper}' is malformed: {ve}")
        return None, f"Path '{path_param_from_wrapper}' is malformed."
    except OSError as ose:
        logger.error(f"OS error validating path '{path_param_from_wrapper}' resolved against '{effective_virtual_root}': {ose}", exc_info=False)
        return None, f"Cannot access path '{path_param_from_wrapper}': {ose.strerror}"
    except Exception as e: 
        logger.error(f"Unexpected error validating path '{path_param_from_wrapper}': {e}", exc_info=True)
        return None, f"Invalid path '{path_param_from_wrapper}': {type(e).__name__}"
