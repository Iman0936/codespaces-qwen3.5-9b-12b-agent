#!/usr/bin/env python3
"""
Telegram AGENT bot — model served by llama.cpp (llama-server) in this codespace.

- Agent, not a chatbot: native tool calling (shell, files, web fetch,
  send file, clock). The system prompt follows the Fable pattern: reason
  in English, speak Persian, tools are the model's hands, silent state
  files (memory.txt / auto_system_log.txt) fed back into the prompt,
  a worked example exchange, a Fable-style confirm gate on terminal
  commands, and a 3-strike anti-loop guard.
- Owner-only: it silently ignores everyone except OWNER_ID.
- Replies are STREAMED into Telegram by progressive edits (Daytona pattern):
  the thinking is VISIBLE while it streams, inside an expandable quote,
  and the answer streams right below it. Messages roll over at ~4000 chars
  (Telegram's limit) with confirmed-delivery bookkeeping, and every edit
  rides out Telegram's RetryAfter flood limits.
- A watchdog keeps llama-server alive (relaunches it via server_cmd.sh,
  but only when no server process exists — a loading model looks dead).
"""
import asyncio
import html
import json
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, RetryAfter
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ============================ CONFIG — Qwen3.5 9B =============================
MODEL_ALIAS = "qwen3.5-9b"
SAMPLING = {  # the card's recommended settings (official Qwen, thinking mode)
    "temperature": 0.6, "top_p": 0.95, "top_k": 20, "min_p": 0.0,
}
MAX_HISTORY_CHARS = 32000   # the server runs a 131072-token context
# =============================================================================

API_URL = "http://127.0.0.1:8080/v1/chat/completions"
HEALTH_URL = "http://127.0.0.1:8080/health"
BASE_DIR = Path(__file__).resolve().parent
WORK_DIR = BASE_DIR / "work"
WORK_DIR.mkdir(exist_ok=True)
HISTORY_FILE = BASE_DIR / "history.json"
MAX_TOKENS = 8192
MAX_STEPS = 20
BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])

histories: dict[int, list[dict]] = {}


def _load_histories() -> None:
    try:
        for cid, msgs in json.loads(HISTORY_FILE.read_text(encoding="utf-8")).items():
            histories[int(cid)] = msgs
    except Exception:
        pass


