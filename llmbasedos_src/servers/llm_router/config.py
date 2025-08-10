# Fichier: llmbasedos_src/servers/llm_router/config.py
import os
from typing import Optional

# --- LLM Provider Settings ---
OPENAI_API_KEY: Optional[str] = os.getenv("OPENAI_API_KEY")
GEMINI_API_KEY: Optional[str] = os.getenv("GEMINI_API_KEY")
OLLAMA_BASE_URL: str = os.getenv("OLLAMA_BASE_URL", "http://ollama:11434")
DEFAULT_MODEL: str = os.getenv("LOCAL_LLM", "gemma:2b")
LLM_PROVIDER_BACKEND: str = os.getenv("LLM_PROVIDER_BACKEND", "ollama").lower()

# --- Cache Settings ---
REDIS_HOST: str = os.getenv("REDIS_HOST", "redis")
REDIS_PASSWORD: Optional[str] = os.getenv("REDIS_PASSWORD")
CACHE_EXPIRATION_SECONDS: int = 3600 * 24