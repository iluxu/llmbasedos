# llmbasedos/gateway/upstream.py
import logging
import httpx
import json
from typing import Any, Dict, List, Optional, AsyncGenerator, Tuple # Added Tuple

from .config import (
    OPENAI_API_KEY, OPENAI_API_BASE_URL, AVAILABLE_LLM_MODELS,
    LLAMA_CPP_API_BASE_URL, DEFAULT_LLM_PROVIDER
)
from .auth import LicenceDetails # For model tiering & usage recording

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

def _get_model_config(requested_model_alias: Optional[str]) -> Tuple[str, str, str]:
    """
    Resolves a model alias to (provider_type, provider_model_name, api_base_url).
    Raises ValueError if alias not found or config incomplete.
    """
    model_config = AVAILABLE_LLM_MODELS.get(requested_model_alias or f"default_{DEFAULT_LLM_PROVIDER}")
    if not model_config:
        raise ValueError(f"LLM model alias '{requested_model_alias}' not found in AVAILABLE_LLM_MODELS.")

    provider_type = model_config["provider"]
    provider_model_name = model_config["model_name"]
    
    api_base_url = ""
    if provider_type == "openai":
        if not OPENAI_API_KEY: raise ValueError("OpenAI API key not configured.")
        api_base_url = OPENAI_API_BASE_URL
    elif provider_type == "llama_cpp":
        if not LLAMA_CPP_API_BASE_URL: raise ValueError("Llama.cpp API base URL not configured.")
        api_base_url = LLAMA_CPP_API_BASE_URL # e.g. http://host:port/v1
    else:
        raise ValueError(f"Unsupported LLM provider type: {provider_type} for model '{requested_model_alias}'.")
    
    return provider_type, provider_model_name, api_base_url.rstrip('/')


async def call_llm_chat_completion(
    messages: List[Dict[str, str]],
    licence: LicenceDetails, # Used for permission checks by caller (auth.py)
    requested_model_alias: Optional[str] = None,
    stream: bool = False,
    **kwargs: Any # temperature, max_tokens etc.
) -> AsyncGenerator[Dict[str, Any], None]: # Yields MCP-like stream events
    """
    Proxies chat completion to configured LLM.
    Caller (dispatch.py) handles auth.py checks for model permission and quotas before calling this.
    This function focuses on the HTTP call and streaming.
    """
    try:
        provider_type, actual_model_name, api_base_url = _get_model_config(requested_model_alias)
    except ValueError as e_conf: # Configuration error for the model
        logger.error(f"LLM model config error for alias '{requested_model_alias}': {e_conf}")
        # Yield a single error event for the stream
        yield {"event": "error", "data": {"message": str(e_conf), "provider": "config", "model_name": requested_model_alias}}
        return

    endpoint = f"{api_base_url}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if provider_type == "openai":
        headers["Authorization"] = f"Bearer {OPENAI_API_KEY}"
    # llama.cpp OpenAI-compatible endpoint usually doesn't require auth header

    payload = {"model": actual_model_name, "messages": messages, "stream": stream, **kwargs}
    
    # Estimate input tokens (very roughly) for potential recording by caller
    # A proper tokenizer (tiktoken for OpenAI) would be needed for accuracy.
    # input_tokens_estimate = sum(len(m.get("content","")) for m in messages) // 3 # Very rough
    # This function should not deal with token counting, caller (dispatch) should.

    timeout_seconds = 300.0 # Generous timeout for LLM responses
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            logger.debug(f"LLM Req to {endpoint} (Model: {actual_model_name}, Stream: {stream}): {json.dumps(payload)[:200]}...")
            async with client.stream("POST", endpoint, headers=headers, json=payload) as response:
                if response.status_code >= 400: # HTTP error from LLM provider
                    error_body = await response.aread()
                    logger.error(f"LLM API error {response.status_code} from {provider_type}/{actual_model_name}: {error_body.decode(errors='ignore')}")
                    yield {"event": "error", "data": {
                        "message": f"LLM API Error {response.status_code}",
                        "provider": provider_type, "model_name": actual_model_name,
                        "status_code": response.status_code, "details": error_body.decode(errors='ignore')
                    }}
                    return # Stop generation

                # Stream response handling (OpenAI SSE format assumed for both)
                if stream:
                    async for line in response.aiter_lines():
                        if line.startswith("data: "):
                            line_data = line.removeprefix("data: ").strip()
                            if line_data == "[DONE]":
                                yield {"event": "done", "data": {"provider": provider_type, "model_name": actual_model_name}}
                                break
                            try:
                                chunk = json.loads(line_data)
                                yield {"event": "chunk", "data": chunk} # Forward raw provider chunk
                            except json.JSONDecodeError:
                                logger.warning(f"Failed to decode LLM stream chunk from {provider_type}: {line_data}")
                        elif line.strip(): # Non-empty, non-data line (should not happen often)
                             logger.warning(f"Unexpected line in LLM stream from {provider_type}: {line}")
                else: # Non-streamed response
                    full_response_bytes = await response.aread()
                    try:
                        result = json.loads(full_response_bytes)
                        yield {"event": "chunk", "data": result} # Single chunk with full result
                        yield {"event": "done", "data": {"provider": provider_type, "model_name": actual_model_name}}
                    except json.JSONDecodeError:
                        logger.error(f"Failed to decode full LLM response from {provider_type}: {full_response_bytes.decode(errors='ignore')}")
                        yield {"event": "error", "data": {
                            "message": "Invalid JSON response from LLM (non-stream)",
                            "provider": provider_type, "model_name": actual_model_name,
                            "details": full_response_bytes.decode(errors='ignore')
                        }}
    except httpx.RequestError as e_req: # Network errors, timeouts before response started
        logger.error(f"LLM request error to {provider_type}/{actual_model_name}: {e_req}", exc_info=True)
        yield {"event": "error", "data": {
            "message": f"LLM Request Error: {type(e_req).__name__}",
            "provider": provider_type, "model_name": actual_model_name, "details": str(e_req)
        }}
    except Exception as e_gen: # Other unexpected errors
        logger.error(f"Unexpected error during LLM call to {provider_type}/{actual_model_name}: {e_gen}", exc_info=True)
        yield {"event": "error", "data": {
            "message": f"Unexpected error in LLM proxy: {type(e_gen).__name__}",
            "provider": provider_type, "model_name": actual_model_name, "details": str(e_gen)
        }}
