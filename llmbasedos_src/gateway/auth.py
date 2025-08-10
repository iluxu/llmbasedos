# llmbasedos_src/gateway/auth.py
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, List
import hashlib
import yaml 

from fastapi import WebSocket # Utilisé pour l'annotation de type et isinstance
from pydantic import BaseModel, Field, field_validator

from .config import (
    LICENCE_FILE_PATH, LICENCE_TIERS_CONFIG_PATH, DEFAULT_LICENCE_TIERS,
    JSONRPC_AUTH_ERROR, JSONRPC_RATE_LIMIT_ERROR, JSONRPC_PERMISSION_DENIED_ERROR,
    JSONRPC_LLM_QUOTA_EXCEEDED_ERROR, JSONRPC_LLM_MODEL_NOT_ALLOWED_ERROR
)

logger = logging.getLogger("llmbasedos.gateway.auth")

CLIENT_USAGE_RECORDS: Dict[str, Dict[str, Any]] = {}
LOADED_LICENCE_TIERS: Dict[str, Dict[str, Any]] = {}

class LicenceDetails(BaseModel):
    tier: str = "FREE"
    key_id: Optional[str] = None
    user_identifier: Optional[str] = None
    expires_at: Optional[datetime] = None
    is_valid: bool = False
    raw_content: Optional[str] = None
    rate_limit_requests: int = 0
    rate_limit_window_seconds: int = 3600
    allowed_capabilities: List[str] = Field(default_factory=list)
    llm_access: bool = False
    allowed_llm_models: List[str] = Field(default_factory=list)
    max_llm_tokens_per_request: int = 0
    max_llm_tokens_per_day: int = 0

    def __init__(self, **data: Any):
        super().__init__(**data)
        self._apply_tier_settings()

    def _apply_tier_settings(self):
        global LOADED_LICENCE_TIERS
        if not LOADED_LICENCE_TIERS:
            _load_licence_tiers_config()
        tier_config = LOADED_LICENCE_TIERS.get(self.tier, LOADED_LICENCE_TIERS.get("FREE", DEFAULT_LICENCE_TIERS.get("FREE", {})))
        self.rate_limit_requests = tier_config.get("rate_limit_requests", 0)
        self.rate_limit_window_seconds = tier_config.get("rate_limit_window_seconds", 3600)
        self.allowed_capabilities = tier_config.get("allowed_capabilities", [])
        self.llm_access = tier_config.get("llm_access", False)
        self.allowed_llm_models = tier_config.get("allowed_llm_models", [])
        self.max_llm_tokens_per_request = tier_config.get("max_llm_tokens_per_request", 0)
        self.max_llm_tokens_per_day = tier_config.get("max_llm_tokens_per_day", 0)

    @field_validator('expires_at', mode='before')
    @classmethod
    def ensure_timezone_aware(cls, v):
        if isinstance(v, datetime) and v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v

_CACHED_LICENCE: Optional[LicenceDetails] = None
_LICENCE_FILE_MTIME: Optional[float] = None
_LICENCE_TIERS_FILE_MTIME: Optional[float] = None

