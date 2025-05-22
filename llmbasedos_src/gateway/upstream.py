import logging
import httpx
import json
from typing import Any, Dict, List, Optional, AsyncGenerator, Tuple

# Importez UNIQUEMENT les constantes de config.py qui sont réellement nécessaires au niveau du module ici.
# Les clés spécifiques (OPENAI_API_KEY) et les URLs de base seront récupérées via AVAILABLE_LLM_MODELS.
from .config import (
    AVAILABLE_LLM_MODELS,   # La structure qui définit tous les modèles et leurs détails
    DEFAULT_LLM_PROVIDER,   # Pour résoudre un alias par défaut
    # OPENAI_API_KEY est maintenant supposé être DANS AVAILABLE_LLM_MODELS pour les modèles openai, ou une variable globale
    # que _get_model_config vérifie. Pour simplifier, je vais supposer que _get_model_config
    # récupère aussi la clé API si nécessaire à partir de AVAILABLE_LLM_MODELS ou d'une config globale.
    # Assurons-nous que OPENAI_API_KEY est disponible si un modèle openai est sélectionné.
    OPENAI_API_KEY # Nécessaire si la fonction _get_model_config ne la récupère pas d'ailleurs
)
from .auth import LicenceDetails

logger = logging.getLogger("llmbasedos.gateway.upstream")

class LLMError(Exception):
    def __init__(self, message, provider=None, model_name=None, status_code=None, details=None):
        super().__init__(message)
        self.provider = provider
        self.model_name = model_name
        self.status_code = status_code
        self.details = details
    def __str__(self):
        return f"LLMError (Provider: {self.provider}, Model: {self.model_name}, Status: {self.status_code}): {self.args[0]}"

def _get_model_config(requested_model_alias: Optional[str]) -> Tuple[str, str, str, Optional[str]]:
    """
    Resolves a model alias to (provider_type, provider_model_name, api_base_url, api_key_if_needed).
    API key is specifically for OpenAI in this example.
    Raises ValueError if alias not found or config incomplete.
    """
    # Construct a default alias if none is provided
    effective_alias = requested_model_alias
    if not effective_alias:
        # Find a model that is marked as default for the DEFAULT_LLM_PROVIDER
        for alias, config_data in AVAILABLE_LLM_MODELS.items():
            if config_data.get("provider") == DEFAULT_LLM_PROVIDER and config_data.get("is_default", False):
                effective_alias = alias
                break
        if not effective_alias: # Fallback if no specific default found for the provider
            raise ValueError(f"No default LLM model alias found for provider '{DEFAULT_LLM_PROVIDER}'.")
    
    model_config = AVAILABLE_LLM_MODELS.get(effective_alias)
    if not model_config:
        raise ValueError(f"LLM model alias '{effective_alias}' not found in AVAILABLE_LLM_MODELS.")

    provider_type = model_config.get("provider")
    provider_model_name = model_config.get("model_name")
    api_base_url = model_config.get("api_base_url")
    api_key = model_config.get("api_key") # Can be None

    if not all([provider_type, provider_model_name, api_base_url]):
        raise ValueError(f"Incomplete configuration for model alias '{effective_alias}': provider, model_name, and api_base_url are required.")

    if provider_type == "openai":
        # For OpenAI, an API key is essential. It can come from model_config or global OPENAI_API_KEY.
        # Prioritize key from model_config if present.
        key_to_use = api_key or OPENAI_API_KEY # OPENAI_API_KEY from .config import
        if not key_to_use:
            raise ValueError(f"OpenAI API key not configured for model alias '{effective_alias}' or globally.")
        api_key = key_to_use # Ensure api_key is set for return
    elif provider_type == "llama_cpp":
        # llama.cpp OpenAI-compatible endpoint usually doesn't require an API key.
        pass # api_key will be None if not set in config
    else:
        raise ValueError(f"Unsupported LLM provider type: {provider_type} for model alias '{effective_alias}'.")
    
    return provider_type, provider_model_name, api_base_url.rstrip('/'), api_key


