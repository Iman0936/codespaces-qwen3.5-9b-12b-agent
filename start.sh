#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# THE one command:   bash start.sh
#
# Nothing in this project runs automatically — no postCreate, no
# postStart, no terminal hooks. You open the codespace, run this once,
# and it does — in front of you, in this terminal — whatever is still
# missing, then starts the model server + the bot:
#   1. bot token + your numeric ID   (asked ONCE, saved in secrets.env)
#   2. llama.cpp binary              (downloaded once, then cached)
#   3. the model                     (downloaded once, cached in models/)
#   4. Python deps                   (installed once, into .venv)
#   5. server + bot                  (started detached — they keep
#                                     running when you close the terminal)
# Run it again any time: everything already done is skipped in seconds.
# Stop the processes with:   bash stop.sh    (or just stop the codespace —
# files, model cache, credentials and the agent's memory all persist).
# ─────────────────────────────────────────────────────────────────────────────
cd "$(dirname "$0")"

# ======================= CONFIG — Qwen3.5 9B =======================
MODEL_URL="https://huggingface.co/HauhauCS/Qwen3.5-9B-Uncensored-HauhauCS-Aggressive/resolve/main/Qwen3.5-9B-Uncensored-HauhauCS-Aggressive-Q4_K_M.gguf"
MODEL_ALIAS="qwen3.5-9b"
MODEL_CTX=131072
# The card (quoting the official Qwen authors) asks for at least 128K
# context to preserve thinking quality — the hybrid linear-attention
# architecture keeps the KV cache small, so 128K fits on CPU.
EXTRA_ARGS=""
# ====================================================================

MODEL_FILE="$(basename "${MODEL_URL%%\?*}")"
mkdir -p models llama

echo "==> [1/5] bot credentials"
if [ -f secrets.env ]; then
  echo "    secrets.env present ✅ (token + ID already saved — not asking again)"
else
  # Optional route: credentials supplied as Codespaces secrets / env vars.
  if [ -n "${BOT_TOKEN:-}" ] && [ -n "${OWNER_ID:-}" ]; then
    if curl -s "https://api.telegram.org/bot${BOT_TOKEN}/getMe" | grep -q '"ok":true'; then
      printf 'BOT_TOKEN=%s\nOWNER_ID=%s\n' "$BOT_TOKEN" "$OWNER_ID" > secrets.env
      chmod 600 secrets.env
      echo "    credentials taken from environment ✅ — saved to secrets.env"
    fi
  fi
fi
if [ ! -f secrets.env ]; then
  echo "    First run — two things, asked once and never again:"
  read -rp "    Telegram bot token (from BotFather): " BOT_TOKEN
  read -rp "    Your numeric Telegram user ID (from @userinfobot): " OWNER_ID
  if [ -z "$BOT_TOKEN" ] || [ -z "$OWNER_ID" ]; then
    echo "    ❌ empty — nothing saved. Run again: bash start.sh"; exit 1
  fi
  if ! curl -s "https://api.telegram.org/bot${BOT_TOKEN}/getMe" | grep -q '"ok":true'; then
    echo "    ❌ Telegram rejected that token — nothing saved. Run again: bash start.sh"; exit 1
  fi
  case "$OWNER_ID" in
    *[!0-9]*|"") echo "    ❌ user ID must be digits only — nothing saved."; exit 1 ;;
  esac
  printf 'BOT_TOKEN=%s\nOWNER_ID=%s\n' "$BOT_TOKEN" "$OWNER_ID" > secrets.env
  chmod 600 secrets.env
  echo "    token OK ✅ — saved to secrets.env"
fi

echo "==> [2/5] llama.cpp binary (pinned b11878)"
if [ ! -x llama/llama-server ]; then
  ASSET_URL="https://github.com/ggml-org/llama.cpp/releases/download/b11878/llama-b11878-bin-ubuntu-x64.tar.gz"
  mkdir -p llama/extract
  if curl -fL -o llama/pkg "$ASSET_URL" && tar -xzf llama/pkg -C llama/extract 2>/dev/null; then
    BIN_PATH="$(find llama/extract -name llama-server -type f | head -1)"
    [ -n "$BIN_PATH" ] && cp -r "$(dirname "$BIN_PATH")"/. llama/
    chmod +x llama/llama-server 2>/dev/null || true
  fi
  rm -rf llama/extract llama/pkg
