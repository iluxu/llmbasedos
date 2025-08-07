cat > create_arc_manager.sh << 'EOF'
#!/bin/bash

set -e

SERVICE_NAME="arc_manager"
SERVICE_DIR="llmbasedos_src/servers/${SERVICE_NAME}"

echo "### Création du service MCP : ${SERVICE_NAME} ###"
echo
echo "--> Création du répertoire : ${SERVICE_DIR}"
mkdir -p "${SERVICE_DIR}"
echo "    [+] Répertoire créé."
echo
echo "--> Création des fichiers..."

touch "${SERVICE_DIR}/__init__.py"
echo "    [+] Créé : ${SERVICE_DIR}/__init__.py"

touch "${SERVICE_DIR}/requirements.txt"
echo "    [+] Créé : ${SERVICE_DIR}/requirements.txt (vide)"

cat > "${SERVICE_DIR}/caps.json" << 'CAPS_EOL'
{
    "service_name": "arc_manager",
    "description": "Manages the lifecycle of Arcs and Sentinels.",
    "version": "0.1.0",
    "capabilities": [
        {
            "method": "mcp.arc.create",
            "description": "Creates the initial file structure for a new Arc.",
            "params_schema": { "type": "array", "items": [{"type": "object", "properties": {"arc_name": { "type": "string" }, "specialty": { "type": "string"}}, "required": ["arc_name", "specialty"]}] }
        },
        {