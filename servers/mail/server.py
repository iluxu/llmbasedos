# llmbasedos/servers/mail/server.py
import asyncio
import logging # Logger obtained from MCPServer
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
from email.parser import BytesParser # Standard library
from email.header import decode_header, make_header # Standard library
from email.utils import parseaddr, parsedate_to_datetime, getaddresses # Standard library
from datetime import datetime, timezone # Standard library
import base64 # Standard library

from imapclient import IMAPClient # External dep
from imapclient.exceptions import IMAPClientError, LoginError # External dep
from icalendar import Calendar # External dep
import yaml # External dep for account config

# --- Import Framework ---
# Supposons que ce framework est dans le PYTHONPATH
# from llmbasedos.mcp_server_framework import MCPServer
# Pour le rendre exécutable en standalone pour le moment, je vais simuler MCPServer
# Dans votre projet réel, vous utiliserez votre import.
if __name__ == '__main__': # Simulation pour exécution directe
    class MCPServer: # Minimal mock for standalone testing
        def __init__(self, server_name, caps_file_path_str, custom_error_code_base=0):
            self.server_name = server_name
            self.caps_file_path = Path(caps_file_path_str)
            self.custom_error_code_base = custom_error_code_base
            self.logger = logging.getLogger(f"llmbasedos.servers.{server_name}") # Mock logger
            self.logger.setLevel(os.getenv(f"LLMBDO_{server_name.upper()}_LOG_LEVEL", "INFO").upper())
            if not self.logger.hasHandlers():
                ch = logging.StreamHandler()
                ch.setFormatter(logging.Formatter(f"%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
                self.logger.addHandler(ch)
            
            self.executor = None # Placeholder
            self.on_startup = None
            self.on_shutdown = None
            self._handlers = {}

        def register(self, method_name):
            def decorator(func):
                self._handlers[method_name] = func
                return func
            return decorator

        async def run_in_executor(self, func, *args):
            # In a real scenario, this would use a ThreadPoolExecutor
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(self.executor, func, *args) # executor would be properly initialized

        async def start(self):
            # Mock start method
            if self.on_startup: await self.on_startup(self)
            self.logger.info(f"Mock MCPServer '{self.server_name}' started. Socket path would be used here.")
            # Simulate running forever (e.g. handling connections)
            try:
                while True: await asyncio.sleep(3600) # Sleep for a long time
            except asyncio.CancelledError:
                self.logger.info(f"Mock MCPServer '{self.server_name}' cancelled.")
            finally:
                if self.on_shutdown: await self.on_shutdown(self)
                self.logger.info(f"Mock MCPServer '{self.server_name}' shut down.")
    # Fin de la simulation MCPServer
else:
    from llmbasedos.mcp_server_framework import MCPServer


# --- Server Specific Configuration ---
SERVER_NAME = "mail"
CAPS_FILE_PATH_STR = str(Path(__file__).parent / "caps.json") # Relative to this file

# --- Configuration via Environment Variables with Defaults ---
# Chemin du fichier de configuration des comptes mail
MAIL_ACCOUNTS_CONFIG_FILE_STR: str = os.getenv(
    "LLMBDO_MAIL_ACCOUNTS_CONFIG_PATH",  # Variable d'environnement pour Docker/ déploiement
    str(Path(__file__).parent / "mail_accounts.yaml") # Fallback local pour dev (à côté de server.py)
)
MAIL_ACCOUNTS_CONFIG_FILE_PATH = Path(MAIL_ACCOUNTS_CONFIG_FILE_STR)

# Codes d'erreur personnalisés (pas besoin de modifier si MCPServer les gère)
MAIL_CUSTOM_ERROR_BASE = -32030
MAIL_AUTH_ERROR_CODE = MAIL_CUSTOM_ERROR_BASE - 1


# Initialize server instance (utilisera le logger configuré par MCPServer)
mail_server = MCPServer(SERVER_NAME, CAPS_FILE_PATH_STR, custom_error_code_base=MAIL_CUSTOM_ERROR_BASE)

# Attach server-specific state (sera peuplé par on_mail_server_startup_hook)
mail_server.mail_accounts: Dict[str, Dict[str, Any]] = {} # type: ignore


# --- Helper functions (utilisent mail_server.logger) ---
def _decode_email_header_str(header_value: Union[str, bytes, None]) -> str:
    if header_value is None: return ""
    try:
        decoded_header = make_header(decode_header(header_value))
        return str(decoded_header)
    except Exception as e:
        mail_server.logger.warning(f"Could not fully decode header: '{str(header_value)[:50]}...': {e}")
        if isinstance(header_value, bytes): return header_value.decode('latin-1', errors='replace')
        return str(header_value)

def _parse_address_list_str(header_value: str) -> List[str]:
    parsed_addrs = []
    for realname, email_address in getaddresses([header_value]):
        if email_address:
            if realname: parsed_addrs.append(f"{_decode_email_header_str(realname)} <{email_address}>")
            else: parsed_addrs.append(email_address)
    return parsed_addrs


# --- IMAP Client Context Manager ---
class IMAPConnection:
    def __init__(self, server: MCPServer, account_id: str):
        self.server = server
        self.account_id = account_id
        self.client: Optional[IMAPClient] = None
        self.acc_conf = server.mail_accounts.get(account_id) # type: ignore

    def __enter__(self) -> IMAPClient: # Renvoie IMAPClient, pas self
        if not self.acc_conf:
            self.server.logger.error(f"IMAP config not found for account ID '{self.account_id}'.")
            raise ValueError(f"Account ID '{self.account_id}' configuration not found.") # Internal error or bad param
        
        host = self.acc_conf.get("host")
        port = int(self.acc_conf.get("port", 993 if self.acc_conf.get("ssl", True) else 143))
        user = self.acc_conf.get("user")
        password = self.acc_conf.get("password") # WARNING: Still plaintext password handling
        use_ssl = self.acc_conf.get("ssl", True)
        use_starttls = self.acc_conf.get("starttls", False)
        # Timeout pour les opérations socket IMAP, configurable via ENV
        imap_timeout_sec = int(os.getenv("LLMBDO_MAIL_IMAP_TIMEOUT_SEC", "30"))


        if not all([host, user, password]):
            self.server.logger.error(f"Incomplete IMAP config for account '{self.account_id}'. Missing host, user, or password.")
            raise ValueError(f"Incomplete IMAP config for account '{self.account_id}'.")

        try:
            self.server.logger.debug(f"IMAP: Connecting to {host}:{port} for {self.account_id} (SSL: {use_ssl}, STARTTLS: {use_starttls}, Timeout: {imap_timeout_sec}s)")
            self.client = IMAPClient(host=host, port=port, ssl=use_ssl, timeout=imap_timeout_sec)
            
            # STARTTLS should only be attempted if not already using SSL and if enabled
            if use_starttls and not use_ssl:
                self.server.logger.debug(f"IMAP: Attempting STARTTLS for {self.account_id}")
                self.client.starttls() # This upgrades the connection to TLS

            # TODO: Implement OAuth2 logic based on self.acc_conf.get("auth_type")
            # if self.acc_conf.get("auth_type") == "oauth2":
            #     # token = get_oauth_token_for_account(self.account_id)
            #     # self.client.oauth2_login(user, token)
            #     pass
            # else:
            self.server.logger.debug(f"IMAP: Logging in as '{user}' for account '{self.account_id}'")
            self.client.login(user, password)
            
            self.server.logger.info(f"IMAP: Login successful for account '{self.account_id}'.")
            return self.client
        except LoginError as e:
            self.server.logger.error(f"IMAP login failed for {self.account_id} on {host}: {e}")
            # Important: Ne pas exposer les détails de l'erreur de login au client final pour la sécurité.
            raise ConnectionRefusedError(f"Authentication failed for mail account '{self.account_id}'.")
        except IMAPClientError as e: # Includes socket errors, timeouts during connection
            self.server.logger.error(f"IMAP client error for {self.account_id} on {host}: {e}", exc_info=True)
            raise ConnectionError(f"IMAP connection error for account '{self.account_id}'. Details: {type(e).__name__}")
        except Exception as e:
            self.server.logger.error(f"Unexpected error during IMAP connect/login for {self.account_id}: {e}", exc_info=True)
            raise ConnectionError(f"Unexpected IMAP error for account '{self.account_id}'.")

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.client:
            try:
                self.server.logger.debug(f"IMAP: Attempting logout for {self.account_id}")
                self.client.logout()
                self.server.logger.info(f"IMAP: Logged out successfully for {self.account_id}")
            except IMAPClientError as e:
                self.server.logger.warning(f"IMAP error during logout for {self.account_id}: {e}. Connection might have been already closed.")
            except Exception as e_logout: # Other unexpected errors
                 self.server.logger.error(f"Unexpected error during IMAP logout for {self.account_id}: {e_logout}", exc_info=True)
            finally: # Ensure client is None regardless of logout success/failure
                self.client = None

# --- Mail Capability Handlers ---
@mail_server.register("mcp.mail.listAccounts")
async def handle_mail_list_accounts(server: MCPServer, request_id: str, params: List[Any]):
    # Validation des paramètres (si nécessaire) est gérée par le framework MCPServer via caps.json
    if not server.mail_accounts: # type: ignore
        server.logger.info("mcp.mail.listAccounts: No mail accounts configured or loaded.")
        return []
    
    return [
        {"account_id": acc_id, "email_address": conf.get("email", conf.get("user")), "type": "imap"}
        for acc_id, conf in server.mail_accounts.items() # type: ignore
    ]

@mail_server.register("mcp.mail.listFolders")
async def handle_mail_list_folders(server: MCPServer, request_id: str, params: List[Any]):
    # `params` est déjà validé par le framework MCPServer contre le `params_schema` de caps.json
    account_id = params[0]
    if account_id not in server.mail_accounts: # type: ignore
        server.logger.warning(f"mcp.mail.listFolders: Account ID '{account_id}' not found in server configuration.")
        # Le framework devrait retourner une erreur basée sur le schema ou nous levons une ValueError
        raise ValueError(f"Account ID '{account_id}' not configured on this server.")

    def list_folders_sync_blocking_op(): # Renommé pour clarifier que c'est l'opération bloquante
        with IMAPConnection(server, account_id) as client:
            folders_raw = client.list_folders()
            return [{"name": name_bytes.decode('utf-8', 'surrogateescape'), 
                     "path": name_bytes.decode('utf-8', 'surrogateescape'),
                     "flags": [f.decode('ascii', 'ignore') for f in flags_tuple]} 
                    for flags_tuple, _, name_bytes in folders_raw]
    try:
        return await server.run_in_executor(list_folders_sync_blocking_op)
    except (ConnectionRefusedError, ConnectionError) as conn_e: # Erreurs levées par IMAPConnection
        # Ces erreurs sont spécifiques à la connexion et à l'authentification.
        # Elles devraient être mappées à une erreur MCP appropriée.
        # Le framework MCPServer pourrait avoir un mécanisme pour cela, ou nous utilisons MAIL_AUTH_ERROR_CODE.
        raise mail_server.create_custom_error(
            MAIL_AUTH_ERROR_CODE,
            str(conn_e), # Message de l'erreur de connexion
            data={"account_id": account_id}
        ) from conn_e
    except IMAPClientError as imap_e: # Autres erreurs IMAP après connexion
        server.logger.error(f"IMAPClientError in listFolders for {account_id}: {imap_e}", exc_info=True)
        raise RuntimeError(f"Server error while listing folders for account '{account_id}'.") from imap_e


@mail_server.register("mcp.mail.listMessages")
async def handle_mail_list_messages(server: MCPServer, request_id: str, params: List[Any]):
    account_id, folder_name_str = params[0], params[1]
    options = params[2] if len(params) > 2 else {}
    if account_id not in server.mail_accounts: raise ValueError(f"Account ID '{account_id}' not found.") # type: ignore
    
    limit = options.get("limit", 25)
    search_criteria_str = options.get("search_criteria", "ALL")

    def list_messages_sync_blocking_op():
        with IMAPConnection(server, account_id) as client:
            # IMAPClient recommande d'utiliser des chaînes str pour select_folder, il gère l'encodage.
            server.logger.debug(f"IMAP: Selecting folder '{folder_name_str}' for account {account_id}")
            select_info = client.select_folder(folder_name_str, readonly=True) # Readonly pour list
            server.logger.debug(f"IMAP: Folder select_info: {select_info}")

            # Utiliser UID SEARCH. La criteria doit être en bytes pour certains serveurs, ou str avec charset.
            # IMAPClient essaie de gérer cela. Si erreur, essayer .encode('utf-8').
            server.logger.debug(f"IMAP: Searching messages with criteria '{search_criteria_str}' (UID mode)")
            try:
                message_uids_bytes_list = client.search(criteria=search_criteria_str, charset='UTF-8', uid=True)
            except IMAPClientError as search_err: # Si le charset pose problème
                server.logger.warning(f"IMAP search with UTF-8 charset failed for '{search_criteria_str}', trying raw bytes: {search_err}")
                message_uids_bytes_list = client.search(criteria=search_criteria_str.encode('utf-8', 'surrogateescape'), uid=True)

            message_uids = sorted([int(uid_b) for uid_b in message_uids_bytes_list], reverse=True)
            server.logger.debug(f"IMAP: Found {len(message_uids)} UIDs, fetching first {limit}")
            
            uids_to_fetch = message_uids[:limit]
            if not uids_to_fetch: return []

            fetch_items = ['UID', 'ENVELOPE', 'FLAGS', 'BODYSTRUCTURE', 'INTERNALDATE']
            raw_fetch_data = client.fetch(uids_to_fetch, fetch_items, uid=True)
            
            messages_result_list = []
            for uid_bytes_key, data_dict in raw_fetch_data.items():
                uid = int(uid_bytes_key)
                env = data_dict.get(b'ENVELOPE')
                flags_bytes_tuple = data_dict.get(b'FLAGS', tuple()) # FLAGS est un tuple de bytes
                bs = data_dict.get(b'BODYSTRUCTURE')
                internal_date_dt = data_dict.get(b'INTERNALDATE')
                if not env: continue
                
                has_attach = False # Simplification
                if bs:
                    try: # Check for common indicators of attachments in BODYSTRUCTURE
                        bs_str_repr = str(bs).lower() # Convert complex structure to string for simple check
                        if '("attachment"' in bs_str_repr or '("filename"' in bs_str_repr or \
                           '("name"' in bs_str_repr and 'text/' not in bs_str_repr: # Avoid matching name on text parts
                            has_attach = True
                    except: pass

                msg_date_to_use = env.date or internal_date_dt
                
                # From/To can be None or empty list in ENVELOPE
                from_addrs = env.from_ if env.from_ else []
                to_addrs = env.to if env.to else []

                messages_result_list.append({
                    "uid": uid,
                    "subject": _decode_email_header_str(env.subject),
                    "from": [_decode_email_header_str(addr.addr_spec) for addr in from_addrs if addr and hasattr(addr, 'addr_spec')],
                    "to": [_decode_email_header_str(addr.addr_spec) for addr in to_addrs if addr and hasattr(addr, 'addr_spec')],
                    "date": msg_date_to_use.astimezone(timezone.utc).isoformat() if msg_date_to_use else None,
                    "seen": b'\\Seen' in flags_bytes_tuple,
                    "has_attachments": has_attach
                })
            messages_result_list.sort(key=lambda m: m['uid'], reverse=True)
            return messages_result_list

    try: return await server.run_in_executor(list_messages_sync_blocking_op)
    except (ConnectionRefusedError, ConnectionError) as conn_e:
        raise mail_server.create_custom_error(MAIL_AUTH_ERROR_CODE, str(conn_e), data={"account_id": account_id})
    except ValueError as ve: # e.g. account not found, folder not found (from select_folder)
        server.logger.warning(f"ValueError in listMessages for {account_id}/{folder_name_str}: {ve}")
        raise mail_server.create_custom_error(server.ERROR_INVALID_PARAMS, str(ve), data={"folder": folder_name_str})
    except IMAPClientError as imap_e:
        server.logger.error(f"IMAPClientError in listMessages for {account_id}/{folder_name_str}: {imap_e}", exc_info=True)
        raise RuntimeError(f"Server error listing messages for '{folder_name_str}'.")


@mail_server.register("mcp.mail.getMessage")
async def handle_mail_get_message(server: MCPServer, request_id: str, params: List[Any]):
    account_id, folder_name_str, msg_uid_int = params[0], params[1], params[2]
    options = params[3] if len(params) > 3 else {}
    if account_id not in server.mail_accounts: raise ValueError(f"Account ID '{account_id}' not found.") # type: ignore

    body_pref_list = options.get("body_preference", ["text/plain", "text/html"])
    fetch_attach_flag = options.get("fetch_attachments", False)
    max_attach_kb = int(os.getenv("LLMBDO_MAIL_MAX_ATTACH_INLINE_KB", "1024")) # Configurable max size for inline base64 attachment
    max_attach_bytes = max_attach_kb * 1024

    def get_message_sync_blocking_op():
        with IMAPConnection(server, account_id) as client:
            server.logger.debug(f"IMAP: Selecting folder '{folder_name_str}' for getMessage UID {msg_uid_int}")
            client.select_folder(folder_name_str) # No readonly=True for get, might involve implicit \Seen flag set by server
            
            raw_msg_fetch_data = client.fetch([msg_uid_int], ['UID', b'RFC822'], uid=True)
            
            message_content_bytes = None
            for key_uid_bytes, data_dict in raw_msg_fetch_data.items():
                if int(key_uid_bytes) == msg_uid_int:
                    message_content_bytes = data_dict.get(b'RFC822'); break
            if not message_content_bytes:
                raise ValueError(f"Message UID {msg_uid_int} not found or content missing in '{folder_name_str}'.")

            email_msg_obj = BytesParser().parsebytes(message_content_bytes)
            headers_dict = {key: _decode_email_header_str(value) for key, value in email_msg_obj.items()}
            body_plain_str, body_html_str, attachments_list_res = None, None, []

            # Iterate MIME parts: first collect preferred, then fallbacks
            # Pass 1: Collect preferred body types
            for part in email_msg_obj.walk():
                if part.is_multipart(): continue # Skip multipart containers themselves
                
                content_type_main = part.get_content_type().lower()
                content_disposition_str = str(part.get("Content-Disposition", "")).lower()

                is_attachment_like = "attachment" in content_disposition_str or part.get_filename() is not None

                if not is_attachment_like: # Potential body part
                    if content_type_main == "text/plain" and "text/plain" in body_pref_list and body_plain_str is None:
                        payload_b = part.get_payload(decode=True) or b''
                        charset = part.get_content_charset() or 'utf-8'
                        body_plain_str = payload_b.decode(charset, errors='replace')
                    elif content_type_main == "text/html" and "text/html" in body_pref_list and body_html_str is None:
                        payload_b = part.get_payload(decode=True) or b''
                        charset = part.get_content_charset() or 'utf-8'
                        body_html_str = payload_b.decode(charset, errors='replace')
            
            # Pass 2: Collect attachments and fallback body types if preferred not found
            for part in email_msg_obj.walk():
                if part.is_multipart(): continue

                content_type_main = part.get_content_type().lower()
                content_disposition_str = str(part.get("Content-Disposition", "")).lower()
                is_attachment_like = "attachment" in content_disposition_str or part.get_filename() is not None

                if is_attachment_like:
                    filename_decoded = _decode_email_header_str(part.get_filename() or f"attachment_{len(attachments_list_res)+1}")
                    payload_bytes_att = part.get_payload(decode=True) or b''
                    att_data = {"filename": filename_decoded, "mime_type": content_type_main,
                                "size": len(payload_bytes_att), "content_id": part.get("Content-ID")}
                    if fetch_attach_flag:
                        if len(payload_bytes_att) <= max_attach_bytes:
                            att_data["content_base64"] = base64.b64encode(payload_bytes_att).decode('ascii')
                        else: att_data["content_base64"] = f"CONTENT_SKIPPED_TOO_LARGE (>{max_attach_kb}KB)"
                    attachments_list_res.append(att_data)
                else: # Fallback for body parts if not found in pass 1
                    if content_type_main == "text/plain" and body_plain_str is None:
                         body_plain_str = (part.get_payload(decode=True)or b'').decode(part.get_content_charset() or 'utf-8', 'replace')
                    elif content_type_main == "text/html" and body_html_str is None:
                         body_html_str = (part.get_payload(decode=True)or b'').decode(part.get_content_charset() or 'utf-8', 'replace')
            
            msg_date_hdr = headers_dict.get("Date"); parsed_dt_iso = None
            if msg_date_hdr:
                try: parsed_dt_iso = parsedate_to_datetime(msg_date_hdr).astimezone(timezone.utc).isoformat()
                except: server.logger.debug(f"Could not parse date header '{msg_date_hdr}' for UID {msg_uid_int}")
            
            return {
                "uid": msg_uid_int, "subject": headers_dict.get("Subject", ""),
                "from": _parse_address_list_str(headers_dict.get("From", "")),
                "to": _parse_address_list_str(headers_dict.get("To", "")),
                "cc": _parse_address_list_str(headers_dict.get("Cc", "")),
                "date": parsed_dt_iso, "headers": headers_dict,
                "body_plain": body_plain_str, "body_html": body_html_str,
                "attachments": attachments_list_res if attachments_list_res else None
            }
    try: return await server.run_in_executor(get_message_sync_blocking_op)
    except (ConnectionRefusedError, ConnectionError) as conn_e:
        raise mail_server.create_custom_error(MAIL_AUTH_ERROR_CODE, str(conn_e), data={"account_id": account_id})
    except ValueError as ve: # e.g. UID not found by handler
        server.logger.warning(f"ValueError in getMessage for {account_id}/{folder_name_str}/{msg_uid_int}: {ve}")
        raise mail_server.create_custom_error(server.ERROR_RESOURCE_NOT_FOUND, str(ve), data={"uid": msg_uid_int})
    except IMAPClientError as imap_e:
        server.logger.error(f"IMAPClientError in getMessage for {account_id}/{folder_name_str}/{msg_uid_int}: {imap_e}", exc_info=True)
        raise RuntimeError(f"Server error getting message UID {msg_uid_int}.")


@mail_server.register("mcp.mail.parseIcalendar")
async def handle_mail_parse_icalendar(server: MCPServer, request_id: str, params: List[Any]):
    ical_data_str = params[0]
    # Optional context params: account_id, message_uid, attachment_filename (params[1] to params[3])
    # Not used in this parser, but could be logged or used for context if needed.

    def parse_ical_sync_blocking_op():
        try:
            cal: Calendar = Calendar.from_ical(ical_data_str) # type: ignore
            parsed_components = []
            for component in cal.walk():
                comp_name_str = str(component.name) # Ensure it's a string
                comp_data: Dict[str, Any] = {"type": comp_name_str}
                if comp_name_str in ["VEVENT", "VTODO", "VJOURNAL"]:
                    for prop_name_bstr in component: # Iterate over property names (often bytes in icalendar)
                        prop_name_str = str(prop_name_bstr).lower() # Normalize to lower string
                        prop_val = component.get(prop_name_bstr)
                        
                        if prop_val is None: continue

                        if prop_name_str in ["summary", "location", "description", "uid", "status", "priority", "url", "categories", "class"]:
                            comp_data[prop_name_str] = str(prop_val) if not isinstance(prop_val, list) else [_decode_email_header_str(v) for v in prop_val]
                        elif prop_name_str in ["dtstart", "dtend", "created", "last-modified", "dtstamp", "due", "completed", "exdate", "rdate"]:
                            # Handle VDDDTypes (can be date or datetime)
                            if hasattr(prop_val, 'dt'): # It's a vDDDLists or vDDDTypes object
                                dt_obj = prop_val.dt
                                if isinstance(dt_obj, list): # For EXDATE/RDATE which can have multiple values
                                    comp_data[prop_name_str] = []
                                    for d_item in dt_obj:
                                        if isinstance(d_item, datetime) and d_item.tzinfo is None:
                                            d_item = d_item.replace(tzinfo=timezone.utc)
                                        comp_data[prop_name_str].append(d_item.isoformat())
                                else: # Single datetime/date
                                    if isinstance(dt_obj, datetime) and dt_obj.tzinfo is None:
                                        dt_obj = dt_obj.replace(tzinfo=timezone.utc)
                                    comp_data[prop_name_str] = dt_obj.isoformat()
                            else: # If it's already a string or other simple type
                                comp_data[prop_name_str] = str(prop_val)
                        elif prop_name_str == "duration":
                            comp_data["duration"] = str(prop_val.to_ical().decode())
                        elif prop_name_str == "organizer":
                            comp_data["organizer"] = prop_val.params.get('CN', prop_val.to_ical().decode().replace('MAILTO:', ''))
                        elif prop_name_str == "attendee":
                            if "attendees" not in comp_data: comp_data["attendees"] = []
                            attendee_str = prop_val.params.get('CN', prop_val.to_ical().decode().replace('MAILTO:', ''))
                            comp_data["attendees"].append(attendee_str)
                        # Add more specific property parsers as needed (RRULE, GEO, etc.)

                if comp_name_str != "VCALENDAR":
                    parsed_components.append(comp_data)
            return parsed_components
        except Exception as e_ical:
            server.logger.error(f"Error parsing iCalendar data: {e_ical}", exc_info=True)
            # Raise specific error type that framework can map to MCP error
            raise ValueError(f"Failed to parse iCalendar data: {str(e_ical)[:100]}")

    return await server.run_in_executor(parse_ical_sync_blocking_op)


# --- Server Lifecycle Hooks ---
async def on_mail_server_startup_hook(server: MCPServer):
    server.logger.info(f"Mail Server '{server.server_name}' custom startup: Loading mail accounts...")
    
    # This function is sync, so needs to be run in executor if it does I/O
    # For YAML loading, it's usually fast enough, but good practice for consistency
    def _load_config_sync():
        # Clear previous configs if any (e.g., on reload if framework supports it)
        server.mail_accounts.clear() # type: ignore

        if not MAIL_ACCOUNTS_CONFIG_FILE_PATH.exists():
            server.logger.warning(f"Mail accounts config file not found: {MAIL_ACCOUNTS_CONFIG_FILE_PATH}. No accounts will be available.")
            return

        try:
            with MAIL_ACCOUNTS_CONFIG_FILE_PATH.open('r') as f:
                loaded_config_yaml = yaml.safe_load(f)
            
            if not isinstance(loaded_config_yaml, dict) or "accounts" not in loaded_config_yaml:
                server.logger.error(f"Invalid format in {MAIL_ACCOUNTS_CONFIG_FILE_PATH}: Must be a dictionary with a top-level 'accounts' key.")
                return

            accounts_dict_from_yaml = loaded_config_yaml["accounts"]
            if not isinstance(accounts_dict_from_yaml, dict):
                server.logger.error(f"'accounts' key in {MAIL_ACCOUNTS_CONFIG_FILE_PATH} does not contain a dictionary.")
                return

            # Basic validation and storing
            valid_accounts_loaded = 0
            for acc_id, conf_dict in accounts_dict_from_yaml.items():
                if not isinstance(conf_dict, dict):
                    server.logger.warning(f"Account '{acc_id}' in config has invalid format (not a dict). Skipping.")
                    continue
                if not all(k in conf_dict for k in ["host", "user", "password"]):
                    server.logger.warning(f"Account '{acc_id}' is missing required fields (host, user, password). Skipping.")
                    continue
                
                # Set defaults if not present
                conf_dict.setdefault("ssl", True) # Default to SSL
                conf_dict.setdefault("port", 993 if conf_dict["ssl"] else 143)
                conf_dict.setdefault("email", conf_dict["user"]) # Email defaults to username
                conf_dict.setdefault("starttls", False)
                conf_dict.setdefault("auth_type", "password") # Default auth type

                server.mail_accounts[acc_id] = conf_dict # type: ignore
                valid_accounts_loaded += 1
            
            server.logger.info(f"Successfully loaded {valid_accounts_loaded} mail account(s) from {MAIL_ACCOUNTS_CONFIG_FILE_PATH}.")

        except yaml.YAMLError as ye:
            server.logger.error(f"Error parsing YAML from {MAIL_ACCOUNTS_CONFIG_FILE_PATH}: {ye}", exc_info=True)
        except Exception as e:
            server.logger.error(f"Unexpected error loading mail accounts config: {e}", exc_info=True)

    await server.run_in_executor(_load_config_sync)


async def on_mail_server_shutdown_hook(server: MCPServer):
    server.logger.info(f"Mail Server '{server.server_name}' custom shutdown hook called.")
    # No specific global resources to release for IMAP usually, connections are per-request.

mail_server.on_startup = on_mail_server_startup_hook # type: ignore
mail_server.on_shutdown = on_mail_server_shutdown_hook # type: ignore

# --- Main Entry Point (if run directly, for MCPServer framework) ---
if __name__ == "__main__":
    # Setup basic logging if MCPServer doesn't do it early enough for this script's messages
    if not mail_server.logger.hasHandlers():
        _sh = logging.StreamHandler()
        _sh.setFormatter(logging.Formatter(f"%(asctime)s - {mail_server.server_name} (main) - %(levelname)s - %(message)s"))
        mail_server.logger.addHandler(_sh)
        mail_server.logger.setLevel(os.getenv(f"LLMBDO_{SERVER_NAME.upper()}_LOG_LEVEL", "INFO").upper())
    
    mail_server.logger.info(f"Starting Mail Server '{SERVER_NAME}' directly via __main__...")
    try:
        asyncio.run(mail_server.start()) # MCPServer should handle its own socket creation and loop
    except KeyboardInterrupt:
        mail_server.logger.info(f"Mail Server '{SERVER_NAME}' (main) stopped by KeyboardInterrupt.")
    except Exception as e_main:
        mail_server.logger.critical(f"Mail Server '{SERVER_NAME}' (main) crashed: {e_main}", exc_info=True)
    finally:
        mail_server.logger.info(f"Mail Server '{SERVER_NAME}' (main) exiting.")