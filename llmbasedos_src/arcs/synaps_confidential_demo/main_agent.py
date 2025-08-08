import os
import json
import requests

LLM_ROUTER_URL = "http://localhost:8000/mcp.llm.route"   # Paas dev
MODEL = os.getenv("LOCAL_LLM", "gemma:2b")

# === Charge éventuellement un contexte confidentiel depuis un fichier local ===
context_path = os.path.join(os.path.dirname(__file__), "context.txt")
if os.path.exists(context_path):
    with open(context_path, "r") as f:
        CONTEXT_DATA = f.read().strip()
else:
    CONTEXT_DATA = "Synaps Network is an independent research network focused on environment, economics, and technology."

# === Prompts pour les 3 effets ===
styles = {
    "SWOT Analysis": f"Using ONLY the following confidential context:\n\n{CONTEXT_DATA}\n\nGive me a concise SWOT analysis with exactly:\n- 3 strengths\n- 3 weaknesses\n- 3 opportunities",
    "Executive Statements": f"Using ONLY the following confidential context:\n\n{CONTEXT_DATA}\n\nGive me exactly 3 short executive strategic statements for a closed board meeting.",
    "Visionary Storytelling": f"Using ONLY the following confidential context:\n\n{CONTEXT_DATA}\n\nWrite exactly 3 short visionary sentences describing Synaps Network for internal morale boosting."
}

def send_to_llm(prompt):
    """Envoi une requête JSON-RPC vers le LLM Router."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "mcp.llm.route",
        "params": [
            {
                "messages": [
                    {"role": "user", "content": prompt}
                ],
                "options": {"model": MODEL}
            }
        ]
    }
    try:
        r = requests.post(LLM_ROUTER_URL, json=payload, timeout=600)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            return f"❌ LLM Error: {data['error']}"
        # Selon format OpenAI ou Ollama local
        if isinstance(data.get("result"), dict):
            msg = data["result"].get("message", {}).get("content", None)
            if msg:
                return msg
        return json.dumps(data, indent=2)
    except Exception as e:
        return f"❌ Request failed: {str(e)}"

if __name__ == "__main__":
    print(f"🚀 Sending prompt to LLM Router (LOCAL mode, model={MODEL})...\n")

    for section, prompt in styles.items():
        print(f"\n==============================")
        print(f"   {section.upper()}")
        print(f"==============================\n")
        answer = send_to_llm(prompt)
        print(answer)
