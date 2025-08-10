import asyncio
import os
import json
import hashlib
from pathlib import Path
import redis
import httpx
import time # <-- AJOUTEZ CET IMPORT
# from sentence_transformers import SentenceTransformer  # Not used in local mode yet
# import chromadb  # Not used in local mode yet

from llmbasedos_src.mcp_server_framework import MCPServer
from .config import (
    OLLAMA_BASE_URL, DEFAULT_MODEL, REDIS_HOST, REDIS_PASSWORD,
    CACHE_EXPIRATION_SECONDS, LLM_PROVIDER_BACKEND, GEMINI_API_KEY
)


# --- Configuration ---
SERVER_NAME = "llm_router"
CAPS_FILE_PATH = str(Path(__file__).parent / "caps.json")
llm_router_server = MCPServer(SERVER_NAME, CAPS_FILE_PATH)

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


# --- Gemini Provider ---
class GeminiProvider:
    def __init__(self, server: MCPServer):
        self.server = server
        self.logger = server.logger
        self.api_key = os.getenv("GEMINI_API_KEY")
        if not self.api_key:
            raise ValueError("GEMINI_API_KEY environment variable not set.")
        self.base_url = "https://generativelanguage.googleapis.com/v1beta/models"

    def _to_gemini_format(self, messages: list) -> list:
        # Gemini a un format de message légèrement différent
        gemini_contents = []
        for msg in messages:
            # Gemini n'aime pas le rôle "system", on le transforme en "user"
            role = "user" if msg["role"] in ["user", "system"] else "model"
            gemini_contents.append({"role": role, "parts": [{"text": msg["content"]}]})
        return gemini_contents

    def _from_gemini_format(self, gemini_response: dict, model: str) -> dict:
        # On retraduit la réponse Gemini en format OpenAI pour la cohérence
        text = gemini_response.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
        usage = gemini_response.get("usageMetadata", {})
        return {
            "id": f"chatcmpl-gemini-{int(time.time())}",
            "object": "chat.completion",
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": usage.get("promptTokenCount", 0),
                "completion_tokens": usage.get("candidatesTokenCount", 0),
                "total_tokens": usage.get("totalTokenCount", 0),
            }
        }

    async def execute_call(self, messages: list, options: dict):
        model = options.get("model", "gemini-2.5-pro-preview-06-05")
        api_url = f"{self.base_url}/{model}:generateContent?key={self.api_key}"
        
        payload = {
            "contents": self._to_gemini_format(messages),
            "generationConfig": {
                "temperature": options.get("temperature", 0.7),
                "maxOutputTokens": options.get("max_tokens", 2048)
            }
        }
        
        self.logger.info(f"Calling Gemini at {self.base_url}/{model}...")
        
        async with httpx.AsyncClient(timeout=300.0) as client:
            try:
                response = await client.post(api_url, json=payload)
                response.raise_for_status()
                return self._from_gemini_format(response.json(), model)
            except httpx.HTTPStatusError as e:
                error_text = e.response.text
                self.logger.error(f"Gemini API error: {e.response.status_code} - {error_text}")
                raise RuntimeError(f"Gemini API Error: {e.response.status_code} - {error_text}")
            except Exception as e:
                self.logger.error(f"Error calling Gemini: {e}", exc_info=True)
                raise RuntimeError(f"Network or other error calling Gemini: {str(e)}")



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
            "temperature": options.get("temperature", 0.7)
            # Vous pouvez ajouter d'autres options OpenAI ici si besoin
            # "max_tokens": options.get("max_tokens"),
        }

        # Si vous voulez aussi que le LLM retourne du JSON, il faut ajouter le paramètre "format"
        # C'est important pour votre Arc seo_affiliate qui attend du JSON !
        if options.get("response_format") == "json":
            payload["format"] = "json"
        
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


# --- Choose Provider (via Environment Variable) ---
LLM_PROVIDER_BACKEND = os.getenv("LLM_PROVIDER_BACKEND", "ollama").lower()

intelligence = CacheManager(llm_router_server)
provider = None
if LLM_PROVIDER_BACKEND == "gemini":
    try:
        provider = GeminiProvider(llm_router_server)
        llm_router_server.logger.info("✅ LLM Router backend set to: Gemini")
    except ValueError as e:
        llm_router_server.logger.error(f"Could not initialize Gemini provider: {e}. Falling back to Ollama.")
        provider = OllamaProvider(llm_router_server)
else:
    provider = OllamaProvider(llm_router_server)
    llm_router_server.logger.info(f"✅ LLM Router backend set to: {LLM_PROVIDER_BACKEND} (Ollama)")


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


