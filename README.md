# Qwen3.5-9B Telegram Agent — GitHub Codespaces

A personal AI **agent** on Telegram, powered by a local
Qwen3.5-9B (Uncensored HauhauCS Aggressive, Q4_K_M) served by
llama.cpp on a free GitHub Codespace (4 cores / 16 GB).
The bot answers **only its owner**, calls tools (shell, files, web
fetch), thinks before it acts, and keeps a persistent memory of its
own experience in `work/memory.txt`.

Nothing here runs automatically. There are no lifecycle hooks:
you open the codespace and start everything with **one command**.

## Run it

1. Create a codespace on `main` (Code → Codespaces → Create).
   It opens plain and empty — that's expected.
2. Open a terminal in the codespace and run:

   ```bash
   bash start.sh
   ```

3. **First run only:** it downloads llama.cpp and the model
   (~5.6 GB, once), installs the Python deps, and asks for your
   **bot token** (from BotFather) and your **numeric Telegram user
   ID** (from @userinfobot). Both are validated and saved to
   `secrets.env` — never asked again.
4. When it prints `bot is up ✅`, message your bot on Telegram.

## Every later start

Open the codespace, then:

```bash
bash start.sh
```

Model, credentials and memory are all cached in the workspace, so
this takes about a minute and asks nothing.

## Stop

```bash
bash stop.sh
```

stops the bot and the model server. Stopping the codespace itself
also works — **everything persists** (model cache, `secrets.env`,
chat history, the agent's `work/` memory). Only *deleting* the
codespace wipes it.

## Files

| File | What it is |
|---|---|
| `start.sh` | The one command: setup (first time) + start |
| `stop.sh` | Stops bot + model server |
| `bot.py` | The agent (Telegram, tools, streaming, memory) |
| `models/` | Model cache (downloaded once) |
| `work/` | The agent's memory: `memory.txt` (its own notes) + `auto_system_log.txt` (every command it runs) |
| `secrets.env` | Your token + ID (gitignored, never committed) |
| `server.log` / `bot.log` | Runtime logs — `tail -f bot.log` to watch it work |
