# llmbasedos/servers/mail/server.py
import asyncio
import logging # Logger obtained from MCPServer
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
from email.parser import BytesParser # Standard library
from email.header import decode_header, make_header # Standard library
from email.utils import parseaddr, parsedate_to_datetime # Standard library
from datetime import datetime, timezone # Standard library
import base64 # Standard library

from imapclient import IMAPClient # External dep
from imapclient.exceptions import IMAPClientError, LoginError # External dep
from icalendar import Calendar # External dep
import yaml # External dep for account config

# --- Import Framework ---
from llmbasedos.mcp_server_framework import MCPServer

# --- Server Specific Configuration ---
SERVER_NAME = "mail"
CAPS_FILE_PATH_STR = str(Path(__file__).parent / "caps.json")
MAIL_CUSTOM_ERROR_BASE = -32030 # Base for mail specific errors
MAIL_AUTH_ERROR_CODE = MAIL_CUSTOM_ERROR_BASE - 1 # -32031

MAIL_ACCOUNTS_CONFIG_FILE_PATH = Path(os.getenv("LLMBDO_MAIL_ACCOUNTS_CONFIG", "/etc/llmbasedos/mail_accounts.yaml"))
# MAIL_ACCOUNTS state will be an attribute of the server instance
# Example account structure in YAML:
# account_id_1:
#   email: "user1@example.com"
#   host: "imap.example.com"
#   port: 993 # Default 993 for SSL, 143 for non-SSL
#   user: "user1_login"
#   password: "actual_password_or_app_password" # Sensitive! Use keyring or env vars in prod.
#   ssl: true # Default true
#   starttls: false # Default false
#   auth_type: "password" # or "oauth2" (TODO)
#   # For OAuth2: client_id, client_secret, refresh_token, etc.

# Initialize server instance
mail_server = MCPServer(SERVER_NAME, CAPS_FILE_PATH_STR, custom_error_code_base=MAIL_CUSTOM_ERROR_BASE)

# Attach server-specific state
mail_server.mail_accounts: Dict[str, Dict[str, Any]] = {} # type: ignore

# --- Helper functions ---
def _decode_email_header_str(header_value: Union[str, bytes, None]) -> str:
    """Decodes email headers robustly into a readable string."""
    if header_value is None: return ""
    try:
        # make_header can handle bytes or str and various encodings
        decoded_header = make_header(decode_header(header_value))
        return str(decoded_header)
    except Exception as e:
        # Fallback for problematic headers
        mail_server.logger.warning(f"Could not fully decode header part: '{str(header_value)[:50]}...': {e}")
        if isinstance(header_value, bytes): return header_value.decode('latin-1', errors='replace')
        return str(header_value) # Should already be string if not bytes

def _parse_address_list_str(header_value: str) -> List[str]:
    """Parses From/To/Cc headers into a list of 'Name <email@example.com>' or 'email@example.com' strings."""
    parsed_addrs = []
    # Address lists can be comma-separated. email.utils.getaddresses handles this well.
    from email.utils import getaddresses
    for realname, email_address in getaddresses([header_value]): # Needs list of headers
        if email_address: # Must have an email address part
            if realname: parsed_addrs.append(f"{realname} <{email_address}>")
            else: parsed_addrs.append(email_address)
    return parsed_addrs


