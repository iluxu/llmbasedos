# llmbasedos/servers/agent/server.py
import asyncio
import logging # Logger obtained from MCPServer
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
from llmbasedos.mcp_server_framework import MCPServer

# --- Server Specific Configuration ---
SERVER_NAME = "agent"
CAPS_FILE_PATH_STR = str(Path(__file__).parent / "caps.json")
AGENT_CUSTOM_ERROR_BASE = -32040

WORKFLOWS_DIR_CONF = Path(os.getenv("LLMBDO_AGENT_WORKFLOWS_DIR", "/etc/llmbasedos/workflows"))
WORKFLOWS_DIR_CONF.mkdir(parents=True, exist_ok=True) # Ensure dir exists

AGENT_EXEC_LOG_DIR_CONF = Path(os.getenv("LLMBDO_AGENT_EXEC_LOG_DIR", f"/var/log/llmbasedos/{SERVER_NAME}_executions"))
AGENT_EXEC_LOG_DIR_CONF.mkdir(parents=True, exist_ok=True)

N8N_LITE_URL_CONF = os.getenv("LLMBDO_N8N_LITE_URL", "http://localhost:5678")

# Initialize server instance
agent_server = MCPServer(SERVER_NAME, CAPS_FILE_PATH_STR, custom_error_code_base=AGENT_CUSTOM_ERROR_BASE)

# Attach server-specific state
agent_server.workflow_definitions: Dict[str, Dict[str, Any]] = {} # type: ignore
agent_server.executions_state: Dict[str, Dict[str, Any]] = {} # type: ignore # Stores execution metadata
# Docker client will be initialized in on_startup hook
agent_server.docker_client: Optional[docker.DockerClient] = None # type: ignore


# --- Workflow Definition Loading (Sync, for executor or startup thread) ---
def _load_workflow_definitions_blocking(server: MCPServer):
    server.workflow_definitions.clear() # type: ignore
    if not WORKFLOWS_DIR_CONF.exists() or not WORKFLOWS_DIR_CONF.is_dir():
        server.logger.warning(f"Workflows dir {WORKFLOWS_DIR_CONF} not found."); return
    
    for filepath in WORKFLOWS_DIR_CONF.glob("*.yaml"): # Allow .yml too if needed
        try:
            with filepath.open('r') as f: workflow_yaml = yaml.safe_load(f)
            if not isinstance(workflow_yaml, dict):
                server.logger.warning(f"Workflow file {filepath.name} is not a valid YAML dictionary. Skipping."); continue

            wf_id = str(workflow_yaml.get("id", filepath.stem)) # Ensure string
            wf_name = str(workflow_yaml.get("name", wf_id))
            server.workflow_definitions[wf_id] = { # type: ignore
                "workflow_id": wf_id, "name": wf_name, "description": workflow_yaml.get("description"),
                "path": str(filepath), "parsed_yaml": workflow_yaml,
                "input_schema": workflow_yaml.get("input_schema") # For mcp.agent.listWorkflows
            }
            server.logger.info(f"Loaded workflow: '{wf_name}' (ID: {wf_id}) from {filepath.name}")
        except yaml.YAMLError as ye: server.logger.error(f"Error parsing YAML for {filepath.name}: {ye}")
        except Exception as e: server.logger.error(f"Error loading workflow {filepath.name}: {e}", exc_info=True)
    server.logger.info(f"Loaded {len(server.workflow_definitions)} workflow definitions.") # type: ignore

# --- Workflow Execution Target (Runs in a separate Python thread) ---
# This function is complex and contains blocking calls (Docker, HTTP requests, time.sleep).
# It's designed to run in a thread spawned by `handle_agent_run_workflow`.
# Individual blocking parts within *could* use server.run_in_executor if they were complex enough
# to warrant their own sub-threads, but often a single dedicated thread per workflow exec is fine.

