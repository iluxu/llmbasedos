import asyncio, os, json, random, time, socket
from llmbasedos_src.mcp_server_framework import MCPServer

AWAKE = MCPServer("awake", __file__.replace("server.py","caps.json"))

# --- HELPER ROBUSTE POUR LES APPELS MCP INTERNES ---
async def _mcp_call_socket(method: str, params: list):
    loop = asyncio.get_running_loop()
    def blocking_call():
        # Tous les appels internes doivent passer par le gateway pour l'auth, etc.
        socket_path = "/run/mcp/gateway.sock"
        if not os.path.exists(socket_path):
            raise FileNotFoundError(f"Gateway socket not found at {socket_path}")

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(300.0) # Timeout long pour les appels LLM
            sock.connect(socket_path)
            
            payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": f"awake-call-{time.time()}"}
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
            
            return response_data.get("result")

    return await loop.run_in_executor(None, blocking_call)

@AWAKE.register_method("mcp.awake.tick")
async def handle_tick(server: MCPServer, _id, params: list):
    server.logger.info("AWAKE TICK: Waking up to find a new opportunity...")

    # 1. "PENSÉE" : Demander au LLM une idée de sujet chaud pour l'affiliation
    base_keyword = "crypto monnaie pour débutant"
    prompt = f"Donne-moi UN seul sujet d'article très spécifique et tendance autour de '{base_keyword}' pour un blog d'affiliation. Le sujet doit être précis et accrocheur. Réponds juste avec le sujet, rien d'autre."
    
    llm_params = [{"messages": [{"role": "user", "content": prompt}], "options": {"model": "gemini-1.5-pro-latest"}}]
    
    try:
        llm_response = await _mcp_call_socket("mcp.llm.chat", llm_params)
        new_keyword = llm_response['choices'][0]['message']['content'].strip().replace('"', '')
        server.logger.info(f"AWAKE TICK: New idea from LLM: '{new_keyword}'")
    except Exception as e:
        server.logger.error(f"AWAKE TICK: Failed to get idea from LLM: {e}", exc_info=True)
        return {"triggered": False, "reason": "LLM idea generation failed"}

    # 2. "ACTION" : Appeler l'Arc seo_affiliate pour créer le site
    server.logger.info(f"AWAKE TICK: Triggering seo_affiliate Arc for '{new_keyword}'")
    
    site_id = f"site-{new_keyword.lower().replace(' ', '-')[:20]}-{int(time.time())}"
    
    action_params = [{
        "site_id": site_id,
        "keyword": new_keyword,
        "affiliate_link": "https://monlien-secret.com/ref=luca-auto",
        "n_articles": 1,
        "lang": "fr",
        "dry": False # On génère pour de vrai !
    }]
    
    try:
        seo_result = await _mcp_call_socket("mcp.seo_aff.generate_site", action_params)
        server.logger.info(f"AWAKE TICK: seo_affiliate Arc executed successfully.")
        
        # On stocke une trace de l'action dans notre nouveau KV store
        await _mcp_call_socket("mcp.kv.set", [f"awake:last_run:{int(time.time())}", {"site_id": site_id, "keyword": new_keyword}])
        
        return {"triggered": True, "action": "mcp.seo_aff.generate_site", "details": seo_result}
    except Exception as e:
        server.logger.error(f"AWAKE TICK: Failed to execute seo_affiliate Arc: {e}", exc_info=True)
        return {"triggered": False, "reason": f"seo_affiliate Arc failed: {e}"}

if __name__ == "__main__":
    asyncio.run(AWAKE.start())