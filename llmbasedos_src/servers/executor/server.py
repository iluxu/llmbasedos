# llmbasedos_src/servers/executor/server.py
import asyncio
import os
import uuid
import docker
from pathlib import Path
from llmbasedos_src.mcp_server_framework import MCPServer

SERVER_NAME = "executor"
CAPS_FILE_PATH = str(Path(__file__).parent / "caps.json")
executor_server = MCPServer(SERVER_NAME, CAPS_FILE_PATH)

# Initialisation du client Docker
try:
    docker_client = docker.from_env()
except Exception as e:
    executor_server.logger.critical(f"Impossible de se connecter au démon Docker: {e}")
    docker_client = None

@executor_server.register_method("mcp.agent.run")
async def handle_agent_run(server: MCPServer, request_id, params: list):
    if not docker_client:
        raise RuntimeError("Le service Executor ne peut pas fonctionner sans connexion à Docker.")
    
    options = params[0]
    tenant_id = options.get("tenant_id", "default_user")
    script_content = options.get("agent_script", "")
    requirements = options.get("requirements", [])

    server.logger.info(f"Déploiement d'un agent pour le tenant '{tenant_id}'...")

    # Logique de déploiement d'un conteneur éphémère à implémenter ici
    # 1. Créer un contexte de build (dossier temporaire, Dockerfile, agent.py)
    # 2. Builder l'image (client.images.build(...))
    # 3. Lancer le conteneur (client.containers.run(...))
    #    - Monter le volume de données du tenant
    #    - Connecter au réseau 'llmbasedos-net'
    # 4. Retourner l'ID du conteneur

    # Réponse simulée pour le moment
    job_id = f"agent-job-{uuid.uuid4().hex[:8]}"
    server.logger.info(f"[MOCK] Lancement du job d'agent {job_id}")
    
    return {
        "job_id": job_id,
        "status": "pending_implementation",
        "message": "La logique de lancement de conteneur doit être implémentée."
    }

if __name__ == "__main__":
    asyncio.run(executor_server.start())
