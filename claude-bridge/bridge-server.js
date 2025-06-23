// claude-bridge/bridge-server.js
const WebSocket = require('ws');
const readline = require('readline');

// L'URL de ton gateway llmbasedos qui tourne dans Docker
const LLMBASEDO_GATEWAY_URL = 'ws://localhost:8000/ws';

// --- Communication avec Claude Desktop via stdio ---
const rl = readline.createInterface({
    input: process.stdin,
    output: process.stdout,
    terminal: false
});

// --- Communication avec le Gateway llmbasedos via WebSocket ---
let ws;
let pendingRequests = new Map();

function connectToGateway() {
    ws = new WebSocket(LLMBASEDO_GATEWAY_URL);

    ws.on('open', () => {
        // Envoyer un message de succès à Claude (si nécessaire, ou juste logguer)
        console.error('Bridge: Connected to llmbasedos gateway.');
        // Claude envoie un 'initialize' que nous devons gérer
    });

    ws.on('message', (data) => {
        try {
            const response = JSON.parse(data);
            // Log pour le debug du pont
            console.error(`Bridge: Received from gateway: ${JSON.stringify(response).substring(0, 200)}`);
            
            // Relayer la réponse à Claude
            process.stdout.write(JSON.stringify(response) + '\n');

            // Si c'est une réponse à une requête spécifique, résoudre la promesse (non utilisé dans ce modèle simple)
            if (response.id && pendingRequests.has(response.id)) {
                pendingRequests.get(response.id).resolve(response);
                pendingRequests.delete(response.id);
            }
        } catch (e) {
            console.error('Bridge: Error parsing message from gateway:', e);
        }
    });

    ws.on('error', (err) => {
        console.error('Bridge: WebSocket error:', err.message);
        // Informer Claude de l'erreur
        const errorResponse = { jsonrpc: '2.0', error: { code: -32000, message: `Failed to connect to llmbasedos gateway: ${err.message}` }, id: null };
        process.stdout.write(JSON.stringify(errorResponse) + '\n');
    });

    ws.on('close', () => {
        console.error('Bridge: Disconnected from llmbasedos gateway. Attempting to reconnect...');
        setTimeout(connectToGateway, 5000); // Tente de se reconnecter après 5s
    });
}

// Gérer les lignes de commande (requêtes JSON) venant de Claude
rl.on('line', (line) => {
    try {
        const request = JSON.parse(line);
        console.error(`Bridge: Received from Claude: ${line.substring(0, 200)}`);

        // Le premier message de Claude est 'initialize'
        if (request.method === 'initialize') {
            // On doit répondre à l'initialisation. On va demander au gateway ses capacités.
            const helloRequest = { jsonrpc: "2.0", method: "mcp.hello", params: [], id: "init-hello" };
            ws.send(JSON.stringify(helloRequest));
            
            // Attendre la réponse de mcp.hello pour construire la réponse à 'initialize'
            // C'est la partie la plus complexe. Pour une démo simple, on peut tricher
            // et renvoyer une réponse d'initialisation statique.
            // Ou, mieux, on attend la réponse de mcp.hello.
            const originalInitializeId = request.id;
            const promise = new Promise((resolve) => {
                pendingRequests.set("init-hello", { resolve });
            });

            promise.then(helloResponse => {
                const capabilities = helloResponse.result.reduce((acc, method) => {
                    // Transformer "mcp.fs.list" en une structure que Claude comprend
                    const [_, service, action] = method.split('.');
                    if (!acc[service]) {
                        acc[service] = { commands: [] };
                    }
                    acc[service].commands.push(action);
                    return acc;
                }, {});

                const initResponse = {
                    jsonrpc: "2.0",
                    id: originalInitializeId,
                    result: {
                        capabilities: {
                            // Structure attendue par Claude.
                            // C'est une supposition, il faut vérifier leur doc.
                            // Pour l'instant, on suppose qu'il veut juste les méthodes.
                        },
                        // Traduire les capacités en "tools" pour Claude
                        tools: helloResponse.result.map(method => ({ name: method, description: `Executes the ${method} command.`}))
                    }
                };
                // Répondre à Claude
                process.stdout.write(JSON.stringify(initResponse) + '\n');
            });

        } else {
            // Pour toutes les autres requêtes, les relayer directement
            if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify(request));
            } else {
                console.error('Bridge: WebSocket not ready, cannot send request to gateway.');
                const errorResponse = { jsonrpc: '2.0', error: { code: -32001, message: 'llmbasedos gateway is not connected.' }, id: request.id };
                process.stdout.write(JSON.stringify(errorResponse) + '\n');
            }
        }
    } catch (e) {
        console.error('Bridge: Error parsing line from Claude:', e);
    }
});

// Démarrer la connexion au gateway
connectToGateway();