fi
if [ ! -x llama/llama-server ]; then
  echo "    prebuilt download failed — building from source (slower)"
  git clone --depth 1 https://github.com/ggml-org/llama.cpp llama-src
  cmake -B llama-src/build -S llama-src -DCMAKE_BUILD_TYPE=Release > /dev/null
  cmake --build llama-src/build -j "$(nproc)" --target llama-server
  cp llama-src/build/bin/llama-server llama/
  cp llama-src/build/bin/*.so llama/ 2>/dev/null || true
fi
echo "    OK: $(./llama/llama-server --version 2>/dev/null | head -1 || echo 'binary ready')"

echo "==> [3/5] model: $MODEL_FILE (cached in the workspace)"
if [ ! -s "models/$MODEL_FILE" ]; then
  curl -L -C - -o "models/$MODEL_FILE" "$MODEL_URL"
else
  echo "    already downloaded ✅ — using the cache"
fi
ls -lh "models/$MODEL_FILE"
cat > config.env << EOF_CFG
MODEL_PATH=models/$MODEL_FILE
MODEL_ALIAS=$MODEL_ALIAS
MODEL_CTX=$MODEL_CTX
EXTRA_ARGS="$EXTRA_ARGS"
EOF_CFG

echo "==> [4/5] Python deps (telegram + httpx + curl_cffi + playwright)"
if [ -x .venv/bin/python ] && .venv/bin/python -c "import telegram, httpx, curl_cffi, playwright" 2>/dev/null; then
  echo "    .venv ready ✅"
else
  # The slim devcontainers base image ships python3 WITHOUT pip/venv —
  # install them first or this step fails with "pip3: command not found".
  if ! python3 -c "import venv, pip" 2>/dev/null; then
    echo "    installing python3-venv + python3-pip via apt..."
    sudo apt-get update -qq && sudo apt-get install -y -qq python3-venv python3-pip || true
  fi
  if python3 -m venv .venv 2>/dev/null; then
    ./.venv/bin/pip install -q --upgrade pip
    ./.venv/bin/pip install -q -r requirements.txt
  else
    pip3 install -q --break-system-packages -r requirements.txt || pip3 install -q -r requirements.txt
  fi
  echo "    deps installed ✅"
fi
# Fable-parity extras: tor for the prompt's anti-bot bypass rule and
# Chromium for Playwright. Both NON-FATAL — the bot works without them.
if ! command -v tor >/dev/null 2>&1; then
  sudo apt-get update -qq > /dev/null 2>&1 || true
  sudo apt-get install -y -qq --no-install-recommends tor > /dev/null 2>&1 || echo "    (tor install skipped — Tor bypass unavailable)"
fi
if [ -x ./.venv/bin/playwright ] && [ ! -d "$HOME/.cache/ms-playwright" ]; then
  ( setsid ./.venv/bin/playwright install --with-deps chromium >> playwright-install.log 2>&1 & ) || true
  echo "    Chromium for Playwright installs in the background (playwright-install.log)"
fi

echo "==> [5/5] model server + bot"
source config.env

# Already running? Nothing to do. (Process pattern only — a pid file's
# number can collide with an unrelated process after a container
# restart, so it is never trusted as proof of life.)
if pgrep -f "python.*bot\.py" >/dev/null; then
  echo "    ✅ already running — the bot is up."
  echo "       Restart it with:  bash stop.sh && bash start.sh"
  exit 0
fi

# The exact server launch line lives in server_cmd.sh — the bot's
# watchdog re-uses this same file to resurrect the server if it dies.
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
    [ $((i % 5)) -eq 0 ] && echo "    … loading the model ($((i * 2))s)"
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
  echo "    model server starting (pid $(cat server.pid)) — waiting for it to load..."
  if ! wait_health "$(cat server.pid)"; then
    if [ -n "$EXTRA_ARGS" ]; then
      echo "    launch with extra args died — retrying plain (see server.log)"
      pkill -f "llama-server.*--port 8080" 2>/dev/null || true
      sleep 1
      write_server_cmd ""
      setsid nohup bash server_cmd.sh >/dev/null 2>&1 &
      echo $! > server.pid
      wait_health "$(cat server.pid)" || { echo "    ❌ server DIED — last server.log lines:"; tail -20 server.log; exit 1; }
    else
      echo "    ❌ server DIED — last server.log lines:"; tail -20 server.log; exit 1
    fi
  fi
fi
echo "    model server is up ✅"

set -a; source secrets.env; set +a
if [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY=python3; fi
setsid nohup "$PY" bot.py > bot.log 2>&1 &
echo $! > bot.pid
sleep 3
if pgrep -f "python.*bot\.py" >/dev/null; then
  echo "    🤖 bot is up ✅ — open Telegram and message it."
  echo "       Watch it here:  tail -f bot.log"
else
  echo "    ❌ bot died at start — last bot.log lines:"; tail -20 bot.log; exit 1
fi
