// playwright-mcp/mcp-server.js

const express = require('express');
const { chromium } = require('playwright');
const { v4: uuidv4 } = require('uuid');

const app = express();
app.use(express.json());

const PORT = 5678;
const sessions = new Map();
let browserInstance = null;

// Au démarrage, on pré-lance une instance de Chromium pour que les sessions
// se créent quasi-instantanément. C'est une optimisation clé.
(async () => {
    try {
        console.log('[MCP Server] Pre-launching persistent browser instance...');
        browserInstance = await chromium.launch({
            headless: true,
            args: [
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-blink-features=AutomationControlled'
            ]
        });
        console.log('[MCP Server] Browser is ready!');
    } catch (e) {
        console.error('[MCP Server] FATAL: Failed to pre-launch browser:', e);
        process.exit(1); // Arrête le serveur s'il ne peut pas lancer le navigateur.
    }
})();

// Endpoint de santé, essentiel pour le healthcheck de Docker Compose.
app.get('/health', (req, res) => {
    res.json({ 
        status: 'ok', 
        ready: browserInstance !== null,
        active_sessions: sessions.size 
    });
});

// Endpoint pour créer une nouvelle session de scraping.
app.get('/sse', async (req, res) => {
    res.setHeader('Content-Type', 'text/event-stream');
    res.setHeader('Cache-Control', 'no-cache');
    
    const sessionId = uuidv4();
    
    if (!browserInstance) {
        console.error(`[Session ${sessionId}] Refused: Browser not ready.`);
        res.status(503).end('Browser not available');
        return;
    }
    
    try {
        const context = await browserInstance.newContext({
            viewport: { width: 1920, height: 1080 },
            userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        });
        const page = await context.newPage();
        
        sessions.set(sessionId, { context, page, created: Date.now() });
        
        console.log(`[Session ${sessionId}] Created successfully.`);
        res.write(`data: /session/${sessionId}\n\n`);
        res.end();
    } catch (e) {
        console.error(`[Session ${sessionId}] Failed to create context/page:`, e);
        res.status(500).end('Session creation failed');
    }
});

// Endpoint pour exécuter des commandes dans une session active.
app.post('/session/:sessionId', async (req, res) => {
    const { sessionId } = req.params;
    const { method, params, id } = req.body;
    
    const session = sessions.get(sessionId);
    if (!session) {
        return res.status(404).json({ jsonrpc: '2.0', error: { code: -32000, message: 'Session not found' }, id });
    }
    
    try {
        let result = {};
        switch (method) {
            case 'initialize': // On garde initialize pour la compatibilité du protocole
                result = { protocolVersion: '2024-11-05', server: 'custom-playwright-v1' };
                break;
                
            case 'browser_navigate':
                await session.page.goto(params.url, { waitUntil: 'domcontentloaded', timeout: 30000 });
                result = { success: true, url: session.page.url() };
                break;
                
            case 'browser_snapshot':
                result = {
                    html: await session.page.content(),
                    title: await session.page.title(),
                    url: session.page.url(),
                    text: await session.page.evaluate(() => document.body?.innerText || '')
                };
                break;
            default:
                throw new Error(`Unknown method: ${method}`);
        }
        res.json({ jsonrpc: '2.0', result, id });
    } catch (error) {
        console.error(`[Session ${sessionId}] Error during method '${method}':`, error.message);
        res.status(500).json({ jsonrpc: '2.0', error: { code: -32603, message: error.message }, id });
    }
});

// Nettoyage des vieilles sessions toutes les minutes.
setInterval(async () => {
    const now = Date.now();
    for (const [id, session] of sessions.entries()) {
        if (now - session.created > 300000) { // 5 minutes
            try {
                await session.context.close(); // Ferme juste le contexte, pas le navigateur entier
                sessions.delete(id);
                console.log(`[Session ${id}] Cleaned up successfully.`);
            } catch (e) {
                console.error(`[Session ${id}] Error during cleanup:`, e);
                sessions.delete(id); // On la supprime même en cas d'erreur
            }
        }
    }
}, 60000);

app.listen(PORT, '0.0.0.0', () => {
    console.log(`[MCP Server] Custom Playwright server listening on port ${PORT}`);
});