#!/usr/bin/env bash
QUERY="$1"

if [ -z "$QUERY" ]; then
  echo "❌ Usage: $0 \"Your confidential question\""
  exit 1
fi

echo "📂 Checking containers..."
docker compose -f docker-compose.dev.yml ps

echo "🚀 Sending to LLM Router (Gemma:2b LOCAL)..."
curl -s http://localhost:8000/mcp.llm.route \
  -H "Content-Type: application/json" \
  -d "{
    \"jsonrpc\": \"2.0\",
    \"id\": 1,
    \"method\": \"mcp.llm.route\",
    \"params\": [
      {
        \"messages\": [
          { \"role\": \"user\", \"content\": \"$QUERY\" }
        ],
        \"options\": { \"model\": \"gemma:2b\" }
      }
    ]
  }" | jq -r '.result'