def _load_licence_tiers_config():
    global LOADED_LICENCE_TIERS, _LICENCE_TIERS_FILE_MTIME
    current_mtime = None
    config_exists = False
    if LICENCE_TIERS_CONFIG_PATH.exists():
        try:
            current_mtime = LICENCE_TIERS_CONFIG_PATH.stat().st_mtime
            config_exists = True
        except FileNotFoundError: pass
    if LOADED_LICENCE_TIERS and current_mtime == _LICENCE_TIERS_FILE_MTIME and config_exists:
        return
    logger.info("Loading/Re-loading licence tier definitions...")
    default_free_tier = {"rate_limit_requests": 1000, "rate_limit_window_seconds": 3600, "allowed_capabilities": ["mcp.hello"], "llm_access": False, "allowed_llm_models": [], "max_llm_tokens_per_request": 0, "max_llm_tokens_per_day": 0}
    LOADED_LICENCE_TIERS = {k: v.copy() for k, v in DEFAULT_LICENCE_TIERS.items()}
    if "FREE" not in LOADED_LICENCE_TIERS: LOADED_LICENCE_TIERS["FREE"] = default_free_tier.copy()

    if config_exists:
        try:
            with LICENCE_TIERS_CONFIG_PATH.open('r', encoding='utf-8') as f:
                custom_tiers = yaml.safe_load(f)
            if isinstance(custom_tiers, dict):
                for tier_name, tier_conf in custom_tiers.items():
                    if tier_name in LOADED_LICENCE_TIERS and isinstance(LOADED_LICENCE_TIERS[tier_name], dict) and isinstance(tier_conf, dict):
                        LOADED_LICENCE_TIERS[tier_name].update(tier_conf)
                    else: LOADED_LICENCE_TIERS[tier_name] = tier_conf
                logger.info(f"Successfully loaded and merged custom tiers from {LICENCE_TIERS_CONFIG_PATH}")
            else: logger.warning(f"Custom tiers config {LICENCE_TIERS_CONFIG_PATH} not a valid YAML dict. Using defaults.")
        except Exception as e: logger.error(f"Error loading/parsing {LICENCE_TIERS_CONFIG_PATH}: {e}. Using defaults/previous.", exc_info=True)
    else: logger.info(f"Licence tiers config {LICENCE_TIERS_CONFIG_PATH} not found. Using default tiers.")
    _LICENCE_TIERS_FILE_MTIME = current_mtime

def _parse_licence_key_content(content: str) -> LicenceDetails:
    try:
        key_data = yaml.safe_load(content)
        if not isinstance(key_data, dict): raise ValueError("Licence key content is not a valid YAML/JSON dictionary.")
        tier = str(key_data.get("tier", "FREE")).upper()
        user_id = str(key_data.get("user_id", "anonymous_licence_user"))
        expiry_str = key_data.get("expires_at")
        key_id_hash = hashlib.sha256(content.strip().encode('utf-8')).hexdigest()[:16]

        expires_at_dt: Optional[datetime] = None
        if expiry_str:
            try:
                expires_at_dt = datetime.fromisoformat(str(expiry_str).replace("Z", "+00:00"))
                if expires_at_dt.tzinfo is None: expires_at_dt = expires_at_dt.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) > expires_at_dt:
                    logger.warning(f"Licence key for {user_id} (KeyID: {key_id_hash}) expired on {expiry_str}.")
                    return LicenceDetails(tier="FREE", key_id=key_id_hash, user_identifier=user_id, expires_at=expires_at_dt, is_valid=False, raw_content=content)
            except ValueError: logger.error(f"Invalid expiry date format '{expiry_str}' in licence. Ignoring expiry.")
        
        _load_licence_tiers_config()
        if tier not in LOADED_LICENCE_TIERS:
            logger.warning(f"Unknown licence tier '{tier}' for KeyID {key_id_hash}. Defaulting to FREE.")
            return LicenceDetails(tier="FREE", key_id=key_id_hash, user_identifier=user_id, expires_at=expires_at_dt, is_valid=(tier=="FREE"), raw_content=content)

        logger.info(f"Licence parsed: Tier '{tier}', User '{user_id}', KeyID '{key_id_hash}', Expires '{expiry_str or 'N/A'}'")
        return LicenceDetails(tier=tier, key_id=key_id_hash, user_identifier=user_id, expires_at=expires_at_dt, is_valid=True, raw_content=content)
    except Exception as e:
        logger.error(f"Error parsing licence key content: {e}. Defaulting to FREE tier.", exc_info=True)
        return LicenceDetails(tier="FREE", is_valid=True, raw_content=content if isinstance(content,str) else str(content))

