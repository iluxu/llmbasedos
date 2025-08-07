# llmbasedos_src/servers/mail/server.py
import asyncio
import logging # Logger sera configuré par MCPServer
import os
import sys # Pour sys.exit en cas d'erreur critique au démarrage
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
from email.parser import BytesParser
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime, getaddresses
from datetime import datetime, timezone
import base64

import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header

from imapclient import IMAPClient
from imapclient.exceptions import IMAPClientError, LoginError
from icalendar import Calendar
import yaml

# --- Import du Framework MCP ---
from llmbasedos_src.mcp_server_framework import MCPServer
# common_utils n'est pas utilisé directement ici, mais si un autre handler en avait besoin, on l'importerait.
# from llmbasedos.common_utils import validate_mcp_path_param

# --- Configuration Spécifique au Serveur ---
SERVER_NAME = "mail"
CAPS_FILE_PATH_STR = str(Path(__file__).parent / "caps.json") # Relatif à ce fichier

# Chemin du fichier de configuration des comptes mail
# Priorité à la variable d'environnement, sinon chemin par défaut DANS le conteneur
MAIL_ACCOUNTS_CONFIG_FILE_STR: str = os.getenv(
    "LLMBDO_MAIL_ACCOUNTS_CONFIG_PATH",
    "/etc/llmbasedos/mail_accounts.yaml" # C'est ici que Docker Compose le monte
)
MAIL_ACCOUNTS_CONFIG_FILE_PATH = Path(MAIL_ACCOUNTS_CONFIG_FILE_STR)

# Codes d'erreur personnalisés
MAIL_CUSTOM_ERROR_BASE = -32030 # Base pour les erreurs spécifiques au serveur mail
MAIL_AUTH_ERROR_CODE = MAIL_CUSTOM_ERROR_BASE - 1 # Erreur d'authentification ou de connexion
# Vous pouvez définir d'autres sous-codes si nécessaire

# Initialisation de l'instance du serveur
# Le logger est automatiquement configuré par MCPServer
mail_server = MCPServer(
    server_name=SERVER_NAME,
    caps_file_path_str=CAPS_FILE_PATH_STR,
    custom_error_code_base=MAIL_CUSTOM_ERROR_BASE
)

# Attacher l'état spécifique au serveur (sera peuplé par le hook de démarrage)
mail_server.mail_accounts: Dict[str, Dict[str, Any]] = {} # type: ignore


# --- Fonctions Utilitaires (utilisent mail_server.logger) ---
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
    if not header_value: # Gérer le cas où header_value est vide
        return parsed_addrs
    for realname, email_address in getaddresses([header_value]): # getaddresses attend une liste
        if email_address:
            if realname: parsed_addrs.append(f"{_decode_email_header_str(realname)} <{email_address}>")
            else: parsed_addrs.append(email_address)
    return parsed_addrs


