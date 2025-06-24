# prospecting_app.py
import json
import socket
import uuid
import re
import time
import os
import traceback # Pour un meilleur logging d'erreur

# Classe d'exception personnalisée pour les erreurs MCP
class MCPError(Exception):
    def __init__(self, message, code=None, data=None):
        super().__init__(message)
        self.code = code
        self.data = data
        self.message = message # Garder le message original de l'erreur
    def __str__(self):
        return f"MCPError (Code: {self.code}): {self.message}" + (f" Data: {self.data}" if self.data else "")

def mcp_call(method: str, params: list = []):
    raw_response_str = "" # Pour logging en cas d'erreur de parsing JSON
    try:
        service_name = "gateway" if method.startswith("mcp.llm.") else method.split('.')[1]
        socket_path = f"/run/mcp/{service_name}.sock"
        
        max_wait = 10
        waited = 0
        while not os.path.exists(socket_path):
            if waited >= max_wait:
                raise FileNotFoundError(f"Service socket '{socket_path}' not found after {max_wait}s for method '{method}'")
            time.sleep(0.5)
            waited += 0.5
        
        # print(f"DEBUG mcp_call: Connecting to {socket_path} for method {method} with params {params}")

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(300.0) # Timeout généreux pour les opérations longues (LLM, embed)
            sock.connect(socket_path)
            payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": str(uuid.uuid4())}
            # print(f"DEBUG mcp_call: Sending payload: {json.dumps(payload)}")
            
            sock.sendall(json.dumps(payload).encode('utf-8') + b'\0')
            
            buffer = bytearray()
            # Lire la réponse jusqu'au délimiteur nul
            # Un timeout ici aussi pourrait être utile si le serveur ne répond pas
            # sock.settimeout(120.0) # Timeout pour la réception
            while b'\0' not in buffer:
                chunk = sock.recv(16384)
                if not chunk: raise ConnectionError("Connection closed by server while awaiting response.")
                buffer.extend(chunk)
            
            response_bytes, _ = buffer.split(b'\0', 1)
            raw_response_str = response_bytes.decode('utf-8')
            # print(f"DEBUG mcp_call: Received response string: {raw_response_str}")
            
            raw_response = json.loads(raw_response_str)

            if "error" in raw_response and raw_response["error"]:
                err = raw_response["error"]
                raise MCPError(err.get("message", "Unknown MCP error from server"), err.get("code"), err.get("data"))
            if "result" in raw_response:
                return raw_response["result"] # Retourne directement le contenu de "result"
            
            raise MCPError(f"Invalid MCP response (missing 'result' or 'error' key): {raw_response_str}", -32603) # Code d'erreur interne JSON-RPC

    except FileNotFoundError as e:
        print(f"[ERROR in mcp_call for '{method}'] Socket file not found: {e}")
        raise MCPError(str(e), -32000, {"method": method, "reason": "service_socket_not_found"}) from e
    except (ConnectionError, socket.timeout, BrokenPipeError) as e:
        print(f"[ERROR in mcp_call for '{method}'] Connection/Socket error: {e}")
        raise MCPError(f"Connection/Socket error: {e}", -32001, {"method": method}) from e
    except json.JSONDecodeError as e:
        print(f"[ERROR in mcp_call for '{method}'] JSONDecodeError: {e}. Response was: '{raw_response_str}'")
        raise MCPError(f"Failed to decode JSON response from server: {e}", -32700, {"method": method, "raw_response": raw_response_str}) from e
    except MCPError: # Re-lever les MCPError déjà formatées
        raise
    except Exception as e:
        print(f"[ERROR in mcp_call for '{method}'] Unexpected {type(e).__name__}: {e}")
        traceback.print_exc() # Imprimer la trace complète pour les erreurs inattendues
        raise MCPError(f"Unexpected error in mcp_call: {type(e).__name__} - {e}", -32603, {"method": method}) from e