def get_licence_details() -> LicenceDetails:
    global _CACHED_LICENCE, _LICENCE_FILE_MTIME
    _load_licence_tiers_config() 
    current_key_mtime = None
    try:
        if LICENCE_FILE_PATH.exists(): current_key_mtime = LICENCE_FILE_PATH.stat().st_mtime
    except FileNotFoundError: pass

    if _CACHED_LICENCE and current_key_mtime == _LICENCE_FILE_MTIME and _CACHED_LICENCE.tier in LOADED_LICENCE_TIERS:
        _CACHED_LICENCE._apply_tier_settings() 
        return _CACHED_LICENCE

    if not LICENCE_FILE_PATH.exists():
        logger.info(f"Licence file not found at {LICENCE_FILE_PATH}. Using default FREE tier.")
        _CACHED_LICENCE = LicenceDetails(tier="FREE", is_valid=True)
        _LICENCE_FILE_MTIME = None
        return _CACHED_LICENCE
    try:
        logger.info(f"Loading/Re-loading licence key from {LICENCE_FILE_PATH}")
        content = LICENCE_FILE_PATH.read_text(encoding='utf-8')
        _CACHED_LICENCE = _parse_licence_key_content(content)
        _LICENCE_FILE_MTIME = current_key_mtime
        return _CACHED_LICENCE
    except Exception as e:
        logger.error(f"Failed to load/parse licence key {LICENCE_FILE_PATH}: {e}. Using default FREE tier.", exc_info=True)
        _CACHED_LICENCE = LicenceDetails(tier="FREE", is_valid=True)
        _LICENCE_FILE_MTIME = current_key_mtime
        return _CACHED_LICENCE

def _get_client_identifier_for_quota(licence: LicenceDetails, client_id_for_free_tier_check: str) -> str:
    if licence.is_valid and licence.key_id:
        return licence.key_id
    if not client_id_for_free_tier_check:
        logger.warning("_get_client_identifier_for_quota: client_id_for_free_tier_check was empty, using generic fallback.")
        return "free_tier_generic_unknown_source"
    return client_id_for_free_tier_check

def check_rate_limit(licence: LicenceDetails, client_id_for_free_tier_check: str) -> Tuple[bool, Optional[str], Optional[int]]:
    if licence.tier == "ELITE" and licence.rate_limit_requests == 0:
        return True, None, None

    client_id = _get_client_identifier_for_quota(licence, client_id_for_free_tier_check)
    now_utc = datetime.now(timezone.utc)
    
    if client_id not in CLIENT_USAGE_RECORDS: 
        CLIENT_USAGE_RECORDS[client_id] = {"requests": [], "llm_tokens": {}}
    
    window_start = now_utc - timedelta(seconds=licence.rate_limit_window_seconds)
    CLIENT_USAGE_RECORDS[client_id]["requests"] = [ts for ts in CLIENT_USAGE_RECORDS[client_id]["requests"] if ts > window_start]

    if len(CLIENT_USAGE_RECORDS[client_id]["requests"]) < licence.rate_limit_requests:
        CLIENT_USAGE_RECORDS[client_id]["requests"].append(now_utc)
        return True, None, None
    else:
        next_allowed_ts = CLIENT_USAGE_RECORDS[client_id]["requests"][0] + timedelta(seconds=licence.rate_limit_window_seconds)
        wait_seconds = max(0, int((next_allowed_ts - now_utc).total_seconds()))
        msg = (f"Rate limit exceeded for tier '{licence.tier}' (Client ID: '{client_id}'). "
               f"Limit: {licence.rate_limit_requests} reqs / {licence.rate_limit_window_seconds // 60} mins. "
               f"Try again in {wait_seconds}s.")
        logger.warning(msg)
        return False, msg, JSONRPC_RATE_LIMIT_ERROR

