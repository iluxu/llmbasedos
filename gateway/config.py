# llmbasedos/gateway/config.py
import os
from pathlib import Path
from typing import Dict, Any, List
import logging
import yaml # Pour charger les tiers de licence depuis un fichier

# --- Core Gateway Settings ---
GATEWAY_HOST: str = os.getenv("LLMBDO_GATEWAY_HOST", "0.0.0.0")
GATEWAY_WEB_PORT: int = int(os.getenv("LLMBDO_GATEWAY_WEB_PORT", "8000"))
GATEWAY_UNIX_SOCKET_PATH: str = os.getenv("LLMBDO_GATEWAY_UNIX_SOCKET_PATH", "/run/mcp/gateway.sock")

# --- MCP Settings ---
MCP_CAPS_DIR: Path = Path(os.getenv("LLMBDO_MCP_CAPS_DIR", "/run/mcp"))
MCP_CAPS_DIR.mkdir(parents=True, exist_ok=True)

# --- Licence & Auth Settings ---
LICENCE_FILE_PATH: Path = Path(os.getenv("LLMBDO_LICENCE_FILE_PATH", "/etc/llmbasedos/lic.key"))
LICENCE_TIERS_CONFIG_PATH: Path = Path(os.getenv("LLMBDO_LICENCE_TIERS_CONFIG_PATH", "/opt/llmbasedos/gateway/licence_tiers.yaml")) # Chemin par défaut dans le conteneur

# Default tiers definition (fallback if file not found or invalid)
DEFAULT_LICENCE_TIERS: Dict[str, Dict[str, Any]] = {
    "FREE": {
        "rate_limit_requests": 10, "rate_limit_window_seconds": 3600,
        "allowed_capabilities": ["mcp.hello", "mcp.listCapabilities", "mcp.licence.check", "mcp.fs.list"],
        "llm_access": False,
    },
    "PRO": {
        "rate_limit_requests": 1000, "rate_limit_window_seconds": 3600,
        "allowed_capabilities": [
            "mcp.hello", "mcp.listCapabilities", "mcp.licence.check",
            "mcp.fs.*", "mcp.mail.list", "mcp.mail.read", "mcp.llm.chat",
            "mcp.sync.*", "mcp.agent.listWorkflows", "mcp.agent.runWorkflow", "mcp.agent.getWorkflowStatus" # Added sync and agent
        ],
        "llm_access": True, "allowed_llm_models": ["*"], # Simpler: allow all configured models for PRO
    },
    "ELITE": {
        "rate_limit_requests": 10000, "rate_limit_window_seconds": 3600,
        "allowed_capabilities": ["*"], "llm_access": True, "allowed_llm_models": ["*"],
    }
}
LICENCE_TIERS: Dict[str, Dict[str, Any]] = DEFAULT_LICENCE_TIERS
try:
    if LICENCE_TIERS_CONFIG_PATH.exists():
        with LICENCE_TIERS_CONFIG_PATH.open('r') as f:
            loaded_tiers = yaml.safe_load(f)
            if isinstance(loaded_tiers, dict) and loaded_tiers: # Basic validation
                LICENCE_TIERS = loaded_tiers
                logging.info(f"Loaded licence tiers from {LICENCE_TIERS_CONFIG_PATH}")
            else:
                logging.warning(f"Invalid or empty licence tiers config at {LICENCE_TIERS_CONFIG_PATH}. Using defaults.")
    else:
        logging.info(f"Licence tiers config file not found at {LICENCE_TIERS_CONFIG_PATH}. Using default tiers.")
except Exception as e:
    logging.error(f"Error loading licence tiers from {LICENCE_TIERS_CONFIG_PATH}: {e}. Using defaults.", exc_info=True)


# --- Upstream LLM Settings ---
OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "sk-not_set_please_provide_via_env")
LLAMA_CPP_URL: str = os.getenv("LLAMA_CPP_URL", "http://localhost:8080") # Often needs to be host.docker.internal or service name in Docker
DEFAULT_LLM_PROVIDER: str = os.getenv("LLMBDO_DEFAULT_LLM_PROVIDER", "openai")
OPENAI_DEFAULT_MODEL: str = os.getenv("OPENAI_DEFAULT_MODEL", "gpt-3.5-turbo")
LLAMA_CPP_DEFAULT_MODEL: str = os.getenv("LLAMA_CPP_DEFAULT_MODEL", "local-model") # llama.cpp model often an alias

# --- Logging ---
LOG_LEVEL_STR: str = os.getenv("LLMBDO_LOG_LEVEL", "INFO").upper()
LOG_LEVEL: int = logging.getLevelName(LOG_LEVEL_STR) # GetLevelName handles invalid names gracefully
if not isinstance(LOG_LEVEL, int): # Fallback if invalid string
    LOG_LEVEL = logging.INFO
    LOG_LEVEL_STR = "INFO"


# Using the same LOGGING_CONFIG structure as before, it's already fairly robust.
# Ensure formatters and handlers are appropriate for container logging (e.g., stdout).
LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {
            "()": "python_json_logger.jsonlogger.JsonFormatter",
            "format": "%(asctime)s %(levelname)s %(name)s %(module)s %(funcName)s %(lineno)d %(message)s"
        },
        "simple": { # Good for console output / supervisord logs if not sending to a log aggregator
            "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        },
    },
    "handlers": {
        "console": { # Default to simple for TTY, JSON for services if preferred
            "class": "logging.StreamHandler",
            "formatter": os.getenv("LLMBDO_LOG_FORMAT", "simple"), # Allow overriding formatter via ENV
            "stream": "ext://sys.stdout" # Ensures logs go to stdout for Docker
        }
    },
    "root": { # Root logger configuration
        "handlers": ["console"],
        "level": LOG_LEVEL, # Global log level
    },
    "loggers": { # Per-module log levels
        "uvicorn": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "fastapi": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "websockets": {"handlers": ["console"], "level": "WARNING", "propagate": False}, # Less verbose websockets
        "llmbasedos": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False}, # Our app's logger
        # Add other libraries if they are too noisy
        "httpx": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        "watchdog": {"handlers": ["console"], "level": "WARNING", "propagate": False},
    }
}
# Ensure the chosen formatter for console exists
if LOGGING_CONFIG["handlers"]["console"]["formatter"] not in LOGGING_CONFIG["formatters"]:
    LOGGING_CONFIG["handlers"]["console"]["formatter"] = "simple"


# JSON RPC Default Error Codes
JSONRPC_PARSE_ERROR: int = -32700
JSONRPC_INVALID_REQUEST: int = -32600
JSONRPC_METHOD_NOT_FOUND: int = -32601
JSONRPC_INVALID_PARAMS: int = -32602
JSONRPC_INTERNAL_ERROR: int = -32603
# Custom error codes for auth/rate limiting
JSONRPC_AUTH_ERROR: int = -32000
JSONRPC_RATE_LIMIT_ERROR: int = -32001
JSONRPC_PERMISSION_DENIED_ERROR: int = -32002