def _execute_workflow_in_thread(
    server: MCPServer, execution_id: str, workflow_id: str, inputs: Optional[Dict[str, Any]]
):
    exec_log_file = AGENT_EXEC_LOG_DIR_CONF / f"{execution_id}.log"
    # Thread-local logger or pass server.logger carefully
    # For simplicity, use a local function that writes to file and logs via server.logger
    def _log_exec(message: str, level: str = "info"):
        ts = datetime.now(timezone.utc).isoformat()
        log_line = f"{ts} [{level.upper()}] {message}\n"
        try:
            with open(exec_log_file, 'a', encoding='utf-8') as lf: lf.write(log_line)
        except Exception as e_log_write: server.logger.error(f"Exec {execution_id}: Failed to write to log file {exec_log_file}: {e_log_write}")
        # Use server's logger, ensuring thread-safety if logger handlers are not thread-safe
        # Python's default logging handlers are thread-safe.
        getattr(server.logger, level, server.logger.info)(f"Exec {execution_id} (WF {workflow_id}): {message}")

    _log_exec(f"Execution thread started. Inputs: {inputs}")
    exec_data = server.executions_state[execution_id] # type: ignore # Assumes entry exists
    exec_data["status"] = "running"
    exec_data["start_time"] = datetime.now(timezone.utc)
    
    # Store context for potential cancellation/cleanup
    docker_container_obj: Optional[docker.models.containers.Container] = None # type: ignore
    # cancellation_event = exec_data.get("cancellation_event") # threading.Event()

    try:
        if workflow_id not in server.workflow_definitions: # type: ignore
            raise ValueError(f"Workflow definition ID '{workflow_id}' not found at execution time.")
        
        wf_config = server.workflow_definitions[workflow_id]["parsed_yaml"] # type: ignore
        wf_type = wf_config.get("type", "simple_sequential") # Default type

        if wf_type == "docker":
            if not server.docker_client: raise RuntimeError("Docker client not available for Docker workflow.") # type: ignore
            
            docker_image = wf_config.get("docker_image")
            if not docker_image: raise ValueError("Docker image not specified in workflow config.")
            
            # Merge global inputs with step-specific env vars or command args (complex)
            # For now, simple: inputs are primary environment variables.
            # TODO: Templating for command using inputs (e.g. Jinja2)
            command_list = wf_config.get("command") # Should be list of strings or single string
            environment_dict = {str(k).upper(): str(v) for k,v in (inputs or {}).items()} # Pass inputs as ENV
            environment_dict.update(wf_config.get("environment", {})) # Merge with workflow-defined env

            _log_exec(f"Starting Docker container: Image='{docker_image}', Cmd='{command_list}', EnvKeys='{list(environment_dict.keys())}'")
            # This `run` call is blocking if detach=False. For managed execution, detach=True.
            container = server.docker_client.containers.run( # type: ignore
                image=docker_image, command=command_list, environment=environment_dict,
                detach=True, remove=False # Keep container for logs/inspection, remove in finally
            )
            docker_container_obj = container # Store for potential cleanup/stop
            exec_data["docker_container_id"] = container.id
            _log_exec(f"Docker container {container.id} started.")

            # Stream logs (this is blocking)
            for log_entry in container.logs(stream=True, follow=True, timestamps=True, stdout=True, stderr=True):
                # if cancellation_event and cancellation_event.is_set():
                #    _log_exec("Docker workflow cancellation requested during log streaming.", "warning")
                #    container.stop(timeout=5); break # Attempt to stop container
                _log_exec(f"DOCKER: {log_entry.decode('utf-8').strip()}")
            
            # After logs stream ends, container should have exited. Get final status.
            container.reload() # Refresh container attributes
            container_state = container.attrs['State']
            exit_code = container_state.get('ExitCode', -1) # Default to -1 if not found
            
            if exit_code == 0:
                exec_data["status"] = "completed"
                exec_data["output"] = {"message": "Docker task completed successfully.", "exit_code": 0, "container_id": container.id}
                _log_exec(f"Docker task ({container.id}) completed successfully (ExitCode: {exit_code}).")
            else:
                error_details = container_state.get('Error', f"Docker task non-zero ExitCode: {exit_code}")
                raise RuntimeError(f"Docker task ({container.id}) failed. {error_details}")

        elif wf_type == "n8n_webhook":
            webhook_url_tmpl = wf_config.get("webhook_url") # Can be a template
            if not webhook_url_tmpl: webhook_url_tmpl = f"{N8N_LITE_URL_CONF.rstrip('/')}/webhook/{workflow_id}"
            # TODO: Simple templating for URL if needed, e.g. webhook_url_tmpl.format(**inputs)
            final_webhook_url = webhook_url_tmpl

            _log_exec(f"Calling HTTP webhook (n8n-lite type): {final_webhook_url}")
            # requests.post is blocking, fine in this thread. Timeout important.
            http_timeout = wf_config.get("timeout_seconds", 300) # Default 5 mins
            response = requests.post(final_webhook_url, json=inputs, timeout=http_timeout)
            response.raise_for_status() # Raises HTTPError for 4xx/5xx
            
            exec_data["status"] = "completed"
            try: exec_data["output"] = response.json()
            except requests.exceptions.JSONDecodeError: exec_data["output"] = {"raw_response": response.text}
            _log_exec(f"HTTP webhook call successful. Status: {response.status_code}. Output (preview): {str(exec_data.get('output',''))[:100]}")

        elif wf_type == "simple_sequential": # Example Python-based steps
            _log_exec("Executing simple sequential Python steps...")
            current_output = inputs or {} # Start with inputs as initial data
            for i, step_config in enumerate(wf_config.get("steps", [])):
                # if cancellation_event and cancellation_event.is_set():
                #    _log_exec("Sequential workflow cancellation requested.", "warning"); break
                step_name = step_config.get("name", f"Step_{i+1}")
                step_action = step_config.get("action", "log_message")
                _log_exec(f"Running step '{step_name}': Action='{step_action}'")
                
                if step_action == "log_message":
                    msg = step_config.get("message", "Default log from workflow.")
                    # TODO: Template 'msg' with 'current_output' or 'inputs'
                    _log_exec(f"STEP LOG ({step_name}): {msg}")
                elif step_action == "sleep":
                    duration = float(step_config.get("duration_seconds", 1.0))
                    time.sleep(duration)
                # Add more actions: call_mcp_method, run_script, transform_data etc.
                else: _log_exec(f"Unknown action '{step_action}' in step '{step_name}'. Skipping.", "warning")
            
            exec_data["status"] = "completed"; exec_data["output"] = current_output
            _log_exec("Simple sequential workflow completed.")
        else:
            raise NotImplementedError(f"Workflow type '{wf_type}' execution logic not implemented.")

    except Exception as e_wf:
        _log_exec(f"Workflow execution critically failed: {e_wf}", level="error")
        if execution_id in server.executions_state: # type: ignore # Check if entry still exists
            exec_data["status"] = "failed"
            exec_data["error_message"] = str(e_wf)
    finally:
        if execution_id in server.executions_state: # type: ignore
            exec_data["end_time"] = datetime.now(timezone.utc)
            exec_data["thread_obj"] = None # Mark Python thread as logically done
            # If a cancellation event was used: exec_data["cancellation_event"] = None
        
        if docker_container_obj: # Cleanup Docker container if one was created and not auto-removed
            try:
                docker_container_obj.reload() # Get fresh status
                if wf_config.get("auto_remove_container", True) and docker_container_obj.status in ['exited', 'dead', 'created']:
                    _log_exec(f"Removing Docker container {docker_container_obj.id}")
                    docker_container_obj.remove(v=True) # v=True also removes anonymous volumes
                else:
                    _log_exec(f"Docker container {docker_container_obj.id} (Status: {docker_container_obj.status}) not auto-removed based on config or state.", "debug")
            except Exception as e_docker_clean:
                _log_exec(f"Error during Docker container cleanup for {docker_container_obj.id}: {e_docker_clean}", level="error")
        _log_exec(f"Execution thread finished.")


