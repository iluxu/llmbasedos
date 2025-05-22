# llmbasedos_pkg/gateway/config.py
import os
from pathlib import Path
from typing import Dict, Any, List, Optional
import logging # logging au lieu de logging.config pour info/warning au niveau module
import yaml # Pour charger les tiers de licence depuis un fichier

# --- Core Gateway Settings ---
GATEWAY_HOST: str = os.getenv("LLMBDO_GATEWAY_HOST", "0.0.0.0")
GATEWAY_WEB_PORT: int = int(os.getenv("LLMBDO_GATEWAY_WEB_PORT", "8000"))
GATEWAY_UNIX_SOCKET_PATH: Path = Path(os.getenv("LLMBDO_GATEWAY_UNIX_SOCKET_PATH", "/run/mcp/gateway.sock"))
GATEWAY_EXECUTOR_MAX_WORKERS: int = int(os.getenv("LLMBDO_GATEWAY_EXECUTOR_WORKERS", "4")) # 

# --- MCP Settings ---
MCP_CAPS_DIR: Path = Path(os.getenv("LLMBDO_MCP_CAPS_DIR", "/run/mcp"))
MCP_CAPS_DIR.mkdir(parents=True, exist_ok=True) # Assurer l'existence du répertoire

# --- Licence & Auth Settings ---
LICENCE_FILE_PATH: Path = Path(os.getenv("LLMBDO_LICENCE_FILE_PATH", "/etc/llmbasedos/lic.key"))
# Chemin vers le fichier YAML pour les tiers de licence.
# Dans Docker, ce fichier sera monté. S'il est dans le code source, ajustez le Dockerfile COPY.
# Votre docker-compose monte `./gateway/licence_tiers.yaml` s'il existait là.
# Si c'est un fichier monté via docker-compose à /etc/llmbasedos/licence_tiers.yaml :
LICENCE_TIERS_CONFIG_PATH_STR: str = os.getenv("LLMBDO_LICENCE_TIERS_CONFIG_PATH", "/etc/llmbasedos/licence_tiers.yaml")
LICENCE_TIERS_CONFIG_PATH: Path = Path(LICENCE_TIERS_CONFIG_PATH_STR)

DEFAULT_LICENCE_TIERS: Dict[str, Dict[str, Any]] = {
    "FREE": {
        "rate_limit_requests": 100, "rate_limit_window_seconds": 3600, # Augmenté pour tests
        "allowed_capabilities": ["mcp.hello", "mcp.listCapabilities", "mcp.licence.check", "mcp.fs.list"],
        "llm_access": False, "allowed_llm_models": [],
    },
    "PRO": {
        "rate_limit_requests": 1000, "rate_limit_window_seconds": 3600,
        "allowed_capabilities": [
            "mcp.hello", "mcp.listCapabilities", "mcp.licence.check",
            "mcp.fs.*", "mcp.mail.*", "mcp.llm.chat", # Mail complet pour PRO
            "mcp.sync.*", "mcp.agent.*" # Sync et Agent complet pour PRO
        ],
        "llm_access": True, "allowed_llm_models": ["*"], # Permet tous les modèles configurés
    },
    "ELITE": {
        "rate_limit_requests": 10000, "rate_limit_window_seconds": 3600,
        "allowed_capabilities": ["*"], "llm_access": True, "allowed_llm_models": ["*"],
    }
}
LICENCE_TIERS: Dict[str, Dict[str, Any]] = DEFAULT_LICENCE_TIERS # Fallback
if LICENCE_TIERS_CONFIG_PATH.exists() and LICENCE_TIERS_CONFIG_PATH.is_file():
    try:
        with LICENCE_TIERS_CONFIG_PATH.open('r') as f:
            loaded_tiers = yaml.safe_load(f)
            if isinstance(loaded_tiers, dict) and loaded_tiers.get("tiers"): # Attendre une clé 'tiers'
                LICENCE_TIERS = loaded_tiers["tiers"]
                logging.info(f"Loaded licence tiers from {LICENCE_TIERS_CONFIG_PATH}")
            else:
                logging.warning(f"Invalid or empty 'tiers' structure in {LICENCE_TIERS_CONFIG_PATH}. Using defaults.")
    except Exception as e_tiers:
        logging.error(f"Error loading licence tiers from {LICENCE_TIERS_CONFIG_PATH}: {e_tiers}. Using defaults.", exc_info=True)
else:
    logging.info(f"Licence tiers config file not found at {LICENCE_TIERS_CONFIG_PATH}. Using default tiers.")


# --- Upstream LLM Settings ---
# Clé API OpenAI globale (peut être surchargée par modèle dans AVAILABLE_LLM_MODELS)
OPENAI_API_KEY: Optional[str] = os.getenv("OPENAI_API_KEY") # Maintenant 'Optional' est défini
if not OPENAI_API_KEY:
    logging.warning("OPENAI_API_KEY environment variable not set. OpenAI models may not function.")

# Fournisseur LLM par défaut si un client ne spécifie pas de modèle ou un alias non mappé
DEFAULT_LLM_PROVIDER: str = os.getenv("LLMBDO_DEFAULT_LLM_PROVIDER", "openai")

