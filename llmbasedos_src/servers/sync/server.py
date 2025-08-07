# llmbasedos/servers/sync/server.py
import asyncio
import logging # Logger obtained from MCPServer
import os
from pathlib import Path
import uuid
import subprocess
import signal
import threading # For background job process checker
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Union

# --- Import Framework ---
from llmbasedos_src.mcp_server_framework import MCPServer 
from llmbasedos.common_utils import validate_mcp_path_param # Assurez-vous que fs_server en a besoin

# --- Server Specific Configuration ---
SERVER_NAME = "sync"
CAPS_FILE_PATH_STR = str(Path(__file__).parent / "caps.json")
SYNC_CUSTOM_ERROR_BASE = -32020

RCLONE_CONFIG_PATH_CONF = Path(os.getenv("LLMBDO_RCLONE_CONFIG_PATH", os.path.expanduser("~/.config/rclone/rclone.conf"))).resolve()
RCLONE_EXECUTABLE_CONF = os.getenv("LLMBDO_RCLONE_EXECUTABLE", "rclone")
SYNC_JOB_LOG_DIR_CONF = Path(os.getenv("LLMBDO_SYNC_JOB_LOG_DIR", f"/var/log/llmbasedos/{SERVER_NAME}"))
SYNC_JOB_LOG_DIR_CONF.mkdir(parents=True, exist_ok=True) # Ensure log dir exists

# In-memory stores, managed by the server instance
# SYNC_JOBS_STATE: Dict[str, Dict[str, Any]] = {} # job_id -> job_data (moved to server instance)
# RCLONE_PROCESSES_STATE: Dict[str, subprocess.Popen] = {} # job_id -> Popen (moved to server instance)

# Initialize server instance
sync_server = MCPServer(SERVER_NAME, CAPS_FILE_PATH_STR, custom_error_code_base=SYNC_CUSTOM_ERROR_BASE)

# Attach server-specific state to the instance
sync_server.sync_jobs_state: Dict[str, Dict[str, Any]] = {} # type: ignore
sync_server.rclone_processes_state: Dict[str, subprocess.Popen] = {} # type: ignore
sync_server.job_check_thread_stop_event = threading.Event() # type: ignore
sync_server.job_check_thread: Optional[threading.Thread] = None # type: ignore


# --- Rclone Utilities (Blocking, for executor) ---
def _run_rclone_cmd_blocking(server: MCPServer, args: List[str], job_info_context: str) -> Tuple[int, str, str]:
    cmd = [RCLONE_EXECUTABLE_CONF, f"--config={RCLONE_CONFIG_PATH_CONF}"] + args
    server.logger.info(f"Rclone (ctx: {job_info_context}): Executing {' '.join(cmd)}")
    try:
        # Increased timeout for potentially slower remote operations like listremotes
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=120)
        server.logger.info(f"Rclone (ctx: {job_info_context}) finished with code {proc.returncode}")
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError: msg = f"rclone executable '{RCLONE_EXECUTABLE_CONF}' not found."; server.logger.error(msg); return -1, "", msg
    except subprocess.TimeoutExpired: msg = f"rclone cmd (ctx: {job_info_context}) timed out."; server.logger.error(msg); return -2, "", msg
    except Exception as e: server.logger.error(f"Rclone cmd error (ctx: {job_info_context}): {e}", exc_info=True); return -3, "", str(e)

