#!/bin/bash
# ============================================================
# Cortex — ngrok Webhook Setup Script
# Exposes localhost:7779 to the internet for webhook testing
# ============================================================

set -e

CORTEX_PORT=7779
NGROK_CONFIG="$HOME/.config/ngrok/ngrok.yml"

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║  Cortex Webhook Tunnel Setup                        ║"
echo "╚══════════════════════════════════════════════════════╝"

# ── Step 1: Check ngrok installed ────────────────────────────
if ! command -v ngrok &>/dev/null; then
    echo ""
    echo "📦 Installing ngrok..."
    if command -v brew &>/dev/null; then
        brew install ngrok/ngrok/ngrok
    else
        echo "❌ Homebrew not found. Install ngrok manually:"
        echo "   https://ngrok.com/download"
        exit 1
    fi
fi

echo "✓ ngrok found: $(ngrok version)"

# ── Step 2: Check server is running ──────────────────────────
echo ""
if curl -s http://127.0.0.1:$CORTEX_PORT/api/health &>/dev/null; then
    echo "✓ Cortex server is running on port $CORTEX_PORT"
else
    echo "⚠️  Cortex server not detected on port $CORTEX_PORT"
    echo "   Start it first: cd backend && uvicorn src.api:app --port 7779"
    echo ""
    read -p "Continue anyway? (y/n) " -n 1 -r
    echo
    [[ $REPLY =~ ^[Yy]$ ]] || exit 1
fi

# ── Step 3: Check auth token ──────────────────────────────────
if [ ! -f "$NGROK_CONFIG" ] || ! grep -q "authtoken" "$NGROK_CONFIG" 2>/dev/null; then
    echo ""
    echo "⚠️  ngrok auth token not configured."
    echo "   1. Sign up free at https://dashboard.ngrok.com"
    echo "   2. Copy your auth token"
    echo ""
    read -p "Paste your ngrok auth token: " NGROK_TOKEN
    if [ -n "$NGROK_TOKEN" ]; then
        ngrok config add-authtoken "$NGROK_TOKEN"
        echo "✓ Token saved"
    fi
fi

# ── Step 4: Start tunnel ─────────────────────────────────────
echo ""
echo "🚀 Starting ngrok tunnel on port $CORTEX_PORT..."
echo "   (Press Ctrl+C to stop)"
echo ""

# Start ngrok and capture the public URL
ngrok http $CORTEX_PORT --log=stdout --log-format=json 2>/dev/null &
NGROK_PID=$!

# Wait for tunnel to be ready
sleep 3

# Fetch public URL from ngrok API
PUBLIC_URL=$(curl -s http://127.0.0.1:4040/api/tunnels 2>/dev/null | \
    python3 -c "import sys,json; tunnels=json.load(sys.stdin).get('tunnels',[]); print(tunnels[0]['public_url'] if tunnels else '')" 2>/dev/null)

if [ -n "$PUBLIC_URL" ]; then
    echo "╔══════════════════════════════════════════════════════╗"
    echo "║  ✅ TUNNEL ACTIVE                                    ║"
    echo "╠══════════════════════════════════════════════════════╣"
    printf  "║  Public URL: %-39s║\n" "$PUBLIC_URL"
    echo "╠══════════════════════════════════════════════════════╣"
    echo "║  Webhook URLs to paste in each platform:            ║"
    printf  "║  Notion:   %-42s║\n" "$PUBLIC_URL/api/webhooks/notion"
    printf  "║  Slack:    %-42s║\n" "$PUBLIC_URL/api/webhooks/slack"
    printf  "║  GitHub:   %-42s║\n" "$PUBLIC_URL/api/webhooks/github"
    printf  "║  WhatsApp: %-42s║\n" "$PUBLIC_URL/api/webhooks/whatsapp"
    echo "╠══════════════════════════════════════════════════════╣"
    echo "║  ngrok Dashboard: http://127.0.0.1:4040             ║"
    echo "╚══════════════════════════════════════════════════════╝"

    # Save URL to .env
    ENV_FILE="$(dirname "$0")/.env"
    if [ -f "$ENV_FILE" ]; then
        # Remove old NGROK_URL line
        grep -v "^NGROK_PUBLIC_URL=" "$ENV_FILE" > "$ENV_FILE.tmp" && mv "$ENV_FILE.tmp" "$ENV_FILE"
    fi
    echo "NGROK_PUBLIC_URL=$PUBLIC_URL" >> "$ENV_FILE"
    echo ""
    echo "✓ Public URL saved to .env"
else
    echo "⚠️  Could not fetch public URL. Check http://127.0.0.1:4040"
fi

echo ""
echo "Tunnel running (PID: $NGROK_PID). Press Ctrl+C to stop."
wait $NGROK_PID
