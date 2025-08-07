#!/bin/bash
# Empêche le script de continuer si une commande échoue
set -e

echo "--- llmbasedos entrypoint script ---"

# 1. Ajuster les permissions pour le volume de données principal
# C'est la correction la plus importante. Elle s'exécute APRÈS que
# docker-compose ait monté le volume, garantissant que 'llmuser' puisse écrire.
echo "[Entrypoint] Adjusting ownership of /data..."
# Le `-R` est pour 'récursif', au cas où il y aurait déjà des sous-dossiers.
# `llmuser:llmuser` définit le propriétaire et le groupe.
chown -R llmuser:llmuser /data
echo "[Entrypoint] Permissions for /data set to llmuser."


# 2. Ajuster les permissions pour le dossier des sockets MCP
# Les services doivent pouvoir créer leurs fichiers .sock ici.
echo "[Entrypoint] Adjusting ownership of /run/mcp..."
chown -R llmuser:llmuser /run/mcp
echo "[Entrypoint] Permissions for /run/mcp set to llmuser."


# 3. Ajuster les permissions pour le socket Docker (essentiel pour Playwright/Executor)
# Vérifie que le socket existe avant de changer les permissions.
if [ -e /var/run/docker.sock ]; then
    echo "[Entrypoint] Adjusting ownership of Docker socket..."
    # L'utilisateur 'llmuser' a été ajouté au groupe 'docker' dans le Dockerfile.
    # Donner la propriété au groupe 'docker' est la méthode la plus propre.
    chown root:docker /var/run/docker.sock
    echo "[Entrypoint] Permissions for Docker socket adjusted."
else
    echo "[Entrypoint] Docker socket /var/run/docker.sock not found, skipping."
fi

echo "[Entrypoint] All permissions adjusted. Starting application..."
echo "-------------------------------------"

# Cette commande exécute la suite, c'est-à-dire la commande `CMD`
# définie dans le Dockerfile (qui est /usr/bin/supervisord ...)
exec "$@"