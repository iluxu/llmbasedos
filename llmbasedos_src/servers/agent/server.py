# llmbasedos/servers/agent/server.py
import asyncio
# Logger is obtained from MCPServer instance, no need for direct logging import here if using server.logger
import os
from pathlib import Path
import uuid
import yaml
import docker # For Docker-based agents
import requests # For HTTP-based agents (n8n-lite)
import threading # For managing long-running workflow execution threads
import time # For sleep in simple workflows or polling
from typing import Any, Dict, List, Optional, Union, Callable
from datetime import datetime, timezone
import subprocess # For script-based agents if needed

# --- Import Framework ---
# Assuming mcp_server_framework.py is in llmbasedos/ or a discoverable path
# from llmbasedos.mcp_server_framework import MCPServer
# For now, let's assume it's in a place Python can find it.
# If it's meant to be part of llmbasedos, the import would be:
from llmbasedos.mcp_server_framework import MCPServer 
from llmbasedos.common_utils import validate_mcp_path_param # Assurez-vous que fs_server en a besoin

# --- Server Specific Configuration ---
SERVER_NAME = "agent"
# CAPS_FILE_PATH_STR will be derived by MCPServer if caps.json is in the same dir as this file
# If not, it needs to be explicitly passed. For consistency with your previous files:
CAPS_FILE_PATH_STR = str(Path(__file__).parent / "caps.json")

# Custom error code base for agent-specific errors (e.g., workflow not found)
AGENT_CUSTOM_ERROR_BASE = -32040 # Will be combined with MCPServer's internal error codes

# Configuration paths (can be accessed via server.config later if MCPServer handles config loading)
WORKFLOWS_DIR_CONF = Path(os.getenv("LLMBDO_AGENT_WORKFLOWS_DIR", "/etc/llmbasedos/workflows"))
WORKFLOWS_DIR_CONF.mkdir(parents=True, exist_ok=True) # Ensure dir exists

AGENT_EXEC_LOG_DIR_CONF = Path(os.getenv("LLMBDO_AGENT_EXEC_LOG_DIR", f"/var/log/llmbasedos/{SERVER_NAME}_executions"))
AGENT_EXEC_LOG_DIR_CONF.mkdir(parents=True, exist_ok=True)

N8N_LITE_URL_CONF = os.getenv("LLMBDO_N8N_LITE_URL", "http://localhost:5678")

# Initialize server instance using the framework
# The MCPServer class will handle socket creation, logging setup, etc.
agent_server = MCPServer(
    server_name=SERVER_NAME,
    caps_file_path_str=CAPS_FILE_PATH_STR, # MCPServer might auto-detect if named 'caps.json' in module dir
    custom_error_code_base=AGENT_CUSTOM_ERROR_BASE
)

# Attach server-specific state to the server instance for access within handlers
# These will be initialized in the on_startup_hook
# agent_server.workflow_definitions: Dict[str, Dict[str, Any]] = {}
# agent_server.executions_state: Dict[str, Dict[str, Any]] = {}
# agent_server.docker_client: Optional[docker.DockerClient] = None


# --- Workflow Definition Loading (Sync, to be run in executor or startup thread) ---
def _load_workflow_definitions_blocking(server_instance: MCPServer):
    """
    Loads workflow definitions from YAML files into server_instance.workflow_definitions.
    This is a blocking function, intended to be run via server.run_in_executor.
    """
    # Access attributes via server_instance
    server_instance.workflow_definitions.clear() # type: ignore
    if not WORKFLOWS_DIR_CONF.exists() or not WORKFLOWS_DIR_CONF.is_dir():
        server_instance.logger.warning(f"Workflows directory {WORKFLOWS_DIR_CONF} not found. No workflows loaded.")
        return

    for filepath in WORKFLOWS_DIR_CONF.glob("*.yaml"): # Or .yml
        try:
            with filepath.open('r') as f:
                workflow_yaml = yaml.safe_load(f)
            
            if not isinstance(workflow_yaml, dict): # Basic validation
                server_instance.logger.warning(f"Workflow file {filepath.name} does not contain a valid YAML dictionary. Skipping.")
                continue

            wf_id = str(workflow_yaml.get("id", filepath.stem)) # Use filename stem if ID not in YAML, ensure string
            wf_name = str(workflow_yaml.get("name", wf_id))    # Ensure string
            
            # Store minimal info, full YAML can be parsed on execution if complex
            server_instance.workflow_definitions[wf_id] = { # type: ignore
                "workflow_id": wf_id,
                "name": wf_name,
                "description": workflow_yaml.get("description"),
                "path": str(filepath),
                "parsed_yaml": workflow_yaml, # Store full parsed content
                "input_schema": workflow_yaml.get("input_schema") # For mcp.agent.listWorkflows
            }
            server_instance.logger.info(f"Loaded workflow definition: '{wf_name}' (ID: {wf_id}) from {filepath.name}")
        except yaml.YAMLError as ye:
            server_instance.logger.error(f"Error parsing YAML for workflow {filepath.name}: {ye}")
        except Exception as e:
            server_instance.logger.error(f"Error loading workflow {filepath.name}: {e}", exc_info=True)
    server_instance.logger.info(f"Loaded {len(server_instance.workflow_definitions)} workflow definitions.") # type: ignore