def _save_histories() -> None:
    try:
        HISTORY_FILE.write_text(json.dumps(histories, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


_load_histories()

# ── Fable-pattern state: three plain files the model itself maintains ────────
MEMORY_FILE = WORK_DIR / "memory.txt"
SYSLOG_FILE = WORK_DIR / "auto_system_log.txt"
for _f in (MEMORY_FILE, SYSLOG_FILE):
    _f.touch(exist_ok=True)

tool_error_streak: dict[int, int] = {}


def _tail(path: Path, max_chars: int) -> str:
    try:
        content = path.read_text(encoding="utf-8").strip()
    except Exception:
        return ""
    if len(content) > max_chars:
        return "...[مخفی شد]...\n" + content[-max_chars:]
    return content


def build_system_prompt() -> str:
    now_utc = datetime.now(ZoneInfo("UTC"))
    now_teh = now_utc.astimezone(ZoneInfo("Asia/Tehran"))
    prompt = (
        "You are a brilliant, highly analytical AI Agent with root terminal access "
        "inside a Linux GitHub Codespace. "
        "CRITICAL RULES:\n"
        "1. REASON IN ENGLISH, SPEAK IN PERSIAN: To maximize your analytical IQ, you MUST do all your deep reasoning, problem-solving, and planning in English STRICTLY inside your thinking. Your thinking must be 100% English — NEVER write Persian or any other language inside your thinking, not even a single word. If a thought starts forming in Persian, restart that thought in English. However, your final direct message to the user MUST be exclusively in beautifully formatted Persian (فارسی).\n"
        "2. ANSWERS COME FIRST, COMMANDS ARE OPTIONAL: You are a chat assistant who can also run commands on this machine. Most requests are answered directly in your reply — explanations, texts, and code the user wants to read all belong in the reply itself. Use the tools (run_command, read_file, list_files, fetch_url, send_file, get_datetime) ONLY when the task genuinely needs the machine: real files the user asked for as files, running or installing something, checking system state, fetching a page. 'Write me a code' is a request to READ code — answer in chat, never by creating a file.\n"
        "3. MAXIMIZE EFFICIENCY: Work fast. You are highly encouraged to emit MULTIPLE tool calls in a single response when they are independent, or chain shell commands using `&&`, to accomplish tasks in fewer steps.\n"
        "4. FILE OVERWRITE: Always use `>` to overwrite files, never `>>` for code (to avoid indentation issues). Save python code via `cat > script.py << 'EOF'`.\n"
        "5. ANTI-BOT & TOR BYPASS: If a website blocks you, use Tor (`socks5h://127.0.0.1:9050`) or `curl_cffi`. IMPORTANT: The Tor daemon is NOT running by default! You MUST start it first using run_command (`tor &`) and wait a few seconds before using the proxy.\n"
        "6. SILENT STATE MANAGEMENT: Update memory.txt in the work folder (via run_command: cat > memory.txt <<'EOF' ... EOF — that exact form runs silently, with no confirmation) whenever you learn important details, the current project state, or next steps — silently, without announcing it."
    )
    memory_data = _tail(MEMORY_FILE, 2000)
    if memory_data:
        prompt += f"\n\n--- PAST EXPERIENCES ---\n{memory_data}\n------------------------"
    log_data = _tail(SYSLOG_FILE, 1000)
    if log_data:
        prompt += f"\n\n--- AUTO SYSTEM LOGS ---\n{log_data}\n------------------------"
    prompt += "\n\nFINAL REMINDER: Think ONLY in English — no Persian inside the thinking, ever. Reply to the user ONLY in Persian."
    # The clock goes LAST on purpose: it changes every minute, and
    # anything placed after it would fall out of the server prompt cache.
    prompt += f"\nCurrent Time: {now_teh:%Y-%m-%d %H:%M} Tehran ({now_utc:%H:%M} UTC)."
    return prompt


# Fable's worked example, in native tool-calling form — primes the loop shape.
EXAMPLE = [
    {"role": "user", "content": "یک ریکوئست ساده به گوگل بفرست."},
    {"role": "assistant", "content": "<think>\nThe user wants a simple request sent to Google to check that it is reachable. I will call run_command with curl and keep the output short with head.\n</think>\n\nباشه، الان انجام میدم:",
     "tool_calls": [{"id": "call_0", "type": "function",
                     "function": {"name": "run_command",
                                  "arguments": json.dumps({"command": "curl -s https://google.com | head -n 5"})}}]},
    {"role": "tool", "tool_call_id": "call_0",
     "content": "<!doctype html><html><head><title>Google</title></head><body>...</body></html>"},
    {"role": "assistant", "content": "<think>\nThe curl call returned Google's homepage HTML, so the request worked and the site is reachable. I will confirm this briefly in Persian.\n</think>\n\nانجام شد ✔ گوگل پاسخ داد و در دسترسه."},
]


# A plain chat exchange that primes the thinking language: the thinking
# is fully in English, the reply is fully in Persian. Injected right
# after EXAMPLE on every request; never stored in the chat history.
THINK_EXAMPLE = [
    {"role": "user", "content": "پایتخت فرانسه کجاست؟"},
    {"role": "assistant", "content": "<think>\nThe user is asking for the capital of France. That is Paris. I will answer directly and briefly in Persian.\n</think>\n\nپایتخت فرانسه پاریسه."},
]


# Appended to the current user message in the REQUEST COPY only — the
# stored history keeps the user's clean text. Sitting immediately
# before generation, it is the strongest think-language lever, and no
# chat template can strip it.
THINK_SUFFIX = "\n\n(Think in English only — never in Persian inside your thinking. Reply in Persian only.)"


# A "write me code" exchange: content meant for the user's eyes goes in
# the reply itself (code in a code block), never through a tool.
CODE_EXAMPLE = [
    {"role": "user", "content": "یه کد پایتون بنویس که اعداد ۱ تا ۱۰ رو چاپ کنه."},
    {"role": "assistant", "content": "<think>\nThe user wants a Python code snippet to read in chat. This is content for the user's eyes, not a machine action, so I will put the code directly in my reply and not call any tool.\n</think>\n\nحتماً، این کد اعداد ۱ تا ۱۰ رو چاپ می‌کنه:\n\n```python\nfor i in range(1, 11):\n    print(i)\n```"},
]


def is_error_result(result: str) -> bool:
    return result.startswith("ERROR") or "[exit code:" in result


def log_command(command: str, is_error: bool) -> None:
    try:
        with open(SYSLOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] CMD: {command[:50]}... | Error: {is_error}")
    except Exception:
        pass

# ── tools ────────────────────────────────────────────────────────────────────

TOOLS = [
    {"type": "function", "function": {
        "name": "run_command",
        "description": "Run a shell command in the codespace and return its output.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string"}}, "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a text file inside the work folder.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "list_files",
        "description": "List files in the work folder (or a subfolder of it).",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "fetch_url",
        "description": "Fetch a web page or API URL and return its text content.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"}}, "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "send_file",
        "description": "Send a file from the work folder to the user in this chat.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "get_datetime",
        "description": "Get the current date and time (UTC and Tehran).",
        "parameters": {"type": "object", "properties": {}}}},
]


