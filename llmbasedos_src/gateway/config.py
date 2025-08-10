# llmbasedos_pkg/gateway/config.py
import os
from pathlib import Path
from typing import Dict, Any, List, Optional
import logging
import yaml # Garder l'import, la logique de chargement peut rester pour plus tard

# --- Core Gateway Settings ---
GATEWAY_HOST: str = os.getenv("LLMBDO_GATEWAY_HOST", "0.0.0.0")
GATEWAY_WEB_PORT: int = int(os.getenv("LLMBDO_GATEWAY_WEB_PORT", "8000"))
GATEWAY_UNIX_SOCKET_PATH: Path = Path(os.getenv("LLMBDO_GATEWAY_UNIX_SOCKET_PATH", "/run/mcp/gateway.sock"))
GATEWAY_EXECUTOR_MAX_WORKERS: int = int(os.getenv("LLMBDO_GATEWAY_EXECUTOR_WORKERS", "4"))

# --- MCP Settings ---
MCP_CAPS_DIR: Path = Path(os.getenv("LLMBDO_MCP_CAPS_DIR", "/run/mcp"))
MCP_CAPS_DIR.mkdir(parents=True, exist_ok=True)

# --- Licence & Auth Settings ---
LICENCE_FILE_PATH: Path = Path(os.getenv("LLMBDO_LICENCE_FILE_PATH", "/etc/llmbasedos/lic.key"))
LICENCE_TIERS_CONFIG_PATH_STR: str = os.getenv("LLMBDO_LICENCE_TIERS_CONFIG_PATH", "/etc/llmbasedos/licence_tiers.yaml")
LICENCE_TIERS_CONFIG_PATH: Path = Path(LICENCE_TIERS_CONFIG_PATH_STR)

# --- MODIFICATION PRINCIPALE ICI ---
# Mettre le tier FREE par défaut comme étant totalement permissif pour les tests
DEFAULT_LICENCE_TIERS: Dict[str, Dict[str, Any]] = {
    "FREE": {
        "rate_limit_requests": 10000,             # Très permissif
        "rate_limit_window_seconds": 3600,
        "allowed_capabilities": ["*"],            # Toutes les capacités
        "llm_access": True,                       # Accès LLM autorisé
        "allowed_llm_models": ["*"],              # Tous les modèles LLM
        "max_llm_tokens_per_request": 0,          # 0 = illimité
        "max_llm_tokens_per_day": 0               # 0 = illimité
    },
    "PRO": { # Garder PRO et ELITE pour la structure, même si FREE est utilisé
        "rate_limit_requests": 20000, "rate_limit_window_seconds": 3600,
        "allowed_capabilities": ["*"],
        "llm_access": True, "allowed_llm_models": ["*"],
        "max_llm_tokens_per_request": 0, "max_llm_tokens_per_day": 0
    },
    "ELITE": {
        "rate_limit_requests": 50000, "rate_limit_window_seconds": 3600,
        "allowed_capabilities": ["*"], "llm_access": True, "allowed_llm_models": ["*"],
        "max_llm_tokens_per_request": 0, "max_llm_tokens_per_day": 0
    }
}

LICENCE_TIERS: Dict[str, Dict[str, Any]] = DEFAULT_LICENCE_TIERS # Initialiser avec les défauts modifiés