# --- Workflow Execution Target (Runs in a separate Python thread) ---
# This function contains blocking calls (Docker, HTTP requests, time.sleep).
# It's designed to run in a thread spawned by `handle_agent_run_workflow`.
def _execute_workflow_in_thread(
    server_instance: MCPServer, # Pass the MCPServer instance for logging and config access
    execution_id: str,
    workflow_id: str,
    inputs: Optional[Dict[str, Any]]
):
    # Use server_instance.logger for logging
    # Access server_instance.executions_state, server_instance.docker_client, etc.

    exec_log_file = AGENT_EXEC_LOG_DIR_CONF / f"{execution_id}.log"
    
    def _log_exec(message: str, level: str = "info"):
        timestamp = datetime.now(timezone.utc).isoformat()
        log_line = f"{timestamp} [{level.upper()}] {message}\n"
        try:
            with open(exec_log_file, 'a', encoding='utf-8') as lf:
                lf.write(log_line)
        except Exception as e_log_write:
            # Log to server logger if file write fails
            server_instance.logger.error(f"Exec {execution_id}: Failed to write to log file {exec_log_file}: {e_log_write}")
        
        # Also log to server's main logger
        # Python's default logging handlers are thread-safe.
        getattr(server_instance.logger, level, server_instance.logger.info)(f"Exec {execution_id} (WF {workflow_id}): {message}")

    _log_exec(f"Execution thread started. Inputs: {inputs}")
    
    # Access execution state via server_instance
    # Ensure execution_id exists (should be created before thread starts)
    if execution_id not in server_instance.executions_state: # type: ignore
        _log_exec(f"Critical error: Execution ID {execution_id} not found in state at thread start.", "error")
        return
        
    exec_data = server_instance.executions_state[execution_id] # type: ignore
    exec_data["status"] = "running"
    exec_data["start_time"] = datetime.now(timezone.utc)
    
    docker_container_obj: Optional[docker.models.containers.Container] = None # type: ignore
    # cancellation_event = exec_data.get("cancellation_event") # Assuming this is a threading.Event

    try:
        if workflow_id not in server_instance.workflow_definitions: # type: ignore
            raise ValueError(f"Workflow definition ID '{workflow_id}' not found at execution time.")
        
        wf_config = server_instance.workflow_definitions[workflow_id]["parsed_yaml"] # type: ignore
        wf_type = wf_config.get("type", "simple_sequential")

        if wf_type == "docker":
            if not server_instance.docker_client: # type: ignore
                raise RuntimeError("Docker client not available for Docker workflow.")
            
            docker_image = wf_config.get("docker_image")
            if not docker_image:
                raise ValueError("Docker image not specified in workflow config.")
            
            command_list = wf_config.get("command") # List or string
            environment_dict = {str(k).upper(): str(v) for k,v in (inputs or {}).items()}
            environment_dict.update(wf_config.get("environment", {}))

            _log_exec(f"Starting Docker container: Image='{docker_image}', Cmd='{command_list}', EnvKeys='{list(environment_dict.keys())}'")
            
            container = server_instance.docker_client.containers.run( # type: ignore
                image=docker_image, command=command_list, environment=environment_dict,
                detach=True, remove=False # Keep for logs, remove in finally block
            )
            docker_container_obj = container # Store for potential cleanup
            exec_data["docker_container_id"] = container.id
            _log_exec(f"Docker container {container.id} started.")

            for log_entry in container.logs(stream=True, follow=True, timestamps=True, stdout=True, stderr=True):
                # if cancellation_event and cancellation_event.is_set():
                #     _log_exec("Docker workflow cancellation requested during log streaming.", "warning")
                #     container.stop(timeout=5)
                #     break
                _log_exec(f"DOCKER: {log_entry.decode('utf-8').strip()}")
            
            container.reload()
            container_state = container.attrs['State']
            exit_code = container_state.get('ExitCode', -1)
            
            if exit_code == 0:
                exec_data["status"] = "completed"
                exec_data["output"] = {"message": "Docker task completed successfully.", "exit_code": 0, "container_id": container.id}
                _log_exec(f"Docker task ({container.id}) completed successfully (ExitCode: {exit_code}).")
            else:
                error_details = container_state.get('Error', f"Docker task non-zero ExitCode: {exit_code}")
                raise RuntimeError(f"Docker task ({container.id}) failed. {error_details}")

        elif wf_type == "n8n_webhook":
            webhook_url_tmpl = wf_config.get("webhook_url")
            if not webhook_url_tmpl:
                webhook_url_tmpl = f"{N8N_LITE_URL_CONF.rstrip('/')}/webhook/{workflow_id}"
            final_webhook_url = webhook_url_tmpl # Add templating here if needed

            _log_exec(f"Calling HTTP webhook (n8n-lite type): {final_webhook_url}")
            http_timeout = wf_config.get("timeout_seconds", 300)
            response = requests.post(final_webhook_url, json=inputs, timeout=http_timeout)
            response.raise_for_status()
            
            exec_data["status"] = "completed"
            try:
                exec_data["output"] = response.json()
            except requests.exceptions.JSONDecodeError:
                exec_data["output"] = {"raw_response": response.text}
            _log_exec(f"HTTP webhook call successful. Status: {response.status_code}. Output (preview): {str(exec_data.get('output',''))[:100]}")

        elif wf_type == "simple_sequential":
            _log_exec("Executing simple sequential Python steps...")
            current_output = inputs or {}
            for i, step_config in enumerate(wf_config.get("steps", [])):
                # if cancellation_event and cancellation_event.is_set():
                #     _log_exec("Sequential workflow cancellation requested.", "warning"); break
                step_name = step_config.get("name", f"Step_{i+1}")
                step_action = step_config.get("action", "log_message")
                _log_exec(f"Running step '{step_name}': Action='{step_action}'")
                
                if step_action == "log_message":
                    msg = step_config.get("message", "Default log from workflow.")
                    _log_exec(f"STEP LOG ({step_name}): {msg}")
                elif step_action == "sleep":
                    duration = float(step_config.get("duration_seconds", 1.0))
                    time.sleep(duration)
                else:
                    _log_exec(f"Unknown action '{step_action}' in step '{step_name}'. Skipping.", "warning")
            
            exec_data["status"] = "completed"
            exec_data["output"] = current_output
            _log_exec("Simple sequential workflow completed.")
        else:
            raise NotImplementedError(f"Workflow type '{wf_type}' execution logic not implemented.")

    except Exception as e_wf:
        _log_exec(f"Workflow execution critically failed: {e_wf}", level="error")
        # Ensure exec_data is still valid and accessible
        if execution_id in server_instance.executions_state: # type: ignore
            exec_data["status"] = "failed"
            exec_data["error_message"] = str(e_wf)
    finally:
        if execution_id in server_instance.executions_state: # type: ignore
            exec_data["end_time"] = datetime.now(timezone.utc)
            exec_data["thread_obj"] = None # Mark Python thread as logically done
        
        if docker_container_obj:
            try:
                docker_container_obj.reload()
                # Check config for auto-removal, default to True
                auto_remove = wf_config.get("auto_remove_container", True) if 'wf_config' in locals() else True
                if auto_remove and docker_container_obj.status in ['exited', 'dead', 'created']:
                    _log_exec(f"Removing Docker container {docker_container_obj.id}")
                    docker_container_obj.remove(v=True) # v=True also removes anonymous volumes
                else:
                    _log_exec(f"Docker container {docker_container_obj.id} (Status: {docker_container_obj.status}) not auto-removed.", "debug")
            except Exception as e_docker_clean:
                _log_exec(f"Error during Docker container cleanup for {docker_container_obj.id}: {e_docker_clean}", level="error")
        _log_exec(f"Execution thread finished.")


