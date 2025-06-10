# llmbasedos_src/gateway/registry.py
import json
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple # Tuple n'est pas utilisé, peut être enlevé
import asyncio

from watchdog.observers import Observer
# FileModifiedEvent, FileCreatedEvent, FileDeletedEvent, DirCreatedEvent ne sont pas explicitement utilisés par type
# mais FileSystemEventHandler les gère.
from watchdog.events import FileSystemEventHandler, DirCreatedEvent 

from .config import MCP_CAPS_DIR

logger = logging.getLogger("llmbasedos.gateway.registry")

# CAPABILITY_REGISTRY: method_name (str) -> routing_info (Dict)
#   routing_info = {"socket_path": str, "service_name": str, "method_definition": Dict}
CAPABILITY_REGISTRY: Dict[str, Dict[str, Any]] = {}

# RAW_CAPS_REGISTRY: service_name (str) -> raw_data_from_cap_file (Dict)
RAW_CAPS_REGISTRY: Dict[str, Dict[str, Any]] = {}   

def _clear_service_from_registry(service_name: str):
    logger.debug(f"REGISTRY: Clearing methods for service '{service_name}' from CAPABILITY_REGISTRY.")
    methods_to_remove = [
        mname for mname, details in CAPABILITY_REGISTRY.items() 
        if details.get("service_name") == service_name
    ]
    for mname in methods_to_remove:
        if mname in CAPABILITY_REGISTRY: # Vérifier avant de supprimer
            del CAPABILITY_REGISTRY[mname]
            logger.debug(f"REGISTRY: Unregistered method: '{mname}'")
        else:
            logger.warning(f"REGISTRY: Attempted to unregister non-existent method '{mname}' for service '{service_name}'.")

    if service_name in RAW_CAPS_REGISTRY:
        del RAW_CAPS_REGISTRY[service_name]
        logger.debug(f"REGISTRY: Cleared raw capabilities for service '{service_name}'.")