def check_llm_token_quotas(licence: LicenceDetails, requested_tokens: int, client_id_for_free_tier_check: str) -> Tuple[bool, Optional[str], Optional[int]]:
    if not licence.llm_access:
        return False, f"LLM access denied for tier '{licence.tier}'.", JSONRPC_PERMISSION_DENIED_ERROR
    if licence.max_llm_tokens_per_request == 0 and licence.max_llm_tokens_per_day == 0:
        return True, None, None

    if licence.max_llm_tokens_per_request > 0 and requested_tokens > licence.max_llm_tokens_per_request:
        msg = f"Requested tokens ({requested_tokens}) exceed per-request limit ({licence.max_llm_tokens_per_request}) for tier '{licence.tier}'."
        return False, msg, JSONRPC_LLM_QUOTA_EXCEEDED_ERROR

    if licence.max_llm_tokens_per_day > 0:
        client_id = _get_client_identifier_for_quota(licence, client_id_for_free_tier_check)
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if client_id not in CLIENT_USAGE_RECORDS: CLIENT_USAGE_RECORDS[client_id] = {"requests": [], "llm_tokens": {}}
        if "llm_tokens" not in CLIENT_USAGE_RECORDS[client_id]: CLIENT_USAGE_RECORDS[client_id]["llm_tokens"] = {}
        
        current_daily_usage = CLIENT_USAGE_RECORDS[client_id]["llm_tokens"].get(today_str, 0)
        if current_daily_usage + requested_tokens > licence.max_llm_tokens_per_day:
            msg = (f"Requested tokens ({requested_tokens}) for client '{client_id}' would exceed daily LLM limit ({licence.max_llm_tokens_per_day}). "
                   f"Used today: {current_daily_usage}. Tier: '{licence.tier}'.")
            return False, msg, JSONRPC_LLM_QUOTA_EXCEEDED_ERROR
    return True, None, None

def record_llm_token_usage(licence: LicenceDetails, client_connection_object: Any, tokens_used: int, client_id_for_quota_tracking: str):
    if not licence.llm_access or tokens_used <= 0: return
    if licence.max_llm_tokens_per_day == 0 : return 

    client_id = licence.key_id if licence.is_valid and licence.key_id else client_id_for_quota_tracking
    
    if not client_id: 
        logger.error("record_llm_token_usage: client_id is empty, cannot record usage.")
        return

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if client_id not in CLIENT_USAGE_RECORDS: 
        CLIENT_USAGE_RECORDS[client_id] = {"requests": [], "llm_tokens": {}}
    if "llm_tokens" not in CLIENT_USAGE_RECORDS[client_id]: 
        CLIENT_USAGE_RECORDS[client_id]["llm_tokens"] = {}
    
    current_date = datetime.now(timezone.utc).date()
    for date_str_key in list(CLIENT_USAGE_RECORDS[client_id]["llm_tokens"].keys()):
        try:
            record_date = datetime.strptime(date_str_key, "%Y-%m-%d").date()
            if (current_date - record_date).days > 7: 
                del CLIENT_USAGE_RECORDS[client_id]["llm_tokens"][date_str_key]
        except ValueError: 
            logger.warning(f"Invalid date key '{date_str_key}' in LLM token usage for '{client_id}'. Removing.")
            if date_str_key in CLIENT_USAGE_RECORDS[client_id]["llm_tokens"]:
                 del CLIENT_USAGE_RECORDS[client_id]["llm_tokens"][date_str_key]

    CLIENT_USAGE_RECORDS[client_id]["llm_tokens"][today_str] = \
        CLIENT_USAGE_RECORDS[client_id]["llm_tokens"].get(today_str, 0) + tokens_used
    logger.debug(
        f"LLM Usage: Client '{client_id}' recorded {tokens_used} tokens. Daily total for {today_str}: "
        f"{CLIENT_USAGE_RECORDS[client_id]['llm_tokens'][today_str]} / {licence.max_llm_tokens_per_day or 'unlimited'}."
    )