def _start_rclone_sync_proc_blocking(
    server: MCPServer, job_id: str, source: str, destination: str, extra_args: Optional[List[str]] = None
) -> Tuple[Optional[int], str]: # Returns PID or None, and error_message_str
    
    if job_id in server.rclone_processes_state and server.rclone_processes_state[job_id].poll() is None: # type: ignore
        return None, "Job is already running."

    # Using "copy" for safety by default. Can be overridden by rclone_args if user passes "sync" command.
    # Or, make the command (copy/sync) a parameter.
    rclone_command_verb = "copy" 
    # Check if user provided a verb in extra_args (e.g. "sync", "move")
    # This is a bit naive; a full rclone command parser would be better.
    if extra_args and extra_args[0] in ["sync", "move", "check", "copyto", "moveto", "copy"]:
        rclone_command_verb = extra_args.pop(0) # Use user's verb and remove from args

    cmd = [RCLONE_EXECUTABLE_CONF, f"--config={RCLONE_CONFIG_PATH_CONF}", rclone_command_verb,
           source, destination, "--progress", "-v", "--log-level", "INFO"] # Default log level for rclone
    if extra_args: cmd.extend(extra_args)

    log_file = SYNC_JOB_LOG_DIR_CONF / f"{job_id}.log"
    server.logger.info(f"Job {job_id}: Starting rclone: {' '.join(cmd)}. Log: {log_file}")
    
    try:
        with open(log_file, 'ab') as lf: # Append binary for robustness
            lf.write(f"\n--- Job '{job_id}' started at {datetime.now(timezone.utc).isoformat()} ---\n".encode())
            lf.write(f"Command: {' '.join(cmd)}\n---\n".encode())
            lf.flush()
            # Use os.setsid for process group management on POSIX for reliable termination
            proc = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT, text=False, 
                                    preexec_fn=os.setsid if os.name != 'nt' else None)
        
        server.rclone_processes_state[job_id] = proc # type: ignore
        job_entry = server.sync_jobs_state.get(job_id, {"job_id": job_id, "is_adhoc": True}) # type: ignore
        job_entry.update({
            "source": source, "destination": destination, "rclone_args": extra_args or [],
            "process_pid": proc.pid, "status": "running", 
            "start_time": datetime.now(timezone.utc), "log_file": str(log_file) # Store as string
        })
        server.sync_jobs_state[job_id] = job_entry # type: ignore
        return proc.pid, ""
    except FileNotFoundError: msg = f"rclone executable '{RCLONE_EXECUTABLE_CONF}' not found."; server.logger.error(msg); return None, msg
    except Exception as e: server.logger.error(f"Job {job_id}: Failed to start rclone: {e}", exc_info=True); return None, str(e)

# --- Background Job Process Checker Thread ---
def _job_process_checker_thread_target(server: MCPServer):
    server.logger.info("Rclone job process checker thread started.")
    while not server.job_check_thread_stop_event.is_set(): # type: ignore
        for job_id, process in list(server.rclone_processes_state.items()): # type: ignore # Iterate copy
            if process.poll() is not None: # Process finished
                server.logger.info(f"Job {job_id} (PID {process.pid}) process finished with code {process.returncode}.")
                if job_id in server.sync_jobs_state: # type: ignore
                    job_data = server.sync_jobs_state[job_id] # type: ignore
                    job_data["status"] = "completed" if process.returncode == 0 else "failed"
                    job_data["end_time"] = datetime.now(timezone.utc)
                    job_data["return_code"] = process.returncode
                    job_data["process_pid"] = None # Clear PID as process is gone
                server.rclone_processes_state.pop(job_id, None) # type: ignore # Remove from active
        
        # Wait for a bit or until stop event is set
        server.job_check_thread_stop_event.wait(timeout=5) # Check every 5 seconds # type: ignore
    server.logger.info("Rclone job process checker thread stopped.")


# --- Sync Capability Handlers (decorated) ---
@sync_server.register_method("mcp.sync.listRemotes")
async def handle_sync_list_remotes(server: MCPServer, request_id: str, params: List[Any]):
    ret_code, stdout, stderr = await server.run_in_executor(
        _run_rclone_cmd_blocking, server, ["listremotes"], "mcp.sync.listRemotes"
    )
    if ret_code != 0: raise RuntimeError(f"Failed to list rclone remotes: {stderr or 'Unknown rclone error'}")
    return [line.strip().rstrip(':') for line in stdout.splitlines() if line.strip()]