def _load_capability_file(file_path: Path) -> bool:
    if not file_path.name.endswith(".cap.json"):
        logger.debug(f"REGISTRY: Ignoring non .cap.json file: {file_path.name}")
        return False
    
    service_name = file_path.name.removesuffix(".cap.json").strip() # strip() pour la propreté
    if not service_name:
        logger.warning(f"REGISTRY: Could not derive service name from file: {file_path.name}")
        return False
        
    socket_path_str = str(MCP_CAPS_DIR / f"{service_name}.sock") # Convention

    # Nettoyer les anciennes entrées pour ce service AVANT de tenter de charger les nouvelles.
    # Cela évite les états incohérents si le nouveau fichier est invalide.
    _clear_service_from_registry(service_name) 

    logger.info(f"REGISTRY: Attempting to load capabilities for service '{service_name}' from {file_path.name}.")
    try:
        with file_path.open('r', encoding='utf-8') as f: # Spécifier encoding
            cap_data = json.load(f)
        
        if not isinstance(cap_data, dict):
            logger.error(f"REGISTRY: Invalid JSON format in {file_path.name}: content is not a dictionary. Service '{service_name}' not loaded.")
            return False
        
        RAW_CAPS_REGISTRY[service_name] = cap_data
        logger.info(f"REGISTRY: Successfully parsed JSON for '{service_name}' from {file_path.name}. Storing raw capabilities.")

        capabilities_list = cap_data.get("capabilities")
        if not isinstance(capabilities_list, list):
            logger.error(f"REGISTRY: Invalid {file_path.name}: 'capabilities' key missing or not a list. Service '{service_name}' methods not registered.")
            return False # Le fichier a été parsé, mais pas de capacités à enregistrer. On ne considère pas ça un succès complet.

        methods_registered_this_load = 0
        for cap_item in capabilities_list:
            if not isinstance(cap_item, dict):
                logger.warning(f"REGISTRY: Skipping invalid capability item (not a dict) in {file_path.name} for service '{service_name}'.")
                continue
            
            method_name = cap_item.get("method")
            if not method_name or not isinstance(method_name, str):
                logger.warning(f"REGISTRY: Skipping capability item with missing/invalid 'method' name in {file_path.name} for '{service_name}'.")
                continue
            
            method_name = method_name.strip() # Nettoyer le nom de la méthode
            if not method_name:
                logger.warning(f"REGISTRY: Skipping capability item with empty 'method' name after strip in {file_path.name} for '{service_name}'.")
                continue

            # Convention check (optionnel mais utile pour le logging)
            # expected_prefix = f"mcp.{service_name}."
            # if not method_name.startswith(expected_prefix):
            #     logger.debug(f"REGISTRY: Method '{method_name}' in {file_path.name} for service '{service_name}' "
            #                    f"does not strictly follow '{expected_prefix}action' convention. Registering.")
            
            if method_name in CAPABILITY_REGISTRY:
                logger.warning(f"REGISTRY: Method '{method_name}' from {file_path.name} (service '{service_name}') "
                               f"conflicts with existing method from service '{CAPABILITY_REGISTRY[method_name].get('service_name')}'. Overwriting.")
            
            # LOG pour voir la clé exacte enregistrée
            logger.debug(f"REGISTRY: Registering method key: '{method_name}' (len: {len(method_name)}) for service '{service_name}' -> {socket_path_str}")
            CAPABILITY_REGISTRY[method_name] = {
                "socket_path": socket_path_str, 
                "service_name": service_name,
                "method_definition": cap_item, 
            }
            methods_registered_this_load += 1
        
        if methods_registered_this_load > 0:
            logger.info(f"REGISTRY: Successfully registered {methods_registered_this_load} methods for service '{service_name}'.")
            return True
        else:
            logger.warning(f"REGISTRY: No valid methods found to register for service '{service_name}' in {file_path.name}.")
            return False # Fichier parsé, mais aucune méthode enregistrée.

    except json.JSONDecodeError as e_json:
        logger.error(f"REGISTRY: JSONDecodeError processing capability file {file_path.name} for service '{service_name}': {e_json}. Service not loaded/updated.")
    except Exception as e:
        logger.error(f"REGISTRY: Unexpected error processing capability file {file_path.name} for service '{service_name}': {e}", exc_info=True)
    return False

def discover_capabilities(initial_load: bool = False):
    if initial_load:
        CAPABILITY_REGISTRY.clear()
        RAW_CAPS_REGISTRY.clear()
        logger.info(f"REGISTRY: Initial discovery. Registries cleared. Discovering capabilities in {MCP_CAPS_DIR}...")
    else:
        logger.info(f"REGISTRY: Re-discovering capabilities in {MCP_CAPS_DIR} (non-initial load)...")
    
    if not MCP_CAPS_DIR.exists() or not MCP_CAPS_DIR.is_dir():
        logger.warning(f"REGISTRY: Capability directory {MCP_CAPS_DIR} not found or not a directory. No capabilities loaded/updated.")
        return

    successful_loads = 0
    for file_path in MCP_CAPS_DIR.glob("*.cap.json"):
        if _load_capability_file(file_path): # _load_capability_file gère le logging de succès/échec par fichier
            successful_loads +=1 
            # Note: _load_capability_file retourne True si le JSON est valide et au moins une méthode a été tentée d'être enregistrée.
            # Il pourrait être affiné pour retourner True seulement si des méthodes sont *effectivement* ajoutées/mises à jour.
    
    logger.info(f"REGISTRY: Capability discovery/update scan complete. "
                f"{len(CAPABILITY_REGISTRY)} total methods registered from "
                f"{len(RAW_CAPS_REGISTRY)} services. {successful_loads} cap files processed successfully in this scan.")

