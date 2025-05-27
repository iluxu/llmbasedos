# llmbasedos/gateway/auth.py
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, List
import hashlib
import yaml # For loading licence tiers config

from fastapi import WebSocket # For client context (IP for rate limiting)
from pydantic import BaseModel, Field, field_validator

from .config import (
    LICENCE_FILE_PATH, LICENCE_TIERS_CONFIG_PATH, DEFAULT_LICENCE_TIERS,
    JSONRPC_AUTH_ERROR, JSONRPC_RATE_LIMIT_ERROR, JSONRPC_PERMISSION_DENIED_ERROR,
    JSONRPC_LLM_QUOTA_EXCEEDED_ERROR, JSONRPC_LLM_MODEL_NOT_ALLOWED_ERROR
)

logger = logging.getLogger("llmbasedos.gateway.auth")

# In-memory store for rate limiting & LLM token usage.
# Key: client_identifier (e.g., licence_key_hash or remote_addr for FREE tier)
# Value structure:
# {
#   "requests": [timestamp1, timestamp2, ...],
#   "llm_tokens": {"YYYY-MM-DD": daily_token_count_for_this_client}
# }
CLIENT_USAGE_RECORDS: Dict[str, Dict[str, Any]] = {}
LOADED_LICENCE_TIERS: Dict[str, Dict[str, Any]] = {}


class LicenceDetails(BaseModel):
    tier: str = "FREE"
    key_id: Optional[str] = None
    user_identifier: Optional[str] = None
    expires_at: Optional[datetime] = None
    is_valid: bool = False
    raw_content: Optional[str] = None # For auditing

    # Tier-specific settings, populated by _apply_tier_settings
    rate_limit_requests: int = 0
    rate_limit_window_seconds: int = 3600
    allowed_capabilities: List[str] = Field(default_factory=list)
    llm_access: bool = False
    allowed_llm_models: List[str] = Field(default_factory=list) # Can contain "*"
    max_llm_tokens_per_request: int = 0
    max_llm_tokens_per_day: int = 0

    def __init__(self, **data: Any):
        super().__init__(**data)
        self._apply_tier_settings()

    def _apply_tier_settings(self):
        global LOADED_LICENCE_TIERS
        if not LOADED_LICENCE_TIERS: # Ensure tiers are loaded
            _load_licence_tiers_config()
        
        # Fallback to FREE tier config if current tier is unknown or invalid after parsing
        tier_config = LOADED_LICENCE_TIERS.get(self.tier, LOADED_LICENCE_TIERS.get("FREE", DEFAULT_LICENCE_TIERS["FREE"]))
        
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
            return v.replace(tzinfo=timezone.utc) # Assume UTC if naive
        return v


_CACHED_LICENCE: Optional[LicenceDetails] = None
_LICENCE_FILE_MTIME: Optional[float] = None
_LICENCE_TIERS_FILE_MTIME: Optional[float] = None

def _load_licence_tiers_config():
    """Loads licence tier definitions from YAML, merging with defaults."""
    global LOADED_LICENCE_TIERS, _LICENCE_TIERS_FILE_MTIME
    
    current_mtime = None
    config_exists = False
    if LICENCE_TIERS_CONFIG_PATH.exists():
        try:
            current_mtime = LICENCE_TIERS_CONFIG_PATH.stat().st_mtime
            config_exists = True
        except FileNotFoundError: # Should not happen if exists() is true, but defensive
            pass

    if LOADED_LICENCE_TIERS and current_mtime == _LICENCE_TIERS_FILE_MTIME:
        return # Already loaded and up-to-date

    logger.info("Loading/Re-loading licence tier definitions...")
    LOADED_LICENCE_TIERS = DEFAULT_LICENCE_TIERS.copy() # Start with defaults

    if config_exists:
        try:
            with LICENCE_TIERS_CONFIG_PATH.open('r') as f:
                custom_tiers = yaml.safe_load(f)
            if isinstance(custom_tiers, dict):
                # Deep merge custom tiers into defaults (simple dict update here, can be more granular)
                for tier_name, tier_conf in custom_tiers.items():
                    if tier_name in LOADED_LICENCE_TIERS and isinstance(LOADED_LICENCE_TIERS[tier_name], dict) and isinstance(tier_conf, dict):
                        LOADED_LICENCE_TIERS[tier_name].update(tier_conf)
                    else:
                        LOADED_LICENCE_TIERS[tier_name] = tier_conf
                logger.info(f"Successfully loaded and merged custom tiers from {LICENCE_TIERS_CONFIG_PATH}")
            else:
                logger.warning(f"Custom tiers config file {LICENCE_TIERS_CONFIG_PATH} is not a valid YAML dictionary. Using defaults.")
        except yaml.YAMLError as e:
            logger.error(f"Error parsing licence tiers YAML {LICENCE_TIERS_CONFIG_PATH}: {e}. Using defaults/previous.")
        except Exception as e:
            logger.error(f"Error loading licence tiers file {LICENCE_TIERS_CONFIG_PATH}: {e}. Using defaults/previous.")
    else:
        logger.info(f"Licence tiers config file {LICENCE_TIERS_CONFIG_PATH} not found. Using default tiers.")
    
    _LICENCE_TIERS_FILE_MTIME = current_mtime


