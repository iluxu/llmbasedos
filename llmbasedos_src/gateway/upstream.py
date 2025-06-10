# llmbasedos_src/gateway/upstream.py
import logging
import httpx 
import json
import uuid 
from typing import Any, Dict, List, Optional, AsyncGenerator, Tuple, Union 

from .config import AVAILABLE_LLM_MODELS, DEFAULT_LLM_PROVIDER, OPENAI_API_KEY
from .auth import LicenceDetails 

logger = logging.getLogger("llmbasedos.gateway.upstream")

def _get_model_config(requested_model_alias: Optional[str]) -> Tuple[str, str, str, Optional[str]]:
    """
    Resolves a model alias to (provider_type, provider_model_name, api_base_url, api_key_if_needed).
    Raises ValueError if alias not found or config incomplete.
    """
    effective_alias = requested_model_alias
    if not effective_alias: 
        for alias, config_data in AVAILABLE_LLM_MODELS.items():
            if config_data.get("provider") == DEFAULT_LLM_PROVIDER and config_data.get("is_default", False):
                effective_alias = alias
                logger.info(f"LLM Upstream: No model alias requested, using default for provider '{DEFAULT_LLM_PROVIDER}': '{alias}'.")
                break
        if not effective_alias: 
            for alias, config_data in AVAILABLE_LLM_MODELS.items():
                if config_data.get("is_default", False):
                    effective_alias = alias
                    logger.info(f"LLM Upstream: No provider-specific default, using global default model: '{alias}'.")
                    break
            if not effective_alias and AVAILABLE_LLM_MODELS: 
                effective_alias = list(AVAILABLE_LLM_MODELS.keys())[0]
                logger.info(f"LLM Upstream: No default model found. Falling back to first available: '{effective_alias}'.")
            elif not AVAILABLE_LLM_MODELS:
                 raise ValueError("LLM Upstream: No LLM models configured in AVAILABLE_LLM_MODELS.")

    model_config = AVAILABLE_LLM_MODELS.get(str(effective_alias))
    if not model_config:
        raise ValueError(f"LLM model alias '{effective_alias}' not found in AVAILABLE_LLM_MODELS.")
    provider_type = model_config.get("provider")
    provider_model_name = model_config.get("model_name")
    api_base_url = model_config.get("api_base_url")
    api_key = model_config.get("api_key") 
    if not all([provider_type, provider_model_name, api_base_url]):
        raise ValueError(f"Incomplete configuration for model alias '{effective_alias}'. Missing provider, model_name, or api_base_url.")
    if provider_type == "openai":
        key_to_use = api_key or OPENAI_API_KEY 
        if not key_to_use:
            raise ValueError(f"OpenAI API key not configured for model alias '{effective_alias}'.")
        api_key = key_to_use 
    logger.debug(f"LLM Upstream: Resolved model alias '{requested_model_alias}' (effective: '{effective_alias}') to: "
                 f"Provider: {provider_type}, Model Name: {provider_model_name}, API Base: {api_base_url.rstrip('/')}")
    return provider_type, provider_model_name, api_base_url.rstrip('/'), api_key

async def _stream_llm_response_events(
    request_id_for_log: str, 
    timeout_config: httpx.Timeout, # Ne prend plus le client, mais sa config
    method: str, 
    url: str, 
    provider_type_for_log: str, 
    model_name_for_log: str,    
    **kwargs: Any # headers, json
) -> AsyncGenerator[Dict[str, Any], None]:
    """Helper pour gérer la logique de streaming SSE et yield des événements structurés."""
    try:
        # Le client est créé ICI, dans le scope du générateur
        async with httpx.AsyncClient(timeout=timeout_config) as client:
            async with client.stream(method, url, **kwargs) as response:
                if response.status_code >= 400:
                    error_body = await response.aread()
                    error_text = error_body.decode(errors='ignore')
                    logger.error(f"LLM Upstream Stream: API error {response.status_code} from {provider_type_for_log}/{model_name_for_log} (ReqID {request_id_for_log}): {error_text}")
                    yield {"event": "error", "data": {
                        "message": f"LLM API Error {response.status_code}", "provider": provider_type_for_log, 
                        "model_name": model_name_for_log, "status_code": response.status_code, "details": error_text
                    }}
                    return
                logger.info(f"LLM Upstream Stream: Connected to {url} for {model_name_for_log} (ReqID {request_id_for_log}). Streaming response...")
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        line_data = line.removeprefix("data: ").strip()
                        if line_data == "[DONE]": 
                            logger.debug(f"LLM Upstream Stream: Received [DONE] for {model_name_for_log} (ReqID {request_id_for_log}).")
                            yield {"event": "done", "data": {"provider": provider_type_for_log, "model_name": model_name_for_log}}
                            return 
                        try:
                            chunk_data = json.loads(line_data)
                            yield {"event": "chunk", "data": chunk_data}
                        except json.JSONDecodeError:
                            logger.warning(f"LLM Upstream Stream: Failed to decode JSON stream chunk from {provider_type_for_log} (ReqID {request_id_for_log}): '{line_data}'")
                    elif line.strip():
                         logger.warning(f"LLM Upstream Stream: Unexpected line from {provider_type_for_log} (ReqID {request_id_for_log}): '{line}'")
                logger.info(f"LLM Upstream Stream: Finished reading lines from {url} for {model_name_for_log} (ReqID {request_id_for_log}). Did not receive [DONE] explicitly.")
                yield {"event": "done", "data": {"provider": provider_type_for_log, "model_name": model_name_for_log, "note": "Stream ended by EOF, not [DONE]"}}
    except httpx.HTTPStatusError as e_http_status:
        logger.error(f"LLM Upstream Stream: HTTPStatusError for {provider_type_for_log}/{model_name_for_log} (ReqID {request_id_for_log}): {e_http_status.response.text}", exc_info=True)
        yield {"event": "error", "data": {"message": f"LLM HTTP Status Error {e_http_status.response.status_code}", "provider": provider_type_for_log, "model_name": model_name_for_log, "status_code": e_http_status.response.status_code, "details": e_http_status.response.text}}
    except Exception as e_stream_helper: 
        logger.error(f"LLM Upstream Stream: Unexpected error in _stream_llm_response_events for {provider_type_for_log}/{model_name_for_log} (ReqID {request_id_for_log}): {e_stream_helper}", exc_info=True)
        yield {"event": "error", "data": {"message": f"Streaming helper error: {type(e_stream_helper).__name__}", "provider": provider_type_for_log, "model_name": model_name_for_log, "details": str(e_stream_helper)}}