async def call_llm_chat_completion(
    messages: List[Dict[str, str]],
    licence: LicenceDetails,
    requested_model_alias: Optional[str] = None,
    stream: bool = False,
    **kwargs: Any # temperature, max_tokens etc.
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    Proxies chat completion to configured LLM.
    Caller (dispatch.py) handles auth.py checks for model permission and quotas before calling this.
    This function focuses on the HTTP call and streaming.
    """
    try:
        provider_type, actual_model_name, api_base_url, provider_api_key = _get_model_config(requested_model_alias)
    except ValueError as e_conf:
        logger.error(f"LLM model config error for alias '{requested_model_alias}': {e_conf}")
        yield {"event": "error", "data": {"message": str(e_conf), "provider": "config", "model_name": requested_model_alias or "default"}}
        return

    endpoint = f"{api_base_url}/chat/completions" # Common for OpenAI-compatible APIs
    headers = {"Content-Type": "application/json"}
    if provider_type == "openai" and provider_api_key:
        headers["Authorization"] = f"Bearer {provider_api_key}"
    
    payload = {"model": actual_model_name, "messages": messages, "stream": stream, **kwargs}
    
    timeout_seconds = 300.0
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            logger.debug(f"LLM Req to {endpoint} (Model: {actual_model_name}, Stream: {stream}): {json.dumps(payload, indent=2)[:500]}...")
            async with client.stream("POST", endpoint, headers=headers, json=payload) as response:
                if response.status_code >= 400:
                    error_body = await response.aread()
                    error_text = error_body.decode(errors='ignore')
                    logger.error(f"LLM API error {response.status_code} from {provider_type}/{actual_model_name}: {error_text}")
                    yield {"event": "error", "data": {
                        "message": f"LLM API Error {response.status_code}",
                        "provider": provider_type, "model_name": actual_model_name,
                        "status_code": response.status_code, "details": error_text
                    }}
                    return

                if stream:
                    async for line in response.aiter_lines():
                        if line.startswith("data: "):
                            line_data = line.removeprefix("data: ").strip()
                            if line_data == "[DONE]":
                                yield {"event": "done", "data": {"provider": provider_type, "model_name": actual_model_name}}
                                break
                            try:
                                chunk = json.loads(line_data)
                                yield {"event": "chunk", "data": chunk}
                            except json.JSONDecodeError:
                                logger.warning(f"Failed to decode LLM stream chunk from {provider_type}: {line_data}")
                        elif line.strip():
                             logger.warning(f"Unexpected line in LLM stream from {provider_type}: {line}")
                else:
                    full_response_bytes = await response.aread()
                    try:
                        result = json.loads(full_response_bytes)
                        yield {"event": "chunk", "data": result}
                        yield {"event": "done", "data": {"provider": provider_type, "model_name": actual_model_name}}
                    except json.JSONDecodeError:
                        error_text = full_response_bytes.decode(errors='ignore')
                        logger.error(f"Failed to decode full LLM response from {provider_type}: {error_text}")
                        yield {"event": "error", "data": {
                            "message": "Invalid JSON response from LLM (non-stream)",
                            "provider": provider_type, "model_name": actual_model_name,
                            "details": error_text
                        }}
    except httpx.RequestError as e_req:
        logger.error(f"LLM request error to {provider_type}/{actual_model_name}: {e_req}", exc_info=True)
        yield {"event": "error", "data": {
            "message": f"LLM Request Error: {type(e_req).__name__}",
            "provider": provider_type, "model_name": actual_model_name, "details": str(e_req)
        }}
    except Exception as e_gen:
        logger.error(f"Unexpected error during LLM call to {provider_type}/{actual_model_name}: {e_gen}", exc_info=True)
        yield {"event": "error", "data": {
            "message": f"Unexpected error in LLM proxy: {type(e_gen).__name__}",
            "provider": provider_type, "model_name": actual_model_name, "details": str(e_gen)
        }}