def _parse_licence_key_content(content: str) -> LicenceDetails:
    """Parses the raw licence key string. Robust parsing and verification needed for prod."""
    try:
        # Example format: TIER:USER_ID:EXPIRY_YYYY-MM-DD (or from a signed JWT, etc.)
        # For now, assume simple YAML or JSON content in the key file for flexibility
        key_data = yaml.safe_load(content) # Allows more structured key files
        if not isinstance(key_data, dict):
            raise ValueError("Licence key content is not a valid YAML/JSON dictionary.")

        tier = str(key_data.get("tier", "FREE")).upper()
        user_id = str(key_data.get("user_id", "anonymous"))
        expiry_str = key_data.get("expires_at") # Expects ISO format string or None
        key_id_hash = hashlib.sha256(content.encode()).hexdigest()[:16]

        expires_at_dt: Optional[datetime] = None
        if expiry_str:
            try:
                expires_at_dt = datetime.fromisoformat(expiry_str.replace("Z", "+00:00"))
                if expires_at_dt.tzinfo is None: # Ensure tz-aware
                    expires_at_dt = expires_at_dt.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) > expires_at_dt:
                    logger.warning(f"Licence key for {user_id} (ID {key_id_hash}) expired on {expiry_str}.")
                    return LicenceDetails(tier="FREE", key_id=key_id_hash, user_identifier=user_id, expires_at=expires_at_dt, is_valid=False, raw_content=content)
            except ValueError:
                logger.error(f"Invalid expiry date format '{expiry_str}' in licence. Ignoring expiry.")
        
        _load_licence_tiers_config() # Ensure tiers are fresh before checking tier existence
        if tier not in LOADED_LICENCE_TIERS:
            logger.warning(f"Unknown licence tier '{tier}'. Defaulting to FREE.")
            return LicenceDetails(tier="FREE", key_id=key_id_hash, user_identifier=user_id, expires_at=expires_at_dt, is_valid=(tier=="FREE"), raw_content=content)

        logger.info(f"Licence parsed: Tier '{tier}', User '{user_id}', KeyID '{key_id_hash}', Expires '{expiry_str or 'N/A'}'")
        return LicenceDetails(tier=tier, key_id=key_id_hash, user_identifier=user_id, expires_at=expires_at_dt, is_valid=True, raw_content=content)
    
    except Exception as e:
        logger.error(f"Error parsing licence key content: {e}. Defaulting to FREE tier.", exc_info=True)
        return LicenceDetails(tier="FREE", is_valid=True, raw_content=content if isinstance(content,str) else str(content))