def _safe_path(path: str) -> Path:
    p = (WORK_DIR / path).resolve()
    if not str(p).startswith(str(WORK_DIR.resolve())):
        raise ValueError("path escapes the work folder")
    return p


async def exec_tool(name: str, args: dict, context, chat_id: int) -> str:
    loop = asyncio.get_running_loop()
    if name == "run_command":
        def _run():
            try:
                r = subprocess.run(args.get("command", ""), shell=True, cwd=WORK_DIR,
                                   capture_output=True, text=True, timeout=180)
                out = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else "")
                text = out.strip() or f"(no output, exit {r.returncode})"
                if r.returncode != 0:
                    text += f"\n[exit code: {r.returncode}]"
                return text[-6000:]
            except subprocess.TimeoutExpired:
                return "ERROR: command timed out after 180s"
            except Exception as e:
                return f"ERROR: {e}"
        return await loop.run_in_executor(None, _run)
    if name == "read_file":
        try:
            return _safe_path(args["path"]).read_text(encoding="utf-8", errors="replace")[:8000]
        except Exception as e:
            return f"ERROR: {e}"
    if name == "write_file":
        return ("ERROR: write_file no longer exists. Create the file with run_command "
                "using a heredoc (cat > path <<'EOF' ... EOF) — the code stays visible "
                "in the chat that way.")
    if name == "list_files":
        try:
            p = _safe_path(args.get("path", "."))
            items = [f.name + ("/" if f.is_dir() else f" ({f.stat().st_size}B)")
                     for f in sorted(p.iterdir())]
            return "\n".join(items) or "(empty)"
        except Exception as e:
            return f"ERROR: {e}"
    if name == "fetch_url":
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True,
                                         headers={"User-Agent": "Mozilla/5.0"}) as c:
                r = await c.get(args["url"])
                return f"HTTP {r.status_code}\n{r.text[:6000]}"
        except Exception as e:
            return f"ERROR: {e}"
    if name == "send_file":
        try:
            p = _safe_path(args["path"])
            with open(p, "rb") as f:
                await context.bot.send_document(chat_id=chat_id, document=f, filename=p.name)
            return f"file sent: {p.name}"
        except Exception as e:
            return f"ERROR: {e}"
    if name == "get_datetime":
        now = datetime.now(ZoneInfo("UTC"))
        teh = now.astimezone(ZoneInfo("Asia/Tehran"))
        return f"UTC: {now:%Y-%m-%d %H:%M %A} | Tehran: {teh:%Y-%m-%d %H:%M %A}"
    return f"ERROR: unknown tool {name}"


# ── streaming display ────────────────────────────────────────────────────────

THOUGHT_INLINE = re.compile(r"<\|channel>thought(.*?)(?:<channel\|>|$)", re.DOTALL)
TOOLCALL_TEXT = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)


