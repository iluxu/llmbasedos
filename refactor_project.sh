#!/bin/bash
#
# refactor_project.sh - Script pour aligner l'architecture llmbasedos
# avec la feuille de route stratégique.
#
# A exécuter depuis la racine du projet (/home/iluxu/llmbasedos/llmbasedos)

set -e

# --- Couleurs pour un affichage plus clair ---
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${BLUE}--- Début du refactoring du projet llmbasedos ---${NC}"

# --- Vérification initiale ---
if [ ! -f "docker-compose.yml" ] || [ ! -d "llmbasedos_src" ]; then
    echo -e "${RED}ERREUR : Ce script doit être exécuté depuis la racine de votre projet llmbasedos.${NC}"
    exit 1
fi
echo -e "${GREEN}[OK]${NC} Racine du projet détectée."

# ==============================================================================
# == PHASE 1 : SÉCURITÉ ET NETTOYAGE DE BASE
# ==============================================================================
echo -e "\n${BLUE}### PHASE 1 : Sécurité et Nettoyage de Base ###${NC}"

# 1. Sauvegarder et corriger docker-compose.yml
echo -ne "  -> Suppression de 'privileged: true' dans docker-compose.yml... "
cp docker-compose.yml docker-compose.yml.bak
sed -i '/privileged: true/d' docker-compose.yml
echo -e "${GREEN}Fait.${NC} (Sauvegarde créée : docker-compose.yml.bak)"

# 2. Conversion des fichiers texte en UTF-8
echo -e "  -> Conversion des fichiers texte en UTF-8..."
find . -type f \( -name "*.py" -o -name "*.json" -o -name "*.md" -o -name "*.yaml" -o -name "*.yml" -o -name "*.conf" -o -name "*.sh" \) -print0 | while IFS= read -r -d $'\0' file; do
    # Vérifie si le fichier n'est PAS déjà en UTF-8
    if ! file -i "$file" | grep -q "charset=utf-8"; then
        iconv -f "$(file -bi "$file" | sed -e "s/.*[ ]charset=\([^;]\+\).*/\1/")" -t UTF-8 -o "$file.utf8" "$file" && mv "$file.utf8" "$file"
        echo -e "     - Converti : $file"
    fi
done
echo -e "     ${GREEN}Conversion UTF-8 terminée.${NC}"

# ==============================================================================
# == PHASE 2 : REFRACTORING ARCHITECTURAL MAJEUR
# ==============================================================================
echo -e "\n${BLUE}### PHASE 2 : Refactoring Architectural Majeur ###${NC}"

# 1. Corriger la confusion Executor vs. Orchestrator
echo -e "  -> Correction de la confusion Executor/Orchestrator..."
if [ -f "llmbasedos_src/servers/executor/server.py" ]; then
    # Déplacer le bon code de l'executor vers l'orchestrator
    mv llmbasedos_src/servers/executor/server.py llmbasedos_src/servers/orchestrator/server.py
    echo -e "     - Déplacé : executor/server.py -> orchestrator/server.py (écrasement)"
else
    echo -e "     - ${YELLOW}AVERTISSEMENT :${NC} llmbasedos_src/servers/executor/server.py non trouvé, étape ignorée."
fi

# 2. Créer le squelette du VRAI serveur Executor
echo -e "  -> Création du squelette pour le vrai serveur Executor..."
mkdir -p llmbasedos_src/servers/executor
cat > llmbasedos_src/servers/executor/server.py << 'EOF'
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
EOF
echo -e "     - Créé : executor/server.py"

# 3. Mettre à jour les dépendances et capacités de l'Executor
echo "docker" > llmbasedos_src/servers/executor/requirements.txt
echo -e "     - Créé : executor/requirements.txt"

cat > llmbasedos_src/servers/executor/caps.json << 'EOF'
{
    "service_name": "executor",
    "description": "Executes and manages autonomous agent scripts in isolated, sandboxed containers.",
    "version": "1.0.0",
    "capabilities": [
        {
            "method": "mcp.agent.run",
            "description": "Deploys and runs an agent script in a sandboxed container.",
            "params_schema": {
                "type": "array",
                "items": [{
                    "type": "object",
                    "properties": {
                        "tenant_id": {"type": "string", "description": "The user/tenant ID for data sandboxing."},
                        "agent_script": {"type": "string", "description": "The full Python code of the agent."},
                        "requirements": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of pip dependencies."
                        }
                    },
                    "required": ["tenant_id", "agent_script"]
                }]
            }
        }
    ]
}
EOF
echo -e "     - Mis à jour : executor/caps.json"

