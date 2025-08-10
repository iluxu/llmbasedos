#!/bin/bash
# ticker.sh - Version 4, avec affichage des logs

# --- Configuration ---
CONTAINER_NAME="llmbasedos_dev"
LOG_LINES=15 # Nombre de lignes de log à afficher

# --- Fonctions pour la lisibilité ---
print_header() {
    echo ""
    echo "--- $1 ---"
}

# --- Exécution ---
echo "-----------------------------------------------------"
echo "Ticking the Awake Arc... ($(date))"
echo "-----------------------------------------------------"

# Lance le tick et stocke la réponse
TICK_RESPONSE=$(docker exec \
    --user llmuser \
    -e PYTHONPATH=/opt/app \
    "$CONTAINER_NAME" \
    python -m llmbasedos_src.shell.mcp_client "mcp.awake.tick" "[]")

# Affiche la réponse du tick
print_header "Awake Tick Response"
echo "$TICK_RESPONSE" | jq . # Utilise jq pour un affichage joli

# Affiche les logs pertinents pour voir ce qui s'est passé
print_header "Awake Server Logs (last $LOG_LINES lines)"
docker exec "$CONTAINER_NAME" tail -n "$LOG_LINES" /var/log/supervisor/awake-stdout.log

print_header "SEO Affiliate Server Logs (last $LOG_LINES lines)"
docker exec "$CONTAINER_NAME" tail -n "$LOG_LINES" /var/log/supervisor/seo_affiliate-stdout.log

print_header "KV Server Logs (last $LOG_LINES lines)"
docker exec "$CONTAINER_NAME" tail -n "$LOG_LINES" /var/log/supervisor/kv-stdout.log

print_header "Generated Sites"
docker exec "$CONTAINER_NAME" ls -lt /data/sites/

echo "-----------------------------------------------------"
echo "Tick finished."
echo "-----------------------------------------------------"