# --- Gestionnaire de Contexte pour Client IMAP ---
class IMAPConnection:
    def __init__(self, server: MCPServer, account_id: str):
        self.server = server
        self.account_id = account_id
        self.client: Optional[IMAPClient] = None
        # server.mail_accounts est un attribut de l'instance mail_server
        self.acc_conf = server.mail_accounts.get(account_id)

    def __enter__(self) -> IMAPClient:
        if not self.acc_conf:
            self.server.logger.error(f"IMAP config not found for account ID '{self.account_id}'.")
            raise ValueError(f"Account ID '{self.account_id}' configuration not found.")
        
        host = self.acc_conf.get("host")
        port = int(self.acc_conf.get("port", 993 if self.acc_conf.get("ssl", True) else 143))
        user = self.acc_conf.get("user")
        password = self.acc_conf.get("password")
        use_ssl = self.acc_conf.get("ssl", True)
        use_starttls = self.acc_conf.get("starttls", False)
        imap_timeout_sec = int(os.getenv("LLMBDO_MAIL_IMAP_TIMEOUT_SEC", "30"))

        if not all([host, user, password]):
            self.server.logger.error(f"Incomplete IMAP config for account '{self.account_id}'. Missing host, user, or password.")
            raise ValueError(f"Incomplete IMAP config for account '{self.account_id}'.")

        try:
            self.server.logger.debug(f"IMAP: Connecting to {host}:{port} for {self.account_id} (SSL: {use_ssl}, STARTTLS: {use_starttls}, Timeout: {imap_timeout_sec}s)")
            self.client = IMAPClient(host=host, port=port, ssl=use_ssl, timeout=imap_timeout_sec)
            
            if use_starttls and not use_ssl: # STARTTLS uniquement si SSL n'est pas déjà actif
                self.server.logger.debug(f"IMAP: Attempting STARTTLS for {self.account_id}")
                self.client.starttls()

            self.server.logger.debug(f"IMAP: Logging in as '{user}' for account '{self.account_id}'")
            self.client.login(user, password)
            
            self.server.logger.info(f"IMAP: Login successful for account '{self.account_id}'.")
            return self.client
        except LoginError as e:
            self.server.logger.error(f"IMAP login failed for {self.account_id} on {host}: {e}")
            raise ConnectionRefusedError(f"Authentication failed for mail account '{self.account_id}'.")
        except IMAPClientError as e:
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
            except Exception as e_logout:
                 self.server.logger.error(f"Unexpected error during IMAP logout for {self.account_id}: {e_logout}", exc_info=True)
            finally:
                self.client = None

# --- Handlers des Capacités Mail ---
# (Le code des handlers handle_mail_list_accounts, handle_mail_list_folders, etc. reste le même que dans votre version précédente)
# ... ASSUREZ-VOUS DE RECOPIER TOUS VOS HANDLERS ICI ...
# Exemple pour handle_mail_send_email (à adapter pour utiliser les bons champs SMTP de acc_conf) :
@mail_server.register_method("mcp.mail.send_email")
async def handle_send_email(server: MCPServer, request_id: str, params: List[Any]):
    if not params or not isinstance(params[0], dict):
        raise ValueError("Invalid parameters for send_email. Expected a single dictionary object.")

    options = params[0]
    account_id = options.get("account_id")
    to_email_str = options.get("to") # Peut être une string d'adresses séparées par des virgules
    subject = options.get("subject")
    body = options.get("body")

    if not all([account_id, to_email_str, subject, body]):
        raise ValueError("Missing required fields in params: account_id, to, subject, body.")
    
    if account_id not in server.mail_accounts:
        raise ValueError(f"Mail account '{account_id}' not found in configuration.")

    acc_conf = server.mail_accounts[account_id]
    
    from_email = acc_conf.get("email", acc_conf.get("user"))
    smtp_host = acc_conf.get("smtp_host")
    smtp_port = int(acc_conf.get("smtp_port", 587 if acc_conf.get("smtp_use_tls") else 25))
    smtp_user = acc_conf.get("smtp_user", from_email) # Souvent le même
    smtp_password = acc_conf.get("smtp_password", acc_conf.get("password")) # Utiliser un mdp SMTP dédié si dispo
    use_tls = acc_conf.get("smtp_use_tls", True) # STARTTLS par défaut si port 587
    use_ssl_smtp = acc_conf.get("smtp_use_ssl", False) # Pour connexion SSL directe (port 465)

    if not smtp_host:
        raise ValueError(f"SMTP host not configured for account '{account_id}'.")

    def send_sync():
        msg = MIMEMultipart()
        msg['From'] = from_email
        msg['To'] = to_email_str # smtplib gère les listes d'adresses dans la string To
        msg['Subject'] = Header(subject, 'utf-8').encode() # Assurer l'encodage correct du sujet
        msg.attach(MIMEText(body, 'plain', 'utf-8'))

        try:
            server.logger.info(f"SMTP: Connecting to {smtp_host}:{smtp_port} for account {account_id} (SSL: {use_ssl_smtp}, TLS: {use_tls})")
            if use_ssl_smtp:
                smtp_server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=30)
            else:
                smtp_server = smtplib.SMTP(smtp_host, smtp_port, timeout=30)
                if use_tls:
                    smtp_server.starttls()
            
            if smtp_user and smtp_password: # Login seulement si user/pass sont configurés pour SMTP
                server.logger.debug(f"SMTP: Logging in as '{smtp_user}' for account '{account_id}'")
                smtp_server.login(smtp_user, smtp_password)
            
            # send_message s'attend à une liste de destinataires pour le deuxième argument
            # si 'To' contient plusieurs adresses. parseaddr les sépare.
            recipients = [addr[1] for addr in getaddresses([to_email_str]) if addr[1]]
            if not recipients:
                raise ValueError(f"No valid recipient email addresses found in '{to_email_str}'.")

            smtp_server.send_message(msg, from_addr=from_email, to_addrs=recipients)
            smtp_server.quit()
            server.logger.info(f"Email successfully sent via '{account_id}' to: {', '.join(recipients)}.")
            return {"status": "success", "recipient": to_email_str}
        except smtplib.SMTPAuthenticationError as e_auth:
            server.logger.error(f"SMTP Authentication error for account {account_id}: {e_auth}", exc_info=True)
            raise ConnectionRefusedError(f"SMTP Authentication failed for account '{account_id}'. Check credentials/app password.")
        except Exception as e:
            server.logger.error(f"Failed to send email via account {account_id}: {e}", exc_info=True)
            raise RuntimeError(f"SMTP Error: {e}")

    return await server.run_in_executor(send_sync)