# --- Agent Capability Handlers (decorated for MCPServer framework) ---
@agent_server.register_method("mcp.agent.listWorkflows")
async def handle_agent_list_workflows(server: MCPServer, request_id: Optional[Union[str, int]], params: List[Any]):
    # Ensure workflow definitions are loaded if not already
    if not server.workflow_definitions: # type: ignore
        # _load_workflow_definitions_blocking is sync, run in executor
        await server.run_in_executor(_load_workflow_definitions_blocking, server)
    
    return [
        {
            "workflow_id": wf["workflow_id"],
            "name": wf["name"],
            "description": wf.get("description"),
            "input_schema": wf.get("input_schema") # From caps.json example
        }
        for wf_id, wf in server.workflow_definitions.items() # type: ignore
    ]

@agent_server.register_method("mcp.agent.runWorkflow")
async def handle_agent_run_workflow(server: MCPServer, request_id: Optional[Union[str, int]], params: List[Any]):
    # Schema validation would be handled by MCPServer based on caps.json if framework supports it.
    # Assuming params = [workflow_id_str, optional_inputs_dict]
    if not params or not isinstance(params[0], str):
        # This error should ideally be caught by framework's param validation based on caps.json
        raise server.create_custom_error(request_id, -1, "Invalid params: workflow_id (string) is required.")
        
    workflow_id = params[0]
    inputs = params[1] if len(params) > 1 and isinstance(params[1], dict) else {}

    if workflow_id not in server.workflow_definitions: # type: ignore
        raise server.create_custom_error(request_id, -2, f"Workflow ID '{workflow_id}' not found.")

    execution_id = f"exec_{uuid.uuid4().hex[:12]}"
    # cancellation_event = threading.Event() # For cooperative cancellation within the thread

    # Initialize execution state before starting the thread
    server.executions_state[execution_id] = { # type: ignore
        "execution_id": execution_id,
        "workflow_id": workflow_id,
        "status": "pending", # Will be updated by the thread
        "inputs": inputs,
        "output": None,
        "error_message": None,
        "log_file": str(AGENT_EXEC_LOG_DIR_CONF / f"{execution_id}.log"),
        # "cancellation_event": cancellation_event
    }

    # Workflow execution (_execute_workflow_in_thread) is blocking, so run it in a new Python thread.
    # This is different from server.run_in_executor which uses a shared thread pool.
    # For long-running, independent tasks, a new thread is often more appropriate.
    thread = threading.Thread(
        target=_execute_workflow_in_thread,
        args=(server, execution_id, workflow_id, inputs),
        daemon=True # Allows main program to exit even if threads are active (though join on shutdown is better)
    )
    server.executions_state[execution_id]["thread_obj"] = thread # type: ignore
    thread.start()
    
    return {"execution_id": execution_id, "workflow_id": workflow_id, "status": "started"}


