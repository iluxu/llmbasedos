#!/bin/bash
# build-playwright-mcp.sh

set -e

echo "🔨 Construction de l'image Playwright MCP..."

# Créer le répertoire pour les fichiers Playwright MCP
mkdir -p playwright-mcp

# Créer le Dockerfile pour Playwright MCP
cat > playwright-mcp/Dockerfile << 'EOF'
FROM mcr.microsoft.com/playwright:v1.40.0-focal

# Installation des dépendances Node.js
WORKDIR /app

RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Initialiser le projet Node.js
RUN npm init -y && \
    npm install express playwright uuid

# Copier le serveur MCP
COPY mcp-server.js /app/mcp-server.js

# Port exposé
EXPOSE 5678

# Health check
HEALTHCHECK --interval=5s --timeout=10s --retries=3 \
    CMD curl -f http://localhost:5678/health || exit 1

# Commande de démarrage
CMD ["node", "/app/mcp-server.js"]
EOF

# Créer le serveur MCP JavaScript (version simplifiée)
cat > playwright-mcp/mcp-server.js << 'EOF'
const express = require('express');
const { chromium } = require('playwright');
const { v4: uuidv4 } = require('uuid');

const app = express();
app.use(express.json());

const PORT = process.env.PORT || 5678;
const sessions = new Map();

// Health check
app.get('/health', (req, res) => {
    res.json({ status: 'ok', sessions: sessions.size });
});

// SSE endpoint
app.get('/sse', (req, res) => {
    res.setHeader('Content-Type', 'text/event-stream');
    res.setHeader('Cache-Control', 'no-cache');
    
    const sessionId = uuidv4();
    const sessionPath = `/session/${sessionId}`;
    
    // Créer la session browser de manière asynchrone
    (async () => {
        try {
            const browser = await chromium.launch({
                headless: true,
                args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage']
            });
            const context = await browser.newContext({
                viewport: { width: 1920, height: 1080 },
                userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            });
            const page = await context.newPage();
            
            sessions.set(sessionId, { 
                browser, 
                context, 
                page,
                created: Date.now()
            });
            
            console.log(`Session ${sessionId} created`);
        } catch (error) {
            console.error(`Failed to create session ${sessionId}:`, error);
        }
    })();
    
    res.write(`data: ${sessionPath}\n\n`);
    res.end();
});

// Session handler
app.post('/session/:sessionId', async (req, res) => {
    const { sessionId } = req.params;
    const { method, params, id } = req.body;
    
    console.log(`Session ${sessionId}: ${method}`);
    
    const session = sessions.get(sessionId);
    if (!session) {
        return res.status(404).json({
            jsonrpc: '2.0',
            error: { code: -32000, message: 'Session not found' },
            id
        });
    }
    
    try {
        let result = {};
        
        switch (method) {
            case 'initialize':
                result = {
                    protocolVersion: '2024-11-05',
                    capabilities: { browser: true }
                };
                break;
                
            case 'browser_navigate':
                await session.page.goto(params.url, {
                    waitUntil: 'domcontentloaded',
                    timeout: 30000
                });
                result = { success: true };
                break;
                
            case 'browser_snapshot':
                const [content, title, url, text] = await Promise.all([
                    session.page.content(),
                    session.page.title(),
                    session.page.url(),
                    session.page.evaluate(() => document.body?.innerText || '')
                ]);
                
                result = { html: content, title, url, text };
                break;
                
            default:
                throw new Error(`Unknown method: ${method}`);
        }
        
        res.json({ jsonrpc: '2.0', result, id });
        
    } catch (error) {
        console.error(`Error in ${method}:`, error);
        res.json({
            jsonrpc: '2.0',
            error: { code: -32603, message: error.message },
            id
        });
    }
});

// Cleanup old sessions
setInterval(async () => {
    const now = Date.now();
    for (const [id, session] of sessions.entries()) {
        if (now - session.created > 300000) { // 5 minutes
            try {
                await session.browser.close();
                sessions.delete(id);
                console.log(`Cleaned up session ${id}`);
            } catch (e) {
                console.error(`Error cleaning session ${id}:`, e);
            }
        }
    }
}, 60000);

app.listen(PORT, '0.0.0.0', () => {
    console.log(`Playwright MCP server running on port ${PORT}`);
});
EOF

# Construire l'image
echo "📦 Construction de l'image Docker..."
cd playwright-mcp
docker build -t playwright-mcp:latest .
cd ..

echo "✅ Image playwright-mcp:latest construite avec succès!"

# Optionnel : Nettoyer les anciens containers
echo "🧹 Nettoyage des anciens containers Playwright..."
docker ps -a --filter "label=llmbasedos.playwright=true" -q | xargs -r docker rm -f 2>/dev/null || true

echo "🚀 Prêt à utiliser! L'image playwright-mcp:latest est disponible."
echo ""
echo "Pour tester manuellement:"
echo "  docker run --rm -p 5678:5678 --name test-playwright playwright-mcp:latest"
echo "  curl http://localhost:5678/health"