def get_licence_details() -> LicenceDetails:
    """Loads and caches licence details, reloading if file or tiers config modified."""
    global _CACHED_LICENCE, _LICENCE_FILE_MTIME, _LICENCE_TIERS_FILE_MTIME

    # Force reload if tiers config changed, as it affects LicenceDetails population
    _load_licence_tiers_config() 

    current_key_mtime = None
    try:
        if LICENCE_FILE_PATH.exists(): current_key_mtime = LICENCE_FILE_PATH.stat().st_mtime
    except FileNotFoundError: pass # Handled below

    if _CACHED_LICENCE and current_key_mtime == _LICENCE_FILE_MTIME: # Key file unchanged
        # Tier settings might have changed, re-apply them if _CACHED_LICENCE exists
        _CACHED_LICENCE._apply_tier_settings() # Ensures it picks up latest tier definitions
        return _CACHED_LICENCE

    if not LICENCE_FILE_PATH.exists():
        logger.warning(f"Licence file not found at {LICENCE_FILE_PATH}. Using FREE tier.")
        _CACHED_LICENCE = LicenceDetails(tier="FREE", is_valid=True)
        _LICENCE_FILE_MTIME = None
        return _CACHED_LICENCE

    try:
        logger.info(f"Loading licence key from {LICENCE_FILE_PATH}")
        content = LICENCE_FILE_PATH.read_text()
        _CACHED_LICENCE = _parse_licence_key_content(content)
        _LICENCE_FILE_MTIME = current_key_mtime
        return _CACHED_LICENCE
    except Exception as e:
        logger.error(f"Failed to load/parse licence key {LICENCE_FILE_PATH}: {e}. Using FREE tier.", exc_info=True)
        _CACHED_LICENCE = LicenceDetails(tier="FREE", is_valid=True)
        _LICENCE_FILE_MTIME = current_key_mtime
        return _CACHED_LICENCE


def check_rate_limit(licence: LicenceDetails, client_websocket: WebSocket) -> Tuple[bool, Optional[str], Optional[int]]:
    """Checks API request rate limits. Returns (allowed, error_message, error_code)."""
    # Elite might still have very high limits, or this check can be skipped.
    if licence.tier == "ELITE" and licence.rate_limit_requests == 0: # 0 means unlimited for ELITE
        return True, None, None

    client_id = licence.key_id if licence.is_valid and licence.key_id else f"ip:{client_websocket.client.host}"
    now_utc = datetime.now(timezone.utc)
    
    if client_id not in CLIENT_USAGE_RECORDS: CLIENT_USAGE_RECORDS[client_id] = {"requests": [], "llm_tokens": {}}
    
    # Prune old request timestamps
    window_start = now_utc - timedelta(seconds=licence.rate_limit_window_seconds)
    CLIENT_USAGE_RECORDS[client_id]["requests"] = [ts for ts in CLIENT_USAGE_RECORDS[client_id]["requests"] if ts > window_start]

    if len(CLIENT_USAGE_RECORDS[client_id]["requests"]) < licence.rate_limit_requests:
        CLIENT_USAGE_RECORDS[client_id]["requests"].append(now_utc)
        return True, None, None
    else:
        # Oldest request in window + window duration gives next allowed time
        next_allowed_ts = CLIENT_USAGE_RECORDS[client_id]["requests"][0] + timedelta(seconds=licence.rate_limit_window_seconds)
        wait_seconds = max(0, int((next_allowed_ts - now_utc).total_seconds()))
        msg = (f"Rate limit exceeded for tier {licence.tier}. "
               f"Limit: {licence.rate_limit_requests} reqs / {licence.rate_limit_window_seconds // 60} mins. "
               f"Try again in {wait_seconds}s.")
        logger.warning(f"Client {client_id}: {msg}")
        return False, msg, JSONRPC_RATE_LIMIT_ERROR

def check_llm_token_quotas(licence: LicenceDetails, client_websocket: WebSocket, requested_tokens: int) -> Tuple[bool, Optional[str], Optional[int]]:
    """Checks LLM token quotas. Returns (allowed, error_message, error_code)."""
    if not licence.llm_access: # Should be caught by permission check already
        return False, "LLM access denied for this tier.", JSONRPC_PERMISSION_DENIED_ERROR

    if licence.max_llm_tokens_per_request == 0 and licence.max_llm_tokens_per_day == 0 : # 0 means unlimited for this tier for LLM
        return True, None, None

    # Per-request limit
    if licence.max_llm_tokens_per_request > 0 and requested_tokens > licence.max_llm_tokens_per_request:
        msg = f"Requested tokens ({requested_tokens}) exceed per-request limit ({licence.max_llm_tokens_per_request}) for tier {licence.tier}."
        return False, msg, JSONRPC_LLM_QUOTA_EXCEEDED_ERROR

    # Per-day limit
    if licence.max_llm_tokens_per_day > 0:
        client_id = licence.key_id if licence.is_valid and licence.key_id else f"ip:{client_websocket.client.host}" # Consistent client ID
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        if client_id not in CLIENT_USAGE_RECORDS: CLIENT_USAGE_RECORDS[client_id] = {"requests": [], "llm_tokens": {}}
        if "llm_tokens" not in CLIENT_USAGE_RECORDS[client_id]: CLIENT_USAGE_RECORDS[client_id]["llm_tokens"] = {}

        current_daily_usage = CLIENT_USAGE_RECORDS[client_id]["llm_tokens"].get(today_str, 0)
        if current_daily_usage + requested_tokens > licence.max_llm_tokens_per_day:
            msg = (f"Requested tokens ({requested_tokens}) would exceed daily limit ({licence.max_llm_tokens_per_day}). "
                   f"Used today: {current_daily_usage}. Tier: {licence.tier}.")
            return False, msg, JSONRPC_LLM_QUOTA_EXCEEDED_ERROR
    
    return True, None, None # Allowed

