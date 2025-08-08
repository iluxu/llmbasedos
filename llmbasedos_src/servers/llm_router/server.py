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
        self.base_url = "http://ollama:11434/api/chat"  # URL inside Docker network
        self.default_model = "gemma:2b"

    async def execute_call(self, messages: list, options: dict):
        model = options.get("model", self.default_model)
        payload = {"model": model, "messages": messages, "stream": False}
        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                r = await client.post(self.base_url, json=payload)
                r.raise_for_status()
                return r.json()
        except Exception as e:
            self.logger.error(f"Ollama call failed: {e}", exc_info=True)
            return {"error": f"Ollama call failed: {str(e)}", "model": model}


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