# --- Agent Capability Handlers ---
@agent_server.register("mcp.agent.listWorkflows")
async def handle_agent_list_workflows(server: MCPServer, request_id: str, params: List[Any]):
    if not server.workflow_definitions: # type: ignore # If empty on first call, try loading
        await server.run_in_executor(_load_workflow_definitions_blocking, server)
    return [
        {"workflow_id": wf["workflow_id"], "name": wf["name"], "description": wf.get("description"), "input_schema": wf.get("input_schema")}
        for wf_id, wf in server.workflow_definitions.items() # type: ignore
    ]

@agent_server.register("mcp.agent.runWorkflow")
async def handle_agent_run_workflow(server: MCPServer, request_id: str, params: List[Any]):
    workflow_id = params[0] # Schema validated: string
    inputs = params[1] if len(params) > 1 else {} # Schema validated: object or absent
    if workflow_id not in server.workflow_definitions: # type: ignore
        raise ValueError(f"Workflow ID '{workflow_id}' not found.")

    execution_id = f"exec_{uuid.uuid4().hex[:12]}"
    # cancellation_event = threading.Event() # For cooperative cancellation
    server.executions_state[execution_id] = { # type: ignore
        "execution_id": execution_id, "workflow_id": workflow_id, "status": "pending",
        "inputs": inputs, "log_file": str(AGENT_EXEC_LOG_DIR_CONF / f"{execution_id}.log"),
        # "cancellation_event": cancellation_event
    }
    # Workflow execution itself is blocking, so run it in a new Python thread.
    # The MCPServer's ThreadPoolExecutor is for short-lived blocking tasks within async handlers,
    # not for spawning very long-running workflow threads.
    thread = threading.Thread(target=_execute_workflow_in_thread,
                              args=(server, execution_id, workflow_id, inputs), daemon=True)
    server.executions_state[execution_id]["thread_obj"] = thread # type: ignore
    thread.start()
    
    return {"execution_id": execution_id, "workflow_id": workflow_id, "status": "started"}


