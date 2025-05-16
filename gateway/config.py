# llmbasedos/gateway/config.py
import os
from pathlib import Path
from typing import Dict, Any, List, Optional # Added Optional
import logging # For LOG_LEVEL

# --- Core Gateway Settings ---
GATEWAY_HOST: str = os.getenv("LLMBDO_GATEWAY_HOST", "0.0.0.0")
GATEWAY_WEB_PORT: int = int(os.getenv("LLMBDO_GATEWAY_WEB_PORT", "8000")) # For WebSocket
GATEWAY_UNIX_SOCKET_PATH_STR: str = os.getenv("LLMBDO_GATEWAY_UNIX_SOCKET_PATH", "/run/mcp/gateway.sock")
GATEWAY_UNIX_SOCKET_PATH: Path = Path(GATEWAY_UNIX_SOCKET_PATH_STR)


# --- MCP Settings ---
MCP_CAPS_DIR_STR: str = os.getenv("LLMBDO_MCP_CAPS_DIR", "/run/mcp")
MCP_CAPS_DIR: Path = Path(MCP_CAPS_DIR_STR)
# Ensure MCP_CAPS_DIR exists (useful for dev, in prod systemd or postinstall should handle)
# This will be done by the service creating it (e.g. gateway or postinstall)
# MCP_CAPS_DIR.mkdir(parents=True, exist_ok=True) # Avoid doing this at import time

# --- Licence & Auth Settings ---
LICENCE_FILE_PATH_STR: str = os.getenv("LLMBDO_LICENCE_FILE_PATH", "/etc/llmbasedos/lic.key")
LICENCE_FILE_PATH: Path = Path(LICENCE_FILE_PATH_STR)

LICENCE_TIERS_CONFIG_PATH_STR: str = os.getenv("LLMBDO_LICENCE_TIERS_CONFIG_PATH", "/etc/llmbasedos/licence_tiers.yaml")
LICENCE_TIERS_CONFIG_PATH: Path = Path(LICENCE_TIERS_CONFIG_PATH_STR)

# Default tiers, can be overridden by LICENCE_TIERS_CONFIG_PATH
DEFAULT_LICENCE_TIERS: Dict[str, Dict[str, Any]] = {
    "FREE": {
        "rate_limit_requests": 100, # Increased slightly for better UX
        "rate_limit_window_seconds": 3600, # Per hour
        "allowed_capabilities": ["mcp.hello", "mcp.listCapabilities", "mcp.licence.check", "mcp.fs.list", "mcp.fs.read"], # Read for basic file viewing
        "llm_access": False,
        "max_llm_tokens_per_request": 0, # No LLM access
        "max_llm_tokens_per_day": 0,
    },
    "PRO": {
        "rate_limit_requests": 5000,
        "rate_limit_window_seconds": 3600,
        "allowed_capabilities": [
            "mcp.hello", "mcp.listCapabilities", "mcp.licence.check",
            "mcp.fs.*", "mcp.mail.*", "mcp.sync.list*", "mcp.sync.getJobStatus", # More granular sync
            "mcp.agent.listWorkflows", "mcp.agent.getWorkflowStatus",
            "mcp.llm.chat"
        ],
        "llm_access": True,
        "allowed_llm_models": ["*"], # Example: "gpt-3.5-turbo", "local-model/small"
        "max_llm_tokens_per_request": 4096,
        "max_llm_tokens_per_day": 1000000,
    },
    "ELITE": {
        "rate_limit_requests": 50000,
        "rate_limit_window_seconds": 3600,
        "allowed_capabilities": ["*"], # All capabilities
        "llm_access": True,
        "allowed_llm_models": ["*"], # All configured models
        "max_llm_tokens_per_request": 16384,
        "max_llm_tokens_per_day": 10000000, # Generous limit
    }
}
# Actual LICENCE_TIERS will be loaded in auth.py by merging default and file config

# --- Upstream LLM Settings ---
# These can be further configured per model in a separate config file if needed
OPENAI_API_KEY: Optional[str] = os.getenv("OPENAI_API_KEY")
OPENAI_API_BASE_URL: str = os.getenv("OPENAI_API_BASE_URL", "https://api.openai.com/v1")
LLAMA_CPP_API_BASE_URL: Optional[str] = os.getenv("LLAMA_CPP_API_BASE_URL", "http://localhost:8080/v1") # OpenAI compatible endpoint
DEFAULT_LLM_PROVIDER: str = os.getenv("LLMBDO_DEFAULT_LLM_PROVIDER", "openai") # "openai" or "llama_cpp"

# Model aliases or specific model strings
# Example: "openai/gpt-4", "llama_cpp/mistral-7b-instruct"
# This mapping could be externalized to a config file too.
AVAILABLE_LLM_MODELS: Dict[str, Dict[str, str]] = {
    "default_openai": {"provider": "openai", "model_name": "gpt-3.5-turbo"},
    "gpt-4": {"provider": "openai", "model_name": "gpt-4"},
    "default_llama_cpp": {"provider": "llama_cpp", "model_name": "local-model"}, # Model name might be ignored by llama.cpp if only one loaded
}

# --- Logging ---
LOG_LEVEL_STR: str = os.getenv("LLMBDO_GATEWAY_LOG_LEVEL", "INFO").upper()
LOG_FORMAT: str = os.getenv("LLMBDO_GATEWAY_LOG_FORMAT", "simple") # "simple" or "json"
# LOG_LEVEL will be set by dictConfig

# --- Thread Pool for Blocking Tasks ---
GATEWAY_EXECUTOR_MAX_WORKERS: int = int(os.getenv("LLMBDO_GATEWAY_EXECUTOR_MAX_WORKERS", "4"))

# --- JSON RPC Default Error Codes (moved to mcp_server_framework) ---
# Custom error codes for auth/rate limiting specific to gateway
JSONRPC_AUTH_ERROR: int = -32000
JSONRPC_RATE_LIMIT_ERROR: int = -32001
JSONRPC_PERMISSION_DENIED_ERROR: int = -32002
JSONRPC_LLM_QUOTA_EXCEEDED_ERROR: int = -32004 # New for LLM token quotas
JSONRPC_LLM_MODEL_NOT_ALLOWED_ERROR: int = -32005 # New for LLM model tiering

# Shared secret for internal trusted communication if needed (e.g. for bypassing some checks for shell)
# For now, shell also goes through full auth.
INTERNAL_SHARED_SECRET: Optional[str] = os.getenv("LLMBDO_INTERNAL_SHARED_SECRET")
