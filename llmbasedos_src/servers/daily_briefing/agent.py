# llmbasedos_src/agents/daily_briefing/agent.py
import json
import time
import os

# Ce client sera fourni par l'executor_server dans l'environnement du conteneur de l'agent
from mcp_client import MCPClient

def run():
    """
    Fonction principale de l'agent de briefing quotidien.
    """
    print("--- Daily Briefing Agent: Starting Run ---")
    
    try:
        # Initialise le client pour communiquer avec le PaaS llmbasedos
        client = MCPClient()

        # --- Étape 1: Définir les chemins des fichiers ---
        # Ces chemins sont relatifs au système de fichiers virtuel du tenant/utilisateur.
        tasks_file_path = "/tasks/today.txt"
        briefing_output_path = f"/briefings/{time.strftime('%Y-%m-%d')}_briefing.txt"
        
        print(f"Reading tasks from: {tasks_file_path}")

        # --- Étape 2: Lire le fichier de tâches via le fs_server ---
        try:
            # Note: Le MCPClient injecte automatiquement le tenant_id
            read_params = {"path": tasks_file_path, "encoding": "text"}
            read_result = client.call("mcp.fs.read", [read_params])
            tasks_content = read_result.get("content", "")
            if not tasks_content.strip():
                print("Task file is empty. Nothing to summarize.")
                client.call("mcp.fs.write", [{"path": briefing_output_path, "content": "No tasks found for today.", "encoding": "text"}])
                print(f"Empty briefing written to {briefing_output_path}")
                return
        except Exception as e:
            # Si le fichier n'existe pas, on le signale et on sort.
            # Une vraie app pourrait créer le fichier ici.
            print(f"Error reading task file: {e}")
            print("Please create '/tasks/today.txt' in your data directory.")
            return

        print(f"Successfully read {len(tasks_content)} characters from task file.")

        # --- Étape 3: Utiliser le LLM pour résumer les tâches ---
        prompt = f"""
        You are an expert productivity assistant. Your task is to analyze the following raw text containing today's tasks and priorities.
        Synthesize this information into a clear, concise, and motivating daily briefing.
        Structure the output into three sections:
        1.  **Top 3 Priorities:** The 3 most critical tasks for today.
        2.  **Other Tasks:** A bulleted list of secondary tasks.
        3.  **Suggestion for Success:** A short, encouraging sentence to start the day.

        Here is the raw text:
        ---
        {tasks_content}
        ---
        """

        print("Sending tasks to LLM for summarization...")
        llm_params = {
            "messages": [{"role": "user", "content": prompt}],
            "options": {"model": "gpt-4o-mini"} # Un modèle rapide est parfait pour ça
        }
        
        briefing_result = client.call("mcp.llm.chat", [llm_params])
        briefing_text = briefing_result.get("choices")[0].get("message", {}).get("content", "Failed to generate briefing.")
        
        print("LLM generated the daily briefing.")

        # --- Étape 4: Écrire le résumé dans un nouveau fichier ---
        print(f"Writing briefing to: {briefing_output_path}")
        write_params = {
            "path": briefing_output_path,
            "content": briefing_text,
            "encoding": "text"
        }
        write_result = client.call("mcp.fs.write", [write_params])
        
        if write_result.get("status") == "success":
            print(f"✅ Successfully saved daily briefing to '{briefing_output_path}'.")
        else:
            print(f"⚠️ Failed to save daily briefing. Server response: {write_result}")

    except Exception as e:
        print(f"❌ An unexpected error occurred during the agent run: {e}")
        # Dans un vrai agent, on pourrait utiliser mcp.log.error ou un service de notification.
    finally:
        print("--- Daily Briefing Agent: Run Finished ---")

if __name__ == "__main__":
    run()