# --- IMAP Client Context Manager (for blocking operations in executor) ---
class IMAPConnection: # Simpler name
    def __init__(self, server: MCPServer, account_id: str):
        self.server = server
        self.account_id = account_id
        self.client: Optional[IMAPClient] = None
        self.acc_conf = server.mail_accounts.get(account_id) # type: ignore

    def __enter__(self) -> IMAPClient:
        if not self.acc_conf: raise ValueError(f"Account ID '{self.account_id}' config not found.")
        
        host = self.acc_conf.get("host")
        port = self.acc_conf.get("port", 993 if self.acc_conf.get("ssl", True) else 143)
        user = self.acc_conf.get("user")
        password = self.acc_conf.get("password") # WARNING: Plaintext password handling
        use_ssl = self.acc_conf.get("ssl", True)
        use_starttls = self.acc_conf.get("starttls", False) # Typically false if ssl=true

        if not host or not user or not password:
            raise ValueError(f"Incomplete IMAP config for account '{self.account_id}' (host, user, or password missing).")

        try:
            self.server.logger.debug(f"IMAP: Connecting to {host}:{port} for {self.account_id} (SSL: {use_ssl}, STARTTLS: {use_starttls})")
            # IMAPClient timeout is for socket operations, not login itself.
            self.client = IMAPClient(host=host, port=port, ssl=use_ssl, timeout=30) # 30s timeout
            
            if use_starttls and not use_ssl : # STARTTLS usually on non-SSL port first
                self.server.logger.debug(f"IMAP: Attempting STARTTLS for {self.account_id}")
                self.client.starttls()

            # TODO: Implement OAuth2 logic based on self.acc_conf.get("auth_type")
            self.server.logger.debug(f"IMAP: Logging in as {user} for {self.account_id}")
            self.client.login(user, password)
            self.server.logger.info(f"IMAP: Login successful for account {self.account_id}")
            return self.client
        except LoginError as e:
            self.server.logger.error(f"IMAP login failed for {self.account_id} on {host}: {e}")
            raise ConnectionRefusedError(f"Login failed for account '{self.account_id}': {e}") # Proxy error type
        except IMAPClientError as e:
            self.server.logger.error(f"IMAP client error for {self.account_id} on {host}: {e}", exc_info=True)
            raise ConnectionError(f"IMAP client error for '{self.account_id}': {e}") # Proxy error type
        except Exception as e: # Catch-all for other unexpected issues during connect/login
            self.server.logger.error(f"Unexpected IMAP connection error for {self.account_id}: {e}", exc_info=True)
            raise ConnectionError(f"Unexpected IMAP error connecting to '{self.account_id}': {e}")

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.client:
            try: self.client.logout()
            except IMAPClientError: pass # Ignore logout errors, connection might be dead
            self.server.logger.debug(f"IMAP: Logged out for {self.account_id}")
        self.client = None


# --- Mail Capability Handlers (decorated & using executor) ---
@mail_server.register("mcp.mail.listAccounts")
async def handle_mail_list_accounts(server: MCPServer, request_id: str, params: List[Any]):
    # This is fast, reads from server.mail_accounts (in-memory)
    if not server.mail_accounts: # type: ignore # If empty (e.g. config load failed or no config)
        return [] # Return empty list, startup hook handles loading
    return [
        {"account_id": acc_id, "email_address": conf.get("email", conf.get("user")), "type": "imap"}
        for acc_id, conf in server.mail_accounts.items() # type: ignore
    ]

@mail_server.register("mcp.mail.listFolders")
async def handle_mail_list_folders(server: MCPServer, request_id: str, params: List[Any]):
    account_id = params[0] # Schema validated
    if account_id not in server.mail_accounts: raise ValueError(f"Account ID '{account_id}' not found.") # type: ignore

    def list_folders_sync_blocking():
        with IMAPConnection(server, account_id) as client:
            folders_raw = client.list_folders() # list of (flags_tuple, delimiter_bytes, name_bytes)
            return [{"name": name_bytes.decode('utf-8', 'surrogateescape'), 
                     "path": name_bytes.decode('utf-8', 'surrogateescape'), # Path often same as name for IMAP
                     "flags": [f.decode('ascii', 'ignore') for f in flags_tuple]} 
                    for flags_tuple, _, name_bytes in folders_raw]
    try:
        return await server.run_in_executor(list_folders_sync_blocking)
    except (ConnectionRefusedError, ConnectionError) as conn_e: raise ValueError(str(conn_e)) # Map to param error for auth issues
    except IMAPClientError as imap_e: raise RuntimeError(f"Failed to list folders: {imap_e}")