def split_thought(content: str) -> tuple[str, str]:
    """Some templates stream the thought inline in content — split it out."""
    thought = "".join(THOUGHT_INLINE.findall(content))
    visible = THOUGHT_INLINE.sub("", content)
    return thought, visible


def compose(reasoning: str, content: str, final: bool = False) -> str:
    """What the user sees: thinking + the answer so far.

    While STREAMING, Telegram collapses an expandable quote again on every
    edit — so the live view is a plain quote holding only the TAIL of the
    thought. The full expandable quote lands in the final edit, where no
    more edits follow and it stays open when tapped."""
    parts = []
    r = reasoning.strip()
    if r:
        if final:
            parts.append(f"<blockquote expandable>🧠 {html.escape(r)}</blockquote>")
        else:
            tail = r[-350:]
            if len(r) > 350:
                tail = "…" + tail
            parts.append(f"<blockquote>🧠 {html.escape(tail)}</blockquote>")
    if content.strip():
        parts.append(html.escape(content.strip()))
    return "\n".join(parts) if parts else "🧠 دارم فکر می‌کنم..."


def _partial_json_str(args: str, key: str) -> str:
    """Decode a JSON string field from possibly-incomplete tool-call arguments."""
    m = re.search(r'"' + re.escape(key) + r'"\s*:\s*"', args)
    if not m:
        return ""
    out = []
    i = m.end()
    simple = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f",
              "n": "\n", "r": "\r", "t": "\t"}
    while i < len(args):
        ch = args[i]
        if ch == '"':
            break
        if ch == "\\":
            if i + 1 >= len(args):
                break
            esc = args[i + 1]
            if esc in simple:
                out.append(simple[esc])
                i += 2
                continue
            if esc == "u":
                hexpart = args[i + 2:i + 6]
                if len(hexpart) == 4 and all(c in "0123456789abcdefABCDEF" for c in hexpart):
                    out.append(chr(int(hexpart, 16)))
                    i += 6
                    continue
                break
            break
        out.append(ch)
        i += 1
    return "".join(out)


def _tool_preview(tool_calls: dict) -> str:
    """Live view of the code a tool call is generating right now."""
    if not tool_calls:
        return ""
    slot = tool_calls[max(tool_calls)]
    args = slot.get("arguments", "")
    body = _partial_json_str(args, "content") or _partial_json_str(args, "command")
    if not body:
        return ""
    head = f"🛠 <code>{html.escape(slot.get('name') or 'tool')}</code>"
    path = _partial_json_str(args, "path")
    if path:
        head += f" — <code>{html.escape(path)}</code>"
    tail = body[-1000:]
    if len(body) > 1000:
        tail = "…" + tail
    return f"{head}\n<pre>{html.escape(tail)}</pre>"


def _tool_counter(tool_calls: dict) -> str:
    """Fallback when the live preview would overflow one Telegram message."""
    if not tool_calls:
        return ""
    slot = tool_calls[max(tool_calls)]
    body = _partial_json_str(slot.get("arguments", ""), "content")
    if not body:
   	    return ""
    name = html.escape(slot.get("name") or "tool")
    return f"🛠 <code>{name}</code> — در حال تولید کد... ({len(body)} نویسه)"


def _calls_suffix(calls: list) -> str:
    """Persist the step's generated code in the chat record: file contents
    and the full text of terminal commands (heredocs included), so the code
    stays on screen after the step seals. Silent memory writes are skipped."""
    out = []
    for c in calls:
        try:
            a = json.loads(c.get("arguments") or "{}")
        except ValueError:
            continue
        if c.get("name") == "write_file" and (a.get("content") or ""):
            out.append(f"📄 <code>{html.escape(a.get('path', ''))}</code>\n{html.escape(a['content'])}")
        elif c.get("name") == "run_command":
            cmd = a.get("command") or ""
            if cmd and not _is_mem_write(cmd):
                out.append(f"🖥 <code>run_command</code>\n{html.escape(cmd)}")
    return ("\n\n" + "\n\n".join(out)) if out else ""


