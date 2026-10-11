#!/bin/sh
# Voice agent for local development, in one command: the voice server, a public tunnel to it (Retell can't reach
# localhost), and the Retell agent pointed at the tunnel's new URL. Ctrl-C stops everything.
# Needs RETELL_API_KEY in backend/.env, and cloudflared (brew install cloudflared).
# It must read the same database as the app: run it with the app's DB_PATH (e.g. DB_PATH=demo.db ./voice_dev.sh),
# or callers' tracking links and orders won't be found.
cd "$(dirname "$0")" || exit 1
PORT=${VOICE_PORT:-8002}
echo "Voice agent reads ${DB_PATH:-mill.db} (the app must use the same DB_PATH)."
python3 -m uvicorn voice_app:app --port "$PORT" &
SERVER=$!
LOG=$(mktemp)
cloudflared tunnel --no-autoupdate --url "http://localhost:$PORT" >"$LOG" 2>&1 &
TUNNEL=$!
trap 'kill $SERVER $TUNNEL 2>/dev/null; rm -f "$LOG"; exit' INT TERM EXIT

for _ in $(seq 60); do
  URL=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$LOG" | head -1)
  [ -n "$URL" ] && break
  sleep 1
done
[ -n "$URL" ] || { echo "cloudflared did not give a tunnel URL:"; cat "$LOG"; exit 1; }
sleep 5  # a new quick tunnel takes a few seconds to answer
python3 voice_setup.py "$URL" || exit 1
echo
echo "Voice agent is live through $URL"
echo "Open http://localhost:5173/call.html (or Tracking > an order > BUYER'S CALL PAGE). Ctrl-C here to stop."
wait $SERVER