# ==============================================================================
# == PHASE 3 : NETTOYAGE DU CODE REDONDANT
# ==============================================================================
echo -e "\n${BLUE}### PHASE 3 : Nettoyage du Code Redondant ###${NC}"

# 1. Supprimer le code LLM obsolète du Gateway
echo -ne "  -> Suppression de gateway/upstream.py (obsolète)... "
if [ -f "llmbasedos_src/gateway/upstream.py" ]; then
    rm llmbasedos_src/gateway/upstream.py
    echo -e "${GREEN}Fait.${NC}"
else
    echo -e "${YELLOW}Déjà supprimé.${NC}"
fi

echo -ne "  -> Suppression de l'import 'upstream' dans gateway/dispatch.py... "
if [ -f "llmbasedos_src/gateway/dispatch.py" ]; then
    sed -i "/from . import upstream/d" llmbasedos_src/gateway/dispatch.py
    echo -e "${GREEN}Fait.${NC}"
else
     echo -e "${YELLOW}Fichier non trouvé.${NC}"
fi

# ==============================================================================
# == PHASE 4 : PRÉPARATION POUR LES NOUVELLES FONCTIONNALITÉS
# ==============================================================================
echo -e "\n${BLUE}### PHASE 4 : Préparation pour les Nouvelles Fonctionnalités ###${NC}"

echo -e "  -> Création du squelette pour le service 'predictor'..."
mkdir -p llmbasedos_src/servers/predictor
touch llmbasedos_src/servers/predictor/requirements.txt

cat > llmbasedos_src/servers/predictor/caps.json << 'EOF'
{
    "service_name": "predictor",
    "description": "Provides predictive models and data analysis capabilities.",
    "version": "0.1.0",
    "capabilities": [
        {
            "method": "mcp.predictor.get_crypto_forecast",
            "description": "Provides a price forecast for a given crypto pair.",
            "params_schema": {
                "type": "array",
                "items": [{
                    "type": "object",
                    "properties": {
                        "pair": { "type": "string", "description": "e.g., 'BTC-USD'" },
                        "horizon_hours": { "type": "integer", "description": "Forecast horizon in hours." }
                    },
                    "required": ["pair", "horizon_hours"]
                }]
            }
        }
    ]
}
EOF
echo -e "     - Créé : predictor/caps.json"

cat > llmbasedos_src/servers/predictor/server.py << 'EOF'
# llmbasedos_src/servers/predictor/server.py
import asyncio
from pathlib import Path
from llmbasedos_src.mcp_server_framework import MCPServer
import random

SERVER_NAME = "predictor"
CAPS_FILE_PATH = str(Path(__file__).parent / "caps.json")
predictor_server = MCPServer(SERVER_NAME, CAPS_FILE_PATH)

@predictor_server.register_method("mcp.predictor.get_crypto_forecast")
async def handle_get_crypto_forecast(server: MCPServer, request_id, params: list):
    options = params[0]
    pair = options.get("pair")
    
    server.logger.info(f"[MOCK] Generating forecast for {pair}...")
    await asyncio.sleep(0.5) # Simuler un calcul
    
    # Réponse simulée avec une structure de base
    return {
        "pair": pair,
        "forecast_price": 42000 + random.uniform(-500, 500),
        "confidence": random.uniform(0.65, 0.85),
        "model_version": "mock_v0.1"
    }

if __name__ == "__main__":
    asyncio.run(predictor_server.start())
EOF
echo -e "     - Créé : predictor/server.py"

echo -e "\n${GREEN}### Refactoring terminé avec succès ! ###${NC}"
echo -e "${YELLOW}Résumé des actions :"
echo -e "  - Sécurité renforcée dans 'docker-compose.yml'."
echo -e "  - Fichiers convertis en UTF-8."
echo -e "  - Architecture Executor/Orchestrator corrigée et clarifiée."
echo -e "  - Code LLM obsolète supprimé du Gateway."
echo -e "  - Squelette pour le nouveau service 'predictor' créé."
echo -e "\n${BLUE}Prochaine étape :${NC}"
echo -e "1. Vérifiez les changements avec 'git diff'."
echo -e "2. Lancez ${YELLOW}docker compose up --build -d${NC} pour reconstruire avec la nouvelle structure."
echo -e "3. Commencez à implémenter la logique dans les nouveaux fichiers squelettes !"
