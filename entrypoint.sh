#!/bin/bash
set -e

echo "Entrypoint: Adjusting permissions for llmuser..."

# Ensure base cache directory for Hugging Face exists and has correct ownership
# This will be used by fs_server.
mkdir -p /opt/app/llmbasedos_cache/huggingface/hub
chown -R llmuser:llmuser /opt/app/llmbasedos_cache

# Ensure other critical directories llmuser might need to write to have correct ownership
# These are typically managed by Docker volumes defined in docker-compose.
# Example for FAISS index, if not already handled by volume permissions:
if [ -d "/var/lib/llmbasedos/faiss_index" ]; then
    chown -R llmuser:llmuser /var/lib/llmbasedos/faiss_index
fi
# Example for app logs, if not already handled by volume permissions:
if [ -d "/var/log/llmbasedos" ]; then
    chown -R llmuser:llmuser /var/log/llmbasedos
fi

# Ensure /run/mcp exists and is writable by llmuser (Supervisord group or direct ownership)
# Sockets are created here by services.
mkdir -p /run/mcp
chown llmuser:llmuser /run/mcp # Ou llmuser:root, ou llmuser:llmgroup si llmgroup existe et est pertinent
chmod 775 /run/mcp           # rwxrwxr-x (llmuser et son groupe peuvent écrire)

echo "Entrypoint: Permissions adjusted."

# Execute the command passed to this script (CMD from Dockerfile, which is supervisord)
exec "$@"
