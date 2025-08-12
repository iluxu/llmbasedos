#!/bin/bash
#
# chat.sh - A simple and elegant wrapper to converse with
#           the llmbasedos confidential assistant.
#
# USAGE: ./chat.sh "Your question or information here"
#

# --- Configuration ---
# The session ID for Peter's demo. All conversations will share this memory.
USER_ID="peter_secret_project"

# The name of your main container.
CONTAINER_NAME="llmbasedos_dev"

# The Arc and method we are calling.
MCP_METHOD="mcp.contextual_chat.ask"
# --- End of Configuration ---


# Check if a question was provided.
if [ -z "$@" ]; then
  echo "❌ Error: Please provide a question in quotes."
  echo "   Example: ./chat.sh \"What is the project's launch date?\""
  exit 1
fi

# Capture the entire question, including spaces and special characters.
QUESTION="$@"

# Display a user-friendly message.
echo "👤 You > $QUESTION"
echo "🤖 Assistant > ..."

# Build the Python command that will be executed inside the container.
# This is a mini MCP client that does all the work.
PYTHON_CMD=$(cat <<EOF
import sys, json, socket, textwrap

# --- Parameters ---
socket_path = "/run/mcp/gateway.sock"
user_id = "$USER_ID"
question = """$QUESTION""" # Using triple quotes for robustness
method = "$MCP_METHOD"
# --- End of Parameters ---

try:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(300.0)
        sock.connect(socket_path)
        
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": [user_id, question],
            "id": "chat-sh-call"
        }
        
        sock.sendall(json.dumps(payload).encode('utf-8') + b'\0')
        
        buffer = bytearray()
        while b'\0' not in buffer:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("Connection to the gateway was lost.")
            buffer.extend(chunk)
            
        response_bytes, _ = buffer.split(b'\0', 1)
        response_data = json.loads(response_bytes.decode('utf-8'))
        
        if "error" in response_data:
            errmsg = response_data["error"].get("message", "Unknown error")
            print(f"\n❌ MCP ERROR: {errmsg}")
        elif "result" in response_data and "answer" in response_data["result"]:
            answer = response_data["result"]["answer"]
            # Clean formatting for readability
            wrapped_answer = textwrap.fill(answer, width=80)
            print(wrapped_answer)
        else:
            print(f"\n❌ Unexpected response from the system: {response_data}")

except Exception as e:
    print(f"\n❌ CRITICAL ERROR: {e}")

EOF
)

# Execute the Python command inside the container.
docker exec -i "$CONTAINER_NAME" python -c "$PYTHON_CMD"
