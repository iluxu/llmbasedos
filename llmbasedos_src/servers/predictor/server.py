# llmbasedos_src/servers/predictor/server.py
import asyncio
import random
from pathlib import Path
from llmbasedos_src.mcp_server_framework import MCPServer

SERVER_NAME = "predictor"
CAPS_FILE_PATH = str(Path(__file__).parent / "caps.json")
predictor_server = MCPServer(SERVER_NAME, CAPS_FILE_PATH)

@predictor_server.register_method("mcp.predictor.booking_probability")
async def booking_probability(server: MCPServer, request_id, params: list):
    """
    params[0] = user_id (str)
    MOCK : Retourne une probabilité aléatoire
    """
    user_id = params[0]
    prob = round(random.uniform(0, 1), 2)
    server.logger.info(f"[MOCK] Booking probability for {user_id}: {prob}")
    return {"user_id": user_id, "probability": prob}

@predictor_server.register_method("mcp.predictor.churn_risk")
async def churn_risk(server: MCPServer, request_id, params: list):
    user_id = params[0]
    risk = round(random.uniform(0, 1), 2)
    return {"user_id": user_id, "risk": risk}

if __name__ == "__main__":
    asyncio.run(predictor_server.start())
