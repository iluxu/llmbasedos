# Fichier: llmbasedos_src/shell/mcp_client.py
import sys
import json
import socket
import os
import uuid

def mcp_call(method: str, params_json: str):
    try:
        params = json.loads(params_json)
    except json.JSONDecodeError:
        print(f"Error: Invalid JSON in params: {params_json}", file=sys.stderr)
        sys.exit(1)

    # On se connecte toujours au gateway
    socket_path = "/run/mcp/gateway.sock"
    if not os.path.exists(socket_path):
        print(f"Error: Gateway socket not found at {socket_path}", file=sys.stderr)
        sys.exit(1)
        
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(60.0) 
            sock.connect(socket_path)
            payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": str(uuid.uuid4())}
            sock.sendall(json.dumps(payload).encode('utf-8') + b'\0')
            
            buffer = bytearray()
            while b'\0' not in buffer:
                chunk = sock.recv(4096)
                if not chunk: raise ConnectionError("Connection closed by server.")
                buffer.extend(chunk)
            
            response_bytes, _ = buffer.split(b'\0', 1)
            print(response_bytes.decode('utf-8'))

    except Exception as e:
        print(f"Error during MCP call: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python -m llmbasedos_src.shell.mcp_client <method> '<params_json>'", file=sys.stderr)
        sys.exit(1)
    
    mcp_call(sys.argv[1], sys.argv[2])