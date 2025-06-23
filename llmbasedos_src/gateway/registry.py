# llmbasedos_src/gateway/registry.py
import json
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional
import asyncio
import httpx # Ajout de l'import pour la complétude

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, DirCreatedEvent 

from .config import MCP_CAPS_DIR

logger = logging.getLogger("llmbasedos.gateway.registry")

CAPABILITY_REGISTRY: Dict[str, Dict[str, Any]] = {}
RAW_CAPS_REGISTRY: Dict[str, Dict[str, Any]] = {}   

EXTERNAL_MCP_SERVERS = {
    "mcp_toolkit": {
        "type": "tcp",
        "address": "host.docker.internal:8811",
    }
}

def _clear_service_from_registry(service_name: str):
    logger.debug(f"REGISTRY: Clearing methods for service '{service_name}' from CAPABILITY_REGISTRY.")
    methods_to_remove = [
        mname for mname, details in CAPABILITY_REGISTRY.items() 
        if details.get("service_name") == service_name
    ]
    for mname in methods_to_remove:
        if mname in CAPABILITY_REGISTRY:
            del CAPABILITY_REGISTRY[mname]
            logger.debug(f"REGISTRY: Unregistered method: '{mname}'")
        else:
            logger.warning(f"REGISTRY: Attempted to unregister non-existent method '{mname}' for service '{service_name}'.")

    if service_name in RAW_CAPS_REGISTRY:
        del RAW_CAPS_REGISTRY[service_name]
        logger.debug(f"REGISTRY: Cleared raw capabilities for service '{service_name}'.")

def _load_capability_file(file_path: Path) -> bool:
    if not file_path.name.endswith(".cap.json"):
        return False
    
    service_name = file_path.name.removesuffix(".cap.json").strip()
    if not service_name:
        logger.warning(f"REGISTRY: Could not derive service name from file: {file_path.name}")
        return False
        
    socket_path_str = str(MCP_CAPS_DIR / f"{service_name}.sock")
    _clear_service_from_registry(service_name) 

    logger.info(f"REGISTRY: Attempting to load capabilities for service '{service_name}' from {file_path.name}.")
    try:
        with file_path.open('r', encoding='utf-8') as f:
            cap_data = json.load(f)
        
        if not isinstance(cap_data, dict):
            logger.error(f"REGISTRY: Invalid JSON format in {file_path.name}. Service not loaded.")
            return False
        
        RAW_CAPS_REGISTRY[service_name] = cap_data
        logger.info(f"REGISTRY: Successfully parsed JSON for '{service_name}'.")

        capabilities_list = cap_data.get("capabilities")
        if not isinstance(capabilities_list, list):
            logger.error(f"REGISTRY: Invalid {file_path.name}: 'capabilities' key missing/not a list.")
            return False

        methods_registered_this_load = 0
        for cap_item in capabilities_list:
            if not isinstance(cap_item, dict): continue
            method_name = cap_item.get("method")
            if not method_name or not isinstance(method_name, str): continue
            method_name = method_name.strip()
            if not method_name: continue
            
            if method_name in CAPABILITY_REGISTRY:
                logger.warning(f"REGISTRY: Method '{method_name}' from '{service_name}' conflicts with existing from '{CAPABILITY_REGISTRY[method_name].get('service_name')}'. Overwriting.")
            
            logger.debug(f"REGISTRY: Registering method key: '{method_name}' for service '{service_name}' -> {socket_path_str}")
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
            logger.warning(f"REGISTRY: No valid methods found to register for service '{service_name}'.")
            return False

    except Exception as e:
        logger.error(f"REGISTRY: Error processing capability file {file_path.name}: {e}", exc_info=True)
    return False

def discover_capabilities(initial_load: bool = False):
    if initial_load:
        CAPABILITY_REGISTRY.clear()
        RAW_CAPS_REGISTRY.clear()
        logger.info(f"REGISTRY: Initial discovery. Registries cleared. Discovering in {MCP_CAPS_DIR}...")
    
    if not MCP_CAPS_DIR.is_dir():
        logger.warning(f"REGISTRY: Capability directory {MCP_CAPS_DIR} not found.")
        return

    for file_path in MCP_CAPS_DIR.glob("*.cap.json"):
        _load_capability_file(file_path)
    
    logger.info(f"REGISTRY: Local discovery scan complete. {len(CAPABILITY_REGISTRY)} total methods registered.")

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
    def _process_event(self, event_path_str: str, action: str):
        event_path = Path(event_path_str)
        if event_path.name.endswith(".cap.json") and event_path.is_file():
            logger.info(f"REGISTRY_WATCHER: File event '{action}' for: {event_path.name}.")
            if action == "deleted":
                service_name = event_path.name.removesuffix(".cap.json").strip()
                if service_name:
                    _clear_service_from_registry(service_name)
            else: # created or modified
                _load_capability_file(event_path)

    def on_created(self, event):
        if not event.is_directory: self._process_event(event.src_path, "created")

    def on_modified(self, event):
        if not event.is_directory: self._process_event(event.src_path, "modified")

    def on_deleted(self, event):
        if not event.is_directory: self._process_event(event.src_path, "deleted")

