import asyncio, os, json
import redis.asyncio as redis
from llmbasedos_src.mcp_server_framework import MCPServer

KV = MCPServer("kv", __file__.replace("server.py","caps.json"))

@KV.set_startup_hook
async def on_start(server: MCPServer):
    server.redis_client = redis.from_url(os.getenv("REDIS_URL", "redis://:helloworld@redis:6379/0"), decode_responses=True)
    await server.redis_client.ping()
    server.logger.info("KV Arc connected to Redis.")

@KV.register_method("mcp.kv.set")
async def kv_set(server: MCPServer, _id, params: list):
    key, value, ttl = params[0], params[1], params[2] if len(params) > 2 else None
    await server.redis_client.set(key, json.dumps(value), ex=ttl)
    return {"status": "ok"}

@KV.register_method("mcp.kv.get")
async def kv_get(server: MCPServer, _id, params: list):
    value = await server.redis_client.get(params[0])
    return json.loads(value) if value else None

@KV.register_method("mcp.kv.incrby")
async def kv_incrby(server: MCPServer, _id, params: list):
    return await server.redis_client.incrby(params[0], params[1])

if __name__ == "__main__":
    asyncio.run(KV.start())