@agent_server.register_method("mcp.agent.getWorkflowStatus")
async def handle_agent_get_workflow_status(server: MCPServer, request_id: Optional[Union[str, int]], params: List[Any]):
    if not params or not isinstance(params[0], str):
        raise server.create_custom_error(request_id, -1, "Invalid params: execution_id (string) is required.")
    execution_id = params[0]

    if execution_id not in server.executions_state: # type: ignore
        raise server.create_custom_error(request_id, -3, f"Execution ID '{execution_id}' not found.")
    
    exec_data = server.executions_state[execution_id] # type: ignore
    
    log_preview_list = []
    log_file_path_str = exec_data.get("log_file")
    if log_file_path_str:
        log_file_path = Path(log_file_path_str)
        if log_file_path.exists():
            try:
                # Reading file is IO blocking, could use executor for very large previews or many calls
                with open(log_file_path, 'r', encoding='utf-8', errors='ignore') as lf_read:
                    log_preview_list = [line.strip() for line in lf_read.readlines()[-20:]] # Last 20 lines
            except Exception as e_logread:
                server.logger.warning(f"Could not read log preview for execution {execution_id}: {e_logread}")

    return {
        "execution_id": execution_id,
        "workflow_id": exec_data["workflow_id"],
        "status": exec_data["status"],
        "start_time": exec_data.get("start_time").isoformat() if exec_data.get("start_time") else None,
        "end_time": exec_data.get("end_time").isoformat() if exec_data.get("end_time") else None,
        "output": exec_data.get("output"),
        "error_message": exec_data.get("error_message"),
        "log_preview": log_preview_list
    }