# Configuration des modèles LLM disponibles
# Chaque clé est un "alias" que le client peut utiliser.
# `model_name` est le nom réel pour l'API du fournisseur.
# `api_base_url` est l'URL de base de l'API pour ce modèle.
# `api_key` peut surcharger une clé globale (utile si vous utilisez plusieurs comptes OpenAI).
# `is_default`: True si c'est le modèle par défaut pour ce `provider`.
AVAILABLE_LLM_MODELS: Dict[str, Dict[str, Any]] = {
    "gpt-3.5-turbo": { # Alias
        "provider": "openai",
        "model_name": "gpt-3.5-turbo", # Nom réel pour l'API
        "api_base_url": os.getenv("OPENAI_API_BASE_URL", "https://api.openai.com/v1"),
        "api_key": None, # Utilisera OPENAI_API_KEY global si None ici
        "is_default": True if DEFAULT_LLM_PROVIDER == "openai" else False,
    },
    "gpt-4o": {
        "provider": "openai",
        "model_name": "gpt-4o",
        "api_base_url": os.getenv("OPENAI_API_BASE_URL", "https://api.openai.com/v1"),
        "api_key": None,
        "is_default": False,
    },
    "local-llama": { # Alias pour un modèle llama.cpp local
        "provider": "llama_cpp",
        # `model_name` pour llama.cpp est souvent optionnel si un seul modèle est chargé par le serveur llama.cpp.
        # S'il est spécifié, il doit correspondre à un alias/modèle connu du serveur llama.cpp.
        "model_name": os.getenv("LLAMA_CPP_DEFAULT_MODEL", "default-model-alias"),
        "api_base_url": os.getenv("LLAMA_CPP_API_BASE_URL", "http://localhost:8080/v1"), # typiquement /v1 pour API compatible OpenAI
        "api_key": None, # Généralement pas nécessaire pour llama.cpp
        "is_default": True if DEFAULT_LLM_PROVIDER == "llama_cpp" else False,
    },
    # Exemple pour Ollama (si vous l'utilisez)
    # "ollama-llama3": {
    #     "provider": "ollama", # Vous devrez peut-être ajouter la logique pour 'ollama' dans upstream.py si différente d'OpenAI
    #     "model_name": "llama3", # Nom du modèle dans Ollama
    #     "api_base_url": os.getenv("OLLAMA_API_BASE_URL", "http://localhost:11434/v1"),
    #     "api_key": None,
    #     "is_default": False,
    # },
}

# --- Logging ---
LOG_LEVEL_STR: str = os.getenv("LLMBDO_LOG_LEVEL", "INFO").upper()
LOG_LEVEL_FALLBACK: int = logging.INFO # Renommé pour éviter conflit potentiel
LOG_LEVEL: int = logging.getLevelName(LOG_LEVEL_STR)
if not isinstance(LOG_LEVEL, int):
    logging.warning(f"Invalid LLMBDO_LOG_LEVEL '{LOG_LEVEL_STR}'. Defaulting to INFO.")
    LOG_LEVEL = LOG_LEVEL_FALLBACK
    LOG_LEVEL_STR = logging.getLevelName(LOG_LEVEL_FALLBACK)

# CONFIGURATION DE LOGGING MINIMALE POUR TESTER
LOGGING_CONFIG: Dict[str, Any] = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "simple": {
            "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        }
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "simple", # Utilise uniquement le formateur simple
            "stream": "ext://sys.stdout"
        }
    },
    "root": {
        "handlers": ["console"],
        "level": "DEBUG", # Mettre DEBUG pour voir tout
    },
    "loggers": { 
        # Simplifier les loggers spécifiques pour le test
        "uvicorn": {"level": "INFO", "handlers": ["console"], "propagate": False},
        "fastapi": {"level": "INFO", "handlers": ["console"], "propagate": False},
        "websockets": {"level": "WARNING", "handlers": ["console"], "propagate": False},
        "llmbasedos": {"level": "DEBUG", "handlers": ["console"], "propagate": False}, # Notre app en DEBUG
        "httpx": {"level": "WARNING", "handlers": ["console"], "propagate": False},
        "watchdog": {"level": "WARNING", "handlers": ["console"], "propagate": False},
    }
}

# JSON RPC Default Error Codes
JSONRPC_PARSE_ERROR: int = -32700
JSONRPC_INVALID_REQUEST: int = -32600
JSONRPC_METHOD_NOT_FOUND: int = -32601
JSONRPC_INVALID_PARAMS: int = -32602
JSONRPC_INTERNAL_ERROR: int = -32603
JSONRPC_AUTH_ERROR: int = -32000
JSONRPC_RATE_LIMIT_ERROR: int = -32001
JSONRPC_PERMISSION_DENIED_ERROR: int = -32002
JSONRPC_LLM_QUOTA_EXCEEDED_ERROR: int = -32003 # <<< NOUVEAU CODE D'ERREUR
JSONRPC_LLM_MODEL_NOT_ALLOWED_ERROR: int = -32004