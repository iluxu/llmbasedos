import os, asyncio, json, socket, time
from pathlib import Path
from llmbasedos_src.mcp_server_framework import MCPServer

ANALYSIS = MCPServer("local_analysis", __file__.replace("server.py","caps.json"))

async def _mcp_call_async(method: str, params: list) -> dict:
    loop = asyncio.get_running_loop()
    def blocking_socket_call():
        socket_path = "/run/mcp/gateway.sock"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(300.0)
            sock.connect(socket_path)
            payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": f"arc-call-{time.time()}"}
            sock.sendall(json.dumps(payload).encode('utf-8') + b'\0')
            buffer = bytearray()
            while b'\0' not in buffer:
                chunk = sock.recv(16384)
                if not chunk: raise ConnectionError("Gateway closed connection.")
                buffer.extend(chunk)
            response_bytes, _ = buffer.split(b'\0', 1)
            response_data = json.loads(response_bytes.decode('utf-8'))
            if "error" in response_data and response_data["error"]:
                err = response_data["error"]
                raise RuntimeError(f"MCP call to '{method}' failed: {err.get('message', 'Unknown error')}")
            return response_data.get("result", {})
    return await loop.run_in_executor(None, blocking_socket_call)

@ANALYSIS.register_method("mcp.local_analysis.ask")
async def handle_ask(server: MCPServer, request_id, params: list):
    file_path, question = params
    
    server.logger.info(f"Reading content from virtual path: {file_path}")
    file_content_result = await _mcp_call_async("mcp.fs.read", [file_path, "text"])
    content = file_content_result.get("content", "File is empty or could not be read.")

    prompt = f"Based ONLY on the following context, answer the question.\n\nCONTEXT:\n---\n{content}\n---\n\nQUESTION: {question}"
    
    server.logger.info(f"Sending prompt to local LLM...")
    llm_params = [{
        "messages": [{"role": "user", "content": prompt}],
        "options": {"model": os.getenv("LOCAL_LLM", "llama3:8b")} # Force l'utilisation du LLM local
    }]
    
    llm_result = await _mcp_call_async("mcp.llm.chat", llm_params)
    
    answer = llm_result.get("choices", [{}])[0].get("message", {}).get("content", "LLM failed to generate an answer.")
    return {"answer": answer}

if __name__ == "__main__":
    asyncio.run(ANALYSIS.start())