@sync_server.register_method("mcp.sync.listJobs")
async def handle_sync_list_jobs(server: MCPServer, request_id: str, params: List[Any]):
    # Checker thread updates statuses, this just reads from server.sync_jobs_state
    response = []
    for job_id, job_data in server.sync_jobs_state.items(): # type: ignore
        is_running = (job_id in server.rclone_processes_state and server.rclone_processes_state[job_id].poll() is None) # type: ignore
        entry = {
            "job_id": job_id,
            "description": job_data.get("description", "Ad-hoc job" if job_data.get("is_adhoc") else "N/A"),
            "source": job_data.get("source"), "destination": job_data.get("destination"),
            "status": "running" if is_running else job_data.get("status", "unknown"),
            "is_running": is_running,
            "start_time": job_data.get("start_time").isoformat() if job_data.get("start_time") else None,
            "end_time": job_data.get("end_time").isoformat() if job_data.get("end_time") else None,
            "pid": job_data.get("process_pid") # PID is present if running
        }
        response.append(entry)
    return response

@sync_server.register_method("mcp.sync.runJob")
async def handle_sync_run_job(server: MCPServer, request_id: str, params: List[Any]):
    job_spec = params[0] # Validated by schema
    job_id_prefix = job_spec.get("job_id_prefix", "adhoc")
    source = job_spec["source"]; destination = job_spec["destination"]
    rclone_args = job_spec.get("rclone_args", [])

    exec_job_id = f"{job_id_prefix}_{uuid.uuid4().hex[:8]}"
    
    pid, err_msg = await server.run_in_executor(
        _start_rclone_sync_proc_blocking, server, exec_job_id, source, destination, rclone_args
    )
    if pid is None: raise RuntimeError(f"Failed to start rclone job '{exec_job_id}': {err_msg}")
    return {"job_id": exec_job_id, "status": "started", "pid": pid, "message": f"Job '{exec_job_id}' started."}

@sync_server.register_method("mcp.sync.getJobStatus")
async def handle_sync_get_job_status(server: MCPServer, request_id: str, params: List[Any]):
    job_id = params[0]
    if job_id not in server.sync_jobs_state: raise ValueError(f"Job ID '{job_id}' not found.") # type: ignore

    job_data = server.sync_jobs_state[job_id] # type: ignore
    is_running = (job_id in server.rclone_processes_state and server.rclone_processes_state[job_id].poll() is None) # type: ignore
    status_msg = "running" if is_running else job_data.get("status", "unknown")

    log_preview = []
    log_file_path_str = job_data.get("log_file")
    if log_file_path_str and Path(log_file_path_str).exists():
        try: # Small IO, can be sync here or executor for extreme robustness
            with open(log_file_path_str, 'r', errors='ignore') as lf:
                log_preview = [line.strip() for line in lf.readlines()[-20:]] # Last 20 lines
        except Exception as e: server.logger.warning(f"Could not read log for job {job_id}: {e}")
    
    return {"job_id": job_id, "is_running": is_running, "status_message": status_msg,
            "start_time": job_data.get("start_time").isoformat() if job_data.get("start_time") else None,
            "end_time": job_data.get("end_time").isoformat() if job_data.get("end_time") else None,
            "return_code": job_data.get("return_code"), "log_preview": log_preview}

@sync_server.register_method("mcp.sync.stopJob")
async def handle_sync_stop_job(server: MCPServer, request_id: str, params: List[Any]):
    job_id = params[0]
    if job_id not in server.rclone_processes_state or server.rclone_processes_state[job_id].poll() is not None: # type: ignore
        msg = f"Job '{job_id}' not running or not found."
        if job_id in server.sync_jobs_state: server.sync_jobs_state[job_id]["status"] = "unknown" # type: ignore # Or "not_running"
        return {"job_id": job_id, "status": "not_running", "message": msg}

    process_to_stop = server.rclone_processes_state[job_id] # type: ignore
    server.logger.info(f"Job {job_id}: Attempting to stop rclone process PID {process_to_stop.pid}.")
    try:
        # Sending signal is quick. The process termination is async.
        if os.name != 'nt': os.killpg(os.getpgid(process_to_stop.pid), signal.SIGTERM)
        else: process_to_stop.terminate()
        
        if job_id in server.sync_jobs_state: server.sync_jobs_state[job_id]["status"] = "stopping" # type: ignore
        # The checker thread will eventually update to completed/failed after process exits.
        return {"job_id": job_id, "status": "stopping_signal_sent",
                "message": f"Sent SIGTERM to rclone job {job_id} (PID {process_to_stop.pid})."}
    except Exception as e: # ProcessLookupError if PID no longer exists
        server.logger.error(f"Job {job_id}: Failed to send stop signal (PID {process_to_stop.pid}): {e}", exc_info=True)
        raise RuntimeError(f"Failed to stop job {job_id}: {e}")