@agent_server.register_method("mcp.agent.stopWorkflow")
async def handle_agent_stop_workflow(server: MCPServer, request_id: Optional[Union[str, int]], params: List[Any]):
    if not params or not isinstance(params[0], str):
        raise server.create_custom_error(request_id, -1, "Invalid params: execution_id (string) is required.")
    execution_id = params[0]

    if execution_id not in server.executions_state: # type: ignore
        raise server.create_custom_error(request_id, -3, f"Execution ID '{execution_id}' not found.")
    
    exec_data = server.executions_state[execution_id] # type: ignore
    current_status = exec_data.get("status")

    if current_status not in ["pending", "running"]:
        return {
            "execution_id": execution_id,
            "status": "already_completed_or_not_running",
            "message": f"Workflow execution {execution_id} is not in a stoppable state (current: {current_status})."
        }

    docker_container_id_to_stop = exec_data.get("docker_container_id")
    # cancellation_event_to_set = exec_data.get("cancellation_event") # For cooperative thread cancellation

    if docker_container_id_to_stop and server.docker_client: # type: ignore
        # Stopping Docker container is a blocking IO operation
        def stop_docker_container_blocking_sync():
            try:
                container_to_stop = server.docker_client.containers.get(docker_container_id_to_stop) # type: ignore
                server.logger.info(f"Exec {execution_id}: Sending stop signal to Docker container {docker_container_id_to_stop}")
                container_to_stop.stop(timeout=10) # 10s graceful timeout
                exec_data["status"] = "cancelling" # Workflow thread should update to "cancelled" or "failed"
                return {"execution_id": execution_id, "status": "stop_requested", "message": "Stop signal sent to Docker container."}
            except docker.errors.NotFound: # type: ignore
                # Container already gone
                exec_data["status"] = "unknown_after_stop_notfound" # Or some other terminal state
                return {"execution_id": execution_id, "status": "not_running", "message": "Docker container not found (already gone)."}
            except Exception as e_docker_stop:
                server.logger.error(f"Exec {execution_id}: Error stopping Docker container {docker_container_id_to_stop}: {e_docker_stop}")
                # Re-raise as a custom server error for the framework to handle
                raise server.create_custom_error(request_id, -4, f"Failed to stop Docker container: {e_docker_stop}")
        
        return await server.run_in_executor(stop_docker_container_blocking_sync)
    
    # elif cancellation_event_to_set:
    #     server.logger.info(f"Exec {execution_id}: Setting cancellation event for Python thread.")
    #     cancellation_event_to_set.set()
    #     exec_data["status"] = "cancelling" # Thread should notice this and exit
    #     return {"execution_id": execution_id, "status": "stop_requested", "message": "Cancellation requested for workflow thread."}
    else:
        # If no specific stop mechanism is identified (e.g. simple sequential thread without event)
        return {
            "execution_id": execution_id,
            "status": "not_stoppable",
            "message": "Workflow type does not currently support external stopping or is not a stoppable resource."
        }


# --- Server Lifecycle Hooks (to be registered with MCPServer instance) ---
async def on_agent_server_startup(server: MCPServer):
    """Custom startup actions for the agent server."""
    server.logger.info(f"Agent Server '{server.server_name}' performing custom startup actions...")
    
    # Initialize server-specific state attributes
    server.workflow_definitions = {} # type: ignore
    server.executions_state = {}    # type: ignore
    server.docker_client = None     # type: ignore

    # Initialize Docker client (can be blocking, so use executor)
    try:
        def init_docker_client_sync(): # Blocking function
            return docker.from_env() # type: ignore
        
        server.docker_client = await server.run_in_executor(init_docker_client_sync) # type: ignore
        server.logger.info("Docker client initialized successfully via executor.")
    except Exception as docker_err_init:
        server.logger.warning(f"Failed to initialize Docker client: {docker_err_init}. Docker-based agents will be unavailable.", exc_info=True)
        # server.docker_client remains None
    
    # Load workflow definitions (blocking IO, use executor)
    await server.run_in_executor(_load_workflow_definitions_blocking, server)