async def _edit(msg, text: str) -> bool:
    for _ in range(5):
        try:
            await msg.edit_text(text, parse_mode="HTML")
            return True
        except RetryAfter as e:
            await asyncio.sleep(min(e.retry_after + 1, 30))
        except BadRequest as e:
            return "not modified" in str(e).lower()
        except Exception:
            return False
    return False


async def _send(context, chat_id: int, text: str):
    for _ in range(5):
        try:
            return await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        except RetryAfter as e:
            await asyncio.sleep(min(e.retry_after + 1, 30))
        except Exception:
            return None
    return None


class StreamBox:
    """One logical reply that may span several Telegram messages."""

    def __init__(self, context, chat_id: int, first_msg):
        self.context, self.chat_id, self.current = context, chat_id, first_msg
        self.base = 0  # chars confirmed on screen in sealed messages

    async def show(self, full: str) -> None:
        while len(full) - self.base > 4000:
            if not await _edit(self.current, full[self.base:self.base + 4000]):
                return
            new_msg = await _send(self.context, self.chat_id, "…")
            if new_msg is None:
                return
            self.base += 4000
            self.current = new_msg
        tail = full[self.base:]
        if tail:
            await _edit(self.current, tail)

    async def seal_with_note(self, full: str, note: str) -> None:
        await self.show(full)
        await _send(self.context, self.chat_id, note)


# ── agent loop ───────────────────────────────────────────────────────────────


def _is_mem_write(cmd: str) -> bool:
    """True only for a pure memory.txt heredoc overwrite and nothing else:
    first line cat > memory.txt <<'EOF', last line EOF, and no earlier
    line equal to the tag — so nothing can be smuggled after the heredoc."""
    lines = (cmd or "").strip().splitlines()
    if len(lines) < 2:
        return False
    if not re.match(r"cat\s*>\s*(?:\./)?memory\.txt\s*<<\s*'?(EOF)'?\s*$", lines[0].strip()):
        return False
    return lines[-1].strip() == "EOF" and all(l.strip() != "EOF" for l in lines[1:-1])


def _all_mem_writes(cmd_texts: list) -> bool:
    return bool(cmd_texts) and all(_is_mem_write(c) for c in cmd_texts)

# A lone heredoc file-write: cat > file << 'EOF' ... EOF and nothing else.
HEREDOC_WRITE_RE = re.compile(r"cat\s*>\s*([^\s]+)\s*<<\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?\s*\n(.*?)\n\2\s*$", re.DOTALL)
# Explicit machine intent in the user's own words — never intercept these.
MACHINE_INTENT_RE = re.compile(r"بساز|ایجاد کن|ذخیره|اجرا|نصب کن|سیو|\bcreate\b|\bsave\b|\brun\b|\binstall\b|\bexecute\b", re.IGNORECASE)
# File types that make sense as reading material in chat.
CHAT_FILE_EXTS = {"py", "js", "ts", "jsx", "tsx", "java", "c", "h", "cpp", "hpp", "cs", "go", "rs", "php", "rb", "sh", "bash", "html", "css", "json", "yaml", "yml", "xml", "sql", "txt", "md"}