def record_llm_token_usage(licence: LicenceDetails, client_websocket: WebSocket, tokens_used: int):
    """Records LLM token usage after a successful request."""
    if not licence.llm_access or tokens_used == 0: return
    if licence.max_llm_tokens_per_day == 0 : return # No daily limit to track against for this tier

    client_id = licence.key_id if licence.is_valid and licence.key_id else f"ip:{client_websocket.client.host}"
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if client_id not in CLIENT_USAGE_RECORDS: CLIENT_USAGE_RECORDS[client_id] = {"requests": [], "llm_tokens": {}}
    if "llm_tokens" not in CLIENT_USAGE_RECORDS[client_id]: CLIENT_USAGE_RECORDS[client_id]["llm_tokens"] = {}
    
    # Clean up old daily token records (e.g., older than a few days) to prevent memory leak
    # This could be done in a separate periodic task. For now, simple prune on write.
    current_date = datetime.now(timezone.utc).date()
    for date_str in list(CLIENT_USAGE_RECORDS[client_id]["llm_tokens"].keys()):
        record_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        if (current_date - record_date).days > 7: # Keep 7 days of history
            del CLIENT_USAGE_RECORDS[client_id]["llm_tokens"][date_str]

    CLIENT_USAGE_RECORDS[client_id]["llm_tokens"][today_str] = CLIENT_USAGE_RECORDS[client_id]["llm_tokens"].get(today_str, 0) + tokens_used
    logger.debug(f"Client {client_id} used {tokens_used} LLM tokens. Daily total for {today_str}: {CLIENT_USAGE_RECORDS[client_id]['llm_tokens'][today_str]}")


def check_permission(licence: LicenceDetails, capability_method: str, llm_model_requested: Optional[str] = None) -> Tuple[bool, Optional[str], Optional[int]]:
    """Checks capability permission and LLM model permission if applicable."""
    # Capability permission
    auth_logger.info(f"CHECK_PERM: Received capability_method: '{capability_method}' for tier {licence.tier}") # <<< AJOUTEZ CE LOG
    allowed_caps = licence.allowed_capabilities
    cap_allowed = False
    if "*" in allowed_caps: cap_allowed = True
    else:
        for pattern in allowed_caps:
            if pattern.endswith(".*") and capability_method.startswith(pattern[:-1]): cap_allowed = True; break
            elif capability_method == pattern: cap_allowed = True; break
    
    if not cap_allowed:
        msg = f"Permission denied for method '{capability_method}' with tier '{licence.tier}'."
        logger.warning(msg + f" (Client ID: {licence.key_id or 'anonymous_ip'})")
        return False, msg, JSONRPC_PERMISSION_DENIED_ERROR

    # LLM specific checks if method is mcp.llm.chat
    if capability_method == "mcp.llm.chat":
        if not licence.llm_access: # General LLM access for tier
            msg = f"LLM access is disabled for tier '{licence.tier}'."
            return False, msg, JSONRPC_PERMISSION_DENIED_ERROR
        
        if llm_model_requested: # If a specific model was requested by client
            if "*" not in licence.allowed_llm_models and llm_model_requested not in licence.allowed_llm_models:
                # Could also check against AVAILABLE_LLM_MODELS for existence.
                # For now, just tier permission.
                msg = f"LLM model '{llm_model_requested}' not allowed for tier '{licence.tier}'. Allowed: {licence.allowed_llm_models}"
                return False, msg, JSONRPC_LLM_MODEL_NOT_ALLOWED_ERROR
    
    return True, None, None # All checks passed

