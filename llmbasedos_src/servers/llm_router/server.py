import asyncio
import os
import json
import hashlib
from pathlib import Path
import redis
import httpx
# from sentence_transformers import SentenceTransformer  # Not used in local mode yet
# import chromadb  # Not used in local mode yet

from llmbasedos_src.mcp_server_framework import MCPServer

# --- Configuration ---
SERVER_NAME = "llm_router"
CAPS_FILE_PATH = str(Path(__file__).parent / "caps.json")
llm_router_server = MCPServer(SERVER_NAME, CAPS_FILE_PATH)

# Env Vars
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://ollama:11434")
DEFAULT_MODEL = os.getenv("LOCAL_LLM", "gemma:2b")
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD")
CACHE_EXPIRATION_SECONDS = 3600 * 24

# --- Cache Manager ---
class CacheManager:
    def __init__(self, server: MCPServer):
        self.server = server
        self.logger = server.logger
        try:
            self.redis_client = redis.Redis(
                host=REDIS_HOST,
                port=6379,
                db=0,
                password=REDIS_PASSWORD,
                decode_responses=True
            )
            self.redis_client.ping()
            self.logger.info("✅ Connected to Redis")
        except Exception as e:
            self.logger.error(f"Redis connection failed: {e}")
            self.redis_client = None

    def get_cached_response(self, key: str):
        try:
            if self.redis_client:
                data = self.redis_client.get(key)
                if data:
                    self.logger.info(f"Cache HIT: {key}")
                    return json.loads(data)
        except Exception as e:
            self.logger.error(f"Redis GET error: {e}")

        self.logger.info(f"Cache MISS: {key}")
        return None

    def set_cache(self, key: str, value: dict):
        try:
            if self.redis_client:
                self.redis_client.set(key, json.dumps(value), ex=CACHE_EXPIRATION_SECONDS)
                self.logger.info(f"Cache SET: {key}")
        except Exception as e:
            self.logger.error(f"Redis SET error: {e}")


# --- Ollama Provider (local mode) ---
class OllamaProvider:
    def __init__(self, server: MCPServer):
        self.server = server
        self.logger = server.logger
        self.base_url = f"{OLLAMA_BASE_URL}/v1/chat/completions"  # URL inside Docker network
        self.default_model = "gemma:2b"

    # Dans la classe OllamaProvider de llm_router/server.py

    async def execute_call(self, messages: list, options: dict):
        model = options.get("model", DEFAULT_MODEL)
        
        # Le payload pour /v1/chat/completions est au format OpenAI
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": options.get("temperature", 0.7),
            }
        }
        
        self.logger.info(f"Calling Ollama at {self.base_url} with model: {model}")
        
        async with httpx.AsyncClient(timeout=300.0) as client:
            try:
                # On envoie directement le payload compatible OpenAI
                response = await client.post(self.base_url, json=payload)
                response.raise_for_status()
                
                # Pas besoin de traduire la réponse, elle est déjà au format OpenAI !
                return response.json()
                
            except httpx.HTTPStatusError as e:
                error_text = e.response.text
                self.logger.error(f"Ollama API error: {e.response.status_code} - {error_text}")
                if "model not found" in error_text:
                    raise RuntimeError(f"Ollama error: Model '{model}' not found. Please run 'docker exec llmbasedos_ollama ollama pull {model}'")
                raise RuntimeError(f"Ollama API Error: {e.response.status_code} - {error_text}")
            except Exception as e:
                self.logger.error(f"Error calling Ollama: {e}", exc_info=True)
                raise RuntimeError(f"Network or other error calling Ollama: {str(e)}")


# --- Choose Provider ---
USE_LOCAL_OLLAMA = True
intelligence = CacheManager(llm_router_server)
provider = OllamaProvider(llm_router_server) if USE_LOCAL_OLLAMA else None


# --- Handler ---
@llm_router_server.register_method("mcp.llm.route")
async def handle_route_request(server: MCPServer, request_id, params: list):
    if not provider:
        raise RuntimeError("No LLM provider available.")

    request_data = params[0]
    messages = request_data.get("messages", [])
    options = request_data.get("options", {})

    if not messages:
        raise ValueError("'messages' must not be empty")

    # Cache key
    cache_key_content = f"{options.get('model','auto')}:{json.dumps(messages)}"
    cache_key = f"llmcache:{hashlib.md5(cache_key_content.encode()).hexdigest()}"

    cached = intelligence.get_cached_response(cache_key)
    if cached:
        return cached

    # Call and cache
    result = await provider.execute_call(messages, options)
    intelligence.set_cache(cache_key, result)
    return result


# --- Entrypoint ---
if __name__ == "__main__":
    dev_mode = os.getenv("DEV_MODE", "false").lower() == "true"

    if dev_mode:
        from fastapi import FastAPI
        import uvicorn

        app = FastAPI()

        @app.post("/mcp.llm.route")
        async def http_route(payload: dict):
            result = await handle_route_request(
                llm_router_server,
                payload.get("id"),
                payload.get("params", [])
            )
            return {"jsonrpc": "2.0", "id": payload.get("id"), "result": result}

        uvicorn.run(app, host="0.0.0.0", port=8000)

    else:
        asyncio.run(llm_router_server.start())