# --- Server Lifecycle Hooks for MCPServer ---
async def on_sync_server_startup_hook(server: MCPServer): # Renamed to avoid conflict if MCPServer has same name
    server.logger.info(f"Sync Server '{server.server_name}' custom startup actions...")
    server.job_check_thread_stop_event.clear() # type: ignore
    server.job_check_thread = threading.Thread( # type: ignore
        target=_job_process_checker_thread_target, args=(server,), daemon=True)
    server.job_check_thread.start() # type: ignore

async def on_sync_server_shutdown_hook(server: MCPServer):
    server.logger.info(f"Sync Server '{server.server_name}' custom shutdown actions...")
    server.job_check_thread_stop_event.set() # type: ignore
    if server.job_check_thread and server.job_check_thread.is_alive(): # type: ignore
        server.logger.info("Waiting for job checker thread to stop...")
        server.job_check_thread.join(timeout=7) # Give it a bit more time # type: ignore
        if server.job_check_thread.is_alive(): # type: ignore
            server.logger.warning("Job checker thread did not stop in time.")
    
    # Terminate any remaining rclone processes forcefully
    for job_id, process in list(server.rclone_processes_state.items()): # type: ignore
        if process.poll() is None: # If still running
            server.logger.warning(f"Job {job_id} (PID {process.pid}): Force terminating rclone process on shutdown.")
            try:
                if os.name != 'nt': os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                else: process.kill()
                process.wait(timeout=3) # Brief wait
            except Exception as e_term:
                server.logger.error(f"Job {job_id}: Error force terminating rclone PID {process.pid}: {e_term}")

# Assign hooks to the server instance
sync_server.on_startup = on_sync_server_startup_hook # type: ignore
sync_server.on_shutdown = on_sync_server_shutdown_hook # type: ignore


# --- Main Entry Point ---
if __name__ == "__main__":
    script_logger = logging.getLogger("llmbasedos.servers.sync_script_main")
    # Basic config for this script's logger, MCPServer instance handles its own.
    log_level_main = os.getenv(f"LLMBDO_{SERVER_NAME.upper()}_LOG_LEVEL", "INFO").upper()
    script_logger.setLevel(log_level_main)
    if not script_logger.hasHandlers():
        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter(f"%(asctime)s - SYNC MAIN - %(levelname)s - %(message)s"))
        script_logger.addHandler(ch)

    try:
        # MCPServer's start method will call on_startup and on_shutdown if they are set
        asyncio.run(sync_server.start())
    except KeyboardInterrupt:
        script_logger.info(f"Server '{SERVER_NAME}' (main) stopped by KeyboardInterrupt.")
    except Exception as e_main_sync:
        script_logger.critical(f"Sync Server (main) crashed: {e_main_sync}", exc_info=True)
    finally:
        script_logger.info(f"Sync Server (main) is shutting down...")
        # If asyncio.run() completed or was interrupted, the loop is no longer running.
        # MCPServer's own finally block in start() handles executor shutdown and socket cleanup.
        # on_shutdown hook for this server was already called by MCPServer.start()'s finally block
        # if it was successfully started and then shutdown (e.g. by CancelledError).
        # If startup itself failed, on_shutdown might not have run.
        # For robustness, ensure critical cleanup if thread was started but server.start() didn't run full cycle.
        if sync_server.job_check_thread and sync_server.job_check_thread.is_alive(): # type: ignore
            script_logger.warning("Job check thread still alive after server stop, attempting to stop it now.")
            sync_server.job_check_thread_stop_event.set() # type: ignore
            sync_server.job_check_thread.join(timeout=5) # type: ignore
        script_logger.info(f"Sync Server (main) fully shut down.")
