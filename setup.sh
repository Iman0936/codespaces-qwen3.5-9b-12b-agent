#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# One-time setup — runs automatically ONCE when the codespace is created
# (postCreateCommand), or manually any time with:  bash setup.sh
#   1. asks for the Telegram bot token + your numeric user ID (first time only)
#   2. downloads the llama.cpp CPU binary (pinned) and the model (cached here)
#   3. installs the Python deps into .venv
# Everything lands inside this workspace and PERSISTS across stop/start,
# so later starts skip all of it and the bot comes up in seconds.
# ─────────────────────────────────────────────────────────────────────────────
set -e
cd "$(dirname "$0")"

# ======================= CONFIG — Qwen3.5 9B =======================
MODEL_URL="https://huggingface.co/HauhauCS/Qwen3.5-9B-Uncensored-HauhauCS-Aggressive/resolve/main/Qwen3.5-9B-Uncensored-HauhauCS-Aggressive-Q4_K_M.gguf"
MTP_URL=""
MODEL_ALIAS="qwen3.5-9b"
MODEL_CTX=131072
# The card (quoting the official Qwen authors) asks for at least 128K
# context to preserve thinking quality — the hybrid linear-attention
# architecture keeps the KV cache small, so 128K fits on CPU.
EXTRA_ARGS=""
# ====================================================================

MODEL_FILE="$(basename "${MODEL_URL%%\?*}")"
mkdir -p models llama

echo "==> [1/4] bot credentials (asked once, then never again)"
if [ ! -f secrets.env ] && [ -n "${BOT_TOKEN:-}" ] && [ -n "${OWNER_ID:-}" ]; then
  # Credentials arrived as environment variables (e.g. Codespaces secrets).
  if curl -s "https://api.telegram.org/bot${BOT_TOKEN}/getMe" | grep -q '"ok":true'; then
    printf 'BOT_TOKEN=%s\nOWNER_ID=%s\n' "$BOT_TOKEN" "$OWNER_ID" > secrets.env
    chmod 600 secrets.env
    echo "    credentials taken from environment ✅ — saved to secrets.env"
  fi
fi
if [ -f secrets.env ]; then
  echo "    secrets.env already exists — not asking again."
elif [ -t 0 ]; then
  # A lifecycle (postCreate) terminal may be a pty nobody can type into —
  # never block forever: 180s to answer, then setup CONTINUES without
  # credentials and the student runs this script once in a real terminal.
  read -t 180 -rp "    Telegram bot token (from BotFather): " BOT_TOKEN || BOT_TOKEN=""
  read -t 180 -rp "    Your numeric Telegram user ID (from @userinfobot): " OWNER_ID || OWNER_ID=""
  if [ -z "$BOT_TOKEN" ] || [ -z "$OWNER_ID" ]; then
    echo "    ⚠️ no credentials given (timed out) — continuing WITHOUT them."
    echo "       After creation finishes, run once in a terminal:  bash setup.sh && bash autostart.sh"
  elif ! curl -s "https://api.telegram.org/bot${BOT_TOKEN}/getMe" | grep -q '"ok":true'; then
    echo "    ❌ Telegram rejected that token — nothing saved. Re-run: bash setup.sh"
    exit 1
  else
    case "$OWNER_ID" in
      *[!0-9]*|"") echo "    ❌ user ID must be digits only — nothing saved. Re-run: bash setup.sh"; exit 1 ;;
    esac
    printf 'BOT_TOKEN=%s\nOWNER_ID=%s\n' "$BOT_TOKEN" "$OWNER_ID" > secrets.env
    chmod 600 secrets.env
    echo "    token OK ✅ — saved to secrets.env (never asked again)"
  fi
else
  echo "    ⚠️ no interactive terminal and no secrets.env — run once in a terminal:  bash setup.sh"
fi

if [ ! -f secrets.env ]; then
  REPO_DIR="$(pwd)"
  if ! grep -q "qwen-agent-first-run" ~/.bashrc 2>/dev/null; then
    cat >> ~/.bashrc << EOF_BASHRC
# qwen-agent-first-run
if [ ! -f "$REPO_DIR/secrets.env" ] && [ -t 0 ]; then
  echo "🤖 First run: the agent still needs its bot token + your numeric ID."
  bash "$REPO_DIR/setup.sh" && bash "$REPO_DIR/autostart.sh"
fi
EOF_BASHRC
    echo "    (first-terminal rescue armed: open any terminal if I could not ask for credentials)"
  fi
fi

echo "==> [2/4] llama.cpp binary (pinned b11878)"
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

echo "==> [3/4] model: $MODEL_FILE (cached in the workspace)"
if [ ! -s "models/$MODEL_FILE" ]; then
  curl -L -C - -o "models/$MODEL_FILE" "$MODEL_URL"
fi
ls -lh "models/$MODEL_FILE"
if [ -n "$MTP_URL" ] && [ ! -s "models/$(basename "$MTP_URL")" ]; then
  curl -L -C - -o "models/$(basename "$MTP_URL")" "$MTP_URL"
fi
cat > config.env << EOF_CFG
MODEL_PATH=models/$MODEL_FILE
MODEL_ALIAS=$MODEL_ALIAS
MODEL_CTX=$MODEL_CTX
EXTRA_ARGS="$EXTRA_ARGS"
EOF_CFG

echo "==> [4/4] Python deps (telegram + httpx + curl_cffi + playwright)"
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

# Fable-parity extras: tor for the prompt's anti-bot bypass rule and
# Chromium for Playwright. Both NON-FATAL — the bot works without them.
sudo apt-get update -qq > /dev/null 2>&1 || true
sudo apt-get install -y -qq --no-install-recommends tor > /dev/null 2>&1 || echo "    (tor install skipped — Tor bypass unavailable)"
if [ -x ./.venv/bin/playwright ]; then
  ( setsid ./.venv/bin/playwright install --with-deps chromium >> setup.log 2>&1 & ) || true
  echo "    tor ready; Chromium for Playwright installs in the background (setup.log)"
fi

echo ""
echo "✅ Setup done. From now on, every codespace start brings the bot up by itself."
