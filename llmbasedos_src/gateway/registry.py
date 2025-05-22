# llmbasedos/gateway/registry.py
import json
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
import asyncio

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileModifiedEvent, FileCreatedEvent, FileDeletedEvent, DirCreatedEvent

from .config import MCP_CAPS_DIR

logger = logging.getLogger("llmbasedos.gateway.registry")

CAPABILITY_REGISTRY: Dict[str, Dict[str, Any]] = {} # method_name -> routing_info
RAW_CAPS_REGISTRY: Dict[str, Dict[str, Any]] = {}   # service_name -> raw_caps_data

def _clear_service_from_registry(service_name: str):
    methods_to_remove = [mname for mname, details in CAPABILITY_REGISTRY.items() if details.get("service_name") == service_name]
    for mname in methods_to_remove:
        del CAPABILITY_REGISTRY[mname]
        logger.debug(f"Unregistered method: {mname}")
    if service_name in RAW_CAPS_REGISTRY:
        del RAW_CAPS_REGISTRY[service_name]

def _load_capability_file(file_path: Path) -> bool:
    if not file_path.name.endswith(".cap.json"): return False
    service_name = file_path.name.removesuffix(".cap.json")
    socket_path_str = str(MCP_CAPS_DIR / f"{service_name}.sock") # Convention

    _clear_service_from_registry(service_name) # Clear old entries first

    try:
        with file_path.open('r') as f: cap_data = json.load(f)
        if not isinstance(cap_data, dict):
            logger.error(f"Invalid format in {file_path}: not a dictionary."); return False
        
        RAW_CAPS_REGISTRY[service_name] = cap_data
        logger.info(f"Loaded raw capabilities for '{service_name}' from {file_path.name}")

        capabilities_list = cap_data.get("capabilities")
        if not isinstance(capabilities_list, list):
            logger.error(f"Invalid {file_path.name}: 'capabilities' missing or not a list."); return False

        for cap_item in capabilities_list:
            if not isinstance(cap_item, dict): continue
            method_name = cap_item.get("method")
            if not method_name or not isinstance(method_name, str): continue

            expected_prefix = f"mcp.{service_name}."
            if not method_name.startswith(expected_prefix):
                logger.warning(f"Method '{method_name}' in {file_path.name} for service '{service_name}' "
                               f"does not follow '{expected_prefix}action' convention. Registering anyway.")
            
            if method_name in CAPABILITY_REGISTRY:
                logger.warning(f"Method '{method_name}' from {file_path.name} conflicts with existing from "
                               f"'{CAPABILITY_REGISTRY[method_name]['service_name']}'. Overwriting.")

            CAPABILITY_REGISTRY[method_name] = {
                "socket_path": socket_path_str, "service_name": service_name,
                "method_definition": cap_item, # Includes params_schema, description etc.
            }
            logger.debug(f"Registered method: {method_name} -> {socket_path_str}")
        return True
    except Exception as e:
        logger.error(f"Error processing capability file {file_path}: {e}", exc_info=True)
    return False

def discover_capabilities(initial_load: bool = False):
    """Discovers all .cap.json files. If initial_load, clears registries first."""
    if initial_load:
        CAPABILITY_REGISTRY.clear()
        RAW_CAPS_REGISTRY.clear()
        logger.info(f"Discovering capabilities in {MCP_CAPS_DIR}...")
    
    if not MCP_CAPS_DIR.exists() or not MCP_CAPS_DIR.is_dir():
        if initial_load: logger.warning(f"Caps dir {MCP_CAPS_DIR} not found. No capabilities loaded.")
        return

    loaded_count = 0
    for file_path in MCP_CAPS_DIR.glob("*.cap.json"):
        # On initial load, always try to load. On subsequent calls (from watcher),
        # _load_capability_file handles clearing existing entries for that service.
        if _load_capability_file(file_path):
            loaded_count +=1
    
    if initial_load or loaded_count > 0:
        logger.info(f"Capability discovery/update complete. {len(CAPABILITY_REGISTRY)} methods from "
                    f"{len(RAW_CAPS_REGISTRY)} services registered.")

def get_capability_routing_info(method_name: str) -> Optional[Dict[str, Any]]:
    return CAPABILITY_REGISTRY.get(method_name)