@agent_server.register("mcp.agent.getWorkflowStatus")
async def handle_agent_get_workflow_status(server: MCPServer, request_id: str, params: List[Any]):
    execution_id = params[0] # Schema validated
    if execution_id not in server.executions_state: # type: ignore
        raise ValueError(f"Execution ID '{execution_id}' not found.")
    exec_data = server.executions_state[execution_id] # type: ignore
    
    # Log preview (small IO, sync here is okay, or executor for robustness)
    log_preview_list = []
    log_f_path_str = exec_data.get("log_file")
    if log_f_path_str:
        log_f_path = Path(log_f_path_str)
        if log_f_path.exists():
            try:
                with open(log_f_path, 'r', encoding='utf-8', errors='ignore') as lf_read:
                    log_preview_list = [line.strip() for line in lf_read.readlines()[-20:]] # Last 20
            except Exception as e_logread: server.logger.warning(f"Could not read log for exec {execution_id}: {e_logread}")
    
    return {
        "execution_id": execution_id, "workflow_id": exec_data["workflow_id"], "status": exec_data["status"],
        "start_time": exec_data.get("start_time").isoformat() if exec_data.get("start_time") else None,
        "end_time": exec_data.get("end_time").isoformat() if exec_data.get("end_time") else None,
        "output": exec_data.get("output"), "error_message": exec_data.get("error_message"),
        "log_preview": log_preview_list
    }

@agent_server.register("mcp.agent.stopWorkflow")
async def handle_agent_stop_workflow(server: MCPServer, request_id: str, params: List[Any]):
    execution_id = params[0] # Schema validated
    if execution_id not in server.executions_state: raise ValueError(f"Execution ID '{execution_id}' not found.") # type: ignore
    exec_data = server.executions_state[execution_id] # type: ignore

    current_status = exec_data.get("status")
    if current_status not in ["pending", "running"]: # Check current status
        return {"execution_id": execution_id, "status": "already_completed_or_not_running",
                "message": f"Workflow {execution_id} not stoppable (current status: {current_status})."}

    docker_container_id_to_stop = exec_data.get("docker_container_id")
    # cancellation_event_to_set = exec_data.get("cancellation_event") # For threads

    if docker_container_id_to_stop and server.docker_client: # type: ignore
        def stop_docker_container_blocking(): # For executor
            try:
                container_to_stop = server.docker_client.containers.get(docker_container_id_to_stop) # type: ignore
                server.logger.info(f"Exec {execution_id}: Sending stop signal to Docker container {docker_container_id_to_stop}")
                container_to_stop.stop(timeout=10) # 10s graceful timeout
                # Could also .remove() here if desired
                exec_data["status"] = "cancelling" # Workflow thread will update to "cancelled" or "failed"
                return {"execution_id": execution_id, "status": "stop_requested", "message": "Stop signal sent to Docker container."}
            except docker.errors.NotFound: # type: ignore
                return {"execution_id": execution_id, "status": "not_running", "message": "Docker container already gone."}
            except Exception as e_docker_stop:
                server.logger.error(f"Exec {execution_id}: Error stopping Docker container {docker_container_id_to_stop}: {e_docker_stop}")
                raise RuntimeError(f"Failed to stop Docker container: {e_docker_stop}") # Let framework handle

        return await server.run_in_executor(stop_docker_container_blocking)
    
    # elif cancellation_event_to_set: # For Python threads with cooperative cancellation
    #    server.logger.info(f"Exec {execution_id}: Setting cancellation event for Python thread.")
    #    cancellation_event_to_set.set()
    #    exec_data["status"] = "cancelling"
    #    return {"execution_id": execution_id, "status": "stop_requested", "message": "Cancellation requested for workflow thread."}
    else:
        return {"execution_id": execution_id, "status": "not_stoppable",
                "message": "Workflow type does not support external stopping or not a stoppable resource (e.g. non-Docker task)."}


