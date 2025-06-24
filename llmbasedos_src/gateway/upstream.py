# llmbasedos_src/gateway/upstream.py
import logging
import httpx 
import json
import re
import uuid 
from typing import Any, Dict, List, Optional, AsyncGenerator, Tuple, Union 
from datetime import datetime, timezone
import asyncio

from .config import AVAILABLE_LLM_MODELS, DEFAULT_LLM_PROVIDER, OPENAI_API_KEY, GEMINI_API_KEY
from .auth import LicenceDetails 
from llmbasedos.mcp_server_framework import create_mcp_response, create_mcp_error

logger = logging.getLogger("llmbasedos.gateway.upstream")

def _get_model_config(requested_model_alias: Optional[str]) -> Tuple[str, str, str, Optional[str]]:
    effective_alias = requested_model_alias
    if not effective_alias:
        for alias, config in AVAILABLE_LLM_MODELS.items():
            if config.get("is_default", False):
                effective_alias = alias
                break
        if not effective_alias and AVAILABLE_LLM_MODELS:
            effective_alias = list(AVAILABLE_LLM_MODELS.keys())[0]

    if not effective_alias:
        raise ValueError("No LLM models configured.")

    model_config = AVAILABLE_LLM_MODELS.get(str(effective_alias))
    if not model_config:
        raise ValueError(f"LLM model alias '{effective_alias}' not found.")

    provider, model_name, api_base = model_config.get("provider"), model_config.get("model_name"), model_config.get("api_base_url")
    if not all([provider, model_name, api_base]):
        raise ValueError(f"Incomplete configuration for model alias '{effective_alias}'.")

    api_key = None
    if provider == "openai":
        api_key = OPENAI_API_KEY
    elif provider == "gemini":
        api_key = GEMINI_API_KEY
    
    if not api_key:
        raise ValueError(f"API key for provider '{provider}' not found.")
        
    return provider, model_name, api_base.rstrip('/'), api_key

def _convert_openai_to_gemini(messages: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    contents = []
    for msg in messages:
        role = "model" if msg.get("role") == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": msg.get("content")}]})
    return contents

# Dans llmbasedos_src/gateway/upstream.py

# Dans llmbasedos_src/gateway/upstream.py

def _convert_gemini_to_openai(gemini_response: Dict[str, Any], model_name: str) -> Dict[str, Any]:
    """
    Converts a Gemini response to be compatible with OpenAI's structure.
    This version also cleans the response from markdown code blocks.
    """
    try:
        # Extraire le texte brut
        content_str = gemini_response["candidates"][0]["content"]["parts"][0]["text"]
        
        # CORRECTION : Nettoyer la chaîne de caractères des balises markdown
        cleaned_content = re.sub(r"```json\n?|```", "", content_str).strip()
        
        # On retourne la chaîne nettoyée
        return {
            "id": f"gemini-{uuid.uuid4().hex}",
            "object": "chat.completion",
            "created": int(datetime.now(timezone.utc).timestamp()),
            "model": model_name,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": cleaned_content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0} 
        }
    except (KeyError, IndexError):
        return {"error": gemini_response.get("error", {"message": "Unknown Gemini response format"})}

async def _stream_openai_events(original_request_id: str, **kwargs) -> AsyncGenerator[Dict, Any]:
    async with httpx.AsyncClient(timeout=kwargs.pop('timeout')) as client:
        async with client.stream(**kwargs) as response:
            if response.status_code >= 400:
                error_body = await response.aread()
                yield create_mcp_error(original_request_id, -32012, f"OpenAI API Error {response.status_code}", data=error_body.decode(errors='ignore'))
                return
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    line_data = line.removeprefix("data: ").strip()
                    if line_data == "[DONE]":
                        break
                    try:
                        chunk_data = json.loads(line_data)
                        yield create_mcp_response(original_request_id, result={"type": "llm_chunk", "content": chunk_data})
                    except json.JSONDecodeError: continue
    yield create_mcp_response(original_request_id, result={"type": "llm_stream_end"})

async def _stream_gemini_events(original_request_id: str, **kwargs) -> AsyncGenerator[Dict, Any]:
    timeout_config = kwargs.pop('timeout')
    async with httpx.AsyncClient(timeout=timeout_config) as client:
        async with client.stream(**kwargs) as response:
            if response.status_code >= 400:
                error_body = await response.aread()
                yield create_mcp_error(original_request_id, -32012, f"Gemini API Error {response.status_code}", data=error_body.decode(errors='ignore'))
                return
            
            full_response_text = await response.aread()
            try:
                data_list = json.loads(full_response_text.decode('utf-8'))
                for item in data_list:
                    text_content = item.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                    if text_content:
                        openai_like_chunk = {"choices": [{"delta": {"content": text_content}}]}
                        yield create_mcp_response(original_request_id, result={"type": "llm_chunk", "content": openai_like_chunk})
                        await asyncio.sleep(0.02)
            except Exception as e:
                 yield create_mcp_error(original_request_id, -32013, "Failed to parse Gemini stream", data=str(e))

    yield create_mcp_response(original_request_id, result={"type": "llm_stream_end"})

async def _create_error_generator(request_id: str, error_data: Dict) -> AsyncGenerator[Dict, Any]:
    yield create_mcp_error(request_id, error_data.get('code', -32000), error_data.get('message', 'Unknown error'))

async def call_llm_chat_completion(
    request_id: str,
    messages: List[Dict[str, str]],
    licence: LicenceDetails,
    requested_model_alias: Optional[str] = None,
    stream: bool = False, 
    **kwargs: Any 
) -> Union[Dict[str, Any], AsyncGenerator[Dict[str, Any], None]]:
    
    try:
        provider, model_name, api_base, api_key = _get_model_config(requested_model_alias)
        timeout_config = httpx.Timeout(connect=15.0, read=180.0, write=15.0, pool=10.0)

        if provider == "openai":
            endpoint = f"{api_base}/chat/completions"
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            payload = {"model": model_name, "messages": messages, "stream": stream, **kwargs}
            if stream:
                return _stream_openai_events(original_request_id=request_id, method="POST", url=endpoint, headers=headers, json=payload, timeout=timeout_config)
            else:
                async with httpx.AsyncClient(timeout=timeout_config) as client:
                    response = await client.post(endpoint, headers=headers, json=payload)
                    response.raise_for_status()
                    return response.json()

        elif provider == "gemini":
            action = "streamGenerateContent" if stream else "generateContent"
            endpoint = f"{api_base}/{model_name}:{action}?key={api_key}"
            headers = {"Content-Type": "application/json"}
            payload = {"contents": _convert_openai_to_gemini(messages)}
            
            if stream:
                return _stream_gemini_events(original_request_id=request_id, method="POST", url=endpoint, headers=headers, json=payload, timeout=timeout_config)
            
            async with httpx.AsyncClient(timeout=timeout_config) as client:
                response = await client.post(endpoint, headers=headers, json=payload)
                response.raise_for_status()
                logger.info(f"RAW GEMINI RESPONSE: {response.text}")
                return _convert_gemini_to_openai(response.json(), model_name)

        else:
            raise ValueError(f"Unsupported provider: {provider}")

    except Exception as e:
        logger.error(f"LLM Upstream failed: {e}", exc_info=True)
        error_payload = {"message": str(e), "code": -32010}
        if stream: return _create_error_generator(request_id, error_payload)
        return create_mcp_error(request_id, error_payload['code'], error_payload['message'])