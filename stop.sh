#!/usr/bin/env bash
# Stop the bot + model server started by start.sh.
# The codespace itself keeps running; your files, credentials and
# the agent's memory all stay untouched.
cd "$(dirname "$0")"
pkill -f "python.*bot\.py" 2>/dev/null && echo "bot stopped" || echo "bot was not running"
pkill -f "llama-server.*--port 8080" 2>/dev/null && echo "model server stopped" || echo "model server was not running"
sleep 1
if ! pgrep -f "python.*bot\.py" >/dev/null && ! pgrep -f "llama-server" >/dev/null; then
  echo "✅ everything is down"
fi
