#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Hands-free start — called by .devcontainer/devcontainer.json
# (postStartCommand) on EVERY codespace start. Starts llama-server and the
# Telegram bot DETACHED (setsid+nohup), so they keep running after this
# script returns and after every terminal closes. Safe to run repeatedly.
# ─────────────────────────────────────────────────────────────────────────────
cd "$(dirname "$0")"

if [ ! -f config.env ] || [ ! -f secrets.env ]; then
  echo "autostart: not set up yet — run once in a terminal:  bash setup.sh"
  exit 0
fi
source config.env

# Bot already running? Nothing to do. (Check by process pattern too —
# a stale or recycled pid in bot.pid must not mask a dead bot, and a
# live bot without a pid file must not be duplicated.)
if pgrep -f "python.*bot\.py" >/dev/null || { [ -f bot.pid ] && kill -0 "$(cat bot.pid)" 2>/dev/null; }; then
  echo "autostart: bot already running (pid $(cat bot.pid))"
  exit 0
fi

# The exact server launch line lives in server_cmd.sh — the bot's watchdog
# re-uses this same file to resurrect the server if it ever dies.
write_server_cmd() {
  cat > server_cmd.sh <<EOF
#!/usr/bin/env bash
cd "$PWD"
exec ./llama/llama-server -m "$MODEL_PATH" --jinja -c $MODEL_CTX -t $(nproc) \\
  --cache-type-k q8_0 --cache-type-v q8_0 $1 \\
  --host 127.0.0.1 --port 8080 --alias $MODEL_ALIAS >> server.log 2>&1
EOF
  chmod +x server_cmd.sh
}

wait_health() {  # $1 = pid to death-check; returns 0 when healthy
  for i in $(seq 1 90); do
    code="$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/health || true)"
    [ "$code" = "200" ] && return 0
    kill -0 "$1" 2>/dev/null || return 1
    sleep 2
  done
  return 1
}

code="$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/health || true)"
if [ "$code" != "200" ]; then
  pkill -f "llama-server.*--port 8080" 2>/dev/null || true
  sleep 1
  write_server_cmd "$EXTRA_ARGS"
  setsid nohup bash server_cmd.sh >/dev/null 2>&1 &
  echo $! > server.pid
  echo "autostart: model server starting (pid $(cat server.pid)) — waiting for it to load..."
  if ! wait_health "$(cat server.pid)"; then
    if [ -n "$EXTRA_ARGS" ]; then
      echo "autostart: launch with extra args died — retrying plain (see server.log)"
      pkill -f "llama-server.*--port 8080" 2>/dev/null || true
      sleep 1
      write_server_cmd ""
      setsid nohup bash server_cmd.sh >/dev/null 2>&1 &
      echo $! > server.pid
      wait_health "$(cat server.pid)" || { echo "autostart: server DIED — last server.log lines:"; tail -20 server.log; exit 1; }
    else
      echo "autostart: server DIED — last server.log lines:"; tail -20 server.log; exit 1
    fi
  fi
fi
echo "autostart: model server is up."

set -a; source secrets.env; set +a
if [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY=python3; fi
setsid nohup "$PY" bot.py > bot.log 2>&1 &
echo $! > bot.pid
echo "autostart: Telegram bot started detached (pid $(cat bot.pid)) — watch it with: tail -f bot.log"