_WATCHDOG_OBSERVER: Optional[Observer] = None

async def discover_external_capabilities():
    logger.info(f"REGISTRY: Discovering external capabilities from {len(EXTERNAL_MCP_SERVERS)} server(s)...")
    for name, config in EXTERNAL_MCP_SERVERS.items():
        writer = None
        try:
            conn_type = config.get("type", "http")
            
            if conn_type == "tcp":
                address = config.get("address")
                if not address or ":" not in address:
                    logger.error(f"Invalid TCP address for '{name}': {address}")
                    continue
                host, port_str = address.split(":", 1)
                port = int(port_str)
                
                logger.info(f"Querying external TCP server '{name}' at {host}:{port}...")
                reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=10.0)
                
                request_payload = {"jsonrpc": "2.0", "method": "mcp.listCapabilities", "id": f"disco_{name}"}
                
                # CORRECTION : On envoie la requête et on ferme immédiatement notre côté écriture.
                # C'est une façon plus standard de signaler la fin de la requête sur une connexion TCP simple.
                writer.write(json.dumps(request_payload).encode('utf-8'))
                await writer.drain()
                writer.close() # <-- Fermer le writer après l'envoi

                response_bytes = await asyncio.wait_for(reader.read(), timeout=20.0)
                logger.info(f"REGISTRY: RAW RESPONSE from '{name}': {response_bytes.decode('utf-8', errors='ignore')}")
                if not response_bytes:
                    logger.error(f"External server '{name}' closed connection without sending data.")
                    continue

                data = json.loads(response_bytes)

            elif conn_type == "http":
                url = config.get("url")
                if not url: continue
                async with httpx.AsyncClient() as client:
                    response = await client.post(url, json={"jsonrpc": "2.0", "method": "mcp.listCapabilities", "id": f"disco_{name}"})
                    response.raise_for_status()
                    data = response.json()
            else:
                logger.error(f"Unknown external server type '{conn_type}' for '{name}'")
                continue

            if "result" in data and isinstance(data["result"], list):
                methods_found = 0
                for service in data["result"]:
                    for cap in service.get("capabilities", []):
                        method_name = cap.get("method")
                        if not method_name: continue
                        
                        CAPABILITY_REGISTRY[method_name] = {
                            "socket_path": "external", "service_name": name,
                            "type": conn_type, "config": config,
                            "method_definition": cap
                        }
                        methods_found += 1
                logger.info(f"REGISTRY: Successfully registered {methods_found} methods from external service '{name}'.")
            else:
                logger.error(f"Invalid capability response from '{name}'. 'result' key missing/not a list.")

        except json.JSONDecodeError as e:
            response_preview = response_bytes.decode('utf-8', errors='replace') if 'response_bytes' in locals() else "N/A"
            logger.error(f"Failed to decode JSON from '{name}'. Response preview: '{response_preview}'. Error: {e}", exc_info=True)
        except Exception as e:
            logger.error(f"Failed to discover capabilities from external server '{name}': {type(e).__name__} - {e}", exc_info=True)
        finally:
            if writer and not writer.is_closing():
                writer.close()
                await writer.wait_closed()

async def start_capability_watcher_task():
    global _WATCHDOG_OBSERVER
    
    if not MCP_CAPS_DIR.exists():
        try:
            MCP_CAPS_DIR.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.error(f"Failed to create MCP_CAPS_DIR {MCP_CAPS_DIR}: {e}. Watcher may fail.")
            
    discover_capabilities(initial_load=True)
    await discover_external_capabilities()
    
    event_handler = CapsFileEventHandler()
    _WATCHDOG_OBSERVER = Observer()
    try:
        _WATCHDOG_OBSERVER.schedule(event_handler, str(MCP_CAPS_DIR), recursive=False)
        _WATCHDOG_OBSERVER.start()
        logger.info(f"REGISTRY_WATCHER: Started capability watcher on directory: {MCP_CAPS_DIR}")
    except Exception as e:
        logger.error(f"Failed to start watchdog on {MCP_CAPS_DIR}: {e}", exc_info=True)
        _WATCHDOG_OBSERVER = None 
        return 

    try:
        while _WATCHDOG_OBSERVER and _WATCHDOG_OBSERVER.is_alive():
            await asyncio.sleep(1.0) 
    except asyncio.CancelledError:
        logger.info("REGISTRY_WATCHER: Watcher task cancelled.")
    finally:
        if _WATCHDOG_OBSERVER and _WATCHDOG_OBSERVER.is_alive():
            _WATCHDOG_OBSERVER.stop()
            _WATCHDOG_OBSERVER.join(timeout=2.0)
        _WATCHDOG_OBSERVER = None
        logger.info("REGISTRY_WATCHER: Capability watcher task fully stopped.")