@mail_server.register("mcp.mail.listMessages")
async def handle_mail_list_messages(server: MCPServer, request_id: str, params: List[Any]):
    account_id, folder_name_str = params[0], params[1]
    options = params[2] if len(params) > 2 else {}
    if account_id not in server.mail_accounts: raise ValueError(f"Account ID '{account_id}' not found.") # type: ignore
    
    limit = options.get("limit", 25)
    search_criteria = options.get("search_criteria", "ALL") # IMAP search string

    def list_messages_sync_blocking():
        with IMAPConnection(server, account_id) as client:
            # Folder name needs to be encoded correctly for IMAP. UTF-7 variant often used.
            # imapclient handles encoding for select_folder if given str.
            # However, some servers are picky. Using bytes with surrogateescape can be safer for non-ASCII.
            folder_name_bytes = folder_name_str.encode('utf-8', 'surrogateescape')
            client.select_folder(folder_name_bytes) # or folder_name_str directly

            # UID SEARCH is preferred. Search criteria also needs to be bytes.
            message_uids_bytes = client.search(search_criteria.encode('utf-8'), uid=True)
            message_uids = sorted([int(uid) for uid in message_uids_bytes], reverse=True) # Newest first
            
            uids_to_fetch = message_uids[:limit] # Apply limit
            if not uids_to_fetch: return []

            # ENVELOPE, FLAGS, BODYSTRUCTURE (or INTERNALDATE for better sorting)
            fetch_items = ['UID', 'ENVELOPE', 'FLAGS', 'BODYSTRUCTURE', 'INTERNALDATE']
            raw_fetch_data = client.fetch(uids_to_fetch, fetch_items, uid=True)
            
            messages_result = []
            for uid_bytes_key, data_dict in raw_fetch_data.items():
                uid = int(uid_bytes_key)
                env = data_dict.get(b'ENVELOPE'); flags = data_dict.get(b'FLAGS', b''); bs = data_dict.get(b'BODYSTRUCTURE')
                internal_date = data_dict.get(b'INTERNALDATE') # datetime object or None
                if not env: continue
                
                # Simplified attachment check from BODYSTRUCTURE (can be improved)
                has_attach = False
                if bs:
                    try: # Very crude check for 'attachment' disposition or filename param
                        if b'("attachment"' in str(bs).encode('latin-1', 'backslashreplace') or \
                           b'("filename" ' in str(bs).encode('latin-1', 'backslashreplace'): has_attach = True
                    except: pass # Ignore parsing errors in this crude check

                msg_date = env.date or internal_date # Prefer envelope date, fallback to internaldate
                messages_result.append({
                    "uid": uid, "subject": _decode_email_header_str(env.subject),
                    "from": _parse_address_list_str(_decode_email_header_str(env.from_[0].addr_spec if env.from_ else b'')),
                    "to": _parse_address_list_str(_decode_email_header_str(env.to[0].addr_spec if env.to else b'')),
                    "date": msg_date.astimezone(timezone.utc).isoformat() if msg_date else None,
                    "seen": b'\\Seen' in flags, "has_attachments": has_attach
                })
            # Client expects newest first, which uids_to_fetch (from sorted message_uids) should provide.
            # Re-sort by UID just in case fetch data is not ordered.
            messages_result.sort(key=lambda m: m['uid'], reverse=True)
            return messages_result

    try: return await server.run_in_executor(list_messages_sync_blocking)
    except (ConnectionRefusedError, ConnectionError) as conn_e: raise ValueError(str(conn_e))
    except IMAPClientError as imap_e: raise RuntimeError(f"Failed to list messages: {imap_e}")


