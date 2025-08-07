#!/bin/bash
set -e

echo "### Correction de la structure du projet llmbasedos ###"

# --- 1. Création des fichiers __init__.py manquants ---
echo "--> Création des fichiers __init__.py pour rendre les dossiers importables..."

# Créer le __init__.py dans le dossier 'servers'
if [ ! -f "llmbasedos_src/servers/__init__.py" ]; then
    touch "llmbasedos_src/servers/__init__.py"
    echo "    [+] Créé : llmbasedos_src/servers/__init__.py"
else
    echo "    [~] Déjà présent : llmbasedos_src/servers/__init__.py"
fi

# Parcourir chaque sous-dossier de 'servers' et ajouter un __init__.py si manquant
for SERVICE_DIR in llmbasedos_src/servers/*/; do
    if [ -d "$SERVICE_DIR" ]; then
        INIT_FILE="${SERVICE_DIR}__init__.py"
        if [ ! -f "$INIT_FILE" ]; then
            touch "$INIT_FILE"
            echo "    [+] Créé : $INIT_FILE"
        else
            echo "    [~] Déjà présent : $INIT_FILE"
        fi
    fi
done

echo ""
echo "--> Vérification et création des fichiers server.py manquants..."

# --- 2. Création des fichiers server.py minimalistes ---

# --- Pour crypto_data ---
CRYPTO_SERVER_FILE="llmbasedos_src/servers/crypto_data/server.py"
if [ ! -f "$CRYPTO_SERVER_FILE" ]; then
    echo "    [+] Création du fichier serveur pour crypto_data..."
    cat > "$CRYPTO_SERVER_FILE" << EOL
import asyncio
import logging

logging.basicConfig(level="INFO", format="%(asctime)s - crypto_data - %(levelname)s - %(message)s")

async def main():
    logging.info("Crypto Data Server started (placeholder).")
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Crypto Data Server stopped.")
EOL
else
    echo "    [~] Déjà présent : $CRYPTO_SERVER_FILE"
fi

# --- Pour football_data ---
FOOTBALL_SERVER_FILE="llmbasedos_src/servers/football_data/server.py"
if [ ! -f "$FOOTBALL_SERVER_FILE" ]; then
    echo "    [+] Création du fichier serveur pour football_data..."
    cat > "$FOOTBALL_SERVER_FILE" << EOL
import asyncio
import logging

logging.basicConfig(level="INFO", format="%(asctime)s - football_data - %(levelname)s - %(message)s")

async def main():
    logging.info("Football Data Server started (placeholder).")
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Football Data Server stopped.")
EOL
else
    echo "    [~] Déjà présent : $FOOTBALL_SERVER_FILE"
fi

# --- Pour horse_racing_data (au cas où) ---
HORSE_SERVER_FILE="llmbasedos_src/servers/horse_racing_data/server.py"
if [ ! -f "$HORSE_SERVER_FILE" ]; then
    echo "    [+] Création du fichier serveur pour horse_racing_data..."
    cat > "$HORSE_SERVER_FILE" << EOL
import asyncio
import logging

logging.basicConfig(level="INFO", format="%(asctime)s - horse_racing_data - %(levelname)s - %(message)s")

async def main():
    logging.info("Horse Racing Data Server started (placeholder).")
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Horse Racing Data Server stopped.")
EOL
else
    echo "    [~] Déjà présent : $HORSE_SERVER_FILE"
fi


echo ""
echo "### Terminé ###"
echo "La structure des packages Python et les serveurs de base ont été créés."
echo ""
echo "######################################################################"
echo "# ACTION MANUELLE REQUISE                                            #"
echo "######################################################################"
echo "# 1. Ouvrez votre fichier 'supervisord.conf'.                        #"
echo "# 2. Assurez-vous que les sections pour 'mcp-crypto-data',           #"
echo "#    'mcp-football-data', etc., ne sont PAS commentées.              #"
echo "# 3. Une fois vérifié, vous pouvez reconstruire et relancer :        #"
echo "#    docker compose build                                            #"
echo "#    docker compose up                                               #"
echo "######################################################################"