async def run_agent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    history = histories.setdefault(chat_id, [])
    history.append({"role": "user", "content": update.message.text})
    while len(history) > 2 and sum(len(m["content"]) for m in history) > MAX_HISTORY_CHARS:
        del history[:2]
    messages = list(history)

    system_prompt = build_system_prompt()
    first = await update.message.reply_text("🧠 دارم فکر می‌کنم...")
    box = StreamBox(context, chat_id, first)

    for _step in range(MAX_STEPS):
        reasoning, content = "", ""
        tool_calls: dict[int, dict] = {}
        last_edit, last_shown = 0.0, ""
        last_tp, last_tp_at, last_tp_args = "", 0.0, 0
        try:
            payload_messages = [{"role": "system", "content": system_prompt}] + EXAMPLE + THINK_EXAMPLE + CODE_EXAMPLE + list(messages)
            for _i in range(len(payload_messages) - 1, -1, -1):
                if payload_messages[_i].get("role") == "user":
                    _m = dict(payload_messages[_i])
                    _m["content"] = (_m.get("content") or "") + THINK_SUFFIX
                    payload_messages[_i] = _m
                    break
            payload = {"model": MODEL_ALIAS, "messages": payload_messages, "tools": TOOLS,
                       "max_tokens": MAX_TOKENS, "stream": True, **SAMPLING}
            async with httpx.AsyncClient(timeout=3600) as client:
                async with client.stream("POST", API_URL, json=payload) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            delta = json.loads(data)["choices"][0]["delta"]
                        except (ValueError, KeyError, IndexError):
                            continue
                        if delta.get("reasoning_content"):
                            reasoning += delta["reasoning_content"]
                        if delta.get("content"):
                            content += delta["content"]
                        for tc in delta.get("tool_calls") or []:
                            slot = tool_calls.setdefault(tc.get("index", 0),
                                                         {"name": "", "arguments": ""})
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                slot["name"] = fn["name"]
                            if fn.get("arguments"):
                                slot["arguments"] += fn["arguments"]
                        now = time.monotonic()
                        inline_thought, visible = split_thought(content)
                        preview = compose(reasoning + inline_thought, visible)
                        total_args = sum(len(t2["arguments"]) for t2 in tool_calls.values())
                        if tool_calls and (now - last_tp_at >= 3.5 or total_args - last_tp_args >= 150):
                            last_tp_at, last_tp_args = now, total_args
                            last_tp = _tool_preview(tool_calls)
                            if last_tp and len(preview) + len(last_tp) + 2 > 3900:
                                last_tp = _tool_counter(tool_calls)
                        if last_tp:
                            preview += "\n\n" + last_tp
                        if now - last_edit >= 1.5 and preview != last_shown:
                            last_edit, last_shown = now, preview
                            await box.show(preview)
        except Exception:
            pass  # stream broke — work with whatever arrived

        inline_thought, visible = split_thought(content)
        full_reasoning = reasoning + inline_thought

        # Text-format tool calls (fallback if the template didn't use the API).
        if not tool_calls:
            for m in TOOLCALL_TEXT.finditer(visible):
                try:
                    obj = json.loads(m.group(1))
                    tool_calls[len(tool_calls)] = {
                        "name": obj.get("name", ""),
                        "arguments": json.dumps(obj.get("arguments", {}))}
                    visible = visible.replace(m.group(0), "")
                except ValueError:
                    pass

        if tool_calls:
            calls = [tool_calls[i] for i in sorted(tool_calls)]
            messages.append({"role": "assistant", "content": visible,
                             "tool_calls": [{"id": f"call_{i}", "type": "function",
                                             "function": {"name": c["name"],
                                                          "arguments": c["arguments"]}}
                                            for i, c in enumerate(calls)]})
            names = "، ".join(f"<code>{html.escape(c['name'])}</code>" for c in calls)
            cmd_texts = []
            for c in calls:
                if c["name"] != "run_command":
                    continue
                try:
                    cmd_texts.append(json.loads(c["arguments"] or "{}").get("command", ""))
                except ValueError:
                    cmd_texts.append("")
            silent = _all_mem_writes(cmd_texts)
            # Chat delivery: on the first step, a lone heredoc write is
            # the model "writing" something for the user to READ. Unless
            # the user explicitly asked for machine work, hand the file's
            # content over in chat as the answer — no disk write, no
            # confirm gate. (Prompt rules alone failed at this twice;
            # the harness enforces it instead.)
            if _step == 0 and len(calls) == 1 and cmd_texts and not silent:
                _hw = HEREDOC_WRITE_RE.match(cmd_texts[0].strip())
                if _hw and not MACHINE_INTENT_RE.search(update.message.text or ""):
                    _fname = _hw.group(1).split("/")[-1]
                    _ext = _fname.rsplit(".", 1)[-1].lower() if "." in _fname else ""
                    if _fname != "memory.txt" and (not _ext or _ext in CHAT_FILE_EXTS):
                        _answer = f"📄 {_fname}\n\n{_hw.group(3).strip()}"
                        history.append({"role": "assistant", "content": _answer})
                        _save_histories()
                        await box.show(compose(full_reasoning, _answer, final=True))
                        return
            note = (f"🛠 در حال اجرا: {names}" if not cmd_texts or silent else
                    f"🛠 ابزارها آماده‌ان — دستور ترمینال منتظر تأیید توئه: {names}")
            await box.seal_with_note(compose(full_reasoning, visible, final=True) + _calls_suffix(calls), note)
            approved = True
            if cmd_texts and not silent:
                approved = await ask_confirm(context, chat_id, cmd_texts)
            if not approved:
                # The owner's cancel ends the turn on the spot. Nothing
                # has executed yet and nothing is fed back to the model,
                # so it cannot re-issue the command or improvise a
                # variation. The confirm message already reads
                # "🚫 اجرای دستور لغو شد." — that stays the last word.
                return
            for i, c in enumerate(calls):
                try:
                    args = json.loads(c["arguments"] or "{}")
                except ValueError:
                    args = {}
                result = await exec_tool(c["name"], args, context, chat_id)
                if c["name"] == "run_command":
                    log_command(args.get("command", ""), is_error_result(result))
                if is_error_result(result):
                    tool_error_streak[chat_id] = tool_error_streak.get(chat_id, 0) + 1
                else:
                    tool_error_streak[chat_id] = 0
                messages.append({"role": "tool", "tool_call_id": f"call_{i}",
                                 "content": result})
            if tool_error_streak.get(chat_id, 0) >= 3:
                tool_error_streak[chat_id] = 0
                await _send(context, chat_id,
                            "🛑 سیستم ضدحلقه فعال شد: ۳ خطای پیاپی از ابزار — این دور متوقف شد. آخرین خروجی‌ها رو ببین و قدم بعدی رو بگو.")
                return
            nxt = await _send(context, chat_id, "🧠 دارم فکر می‌کنم...")
            if nxt is None:
                break
            box = StreamBox(context, chat_id, nxt)
            continue

        answer = visible.strip()
        if not answer:
            history.pop()
            msg = ("فکر کردن کل بودجه‌ی توکن رو خورد و به جواب نرسید — سؤالت رو کوتاه‌تر بپرس."
                   if (full_reasoning or content) else
                   "سرور مدل جواب نداد. چند ثانیه صبر کن و دوباره بفرست.")
            await _edit(box.current, msg)
            return
        history.append({"role": "assistant", "content": answer})
        _save_histories()
        await box.show(compose(full_reasoning, answer, final=True))
        return

    await _edit(box.current, "به سقف گام‌های ایجنت رسیدم — کار رو به بخش‌های کوچک‌تر تقسیم کن.")