# --- Server Lifecycle Hooks ---
async def on_agent_server_startup_hook(server: MCPServer):
    server.logger.info(f"Agent Server '{server.server_name}' custom startup actions...")
    # Initialize Docker client
    try:
        # docker.from_env() can be blocking. Run in executor.
        def init_docker_client_sync():
            return docker.from_env() # type: ignore
        server.docker_client = await server.run_in_executor(init_docker_client_sync) # type: ignore
        server.logger.info("Docker client initialized successfully via executor.")
    except Exception as docker_err_init:
        server.logger.warning(f"Failed to initialize Docker client: {docker_err_init}. Docker-based agents will be unavailable.", exc_info=True)
        server.docker_client = None # type: ignore
    
    # Load workflow definitions
    await server.run_in_executor(_load_workflow_definitions_blocking, server)

async def on_agent_server_shutdown_hook(server: MCPServer):
    server.logger.info(f"Agent Server '{server.server_name}' custom shutdown actions...")
    # Attempt to gracefully stop/clean up any active workflow executions
    for exec_id, exec_data in list(server.executions_state.items()): # type: ignore
        if exec_data.get("status") == "running":
            server.logger.info(f"Exec {exec_id}: Attempting cleanup/stop on server shutdown...")
            # This logic is complex: stopping threads is hard, Docker containers can be stopped.
            # cancellation_event = exec_data.get("cancellation_event")
            # if cancellation_event: cancellation_event.set() # Signal thread
            
            container_id_shutdown = exec_data.get("docker_container_id")
            if container_id_shutdown and server.docker_client: # type: ignore
                try:
                    # This stop is blocking, should ideally be run in executor if server needs to shutdown fast
                    # but during shutdown, we might afford to wait a bit.
                    # For a very clean shutdown, this should also be async/non-blocking call to stop.
                    def stop_docker_on_shutdown_sync():
                        cont = server.docker_client.containers.get(container_id_shutdown) # type: ignore
                        cont.stop(timeout=5)
                        if exec_data.get("parsed_yaml",{}).get("auto_remove_container",True): cont.remove(v=True)
                    server.logger.info(f"Exec {exec_id}: Requesting stop for Docker container {container_id_shutdown} on server shutdown.")
                    await server.run_in_executor(stop_docker_on_shutdown_sync) # Run stop in executor
                except Exception as e_stop_shutdown:
                    server.logger.warning(f"Exec {exec_id}: Error stopping Docker container {container_id_shutdown} during server shutdown: {e_stop_shutdown}")
            
            # Wait for Python threads if they were designed for clean exit
            # thread_obj_shutdown = exec_data.get("thread_obj")
            # if thread_obj_shutdown and thread_obj_shutdown.is_alive():
            #    thread_obj_shutdown.join(timeout=5) # Brief wait

agent_server.on_startup = on_agent_server_startup_hook # type: ignore
agent_server.on_shutdown = on_agent_server_shutdown_hook # type: ignore

# --- Main Entry Point ---
if __name__ == "__main__":
    script_logger = logging.getLogger("llmbasedos.servers.agent_script_main")
    log_level_main_agent = os.getenv(f"LLMBDO_{SERVER_NAME.upper()}_LOG_LEVEL", "INFO").upper()
    script_logger.setLevel(log_level_main_agent)
    if not script_logger.hasHandlers():
        ch_agent = logging.StreamHandler()
        ch_agent.setFormatter(logging.Formatter(f"%(asctime)s - AGENT MAIN - %(levelname)s - %(message)s"))
        script_logger.addHandler(ch_agent)

    try:
        asyncio.run(agent_server.start()) # MCPServer.start() will call on_startup/on_shutdown
    except KeyboardInterrupt:
        script_logger.info(f"Agent Server '{SERVER_NAME}' (main) stopped by KeyboardInterrupt.")
    except Exception as e_main_agent:
        script_logger.critical(f"Agent Server (main) crashed: {e_main_agent}", exc_info=True)
    finally:
        script_logger.info(f"Agent Server (main) is shutting down...")
        # Executor shutdown is handled by MCPServer.start()'s finally block.
        script_logger.info(f"Agent Server (main) fully shut down.")