@mail_server.register("mcp.mail.getMessage")
async def handle_mail_get_message(server: MCPServer, request_id: str, params: List[Any]):
    account_id, folder_name_str, msg_uid_int = params[0], params[1], params[2]
    options = params[3] if len(params) > 3 else {}
    if account_id not in server.mail_accounts: raise ValueError(f"Account ID '{account_id}' not found.") # type: ignore

    body_pref = options.get("body_preference", ["text/plain", "text/html"])
    fetch_attach_flag = options.get("fetch_attachments", False)
    max_attach_kb = options.get("max_attachment_size_inline_kb", 1024) # 1MB default

    def get_message_sync_blocking():
        with IMAPConnection(server, account_id) as client:
            folder_name_bytes = folder_name_str.encode('utf-8', 'surrogateescape')
            client.select_folder(folder_name_bytes)
            
            # Fetch full RFC822 email content
            # msg_uid_int must be a list for fetch
            raw_msg_fetch_data = client.fetch([msg_uid_int], ['UID', b'RFC822'], uid=True)
            
            message_content_bytes = None
            # raw_msg_fetch_data keys are UIDs as bytes when uid=True
            for key_uid_bytes, data_dict in raw_msg_fetch_data.items():
                if int(key_uid_bytes) == msg_uid_int: # Match UID
                    message_content_bytes = data_dict.get(b'RFC822')
                    break
            if not message_content_bytes:
                raise ValueError(f"Message UID {msg_uid_int} not found in folder '{folder_name_str}' or content missing.")

            email_msg_obj = BytesParser().parsebytes(message_content_bytes) # Parse with Python's email module
            
            headers_dict = {key: _decode_email_header_str(value) for key, value in email_msg_obj.items()}
            body_plain_str, body_html_str, attachments_list_res = None, None, []

            # Iterate through MIME parts to extract bodies and attachments
            for part in email_msg_obj.walk():
                content_type_main = part.get_content_type()
                content_disposition_str = str(part.get("Content-Disposition"))

                is_attachment_heuristic = "attachment" in content_disposition_str.lower() or \
                                         (part.get_filename() is not None)
                
                if is_attachment_heuristic and not part.is_multipart(): # Not a container for other parts
                    filename_decoded = _decode_email_header_str(part.get_filename() or f"attachment_{len(attachments_list_res)+1}.dat")
                    payload_bytes_att = part.get_payload(decode=True) or b'' # Ensure bytes
                    att_data = {"filename": filename_decoded, "mime_type": content_type_main,
                                "size": len(payload_bytes_att), "content_id": part.get("Content-ID")}
                    if fetch_attach_flag:
                        if len(payload_bytes_att) <= max_attach_kb * 1024 :
                            att_data["content_base64"] = base64.b64encode(payload_bytes_att).decode('ascii')
                        else: att_data["content_base64"] = "CONTENT_TOO_LARGE_OR_NOT_FETCHED"
                    attachments_list_res.append(att_data)
                
                elif not part.is_multipart() and not is_attachment_heuristic : # Potential body part
                    if content_type_main == "text/plain" and body_plain_str is None and "text/plain" in body_pref:
                        payload_b = part.get_payload(decode=True) or b''
                        charset = part.get_content_charset() or 'utf-8'
                        body_plain_str = payload_b.decode(charset, errors='replace')
                    elif content_type_main == "text/html" and body_html_str is None and "text/html" in body_pref:
                        payload_b = part.get_payload(decode=True) or b''
                        charset = part.get_content_charset() or 'utf-8'
                        body_html_str = payload_b.decode(charset, errors='replace')
            
            # Fallback if preferred body types were not found in preferred order
            if not (body_plain_str and "text/plain" in body_pref) or not (body_html_str and "text/html" in body_pref):
                for part in email_msg_obj.walk(): # Second pass for fallbacks
                    if part.is_multipart() or "attachment" in (str(part.get("Content-Disposition")).lower()): continue
                    ct = part.get_content_type()
                    if ct == "text/plain" and body_plain_str is None:
                         body_plain_str = (part.get_payload(decode=True)or b'').decode(part.get_content_charset() or 'utf-8', 'replace')
                    elif ct == "text/html" and body_html_str is None:
                         body_html_str = (part.get_payload(decode=True)or b'').decode(part.get_content_charset() or 'utf-8', 'replace')


            msg_date_hdr = headers_dict.get("Date"); parsed_dt_iso = None
            if msg_date_hdr:
                try: parsed_dt_iso = parsedate_to_datetime(msg_date_hdr).astimezone(timezone.utc).isoformat()
                except: pass # Ignore parsing errors for date
            
            return {
                "uid": msg_uid_int, "subject": _decode_email_header_str(email_msg_obj.get("Subject")),
                "from": _parse_address_list_str(_decode_email_header_str(email_msg_obj.get("From"))),
                "to": _parse_address_list_str(_decode_email_header_str(email_msg_obj.get("To"))),
                "cc": _parse_address_list_str(_decode_email_header_str(email_msg_obj.get("Cc", ""))),
                "date": parsed_dt_iso, "headers": headers_dict,
                "body_plain": body_plain_str, "body_html": body_html_str,
                "attachments": attachments_list_res if attachments_list_res else None
            }
    try: return await server.run_in_executor(get_message_sync_blocking)
    except (ConnectionRefusedError, ConnectionError) as conn_e: raise ValueError(str(conn_e))
    except ValueError as ve: raise ve # e.g. UID not found
    except IMAPClientError as imap_e: raise RuntimeError(f"Failed to get message: {imap_e}")