# ... (vos autres handlers : listAccounts, listFolders, listMessages, getMessage, parseIcalendar)


# --- Hooks de Cycle de Vie du Serveur ---
async def on_mail_server_startup_hook(server: MCPServer):
    server.logger.info(f"Mail Server '{server.server_name}' custom startup: Loading mail accounts from '{MAIL_ACCOUNTS_CONFIG_FILE_PATH}'...")
    
    def _load_config_sync():
        server.mail_accounts.clear()

        if not MAIL_ACCOUNTS_CONFIG_FILE_PATH.exists():
            server.logger.error( # Changé en ERROR car c'est plus critique
                f"CRITICAL: Mail accounts config file not found: {MAIL_ACCOUNTS_CONFIG_FILE_PATH}. "
                f"Mail server will not be functional. Check volume mounts and path in Docker."
            )
            return # Ne pas continuer si le fichier de config est manquant

        try:
            with MAIL_ACCOUNTS_CONFIG_FILE_PATH.open('r', encoding='utf-8') as f: # Spécifier utf-8
                loaded_config_yaml = yaml.safe_load(f)
            
            if not isinstance(loaded_config_yaml, dict) or "accounts" not in loaded_config_yaml:
                server.logger.error(f"Invalid format in {MAIL_ACCOUNTS_CONFIG_FILE_PATH}: Must be a dictionary with a top-level 'accounts' key.")
                return

            accounts_dict_from_yaml = loaded_config_yaml.get("accounts") # Utiliser .get pour éviter KeyError
            if not isinstance(accounts_dict_from_yaml, dict):
                server.logger.error(f"'accounts' key in {MAIL_ACCOUNTS_CONFIG_FILE_PATH} does not contain a dictionary.")
                return

            valid_accounts_loaded = 0
            for acc_id, conf_dict in accounts_dict_from_yaml.items():
                if not isinstance(conf_dict, dict):
                    server.logger.warning(f"Account '{acc_id}' in config has invalid format (not a dict). Skipping.")
                    continue
                # Vérifier les champs IMAP essentiels
                if not all(k in conf_dict for k in ["host", "user", "password"]):
                    server.logger.warning(f"Account '{acc_id}' is missing required IMAP fields (host, user, password). Skipping.")
                    continue
                
                conf_dict.setdefault("ssl", True)
                conf_dict.setdefault("port", 993 if conf_dict["ssl"] else 143)
                conf_dict.setdefault("email", conf_dict["user"])
                conf_dict.setdefault("starttls", False) # Pour IMAP
                conf_dict.setdefault("auth_type", "password")
                
                # Paramètres SMTP optionnels (si non présents, l'envoi pourrait échouer ou utiliser des défauts risqués)
                conf_dict.setdefault("smtp_host", None) # Pas de défaut pour smtp_host
                conf_dict.setdefault("smtp_port", 587 if conf_dict.get("smtp_use_tls", True) else (465 if conf_dict.get("smtp_use_ssl", False) else 25) )
                conf_dict.setdefault("smtp_user", conf_dict["user"])
                conf_dict.setdefault("smtp_password", conf_dict["password"])
                conf_dict.setdefault("smtp_use_tls", True if conf_dict.get("smtp_port") == 587 else False)
                conf_dict.setdefault("smtp_use_ssl", True if conf_dict.get("smtp_port") == 465 else False)


                server.mail_accounts[acc_id] = conf_dict
                valid_accounts_loaded += 1
            
            if valid_accounts_loaded > 0:
                server.logger.info(f"Successfully loaded {valid_accounts_loaded} mail account(s) from {MAIL_ACCOUNTS_CONFIG_FILE_PATH}.")
            else:
                server.logger.warning(f"No valid mail accounts loaded from {MAIL_ACCOUNTS_CONFIG_FILE_PATH}.")


        except yaml.YAMLError as ye:
            server.logger.error(f"Error parsing YAML from {MAIL_ACCOUNTS_CONFIG_FILE_PATH}: {ye}", exc_info=True)
        except Exception as e:
            server.logger.error(f"Unexpected error loading mail accounts config: {e}", exc_info=True)

    # Exécuter le chargement de la configuration dans l'executor du serveur
    # car MCPServer est déjà initialisé avec un executor.
    await server.run_in_executor(_load_config_sync)
    if not server.mail_accounts:
        server.logger.warning("Mail server starting with NO ACCOUNTS configured or loaded. Most capabilities will fail.")