auth_logger = logging.getLogger("llmbasedos.gateway.auth")
def authenticate_and_authorize_request(
    websocket: WebSocket, method_name: str, llm_model_requested: Optional[str] = None, llm_tokens_to_request: int = 0
) -> Tuple[Optional[LicenceDetails], Optional[Dict[str, Any]]]:
    """
    Full auth pipeline for a request.
    Returns (LicenceDetails_if_ok, None) or (None_or_LicenceDetails_for_context, error_dict).
    """
    auth_logger.info(f"AUTH: Received method to check: '{method_name}' for client {websocket.client if websocket else 'unix_client'}") 
    licence = get_licence_details() # Gets current (possibly cached) licence

    # 1. Basic validity (already handled by get_licence_details defaulting to FREE)
    # If licence.is_valid is False but tier is not FREE, it means an issue like expiry.
    # The tier settings on 'licence' would reflect FREE if it was downgraded.

    # 2. Rate Limiting for API calls
    allowed, msg, err_code = check_rate_limit(licence, websocket)
    if not allowed:
        return licence, {"code": err_code, "message": msg} # Return licence for context if needed

    # 3. Permission check for capability and specific LLM model
    allowed, msg, err_code = check_permission(licence, method_name, llm_model_requested)
    if not allowed:
        return licence, {"code": err_code, "message": msg}

    # 4. LLM Token Quotas (only if method is mcp.llm.chat and tokens are requested)
    if method_name == "mcp.llm.chat" and llm_tokens_to_request > 0 : # llm_tokens_to_request could be an estimate
        # Note: `llm_tokens_to_request` for `mcp.llm.chat` is tricky as actual output tokens are unknown.
        # This check is more for services that declare input token cost, or for max_per_request on prompt.
        # For chat, we might only check max_per_day *before* the call, and per_request on prompt length.
        # The actual usage is recorded *after* the call.
        # Let's assume llm_tokens_to_request here is a pre-estimate of prompt tokens for per-request check.
        # For daily quota, we check *before* call if *any* tokens are being requested.
        
        allowed, msg, err_code = check_llm_token_quotas(licence, websocket, llm_tokens_to_request)
        if not allowed:
            return licence, {"code": err_code, "message": msg}

    return licence, None # All checks passed

def get_licence_info_for_mcp_call(client_websocket: WebSocket) -> Dict[str, Any]:
    """Prepares data for mcp.licence.check endpoint."""
    licence = get_licence_details() # Current effective licence
    
    # For displaying "remaining" quota, we need client_id
    client_id = licence.key_id if licence.is_valid and licence.key_id else f"ip:{client_websocket.client.host}"
    
    # API Rate Limit remaining
    requests_remaining = "N/A"
    if licence.rate_limit_requests > 0 : # If there is a limit
        now_utc = datetime.now(timezone.utc)
        window_start = now_utc - timedelta(seconds=licence.rate_limit_window_seconds)
        client_reqs = CLIENT_USAGE_RECORDS.get(client_id, {}).get("requests", [])
        valid_reqs_in_window = [ts for ts in client_reqs if ts > window_start]
        requests_remaining = max(0, licence.rate_limit_requests - len(valid_reqs_in_window))

    # LLM Tokens per day remaining
    llm_tokens_today_remaining = "N/A"
    if licence.llm_access and licence.max_llm_tokens_per_day > 0:
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        used_today = CLIENT_USAGE_RECORDS.get(client_id, {}).get("llm_tokens", {}).get(today_str, 0)
        llm_tokens_today_remaining = max(0, licence.max_llm_tokens_per_day - used_today)

    return {
        "tier": licence.tier,
        "key_id": licence.key_id,
        "user_identifier": licence.user_identifier,
        "is_valid": licence.is_valid,
        "expires_at": licence.expires_at.isoformat() if licence.expires_at else None,
        "effective_permissions": licence.allowed_capabilities,
        "quotas": {
            "api_requests_limit": f"{licence.rate_limit_requests}/{licence.rate_limit_window_seconds // 60}min",
            "api_requests_remaining_in_window": requests_remaining,
            "llm_access": licence.llm_access,
            "allowed_llm_models": licence.allowed_llm_models,
            "max_llm_tokens_per_request": licence.max_llm_tokens_per_request if licence.max_llm_tokens_per_request > 0 else "unlimited",
            "max_llm_tokens_per_day": licence.max_llm_tokens_per_day if licence.max_llm_tokens_per_day > 0 else "unlimited",
            "llm_tokens_today_remaining": llm_tokens_today_remaining,
        },
        "note": "Remaining quotas are specific to your client identifier (licence key or IP)."
    }