def get_all_registered_method_names() -> List[str]:
    return sorted(list(CAPABILITY_REGISTRY.keys()))

def get_detailed_capabilities_list() -> List[Dict[str, Any]]:
    return [
        {"service_name": s_name, "description": raw.get("description", "N/A"),
         "version": raw.get("version", "N/A"), "capabilities": raw.get("capabilities", [])}
        for s_name, raw in RAW_CAPS_REGISTRY.items()
    ]

class CapsFileEventHandler(FileSystemEventHandler):
    def _trigger_rediscovery_for_path(self, path_str: str, action: str):
        event_path = Path(path_str)
        if event_path.name.endswith(".cap.json"):
            logger.info(f"Capability file {action}: {event_path.name}. Updating registry.")
            if action == "deleted":
                service_name = event_path.name.removesuffix(".cap.json")
                _clear_service_from_registry(service_name)
                logger.info(f"Unloaded capabilities for service '{service_name}'.")
            else: # created or modified
                _load_capability_file(event_path) # Reloads specific file
        # No need to rediscover all on single file change, specific load/unload is better.

    def on_created(self, event): # FileSystemEvent or DirCreatedEvent
        if isinstance(event, DirCreatedEvent) and Path(event.src_path) == MCP_CAPS_DIR:
            logger.info(f"Capability directory {MCP_CAPS_DIR} created. Re-discovering all.")
            discover_capabilities(initial_load=False) # Rescan all
        elif not event.is_directory:
            self._trigger_rediscovery_for_path(event.src_path, "created")

    def on_modified(self, event):
        if not event.is_directory: self._trigger_rediscovery_for_path(event.src_path, "modified")

    def on_deleted(self, event):
        if not event.is_directory: self._trigger_rediscovery_for_path(event.src_path, "deleted")

_WATCHDOG_OBSERVER: Optional[Observer] = None

async def start_capability_watcher_task():
    """Async task to run the watchdog observer."""
    global _WATCHDOG_OBSERVER
    
    # Ensure MCP_CAPS_DIR exists before starting watcher, watchdog might fail otherwise
    if not MCP_CAPS_DIR.exists():
        try:
            logger.info(f"MCP_CAPS_DIR {MCP_CAPS_DIR} does not exist. Attempting to create.")
            MCP_CAPS_DIR.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.error(f"Failed to create MCP_CAPS_DIR {MCP_CAPS_DIR}: {e}. Watcher may not start correctly.")
            # Depending on Watchdog version/OS, it might still watch a non-existent path's parent.
            
    discover_capabilities(initial_load=True) # Initial full discovery

    event_handler = CapsFileEventHandler()
    _WATCHDOG_OBSERVER = Observer()
    try:
        _WATCHDOG_OBSERVER.schedule(event_handler, str(MCP_CAPS_DIR), recursive=False)
        _WATCHDOG_OBSERVER.start()
        logger.info(f"Started capability watcher on {MCP_CAPS_DIR}")
    except Exception as e:
        logger.error(f"Failed to start watchdog on {MCP_CAPS_DIR}: {e}. Dynamic updates may fail.", exc_info=True)
        _WATCHDOG_OBSERVER = None # Ensure it's None if start failed
        return # Cannot continue if observer fails to start

    try:
        while _WATCHDOG_OBSERVER and _WATCHDOG_OBSERVER.is_alive(): # Check _WATCHDOG_OBSERVER for None
            await asyncio.sleep(1) # Keep task alive, observer runs in own thread
    except asyncio.CancelledError:
        logger.info("Capability watcher task cancelled.")
    finally:
        if _WATCHDOG_OBSERVER and _WATCHDOG_OBSERVER.is_alive():
            _WATCHDOG_OBSERVER.stop()
        if _WATCHDOG_OBSERVER: # Join only if it was started
            _WATCHDOG_OBSERVER.join(timeout=5) # Wait for observer thread to finish
        _WATCHDOG_OBSERVER = None
        logger.info("Capability watcher stopped.")

async def stop_capability_watcher(): # Called on shutdown
    global _WATCHDOG_OBSERVER
    if _WATCHDOG_OBSERVER and _WATCHDOG_OBSERVER.is_alive():
        logger.info("Stopping capability watcher...")
        _WATCHDOG_OBSERVER.stop()
    # Join is handled by the task's finally block.