def get_capability_routing_info(method_name: str) -> Optional[Dict[str, Any]]:
    # Le `method_name` ici vient de `dispatch.py` qui devrait déjà l'avoir .strip()
    logger.debug(f"REGISTRY: get_capability_routing_info called for method_name: '{method_name}' (type: {type(method_name)}, len: {len(method_name)})")
    
    # Pour un débogage très fin, décommentez pour voir toutes les clés au moment de la recherche :
    # current_keys_snapshot = list(CAPABILITY_REGISTRY.keys())
    # logger.debug(f"REGISTRY: Current keys in CAPABILITY_REGISTRY (total {len(current_keys_snapshot)}): {current_keys_snapshot}")
    
    result = CAPABILITY_REGISTRY.get(method_name)
    
    if result is None:
        logger.warning(f"REGISTRY: Method '{method_name}' NOT FOUND in CAPABILITY_REGISTRY via .get().")
        # Vérifications supplémentaires pour aider au diagnostic
        for k in CAPABILITY_REGISTRY.keys():
            if k.lower() == method_name.lower() and k != method_name:
                logger.warning(f"REGISTRY: Found a case-insensitive match for '{method_name}': Actual key is '{k}'.")
            # Si method_name a été strippé dans dispatch, cette vérif est moins utile ici, mais on la garde.
            if k.strip() == method_name.strip() and k != method_name: 
                 logger.warning(f"REGISTRY: Method name '{method_name}' matches key '{k}' if stripped, but original key might have spaces.")
            # Log pour voir la représentation binaire si on suspecte des caractères invisibles
            # if len(k) == len(method_name) and k != method_name:
            #    logger.debug(f"REGISTRY: Potential non-printable char difference? Key: {k.encode('utf-8')!r} vs Req: {method_name.encode('utf-8')!r}")

    return result

def get_all_registered_method_names() -> List[str]:
    # Retourne une copie triée des clés pour la cohérence et l'affichage
    return sorted(list(CAPABILITY_REGISTRY.keys()))

def get_detailed_capabilities_list() -> List[Dict[str, Any]]:
    # Construit la liste à partir de RAW_CAPS_REGISTRY pour inclure les descriptions de service, etc.
    return [
        {"service_name": s_name, 
         "description": raw.get("description", "N/A"),
         "version": raw.get("version", "N/A"), 
         "capabilities": raw.get("capabilities", [])} # raw.get("capabilities") est la liste des définitions de méthodes
        for s_name, raw in RAW_CAPS_REGISTRY.items()
    ]

class CapsFileEventHandler(FileSystemEventHandler):
    def _process_event(self, event_path_str: str, action: str):
        # Utiliser un lock si plusieurs événements peuvent arriver en même temps pour le même fichier
        # et modifier CAPABILITY_REGISTRY de manière concurrente.
        # Pour l'instant, watchdog envoie généralement les événements en série pour un même chemin.
        # asyncio.Lock() pourrait être utilisé si les handlers étaient des coroutines.
        # Ici, c'est synchrone dans le thread de watchdog.
        
        event_path = Path(event_path_str)
        if event_path.name.endswith(".cap.json") and event_path.is_file(): # S'assurer que c'est un fichier
            logger.info(f"REGISTRY_WATCHER: File event '{action}' for: {event_path.name}.")
            if action == "deleted":
                service_name = event_path.name.removesuffix(".cap.json").strip()
                if service_name:
                    _clear_service_from_registry(service_name)
                    logger.info(f"REGISTRY_WATCHER: Unloaded capabilities for service '{service_name}' due to file deletion.")
            else: # created or modified
                if _load_capability_file(event_path):
                     logger.info(f"REGISTRY_WATCHER: Successfully reloaded capabilities from {event_path.name}.")
                else:
                     logger.warning(f"REGISTRY_WATCHER: Failed to reload capabilities from {event_path.name} (see previous errors).")
        elif event_path.name.endswith(".cap.json"): # Si ce n'est pas un fichier (ex: un dossier nommé .cap.json)
            logger.debug(f"REGISTRY_WATCHER: Ignoring non-file event for {event_path.name}")


    def on_created(self, event):
        if isinstance(event, DirCreatedEvent) and Path(event.src_path) == MCP_CAPS_DIR:
            logger.info(f"REGISTRY_WATCHER: Capability directory {MCP_CAPS_DIR} itself was (re)created. Re-discovering all.")
            discover_capabilities(initial_load=False) 
        elif not event.is_directory:
            self._process_event(event.src_path, "created")

    def on_modified(self, event):
        if not event.is_directory:
            self._process_event(event.src_path, "modified")

    def on_deleted(self, event):
        if not event.is_directory:
            self._process_event(event.src_path, "deleted")