def check_permission(licence: LicenceDetails, capability_method: str, llm_model_requested: Optional[str] = None) -> Tuple[bool, Optional[str], Optional[int]]:
    logger.info(f"CHECK_PERM: Method: '{capability_method}', Tier: {licence.tier}, AllowedCaps: {licence.allowed_capabilities}")
    allowed_caps = licence.allowed_capabilities
    cap_allowed = False
    if "*" in allowed_caps: cap_allowed = True
    else:
        for pattern in allowed_caps:
            if pattern.endswith(".*") and capability_method.startswith(pattern[:-1]): cap_allowed = True; break
            elif capability_method == pattern: cap_allowed = True; break
    
    if not cap_allowed:
        msg = f"Permission denied for method '{capability_method}' with tier '{licence.tier}'."
        logger.warning(msg + f" (Licence KeyID: {licence.key_id or 'N/A'})")
        return False, msg, JSONRPC_PERMISSION_DENIED_ERROR

    if capability_method == "mcp.llm.chat":
        if not licence.llm_access:
            msg = f"LLM access is disabled for tier '{licence.tier}'."
            return False, msg, JSONRPC_PERMISSION_DENIED_ERROR
        if llm_model_requested: 
            if "*" not in licence.allowed_llm_models and llm_model_requested not in licence.allowed_llm_models:
                msg = f"LLM model '{llm_model_requested}' not allowed for tier '{licence.tier}'. Allowed: {licence.allowed_llm_models}"
                return False, msg, JSONRPC_LLM_MODEL_NOT_ALLOWED_ERROR
    return True, None, None

def authenticate_and_authorize_request(
    client_connection_obj: Any, 
    method_name: str, 
    llm_model_requested: Optional[str] = None, 
    llm_tokens_to_request: int = 0
) -> Tuple[Optional[LicenceDetails], Optional[Dict[str, Any]]]:
    
    # ====================================================================
    # == DÉBUT DE LA CORRECTION                                         ==
    # ====================================================================
    client_log_identifier: str
    client_id_for_free_tier_rate_limit: str 

    # 1. Est-ce un vrai WebSocket de FastAPI ?
    if isinstance(client_connection_obj, WebSocket):
        client_log_identifier = f"WebSocket {client_connection_obj.client.host}:{client_connection_obj.client.port}"
        client_id_for_free_tier_rate_limit = f"ip:{client_connection_obj.client.host}"
    
    # 2. Est-ce notre Mock pour une requête HTTP ? (On lui a donné un attribut unique)
    elif hasattr(client_connection_obj, 'is_http_mock'):
        client_log_identifier = f"HTTP client {client_connection_obj.client.host}:{client_connection_obj.client.port}"
        client_id_for_free_tier_rate_limit = f"ip:{client_connection_obj.client.host}"

    # 3. Est-ce notre Mock pour un client UNIX ?
    elif hasattr(client_connection_obj, 'peername_str'):
        client_log_identifier = f"UNIX client {client_connection_obj.peername_str}"
        client_id_for_free_tier_rate_limit = f"unix:{client_connection_obj.peername_str}"
    
    # 4. Fallback si on ne reconnaît pas
    else:
        client_log_identifier = "Unknown client type"
        client_id_for_free_tier_rate_limit = "unknown_client_source_for_ratelimit"
        logger.warning(f"AUTH: Could not determine client type for reliable rate limiting ID: {client_connection_obj}")
    # ====================================================================
    # == FIN DE LA CORRECTION                                           ==
    # ====================================================================

    logger.info(f"AUTH: Method '{method_name}' requested by client: {client_log_identifier}")
    
    licence = get_licence_details() 

    if not licence.is_valid and licence.tier != "FREE":
        logger.warning(f"AUTH: Invalid or expired non-FREE licence key used by {client_log_identifier}. KeyID: {licence.key_id}, Tier in key: {licence.raw_content.split(':')[0] if licence.raw_content else 'N/A'}")
        return licence, {"code": JSONRPC_AUTH_ERROR, "message": "Invalid or expired licence key."}

    allowed, msg, err_code = check_rate_limit(licence, client_id_for_free_tier_rate_limit)
    if not allowed:
        return licence, {"code": err_code, "message": msg} 
    
    allowed, msg, err_code = check_permission(licence, method_name, llm_model_requested)
    if not allowed:
        return licence, {"code": err_code, "message": msg}

    if method_name == "mcp.llm.chat" and llm_tokens_to_request > 0 : 
        allowed, msg, err_code = check_llm_token_quotas(licence, llm_tokens_to_request, client_id_for_free_tier_rate_limit)
        if not allowed:
            return licence, {"code": err_code, "message": msg}
    
    logger.debug(f"AUTH: Request authorized for method '{method_name}' by {client_log_identifier} (Tier: {licence.tier})")
    return licence, None

