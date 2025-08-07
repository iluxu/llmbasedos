import json
import websocket # pip install websocket-client
import uuid
import time

# --- Le script de l'agent "Hello World" qu'on va déployer ---
hello_agent_script = """
import json
import os
from mcp_client import MCPClient # Le client fourni par l'executor

def run():
    print("--- Hello Agent Started ---")
    
    # Initialise le client avec les variables d'environnement injectées
    client = MCPClient() 
    
    print(f"Agent running for tenant: {client.tenant_id}")
    
    # Test 1: Écrire un fichier dans le FS du tenant
    file_content = f"Hello from agent run at {time.strftime('%Y-%m-%d %H:%M:%S')}"
    write_params = {"path": "/agent_output.txt", "content": file_content, "encoding": "text"}
    client.call("mcp.fs.write", [write_params])
    print("File '/agent_output.txt' written successfully.")
    
    # Test 2: Appeler le LLM pour une tâche simple
    llm_params = {
        "messages": [{"role": "user", "content": "Write a one-sentence greeting for a new user."}],
        "options": {"model": "gpt-4o-mini"} # Utilise un modèle rapide et peu coûteux
    }
    greeting_result = client.call("mcp.llm.chat", [llm_params])
    greeting = greeting_result.get("choices")[0].get("message", {}).get("content")
    print(f"LLM generated greeting: {greeting}")
    
    # Écrire le résultat du LLM dans un autre fichier
    client.call("mcp.fs.write", [{"path": "/llm_greeting.txt", "content": greeting, "encoding": "text"}])
    print("LLM greeting saved to '/llm_greeting.txt'")
    
    print("--- Hello Agent Finished ---")

if __name__ == "__main__":
    import time
    run()
"""

def mcp_call_via_websocket(method, params):
    """Fonction helper pour parler au Gateway depuis notre script de test."""
    ws_url = "ws://localhost:8000/ws"
    ws = websocket.create_connection(ws_url)
    
    payload = {
        "jsonrpc": "2.0",
        "method": method,
        "params": params,
        "id": str(uuid.uuid4())
    }
    
    print(f"--> Sending MCP call: {method}")
    ws.send(json.dumps(payload))
    response = ws.recv()
    ws.close()
    
    print(f"<-- Received response.")
    return json.loads(response)

# --- Le Test Lui-même ---
if __name__ == "__main__":
    tenant_id = "tenant_for_test"
    print(f"--- Lancement du test de l'executor pour le tenant '{tenant_id}' ---")

    # 1. Déclencher l'exécution de l'agent
    run_params = {
        "tenant_id": tenant_id,
        "agent_script": hello_agent_script,
        "requirements": [], # Pas de dépendances externes pour ce test simple
        "params": {} # Pas de paramètres pour la fonction run()
    }
    
    response = mcp_call_via_websocket("mcp.agent.run", [run_params])
    print(f"Réponse de mcp.agent.run: {response}")
    
    if "error" in response:
        print("\n❌ ERREUR LORS DU DÉCLENCHEMENT DE L'AGENT.")
    else:
        run_id = response.get("result", {}).get("run_id")
        print(f"\nAgent run '{run_id}' programmé. Attente des résultats...")
        # Dans une vraie appli, on utiliserait mcp.agent.get_status pour poller.
        # Ici, on attend juste un peu et on vérifie les fichiers.
        time.sleep(30) # Laisse le temps au conteneur de se builder et de tourner

        print("\n--- Vérification des résultats ---")
        # 2. Vérifier que les fichiers ont été créés dans le bon répertoire de tenant
        agent_output_path = Path(f"./data/{tenant_id}/agent_output.txt")
        llm_output_path = Path(f"./data/{tenant_id}/llm_greeting.txt")

        if agent_output_path.exists():
            print(f"✅ Fichier agent_output.txt trouvé ! Contenu : {agent_output_path.read_text()[:100]}...")
        else:
            print("❌ Fichier agent_output.txt NON TROUVÉ !")

        if llm_output_path.exists():
            print(f"✅ Fichier llm_greeting.txt trouvé ! Contenu : {llm_output_path.read_text()[:100]}...")
        else:
            print("❌ Fichier llm_greeting.txt NON TROUVÉ !")
            
        print("\n🔎 Pour le débogage, vérifier les logs de l'executor:")
        print("docker compose logs llmbasedos | grep executor")