_WATCHDOG_OBSERVER: Optional[Observer] = None

async def start_capability_watcher_task():
    global _WATCHDOG_OBSERVER
    
    if not MCP_CAPS_DIR.exists():
        try:
            logger.info(f"REGISTRY_WATCHER: MCP_CAPS_DIR {MCP_CAPS_DIR} does not exist. Attempting to create.")
            MCP_CAPS_DIR.mkdir(parents=True, exist_ok=True)
            # Assurer les bonnes permissions pour que les serveurs MCP puissent y écrire leurs .cap.json
            # et que le gateway (llmuser) puisse les lire. L'entrypoint.sh devrait gérer ça.
        except OSError as e:
            logger.error(f"REGISTRY_WATCHER: Failed to create MCP_CAPS_DIR {MCP_CAPS_DIR}: {e}. Watcher may not start correctly.")
            # Ne pas retourner, watchdog pourrait surveiller un parent.
            
    discover_capabilities(initial_load=True) # Découverte initiale complète

    event_handler = CapsFileEventHandler()
    _WATCHDOG_OBSERVER = Observer() # Crée une nouvelle instance à chaque démarrage
    try:
        # recursive=False car les .cap.json sont à la racine de MCP_CAPS_DIR
        _WATCHDOG_OBSERVER.schedule(event_handler, str(MCP_CAPS_DIR), recursive=False)
        _WATCHDOG_OBSERVER.start()
        logger.info(f"REGISTRY_WATCHER: Started capability watcher on directory: {MCP_CAPS_DIR}")
    except Exception as e:
        logger.error(f"REGISTRY_WATCHER: Failed to start watchdog on {MCP_CAPS_DIR}: {e}. Dynamic updates of capabilities will be disabled.", exc_info=True)
        _WATCHDOG_OBSERVER = None 
        return 

    try:
        while _WATCHDOG_OBSERVER and _WATCHDOG_OBSERVER.is_alive():
            await asyncio.sleep(1.0) 
    except asyncio.CancelledError:
        logger.info("REGISTRY_WATCHER: Watcher task cancelled.")
    finally:
        if _WATCHDOG_OBSERVER: # S'assurer qu'il a été initialisé
            if _WATCHDOG_OBSERVER.is_alive():
                logger.info("REGISTRY_WATCHER: Stopping observer thread...")
                _WATCHDOG_OBSERVER.stop()
            # Join est important pour attendre que le thread de l'observer se termine proprement.
            _WATCHDOG_OBSERVER.join(timeout=5.0) 
            if _WATCHDOG_OBSERVER.is_alive():
                 logger.warning("REGISTRY_WATCHER: Observer thread did not stop in time.")
            else:
                 logger.info("REGISTRY_WATCHER: Observer thread stopped.")
        _WATCHDOG_OBSERVER = None # Réinitialiser pour un potentiel redémarrage
        logger.info("REGISTRY_WATCHER: Capability watcher task fully stopped.")

async def stop_capability_watcher(): # Appelé par le lifespan manager du gateway
    logger.info("REGISTRY_WATCHER: Received request to stop capability watcher (delegated to task cancellation).")
    # La logique d'arrêt est maintenant dans le finally de start_capability_watcher_task,
    # qui sera déclenché quand la tâche est annulée par le lifespan manager.
    # Si _WATCHDOG_OBSERVER est None (n'a jamais démarré), il ne se passera rien.
    pass