def get_licence_info_for_mcp_call(client_connection_obj: Any) -> Dict[str, Any]:
    licence = get_licence_details() 
    
    client_id_for_free_tier_display: str
    if isinstance(client_connection_obj, WebSocket):
        client_id_for_free_tier_display = f"ip:{client_connection_obj.client.host}"
    elif hasattr(client_connection_obj, 'is_http_mock'):
        client_id_for_free_tier_display = f"ip:{client_connection_obj.client.host}"
    elif hasattr(client_connection_obj, 'peername_str'):
        client_id_for_free_tier_display = f"unix:{client_connection_obj.peername_str}"
    else:
        client_id_for_free_tier_display = "unknown_client_source_for_display"

    client_id_for_records = _get_client_identifier_for_quota(licence, client_id_for_free_tier_display)
    
    requests_remaining_str = "N/A (unlimited or no limit)"
    if licence.rate_limit_requests > 0 :
        now_utc = datetime.now(timezone.utc)
        window_start = now_utc - timedelta(seconds=licence.rate_limit_window_seconds)
        if client_id_for_records not in CLIENT_USAGE_RECORDS: CLIENT_USAGE_RECORDS[client_id_for_records] = {"requests": [], "llm_tokens": {}}
        client_reqs = CLIENT_USAGE_RECORDS[client_id_for_records].get("requests", [])
        valid_reqs_in_window = [ts for ts in client_reqs if ts > window_start]
        requests_remaining_val = max(0, licence.rate_limit_requests - len(valid_reqs_in_window))
        requests_remaining_str = str(requests_remaining_val)

    llm_tokens_today_remaining_str = "N/A (LLM access disabled or unlimited)"
    if licence.llm_access and licence.max_llm_tokens_per_day > 0:
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if client_id_for_records not in CLIENT_USAGE_RECORDS: CLIENT_USAGE_RECORDS[client_id_for_records] = {"requests": [], "llm_tokens": {}}
        used_today = CLIENT_USAGE_RECORDS[client_id_for_records].get("llm_tokens", {}).get(today_str, 0)
        llm_tokens_today_remaining_val = max(0, licence.max_llm_tokens_per_day - used_today)
        llm_tokens_today_remaining_str = str(llm_tokens_today_remaining_val)
    elif licence.llm_access and licence.max_llm_tokens_per_day == 0:
        llm_tokens_today_remaining_str = "unlimited"

    return {
        "tier": licence.tier,
        "key_id": licence.key_id,
        "user_identifier": licence.user_identifier,
        "is_valid": licence.is_valid,
        "expires_at": licence.expires_at.isoformat() if licence.expires_at else None,
        "effective_permissions": licence.allowed_capabilities,
        "quotas": {
            "api_requests_limit_human": f"{licence.rate_limit_requests}/{licence.rate_limit_window_seconds // 60}min" if licence.rate_limit_requests > 0 else "unlimited",
            "api_requests_remaining_in_window": requests_remaining_str,
            "llm_access": licence.llm_access,
            "allowed_llm_models": licence.allowed_llm_models,
            "max_llm_tokens_per_request": licence.max_llm_tokens_per_request if licence.max_llm_tokens_per_request > 0 else "unlimited",
            "max_llm_tokens_per_day_limit": licence.max_llm_tokens_per_day if licence.max_llm_tokens_per_day > 0 else "unlimited",
            "llm_tokens_today_remaining": llm_tokens_today_remaining_str,
        },
        "note": f"Remaining quotas based on client identifier: '{client_id_for_records}'."
    }