# ── telegram handlers ────────────────────────────────────────────────────────

# ── Fable's confirm gate, rebuilt for the live loop ──────────────────────────
# Terminal commands wait for the owner's ✅/❌ (buttons, or the words
# تایید/لغو). All other tools run immediately. A cancel ends the turn
# on the spot — nothing is executed and nothing is fed to the model.
PENDING_CONFIRMS: dict[int, dict] = {}
BUSY_CHATS: set[int] = set()
CONFIRM_TIMEOUT = 600


async def ask_confirm(context, chat_id: int, commands: list[str]) -> bool:
    event = asyncio.Event()
    slot: dict = {"event": event, "approved": False, "msg": None}
    PENDING_CONFIRMS[chat_id] = slot
    lines = ["⚠️ <b>درخواست دسترسی به ترمینال</b>", "",
             "مدل می‌خواهد این دستور(ها) را اجرا کند:"]
    for cmd in commands:
        lines.append(f"<code>{html.escape(cmd[:300])}</code>")
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ لغو", callback_data="confirm_no"),
        InlineKeyboardButton("✅ تایید", callback_data="confirm_ok"),
    ]])
    msg = await context.bot.send_message(chat_id=chat_id, text="\n".join(lines),
                                         parse_mode="HTML", reply_markup=keyboard)
    slot["msg"] = msg
    try:
        await asyncio.wait_for(event.wait(), timeout=CONFIRM_TIMEOUT)
    except asyncio.TimeoutError:
        slot["approved"] = False
        try:
            await msg.edit_text("⏱ زمان تأیید تمام شد — دستور لغو شد.")
        except Exception:
            pass
    PENDING_CONFIRMS.pop(chat_id, None)
    return slot["approved"]


