# main_agent.py - Divista Alpha
import time
import json
from llmbasedos_src.scripts.email_app import mcp_call  # notre wrapper JSON-RPC

# ID fixe pour test — en prod tu passeras via un mapping ou appel API
USER_ID = "elu_test_001"

def run():
    print("🤖 Divista‑Alpha en ligne...")
    print(f"🎯 Cible : {USER_ID}")

    # 1️⃣ Récupérer historique des messages
    try:
        history = mcp_call("mcp.onlyvue.get_chat_history", [USER_ID])
        print(f"📜 Historique pour {USER_ID} : {json.dumps(history, indent=2, ensure_ascii=False)}")
    except Exception as e:
        print(f"❌ Erreur récupération historique : {e}")
        return

    # 2️⃣ Demander prédiction booking
    try:
        booking = mcp_call("mcp.predictor.booking_probability", [USER_ID])
        prob = booking.get("probability", 0)
        print(f"📊 Probabilité booking : {prob*100:.0f}%")
    except Exception as e:
        print(f"❌ Erreur prédiction booking : {e}")
        return

    # 3️⃣ Logique réactive
    try:
        if prob > 0.8:
            # Message FOMO
            fomo_msg = "Il ne reste qu'une place ce soir… veux-tu la tienne ❤️ ?"
            res = mcp_call("mcp.onlyvue.send_message", [USER_ID, fomo_msg])
            print(f"🚀 Message FOMO envoyé : {res}")
        else:
            # Message doux
            warm_msg = "J’ai pensé à toi toute la journée… 😘"
            res = mcp_call("mcp.onlyvue.send_message", [USER_ID, warm_msg])
            print(f"💬 Message douceur envoyé : {res}")
    except Exception as e:
        print(f"❌ Erreur envoi message : {e}")

if __name__ == "__main__":
    run()
