#!/usr/bin/env bash
QUERY="$1"

if [ -z "$QUERY" ]; then
  echo "❌ Usage: $0 \"Your confidential question\""
  exit 1
fi

echo "📂 Checking containers..."
docker compose -f docker-compose.dev.yml ps

echo "🚀 Sending to Gateway (OpenAI compatible)..."
curl -s http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d "{
    \"model\": \"gemma:2b\",
    \"messages\": [
      { \"role\": \"user\", \"content\": \"$QUERY\" }
    ]
  }" | jq -r '.choices[0].message.content'