import asyncio
import json
import websockets
import configparser
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Header, Footer, Input, Log
from textual.binding import Binding
from textual.message import Message

class ServerResponse(Message):
    """Un message contenant une réponse du serveur."""
    def __init__(self, data: dict) -> None:
        self.data = data
        super().__init__()

class ChatApp(App):
    """Une application TUI pour interagir avec llmbasedos."""

    CSS_PATH = "style.css"
    BINDINGS = [Binding("ctrl+q", "quit", "Quit")]

    def __init__(self):
        super().__init__()
        self.config = configparser.ConfigParser()
        self.config.read("config.ini")
        self.ws_url = self.config.get("connection", "url", fallback="ws://localhost:8000/ws")
        self.user_id = self.config.get("user", "id", fallback="default_user")
        self.websocket = None

    def compose(self) -> ComposeResult:
        yield Header(name="llmbasedos - Assistant Confidentiel")
        yield Footer()
        with Horizontal(id="main-container"):
            yield Log(id="chat-log")
            yield Log(id="memory-feed")
        yield Input(placeholder="Type your message here...", id="chat-input")

    async def on_mount(self) -> None:
        """Au démarrage, on initialise l'UI et on lance le worker de connexion."""
        self.query_one("#chat-log").border_title = "💬 Chat"
        self.query_one("#memory-feed").border_title = "🧠 Memory Feed"
        self.query_one(Input).focus()
        self.run_worker(self.connect_and_listen, exclusive=True)

    async def connect_and_listen(self) -> None:
        """Worker qui gère la connexion WebSocket et écoute les messages."""
        chat_log = self.query_one("#chat-log")
        try:
            chat_log.write("🤖 Assistant: Connecting to llmbasedos gateway...")
            async with websockets.connect(self.ws_url) as ws:
                self.websocket = ws
                chat_log.write("🤖 Assistant: Connected! I'm ready.")
                
                async for message in self.websocket:
                    response_data = json.loads(message)
                    self.post_message(ServerResponse(response_data))

        except Exception as e:
            chat_log.write(f"\n❌ Connection Error: {e}")
            self.websocket = None

    def on_server_response(self, message: ServerResponse) -> None:
        """Gère les messages reçus du worker WebSocket."""
        data = message.data
        chat_log = self.query_one("#chat-log")
        memory_feed = self.query_one("#memory-feed")

        if "result" in data and "answer" in data["result"]:
            answer = data["result"]["answer"]
            # --- AMÉLIORATION DU FORMATAGE ---
            chat_log.write(f"\n🤖 Assistant:\n{answer}")
            memory_feed.write(f"🧠 [CONTEXT] Memory for '{self.user_id}' was accessed and updated.")
        elif "error" in data:
            chat_log.write(f"\n❌ MCP ERROR: {data['error'].get('message', 'Unknown error')}")

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        """Envoie le message de l'utilisateur au backend via WebSocket."""
        user_message = event.value
        if not user_message or not self.websocket:
            return

        chat_log = self.query_one("#chat-log")
        # --- AMÉLIORATION DU FORMATAGE ---
        chat_log.write(f"\n👤 You:\n{user_message}")
        event.input.clear()

        payload = {
            "jsonrpc": "2.0",
            "method": "mcp.contextual_chat.ask",
            "params": [self.user_id, user_message],
            "id": f"tui-call-{asyncio.get_running_loop().time()}"
        }

        try:
            await self.websocket.send(json.dumps(payload))
        except Exception as e:
            chat_log.write(f"\n❌ Send Error: {e}")

if __name__ == "__main__":
    app = ChatApp()
    app.run()