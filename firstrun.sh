#!/usr/bin/env bash
# First-run wizard: asks for the bot token + numeric owner ID ONCE in a
# real terminal (no timers), validates, saves to secrets.env, starts bot.
cd "$(dirname "$0")"
if [ -f secrets.env ]; then
  echo "secrets.env already exists — just starting the bot."
  bash autostart.sh
  exit 0
fi
echo "🤖 First run — two things, asked once and never again:"
read -rp "    Telegram bot token (from BotFather): " BOT_TOKEN
read -rp "    Your numeric Telegram user ID (from @userinfobot): " OWNER_ID
if [ -z "$BOT_TOKEN" ] || [ -z "$OWNER_ID" ]; then
  echo "    ❌ empty — nothing saved. Run again: bash firstrun.sh"; exit 1
fi
if ! curl -s "https://api.telegram.org/bot${BOT_TOKEN}/getMe" | grep -q '"ok":true'; then
  echo "    ❌ Telegram rejected that token — nothing saved. Run again: bash firstrun.sh"; exit 1
fi
case "$OWNER_ID" in
  *[!0-9]*|"") echo "    ❌ user ID must be digits only — nothing saved."; exit 1 ;;
esac
printf 'BOT_TOKEN=%s\nOWNER_ID=%s\n' "$BOT_TOKEN" "$OWNER_ID" > secrets.env
chmod 600 secrets.env
echo "    token OK ✅ — saved to secrets.env. Starting the bot..."
bash autostart.sh
