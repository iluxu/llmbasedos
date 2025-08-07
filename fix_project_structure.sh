#!/bin/bash

# ==============================================================================
# SCRIPT DE MISE EN CONFORMITÉ POUR LLMBASEDOS
# ------------------------------------------------------------------------------
# Ce script corrige la structure du projet pour qu'elle soit un package Python
# valide, en ajoutant les __init__.py et les server.py manquants.
# Exécutez-le depuis la racine de votre projet.
# ==============================================================================

# Stoppe le script si une commande échoue
set -e

echo "### Correction de la structure du projet llmbasedos ###"
echo

# --- 1. Création des fichiers __init__.py ---
echo "--> Phase 1 : Création des fichiers __init__.py..."

# Définir une fonction pour créer un __init__.py si manquant
ensure_init_py() {
    DIR_PATH=$1
    INIT_FILE="${DIR_PATH}/__init__.py"
    if [ ! -f "$INIT_FILE" ]; then
        touch "$INIT_FILE"
        echo "    [+] Créé : $INIT_FILE"
    else
        echo "    [~] Déjà présent : $INIT_FILE"
    fi
}

# Appliquer la fonction sur tous les répertoires nécessaires
ensure_init_py "llmbasedos_src"
ensure_init_py "llmbasedos_src/gateway"
ensure_init_py "llmbasedos_src/servers"
ensure_init_py "llmbasedos_src/shell"
ensure_init_py "llmbasedos_src/scripts"

# Parcourir chaque sous-dossier de 'servers'
for SERVICE_DIR in llmbasedos_src/servers/*/; do
    if [ -d "$SERVICE_DIR" ]; then
        ensure_init_py "$SERVICE_DIR"
    fi
done

echo
echo "--> Phase 2 : Création des fichiers server.py manquants..."

# --- 2. Création des fichiers server.py minimalistes ---

# Définir une fonction pour créer un serveur placeholder
create_placeholder_server() {
    SERVICE_NAME=$1
    SERVER_FILE="llmbasedos_src/servers/${SERVICE_NAME}/server.py"

    if [ ! -f "$SERVER_FILE" ]; then
        echo "    [+] Création du fichier serveur pour ${SERVICE_NAME}..."
        # Utilisation de 'heredoc' pour écrire le contenu du fichier
        cat > "$SERVER_FILE" << EOL
# llmbasedos_src/servers/${SERVICE_NAME}/server.py
import asyncio
import logging
import sys

# Configuration basique du logging pour ce service
logging.basicConfig(
    level="INFO",
    format="%(asctime)s - ${SERVICE_NAME}_server - %(levelname)s - %(message)s"
)

async def main():
    """Fonction principale du serveur placeholder."""
    logging.info("${SERVICE_NAME} server started (placeholder). Process will stay alive.")
    # Boucle infinie pour garder le processus en vie pour supervisord
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("${SERVICE_NAME} server stopped.")
    except Exception as e:
        logging.critical("An unexpected error occurred: %s", e, exc_info=True)
        sys.exit(1)
EOL
    else
        echo "    [~] Déjà présent : $SERVER_FILE"
    fi
}

# Appliquer la fonction sur les services qui en ont besoin
create_placeholder_server "crypto_data"
create_placeholder_server "football_data"
create_placeholder_server "horse_racing_data"
# Ajoute d'autres services ici si nécessaire

echo
echo "### Terminé ###"
echo "La structure des packages et les serveurs de base ont été créés."
echo
echo "######################################################################"
echo "# ÉTAPES SUIVANTES                                                   #"
echo "######################################################################"
echo "# 1. Assurez-vous que supervisord.conf est correctement configuré.   #"
echo "# 2. Reconstruisez votre image Docker pour prendre en compte les     #"
echo "#    nouveaux fichiers s'ils n'étaient pas déjà copiés :             #"
echo "#    docker compose build                                            #"
echo "# 3. Lancez l'application :                                          #"
echo "#    docker compose up                                               #"
echo "######################################################################"
