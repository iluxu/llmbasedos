# llmbasedos/gateway/__init__.py
import logging
import logging.config
import os

from .config import LOGGING_CONFIG, LOG_LEVEL_STR # Supprimer LOG_FORMAT

# Centralized logging configuration for the gateway module
# This should be called once when the gateway starts.
# main.py will call setup_logging().

def setup_gateway_logging():
    log_level_int = logging.getLevelName(LOG_LEVEL_STR)
    
    formatter_class = "python_json_logger.jsonlogger.JsonFormatter" if LOG_FORMAT == "json" else "logging.Formatter"
    formatter_config = {
        "format": "%(asctime)s %(levelname)s %(name)s %(module)s %(funcName)s %(lineno)d %(message)s"
    } if LOG_FORMAT == "json" else {
        "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    }

    LOGGING_CONFIG = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            LOG_FORMAT: {"()": formatter_class, **formatter_config}
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": LOG_FORMAT,
                "stream": "ext://sys.stdout" # Or sys.stderr
            }
        },
        "root": { # Catch-all for other libraries if not configured
            "handlers": ["console"],
            "level": "WARNING",
        },
        "loggers": {
            "llmbasedos.gateway": {"handlers": ["console"], "level": log_level_int, "propagate": False},
            "uvicorn": {"handlers": ["console"], "level": "INFO", "propagate": False},
            "uvicorn.error": {"handlers": ["console"], "level": "INFO", "propagate": False},
            "uvicorn.access": {"handlers": ["console"], "level": "INFO", "propagate": False}, # Access logs
            "fastapi": {"handlers": ["console"], "level": "INFO", "propagate": False},
            "websockets": {"handlers": ["console"], "level": "INFO", "propagate": False}, # For client part
        }
    }
    logging.config.dictConfig(LOGGING_CONFIG)
    logger = logging.getLogger("llmbasedos.gateway")
    logger.info(f"llmbasedos.gateway package initialized. Log level: {LOG_LEVEL_STR}")

# setup_gateway_logging() # Call from main.py on startup instead of module import time