# Logique de chargement du fichier YAML (on la garde, mais elle sera surchargée par les défauts si le fichier n'est pas bon)
# Pour les tests actuels, on s'assure que même si le chargement YAML échoue, FREE est permissif.
# Si vous voulez forcer l'utilisation des défauts ci-dessus pour le test, vous pouvez commenter tout le bloc try-except ci-dessous.
if LICENCE_TIERS_CONFIG_PATH.exists() and LICENCE_TIERS_CONFIG_PATH.is_file():
    try:
        with LICENCE_TIERS_CONFIG_PATH.open('r') as f:
            loaded_config = yaml.safe_load(f) # Renommé pour éviter confusion
            if isinstance(loaded_config, dict) and loaded_config.get("tiers") and isinstance(loaded_config["tiers"], dict):
                # Fusionner intelligemment : les valeurs du YAML écrasent les défauts
                # Si une clé existe dans YAML et dans DEFAULT, YAML gagne.
                # Si une clé existe seulement dans DEFAULT, elle est conservée.
                merged_tiers = {}
                for tier_name, default_conf in DEFAULT_LICENCE_TIERS.items():
                    merged_tiers[tier_name] = default_conf.copy() # Commencer avec une copie du défaut
                    if tier_name in loaded_config["tiers"]:
                        merged_tiers[tier_name].update(loaded_config["tiers"][tier_name]) # Mettre à jour avec les valeurs du YAML

                # Ajouter les tiers du YAML qui ne sont pas dans les défauts (moins probable)
                for tier_name, custom_conf in loaded_config["tiers"].items():
                    if tier_name not in merged_tiers:
                        merged_tiers[tier_name] = custom_conf
                
                LICENCE_TIERS = merged_tiers
                logging.info(f"Loaded and merged licence tiers from {LICENCE_TIERS_CONFIG_PATH} with defaults.")
            else:
                logging.warning(f"Invalid or empty 'tiers' structure in {LICENCE_TIERS_CONFIG_PATH}. Using permissive FREE default.")
                # Dans ce cas, LICENCE_TIERS reste le DEFAULT_LICENCE_TIERS permissif défini ci-dessus
    except Exception as e_tiers:
        logging.error(f"Error loading licence tiers from {LICENCE_TIERS_CONFIG_PATH}: {e_tiers}. Using permissive FREE default.", exc_info=True)
        # LICENCE_TIERS reste le DEFAULT_LICENCE_TIERS permissif
else:
    logging.info(f"Licence tiers config file not found at {LICENCE_TIERS_CONFIG_PATH}. Using permissive FREE default tiers.")



# --- Logging ---
LOG_LEVEL_STR: str = os.getenv("LLMBDO_LOG_LEVEL", "INFO").upper()
LOG_LEVEL_FALLBACK: int = logging.INFO
LOG_LEVEL: int = logging.getLevelName(LOG_LEVEL_STR)
if not isinstance(LOG_LEVEL, int):
    logging.warning(f"Invalid LLMBDO_LOG_LEVEL '{LOG_LEVEL_STR}'. Defaulting to INFO.")
    LOG_LEVEL = LOG_LEVEL_FALLBACK
    LOG_LEVEL_STR = logging.getLevelName(LOG_LEVEL_FALLBACK)

# Utiliser la configuration de logging simplifiée pour les tests
LOGGING_CONFIG: Dict[str, Any] = {
    "version": 1, "disable_existing_loggers": False,
    "formatters": {"simple": {"format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "simple", "stream": "ext://sys.stdout"}},
    "root": {"handlers": ["console"], "level": "DEBUG"}, # DEBUG pour voir tout
    "loggers": { 
        "uvicorn": {"level": "INFO", "handlers": ["console"], "propagate": False},
        "fastapi": {"level": "INFO", "handlers": ["console"], "propagate": False},
        "websockets": {"level": "WARNING", "handlers": ["console"], "propagate": False},
        "llmbasedos": {"level": "DEBUG", "handlers": ["console"], "propagate": False}, # Notre app en DEBUG
        "httpx": {"level": "WARNING", "handlers": ["console"], "propagate": False},
        "watchdog": {"level": "WARNING", "handlers": ["console"], "propagate": False},
    }
}

# JSON RPC Default Error Codes (inchangés)
JSONRPC_PARSE_ERROR: int = -32700
JSONRPC_INVALID_REQUEST: int = -32600
JSONRPC_METHOD_NOT_FOUND: int = -32601
JSONRPC_INVALID_PARAMS: int = -32602
JSONRPC_INTERNAL_ERROR: int = -32603
JSONRPC_AUTH_ERROR: int = -32000
JSONRPC_RATE_LIMIT_ERROR: int = -32001
JSONRPC_PERMISSION_DENIED_ERROR: int = -32002
JSONRPC_LLM_QUOTA_EXCEEDED_ERROR: int = -32003 
JSONRPC_LLM_MODEL_NOT_ALLOWED_ERROR: int = -32004