@mail_server.register("mcp.mail.parseIcalendar")
async def handle_mail_parse_icalendar(server: MCPServer, request_id: str, params: List[Any]):
    ical_data_str = params[0] # Schema validated

    def parse_ical_sync_blocking(): # CPU-bound
        try:
            cal = Calendar.from_ical(ical_data_str) # type: ignore
            parsed_components = []
            for component in cal.walk(): # type: ignore
                comp_data: Dict[str, Any] = {"type": component.name}
                if component.name in ["VEVENT", "VTODO", "VJOURNAL"]:
                    # Extract common fields, ensuring datetime objects are converted to ISO strings
                    for prop_name in ["summary", "location", "description", "uid"]:
                        prop_val = component.get(prop_name)
                        if prop_val is not None: comp_data[prop_name] = str(prop_val)
                    for dt_prop_name in ["dtstart", "dtend", "created", "last-modified", "dtstamp"]:
                        dt_val = component.get(dt_prop_name)
                        if dt_val is not None and hasattr(dt_val, 'dt'): # property has a datetime object
                             # Ensure timezone awareness before isoformat
                            dt_obj = dt_val.dt
                            if isinstance(dt_obj, datetime) and dt_obj.tzinfo is None:
                                dt_obj = dt_obj.replace(tzinfo=timezone.utc) # Assume UTC if naive
                            elif not isinstance(dt_obj, datetime): # date object, no timezone
                                 pass # keep as is, will be YYYY-MM-DD
                            comp_data[dt_prop_name] = dt_obj.isoformat()
                        elif dt_val is not None: # If it's not a dt_val property with .dt (e.g. just string)
                            comp_data[dt_prop_name] = str(dt_val)

                    if component.get('duration'): comp_data["duration"] = str(component.get('duration').to_ical().decode())
                    # TODO: Parse ORGANIZER and ATTENDEE with more detail if needed
                if component.name != "VCALENDAR": # Don't add the root calendar object
                    parsed_components.append(comp_data)
            return parsed_components
        except Exception as e_ical:
            server.logger.error(f"Error parsing iCalendar data: {e_ical}", exc_info=True)
            raise ValueError(f"Failed to parse iCalendar data: {e_ical}")
    
    return await server.run_in_executor(parse_ical_sync_blocking)

# --- Server Lifecycle Hooks ---
def _load_mail_accounts_config_blocking(server: MCPServer): # Sync for executor
    if MAIL_ACCOUNTS_CONFIG_FILE_PATH.exists():
        try:
            with MAIL_ACCOUNTS_CONFIG_FILE_PATH.open('r') as f:
                loaded_accounts = yaml.safe_load(f)
            if isinstance(loaded_accounts, dict):
                server.mail_accounts = loaded_accounts # type: ignore
                server.logger.info(f"Loaded {len(server.mail_accounts)} mail account(s) from {MAIL_ACCOUNTS_CONFIG_FILE_PATH}") # type: ignore
            else: server.logger.error(f"Mail accounts config {MAIL_ACCOUNTS_CONFIG_FILE_PATH} invalid format.")
        except ImportError: server.logger.warning("PyYAML not installed. Cannot load mail accounts config.")
        except Exception as e: server.logger.error(f"Error loading mail accounts config: {e}", exc_info=True)
    else: server.logger.warning(f"Mail accounts config file {MAIL_ACCOUNTS_CONFIG_FILE_PATH} not found. No accounts loaded.")

async def on_mail_server_startup_hook(server: MCPServer):
    server.logger.info(f"Mail Server '{server.server_name}' custom startup...")
    await server.run_in_executor(_load_mail_accounts_config_blocking, server)

async def on_mail_server_shutdown_hook(server: MCPServer):
    server.logger.info(f"Mail Server '{server.server_name}' custom shutdown...")
    # No specific shutdown actions for IMAP clients as they are per-request

mail_server.on_startup = on_mail_server_startup_hook # type: ignore
mail_server.on_shutdown = on_mail_server_shutdown_hook # type: ignore

# --- Main Entry Point ---
if __name__ == "__main__":
    # Basic logging for this script's execution context
    script_logger = logging.getLogger("llmbasedos.servers.mail_script_main")
    log_level_main = os.getenv(f"LLMBDO_{SERVER_NAME.upper()}_LOG_LEVEL", "INFO").upper()
    script_logger.setLevel(log_level_main)
    if not script_logger.hasHandlers(): # Avoid duplicate handlers if already set
        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter(f"%(asctime)s - MAIL MAIN - %(levelname)s - %(message)s"))
        script_logger.addHandler(ch)
    
    try:
        # MCPServer's start method should call on_startup and on_shutdown
        asyncio.run(mail_server.start())
    except KeyboardInterrupt:
        script_logger.info(f"Mail Server '{SERVER_NAME}' (main) stopped by KeyboardInterrupt.")
    except Exception as e_main_mail: # Catch other startup/runtime errors
        script_logger.critical(f"Mail Server (main) crashed: {e_main_mail}", exc_info=True)
    finally:
        script_logger.info(f"Mail Server (main) is shutting down...")
        # Ensure MCPServer's executor is shut down if its own finally block in start() might not run
        # (e.g. if asyncio.run itself raises before server.start fully completes its try/finally)
        if hasattr(mail_server, 'executor') and not mail_server.executor._shutdown: # type: ignore
            mail_server.logger.info("Mail Server main: Explicitly shutting down executor.")
            mail_server.executor.shutdown(wait=False) # Non-blocking, server.start() does wait=True
        script_logger.info(f"Mail Server (main) fully shut down.")