async def on_agent_server_shutdown(server: MCPServer):
    """Custom shutdown actions for the agent server."""
    server.logger.info(f"Agent Server '{server.server_name}' performing custom shutdown actions...")
    
    # Attempt to gracefully stop/clean up any active workflow executions
    for exec_id, exec_data in list(server.executions_state.items()): # type: ignore # Iterate copy
        if exec_data.get("status") == "running":
            server.logger.info(f"Exec {exec_id}: Attempting cleanup/stop on server shutdown...")
            
            # Signal Python threads via cancellation event (if implemented)
            # cancellation_event = exec_data.get("cancellation_event")
            # if cancellation_event:
            #     server.logger.info(f"Exec {exec_id}: Setting cancellation event on shutdown.")
            #     cancellation_event.set()

            container_id_shutdown = exec_data.get("docker_container_id")
            if container_id_shutdown and server.docker_client: # type: ignore
                def stop_docker_on_shutdown_sync_final():
                    try:
                        cont = server.docker_client.containers.get(container_id_shutdown) # type: ignore
                        server.logger.info(f"Exec {exec_id}: Requesting stop for Docker container {container_id_shutdown} on server shutdown.")
                        cont.stop(timeout=5) # Short timeout during shutdown
                        # Auto-remove logic from wf_config (might be complex to get here)
                        # For simplicity, just try to remove if it was not meant to be persistent.
                        # This part is tricky as wf_config might not be easily accessible here.
                        # Assume default auto-remove if container is stopped.
                        cont.reload()
                        if cont.status in ['exited', 'dead']:
                            server.logger.info(f"Exec {exec_id}: Removing container {container_id_shutdown} after stop.")
                            cont.remove(v=True)
                    except docker.errors.NotFound: # type: ignore
                        server.logger.info(f"Exec {exec_id}: Container {container_id_shutdown} already gone during shutdown stop.")
                    except Exception as e_stop_final:
                        server.logger.warning(f"Exec {exec_id}: Error during final stop/remove of Docker container {container_id_shutdown}: {e_stop_final}")
                
                # Run this blocking Docker stop in executor to not stall shutdown too much
                try:
                    await server.run_in_executor(stop_docker_on_shutdown_sync_final)
                except Exception as e_exec_shutdown:
                    server.logger.error(f"Error in executor for stopping container {container_id_shutdown} on shutdown: {e_exec_shutdown}")
            
            # Wait for Python threads if they were designed for clean exit
            # thread_obj_shutdown = exec_data.get("thread_obj")
            # if thread_obj_shutdown and thread_obj_shutdown.is_alive():
            #     server.logger.info(f"Exec {exec_id}: Waiting briefly for workflow thread to join on shutdown.")
            #     thread_obj_shutdown.join(timeout=5) # Brief wait for thread to finish
            #     if thread_obj_shutdown.is_alive():
            #         server.logger.warning(f"Exec {exec_id}: Workflow thread did not exit cleanly on shutdown.")

# Register lifecycle hooks with the server instance
agent_server.set_startup_hook(on_agent_server_startup)
agent_server.set_shutdown_hook(on_agent_server_shutdown)

# --- Main Entry Point (if running this server module directly) ---
if __name__ == "__main__":
    # The MCPServer class should handle its own logger setup for the main script execution if desired,
    # or we can add a specific logger here for the __main__ block.
    # For now, rely on MCPServer's internal logger or default Python logging for this block.
    # Example:
    # main_script_logger = logging.getLogger("llmbasedos.servers.agent_script_main")
    # main_script_logger.setLevel(os.getenv(f"LLMBDO_{SERVER_NAME.upper()}_LOG_LEVEL", "INFO").upper())
    # if not main_script_logger.hasHandlers():
    #    # Add basic handler for this script's direct execution logs
    #    # (MCPServer will have its own more structured logger for server operations)
    #    main_ch = logging.StreamHandler()
    #    main_ch.setFormatter(logging.Formatter(f"%(asctime)s - AGENT_SCRIPT_MAIN - %(levelname)s - %(message)s"))
    #    main_script_logger.addHandler(main_ch)

    # The MCPServer's start() method will run the asyncio event loop.
    try:
        asyncio.run(agent_server.start())
    except KeyboardInterrupt:
        # MCPServer.start() should handle KeyboardInterrupt gracefully for its cleanup.
        # Add a print here if specific message needed for direct run.
        print(f"\nAgent Server '{SERVER_NAME}' (direct run) stopped by KeyboardInterrupt.")
    except Exception as e_main_script:
        print(f"Agent Server '{SERVER_NAME}' (direct run) crashed: {e_main_script}", file=sys.stderr)
        # Optionally log to a file or more structured output if needed
        # main_script_logger.critical(f"Agent Server (main) crashed: {e_main_script}", exc_info=True)
    finally:
        # MCPServer.start() should also ensure its shutdown hook and executor cleanup occurs.
        print(f"Agent Server '{SERVER_NAME}' (direct run) fully shut down.")