async def on_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q is None:
        return
    await q.answer()
    if not q.from_user or q.from_user.id != OWNER_ID or q.message is None:
        return
    slot = PENDING_CONFIRMS.get(q.message.chat_id)
    if slot is None:
        return
    slot["approved"] = (q.data == "confirm_ok")
    try:
        await q.edit_message_text("✅ تأیید شد — در حال اجرا..."
                                  if slot["approved"] else "🚫 اجرای دستور لغو شد.")
    except Exception:
        pass
    slot["event"].set()


def is_owner(update: Update) -> bool:
    return bool(update.effective_user) and update.effective_user.id == OWNER_ID


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        return
    if update.effective_chat.id in BUSY_CHATS:
        await update.message.reply_text("⏳ صبر کن این دور تموم بشه.")
        return
    histories.pop(update.effective_chat.id, None)
    _save_histories()
    await update.message.reply_text("سلام! ایجنتت وصله 🤖 پیام بده، اگه لازم باشه خودش ابزار اجرا می‌کنه.")


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        return
    if update.effective_chat.id in BUSY_CHATS:
        await update.message.reply_text("⏳ صبر کن این دور تموم بشه.")
        return
    histories.pop(update.effective_chat.id, None)
    _save_histories()
    await update.message.reply_text("حافظه‌ی گفتگو پاک شد.")


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update) or not update.message or not update.message.text:
        return
    chat_id = update.effective_chat.id
    slot = PENDING_CONFIRMS.get(chat_id)
    if slot is not None and update.message.text.strip() in ("تایید", "تأیید", "لغو"):
        slot["approved"] = update.message.text.strip() != "لغو"
        if slot.get("msg") is not None:
            try:
                await slot["msg"].edit_text("✅ تأیید شد — در حال اجرا..."
                                            if slot["approved"] else "🚫 اجرای دستور لغو شد.")
            except Exception:
                pass
        slot["event"].set()
        return
    if chat_id in BUSY_CHATS:
        await update.message.reply_text("⏳ هنوز دارم روی پیام قبلیت کار می‌کنم — تموم که شد بعدی رو بفرست.")
        return
    BUSY_CHATS.add(chat_id)
    try:
        await run_agent(update, context)
    finally:
        BUSY_CHATS.discard(chat_id)


# ── server watchdog ──────────────────────────────────────────────────────────

async def watchdog(app: Application) -> None:
    fails = 0
    while True:
        await asyncio.sleep(60)
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                ok = (await c.get(HEALTH_URL)).status_code == 200
        except Exception:
            ok = False
        if ok:
            fails = 0
            continue
        fails += 1
        if fails < 2:
            continue
        fails = 0
        # Never launch a duplicate: a (re)loading model fails health checks.
        alive = subprocess.run(["pgrep", "-f", "llama-server"],
                               capture_output=True).returncode == 0
        cmd = BASE_DIR / "server_cmd.sh"
        if not alive and cmd.exists():
            subprocess.Popen(["setsid", "nohup", "bash", str(cmd)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


async def post_init(app: Application) -> None:
    asyncio.create_task(watchdog(app))  # app.create_task warns pre-run (PTB 21+)


def main() -> None:
    # Single-instance lock: two pollers on one token go to war over
    # getUpdates (Telegram 409) and split the user's messages between
    # them. A second copy of the bot exits at once, whoever started it.
    import fcntl
    _lock = open(BASE_DIR / "bot.lock", "w")
    try:
        fcntl.flock(_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("another bot instance is already running — exiting")
        return
    app = (Application.builder().token(BOT_TOKEN)
           .concurrent_updates(True).post_init(post_init).build())
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CallbackQueryHandler(on_confirm_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.run_polling()


if __name__ == "__main__":
    main()
