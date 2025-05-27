# llmbasedos_src/shell/shell_utils.py
import asyncio
import json
import uuid
import logging
import sys # Pour sys.stdout.flush()
from typing import List, Dict, Any, Optional

# Importer directement les types nécessaires de websockets (pas besoin pour cette fonction si app gère le ws)
# from websockets.client import WebSocketClientProtocol # Non utilisé directement ici
# from websockets.exceptions import ConnectionClosed, ConnectionClosedOK, WebSocketException # Non utilisé directement ici

from rich.console import Console
from rich.text import Text
from rich.syntax import Syntax
from rich.markup import escape # Pour échapper les messages d'erreur

# Import TYPE_CHECKING pour l'annotation de type de ShellApp
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .luca import ShellApp # Pour l'annotation de type 'app'

logger = logging.getLogger("llmbasedos.shell.utils")

async def stream_llm_chat_to_console(
    app: 'ShellApp', # Instance de ShellApp qui gère la connexion et les queues
    messages: List[Dict[str, str]],
    llm_options: Optional[Dict[str, Any]] = None
) -> Optional[str]: # Returns full response text or None on error/no connection
    """
    Initiates an mcp.llm.chat stream request via ShellApp,
    reads chunks from the associated asyncio.Queue, and prints them to the console.
    """
    
    actual_llm_options = {"stream": True, **(llm_options or {})}
    # Forcer stream=True car cette fonction est pour le streaming vers la console
    actual_llm_options["stream"] = True 

    # Demander à ShellApp d'initier le stream et de nous donner l'ID de requête et la queue
    request_id, stream_queue = await app.start_mcp_stream_request(
        "mcp.llm.chat", [messages, actual_llm_options]
    )

    if not request_id or not stream_queue:
        # start_mcp_stream_request a déjà dû afficher une erreur si la connexion a échoué
        logger.error("LLM Stream: Failed to initiate stream request via ShellApp.")
        return None

    app.console.print(Text("Assistant: ", style="bold blue"), end="")
    full_response_text = ""
    
    try:
        while True: # Boucle pour consommer les messages de la queue
            response_json: Optional[Dict[str, Any]] = None # Pour la portée
            try:
                # Obtenir le prochain chunk depuis la queue avec un timeout
                response_json = await asyncio.wait_for(stream_queue.get(), timeout=120.0) # Timeout de 2min par chunk
                stream_queue.task_done() # Indiquer que l'item a été traité

            except asyncio.TimeoutError:
                app.console.print("\n[[error]LLM Stream[/]]: Timeout waiting for response chunk.")
                logger.error(f"LLM Stream: Timeout (ID {request_id}).")
                break # Sortir de la boucle de stream
            
            # Vérifier si le listener a mis une exception dans la queue (ex: connexion perdue)
            if isinstance(response_json, Exception): # Le listener peut mettre une Exception pour signaler la fin
                logger.error(f"LLM Stream: Received exception from queue (ID {request_id}): {response_json}")
                app.console.print(f"\n[[error]LLM Stream Error[/]]: {escape(str(response_json))}")
                break

            # S'assurer que response_json est bien un dictionnaire (pour mypy et la robustesse)
            if not isinstance(response_json, dict):
                logger.error(f"LLM Stream: Received non-dict item from queue (ID {request_id}): {type(response_json)}")
                app.console.print(f"\n[[error]LLM Stream Error[/]]: Received unexpected data type from gateway.")
                break

            logger.debug(f"STREAM_UTIL RCV from Queue (Expected ID {request_id}, Got ID {response_json.get('id')}): {str(response_json)[:200]}")
            
            # Normalement, le listener dans ShellApp ne devrait mettre dans la queue que les messages pour cet ID.
            # Mais une vérification ici peut être une sécurité additionnelle.
            if response_json.get("id") != request_id:
                logger.warning(f"STREAM_UTIL: Mismatched ID in stream queue! Expected {request_id}, got {response_json.get('id')}. Ignoring chunk.")
                continue

            if "error" in response_json:
                err = response_json["error"]
                app.console.print(f"\n[[error]LLM Error (Code {err.get('code')})[/]]: {escape(str(err.get('message')))}")
                if err.get('data'):
                     app.console.print(Syntax(json.dumps(err['data'], indent=2), "json", theme="native", background_color="default"))
                break # Erreur termine le stream

            result = response_json.get("result", {})
            if result.get("type") == "llm_chunk":
                llm_api_chunk = result.get("content", {}) # C'est le payload brut de l'API LLM
                delta = ""
                if isinstance(llm_api_chunk, dict): # Structure type OpenAI
                    delta = llm_api_chunk.get("choices", [{}])[0].get("delta", {}).get("content", "")
                
                if delta:
                    app.console.print(delta, end="")
                    sys.stdout.flush() # Forcer l'affichage immédiat
                    full_response_text += delta
            elif result.get("type") == "llm_stream_end":
                app.console.print() # Nouvelle ligne finale
                logger.info(f"LLM Stream (ID {request_id}) ended successfully. Total length: {len(full_response_text)}")
                break # Fin normale du stream
            else:
                logger.warning(f"STREAM_UTIL: Unknown result type from queue: '{result.get('type')}' for ID {request_id}")
        
    except Exception as e_outer_stream: # Erreur inattendue dans la logique de la boucle while
        logger.error(f"STREAM_UTIL: General error processing stream (ID {request_id}): {e_outer_stream}", exc_info=True)
        app.console.print(f"\n[[error]LLM stream processing error[/]]: {escape(str(e_outer_stream))}")
    finally:
        # Le `finally` est pour le `try` qui entoure la boucle `while`.
        # S'assurer que la queue est retirée des streams actifs de ShellApp si ce n'est pas déjà fait.
        if request_id and request_id in app.active_streams: # Vérifier si request_id a été défini
            logger.debug(f"Cleaning up stream queue for request ID {request_id} in stream_llm_chat_to_console's finally block.")
            # Vider la queue pour éviter que des messages restants ne soient lus par une future instance
            # ou que le listener ne bloque en essayant de mettre dans une queue pleine.
            queue_to_clean = app.active_streams.get(request_id)
            if queue_to_clean:
                while not queue_to_clean.empty():
                    try: queue_to_clean.get_nowait(); queue_to_clean.task_done()
                    except asyncio.QueueEmpty: break
                    except Exception: break # Pour les autres erreurs de queue
            app.active_streams.pop(request_id, None) # Enlever la référence de la queue des streams actifs
            
    return full_response_text