async def on_mail_server_shutdown_hook(server: MCPServer):
    server.logger.info(f"Mail Server '{server.server_name}' custom shutdown hook called.")
    # Aucune ressource globale spécifique à libérer pour IMAP/SMTP ici,
    # car les connexions sont gérées par requête.

# Assigner les hooks à l'instance du serveur
mail_server.set_startup_hook(on_mail_server_startup_hook)
mail_server.set_shutdown_hook(on_mail_server_shutdown_hook)

# --- Point d'Entrée Principal (pour exécution via `python -m ...` par Supervisord) ---
if __name__ == "__main__":
    # Le logger de mail_server est déjà configuré par MCPServer lors de son initialisation.
    # On peut juste ajouter un log pour indiquer le mode d'exécution.
    mail_server.logger.info(
        f"Mail Server '{SERVER_NAME}' starting via __main__ entry point (intended for Supervisord)."
    )
    
    # Vérification critique : le hook de démarrage s'occupera de charger les comptes.
    # Si MAIL_ACCOUNTS_CONFIG_FILE_PATH n'existe pas, on_mail_server_startup_hook loguera une erreur.
    # Le serveur démarrera mais sera probablement non fonctionnel.

    try:
        # mail_server est une instance du VRAI MCPServer.
        # Sa méthode .start() créera le socket UNIX réel.
        asyncio.run(mail_server.start())
    except KeyboardInterrupt:
        mail_server.logger.info(f"Mail Server '{SERVER_NAME}' (main) stopped by KeyboardInterrupt.")
    except Exception as e_main:
        # Log l'erreur critique qui a empêché le serveur de tourner
        mail_server.logger.critical(f"Mail Server '{SERVER_NAME}' (main) crashed: {e_main}", exc_info=True)
        sys.exit(1) # Sortir avec un code d'erreur pour que Supervisord sache qu'il y a eu un problème.
    finally:
        mail_server.logger.info(f"Mail Server '{SERVER_NAME}' (main) exiting.")