def run_prospecting_campaign():
    print("--- Lancement de la campagne de prospection ---")
    try:
        history_file = "/outreach/contact_history.json" # Relatif à la racine virtuelle de fs_server
        history_list = []

        try:
            # print(f"DEBUG: Tentative de lecture du fichier d'historique: {history_file}")
            # mcp_call retourne maintenant directement le contenu de "result" ou lève MCPError
            fs_read_result = mcp_call("mcp.fs.read", [history_file])
            # print(f"DEBUG: Résultat de mcp.fs.read: {json.dumps(fs_read_result, indent=2)}")
            
            content_str = fs_read_result.get("content") # fs_read_result est { "path": ..., "content": ... }
            if content_str and content_str.strip():
                history_list = json.loads(content_str)
                if not isinstance(history_list, list):
                    print(f"AVERTISSEMENT: L'historique décodé n'est pas une liste (type: {type(history_list)}). Réinitialisation.")
                    history_list = []
            else:
                print("INFO: Fichier d'historique trouvé mais vide ou ne contient que des espaces.")
        except MCPError as e:
            # Le code d'erreur pour FileNotFoundError de fs_server est FS_CUSTOM_ERROR_BASE - 3 = -32010 - 3 = -32013
            if e.code == -32013 or (e.message and ("does not exist" in e.message.lower() or "no such file" in e.message.lower())):
                print(f"INFO: Fichier d'historique '{history_file}' non trouvé. Il sera créé.")
            else:
                print(f"ERREUR MCP lors de la lecture de l'historique: {e}. L'historique sera considéré comme vide.")
        except json.JSONDecodeError as e:
            print(f"ERREUR JSON lors du parsing de l'historique: {e}. L'historique sera considéré comme vide.")
            # Si le contenu est corrompu, history_list reste [], et le fichier sera écrasé.
        except Exception as e:
            print(f"ERREUR inattendue lors de la lecture de l'historique: {type(e).__name__} - {e}. L'historique sera vide.")
            traceback.print_exc()


        print(f"1. Historique lu: {len(history_list)} contacts.")

        valid_history_for_prompt = [item for item in history_list if isinstance(item, dict)]
        user_prompt = f"Generate a JSON array of 5 recruiting agencies in Brussels. DO NOT repeat anyone from this list: {json.dumps(valid_history_for_prompt)}"
        
        llm_params = [
            [{"role": "user", "content": user_prompt}], # Liste des messages
            {"model": "gemini-1.5-pro"}                 # Options
        ]
        
        print("2. Appel du LLM...")
        llm_api_result = mcp_call("mcp.llm.chat", llm_params) # llm_api_result est la réponse directe de l'API LLM (normalisée)
        # print(f"DEBUG: Réponse de l'API LLM (via mcp.llm.chat): {json.dumps(llm_api_result, indent=2)}")

        if not llm_api_result or "choices" not in llm_api_result or not llm_api_result["choices"]:
            raise ValueError(f"LLM response is missing 'choices' field or choices are empty. Full API result: {llm_api_result}")

        content_str = llm_api_result['choices'][0].get('message', {}).get('content')
        if content_str is None:
             raise ValueError(f"LLM response 'content' is missing. Full choice: {llm_api_result['choices'][0]}")
        
        # Nettoyer les ```json ... ``` que le LLM pourrait ajouter
        cleaned_content_str = re.sub(r"```json\s*|\s*```", "", content_str).strip()
        
        try:
            new_prospects = json.loads(cleaned_content_str)
        except json.JSONDecodeError as e:
            raise ValueError(f"Failed to decode LLM content into JSON. Error: {e}. Content was: '{cleaned_content_str}'")

        if not isinstance(new_prospects, list): # S'assurer que le LLM a bien retourné une liste
            raise ValueError(f"LLM content did not parse to a JSON list. Parsed type: {type(new_prospects)}. Content was: '{cleaned_content_str}'")

        print(f"3. ✅ SUCCÈS ! {len(new_prospects)} prospects trouvés.")
        print(json.dumps(new_prospects, indent=2))

        if new_prospects:
            valid_new_prospects = [item for item in new_prospects if isinstance(item, dict)] # Filtrer pour ne garder que les dicts
            updated_history = valid_history_for_prompt + valid_new_prospects
            
            # print(f"DEBUG: Écriture de l'historique mis à jour (Total: {len(updated_history)}) dans {history_file}")
            write_result = mcp_call("mcp.fs.write", [history_file, json.dumps(updated_history, indent=2), "text"]) # mcp.fs.write retourne aussi un "result"
            # print(f"DEBUG: Résultat de mcp.fs.write: {json.dumps(write_result, indent=2)}")
            print(f"4. Historique mis à jour. Total: {len(updated_history)}.")
            if write_result.get("status") != "success":
                 print(f"AVERTISSEMENT: mcp.fs.write a retourné un statut inattendu : {write_result}")
        else:
            print("Aucun nouveau prospect trouvé ou généré par le LLM, l'historique n'a pas été modifié.")

    except MCPError as e:
        print(f"\n--- ERREUR MCP DANS LA CAMPAGNE ---")
        print(f"Code: {e.code}, Message: {e.message}")
        if e.data: print(f"Data: {e.data}")
        traceback.print_exc()
    except ValueError as ve: # Erreurs de logique ou de parsing JSON dans ce script
        print(f"\n--- ERREUR DE VALEUR DANS LA CAMPAGNE ---")
        print(f"{ve}")
        traceback.print_exc()
    except Exception as e: # Autres erreurs inattendues
        print(f"\n--- ERREUR FATALE INATTENDUE DANS LA CAMPAGNE ---")
        print(f"{type(e).__name__}: {e}")
        traceback.print_exc()

if __name__ == "__main__":
    run_prospecting_campaign()