async def _create_error_generator(error_data_dict: Dict[str, Any]) -> AsyncGenerator[Dict[str, Any], None]:
    """Helper to yield a single error event and stop."""
    yield {"event": "error", "data": error_data_dict}
    return

async def call_llm_chat_completion(
    messages: List[Dict[str, str]],
    licence: LicenceDetails,
    requested_model_alias: Optional[str] = None,
    stream: bool = False, 
    **kwargs: Any 
) -> Union[Dict[str, Any], AsyncGenerator[Dict[str, Any], None]]:
    upstream_request_id = f"llm_up_{uuid.uuid4().hex[:8]}"
    logger.info(f"LLM Upstream (ReqID {upstream_request_id}): Call for alias '{requested_model_alias}', Client wants stream: {stream}.")

    provider_type: str = "unknown_provider" 
    actual_model_name: str = requested_model_alias or "unknown_model"
    error_payload_base = {"provider": provider_type, "model_name": actual_model_name}

    try:
        provider_type, actual_model_name, api_base_url, provider_api_key = _get_model_config(requested_model_alias)
        error_payload_base = {"provider": provider_type, "model_name": actual_model_name}
    except ValueError as e_conf:
        logger.error(f"LLM Upstream (ReqID {upstream_request_id}): Model config error: {e_conf}")
        error_data = {**error_payload_base, "error": True, "message": str(e_conf), "details": "Configuration error"}
        if stream: return _create_error_generator(error_data)
        else: return error_data

    endpoint = f"{api_base_url}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if provider_type == "openai" and provider_api_key:
        headers["Authorization"] = f"Bearer {provider_api_key}"
    
    payload_to_llm_api = {"model": actual_model_name, "messages": messages, "stream": stream, **kwargs}
    timeout_config = httpx.Timeout(connect=15.0, read=180.0, write=15.0, pool=10.0) 

    try:
        if stream: 
            logger.debug(f"LLM Upstream (ReqID {upstream_request_id}): Returning stream generator for {endpoint}...")
            return _stream_llm_response_events(
                request_id_for_log=upstream_request_id,
                timeout_config=timeout_config,
                method="POST", 
                url=endpoint, 
                provider_type_for_log=provider_type, 
                model_name_for_log=actual_model_name,
                headers=headers, 
                json=payload_to_llm_api
            )
        else: 
            async with httpx.AsyncClient(timeout=timeout_config) as client:
                logger.debug(f"LLM Upstream (ReqID {upstream_request_id}): Initiating NON-STREAMING POST to {endpoint}. Payload: {str(payload_to_llm_api)[:200]}...")
                response = await client.post(endpoint, headers=headers, json=payload_to_llm_api)
                response_text_for_log = "N/A"
                try:
                    response_text_for_log = response.text 
                    if response.status_code >= 400:
                        logger.error(f"LLM Upstream (ReqID {upstream_request_id}): API error {response.status_code} (non-stream): {response_text_for_log}")
                        return {**error_payload_base, "error": True, "status_code": response.status_code, "message": f"LLM API Error {response.status_code}", "details": response_text_for_log}
                    full_response_data = json.loads(response_text_for_log) 
                    logger.info(f"LLM Upstream (ReqID {upstream_request_id}): Rcvd non-streamed response. Preview: {str(full_response_data)[:200]}...")
                    return full_response_data
                except json.JSONDecodeError as e_json_non_stream:
                    logger.error(f"LLM Upstream (ReqID {upstream_request_id}): Failed to decode JSON (non-stream): {e_json_non_stream}. Text: {response_text_for_log[:200]}")
                    return {**error_payload_base, "error": True, "message": "Invalid JSON response from LLM (non-stream)", "details": response_text_for_log[:500]}

    except httpx.TimeoutException as e_timeout:
        logger.error(f"LLM Upstream (ReqID {upstream_request_id}): Timeout: {e_timeout}", exc_info=True)
        error_data = {**error_payload_base, "error": True, "message": f"LLM Request Timeout: {type(e_timeout).__name__}", "details": str(e_timeout)}
        if stream: return _create_error_generator(error_data)
        else: return error_data
    except httpx.RequestError as e_req:
        logger.error(f"LLM Upstream (ReqID {upstream_request_id}): Request error: {e_req}", exc_info=True)
        error_data = {**error_payload_base, "error": True, "message": f"LLM Request Error: {type(e_req).__name__}", "details": str(e_req)}
        if stream: return _create_error_generator(error_data)
        else: return error_data
    except Exception as e_gen: 
        logger.error(f"LLM Upstream (ReqID {upstream_request_id}): Unexpected error: {e_gen}", exc_info=True)
        error_data = {**error_payload_base, "error": True, "message": f"Unexpected error in LLM proxy: {type(e_gen).__name__}", "details": str(e_gen)}
        if stream: return _create_error_generator(error_data)
        else: return error_data