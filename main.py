# main.py
# Single-file: Telegram Bot + Flask Web + SQLite + Monetization + AI (OpenAI + HF) + Developer Agent + Scheduler
# Python 3.11+ recommended. Works with python-telegram-bot == 21.x

import os
import sys
import json
import time
import uuid
import queue
import base64
import random
import logging
import sqlite3
import threading
import datetime as dt
from functools import wraps
from urllib.parse import urlparse, urljoin

from flask import Flask, request, jsonify, make_response, redirect, url_for, Response

# Telegram PTB 21.x
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    constants,
)
from telegram.ext import (
    ApplicationBuilder,
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# Schedules
from apscheduler.schedulers.background import BackgroundScheduler

# AI: OpenAI (1.x) – optional
OPENAI_OK = True
try:
    from openai import OpenAI
except Exception:
    OPENAI_OK = False

# HTTP client
import httpx

# -------------------------
# Config & Logging
# -------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("EconomyBot")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
HUGGINGFACE_API_URL = os.getenv("HUGGINGFACE_API_URL", "https://candyai.com/artificialagents").strip()
HUGGINGFACE_API_TOKEN = os.getenv("HUGGINGFACE_API_TOKEN", "").strip()
DOMAIN = os.getenv("DOMAIN", os.getenv("RENDER_EXTERNAL_URL", "")).strip() or "http://localhost:5000"
PORT = int(os.getenv("PORT", "5000"))
SECRET_TOKEN = os.getenv("SECRET_TOKEN", "super-secret-token-change-me").strip()
ADMIN_USER_ID = os.getenv("ADMIN_USER_ID", "").strip()

if not TELEGRAM_BOT_TOKEN:
    log.error("TELEGRAM_BOT_TOKEN missing in environment.")
    sys.exit(1)

# -------------------------
# DB (SQLite)
# -------------------------
DB_PATH = os.getenv("DB_PATH", "bot.db")

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def db_init():
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id TEXT PRIMARY KEY,
            username TEXT,
            first_seen TIMESTAMP,
            last_seen TIMESTAMP,
            visits INTEGER DEFAULT 0,
            tokens_used INTEGER DEFAULT 0
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS treasury (
            id INTEGER PRIMARY KEY CHECK(id=1),
            earned REAL DEFAULT 0,
            spent REAL DEFAULT 0
        )
    """)
    cur.execute("""
        INSERT OR IGNORE INTO treasury (id, earned, spent) VALUES (1, 0, 0)
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS impressions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TIMESTAMP,
            ip TEXT,
            path TEXT,
            ref TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TIMESTAMP,
            level TEXT,
            message TEXT
        )
    """)
    conn.commit()
    conn.close()
    log.info("DB ready (schema ensured).")

def log_event(level: str, message: str):
    try:
        conn = db()
        conn.execute(
            "INSERT INTO logs (ts, level, message) VALUES (?, ?, ?)",
            (dt.datetime.utcnow(), level, message[:1000])
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning(f"Failed to log event: {e}")

def treasury_add(amount: float):
    conn = db()
    conn.execute("UPDATE treasury SET earned = earned + ? WHERE id=1", (amount,))
    conn.commit()
    conn.close()

def treasury_spend(amount: float):
    conn = db()
    conn.execute("UPDATE treasury SET spent = spent + ? WHERE id=1", (amount,))
    conn.commit()
    conn.close()

def treasury_get():
    conn = db()
    row = conn.execute("SELECT earned, spent FROM treasury WHERE id=1").fetchone()
    conn.close()
    if not row:
        return 0.0, 0.0
    return float(row["earned"]), float(row["spent"])

def user_touch(chat_id: int, username: str | None):
    conn = db()
    now = dt.datetime.utcnow()
    conn.execute("""
        INSERT INTO users (chat_id, username, first_seen, last_seen)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET username=excluded.username, last_seen=excluded.last_seen
    """, (str(chat_id), username, now, now))
    conn.commit()
    conn.close()

def user_add_visit():
    # Called from web landing via cookie/IP
    conn = db()
    # Use special row for "-1" to track anonymous visits as well
    conn.execute("""
        INSERT INTO users (chat_id, username, first_seen, last_seen, visits)
        VALUES ('-1', 'anonymous', ?, ?, 1)
        ON CONFLICT(chat_id) DO UPDATE SET visits = visits + 1, last_seen = excluded.last_seen
    """, (dt.datetime.utcnow(), dt.datetime.utcnow()))
    conn.commit()
    conn.close()

# -------------------------
# Monetization rules
# -------------------------
VISIT_EARN = float(os.getenv("EARN_PER_VISIT", "0.002"))           # $ per site visit
MESSAGE_EARN = float(os.getenv("EARN_PER_MESSAGE", "0.001"))       # $ per message handled
AD_IMPRESSION_EARN = float(os.getenv("EARN_PER_AD", "0.0005"))     # $ per ad tag view

# -------------------------
# AI Clients (OpenAI + HF)
# -------------------------
OAI_CLIENT = None
if OPENAI_OK and OPENAI_API_KEY:
    try:
        OAI_CLIENT = OpenAI(api_key=OPENAI_API_KEY)
        log.info("OpenAI client initialized.")
    except Exception as e:
        log.warning(f"OpenAI init failed: {e}")
else:
    log.info("OpenAI client will be disabled (no key or lib).")

HTTP_TIMEOUT = 60

def ai_complete(prompt: str, system: str | None = None, max_tokens: int = 600, temperature: float = 0.6) -> str:
    """
    First tries OpenAI responses; if unavailable, falls back to HuggingFace endpoint.
    """
    # Try OpenAI
    if OAI_CLIENT:
        try:
            msg = [{"role": "user", "content": prompt}]
            if system:
                msg.insert(0, {"role": "system", "content": system})
            # Use a sensible default model name; can be overridden via env MODEL
            model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
            resp = OAI_CLIENT.chat.completions.create(
                model=model,
                messages=msg,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            out = resp.choices[0].message.content or ""
            if out.strip():
                return out.strip()
        except Exception as e:
            log.warning(f"OpenAI failed, will fallback. {e}")

    # Fallback to "HuggingFace"-style (here your custom URL)
    try:
        headers = {
            "Authorization": f"Bearer {HUGGINGFACE_API_TOKEN}",
            "Content-Type": "application/json",
        }
        payload = {
            "inputs": prompt,
            "parameters": {
                "max_new_tokens": max_tokens,
                "temperature": temperature,
                "return_full_text": False
            }
        }
        with httpx.Client(timeout=HTTP_TIMEOUT) as client:
            r = client.post(HUGGINGFACE_API_URL, headers=headers, json=payload)
            r.raise_for_status()
            data = r.json()
            # Try common response shapes
            if isinstance(data, list) and data and "generated_text" in data[0]:
                return (data[0]["generated_text"] or "").strip()
            if "generated_text" in data:
                return (data["generated_text"] or "").strip()
            if "text" in data:
                return (data["text"] or "").strip()
            # Fallback stringify
            return str(data)[:4000]
    except Exception as e:
        log.error(f"HuggingFace fallback failed: {e}")
        return "⚠️ AI backend currently unavailable. Please try again."

# -------------------------
# Developer Agent (code generator)
# -------------------------
AGENT_SYSTEM = (
    "You are a senior software engineer. Generate production-grade, well-commented code. "
    "Prefer Python/JS unless user specifies. Include setup/run notes succinctly."
)

def generate_code(spec: str) -> str:
    prompt = f"""
You are a full-stack agent. Based on the user's request, produce a working solution.

User spec:
\"\"\"
{spec}
\"\"\"

Deliver:
1) Short plan
2) Complete code blocks (minimal but runnable)
3) Quick run instructions
"""
    return ai_complete(prompt, system=AGENT_SYSTEM, max_tokens=900, temperature=0.4)

# -------------------------
# Telegram Handlers
# -------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_touch(user.id, user.username)
    treasury_add(MESSAGE_EARN)

    keyboard = [
        [InlineKeyboardButton("🧠 Ask AI", callback_data="ask_ai"),
         InlineKeyboardButton("👨‍💻 Generate Code", callback_data="gen_code")],
        [InlineKeyboardButton("💰 Balance", callback_data="balance"),
         InlineKeyboardButton("🌐 Visit Site", url=DOMAIN)],
    ]
    await update.message.reply_html(
        "👋 <b>Welcome!</b>\n"
        "I’m your all-in-one AI assistant + developer agent.\n\n"
        "Type your question, or use /code to generate apps/scripts.\n"
        "Use /balance to see revenue & stats.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    treasury_add(MESSAGE_EARN)
    await update.message.reply_text(
        "Commands:\n"
        "/start - welcome\n"
        "/help - this help\n"
        "/ask <query> - instant AI answer\n"
        "/code <spec> - developer agent code generator\n"
        "/balance - revenue & usage\n"
        "/pro - why upgrade"
    )

async def cmd_pro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    treasury_add(MESSAGE_EARN)
    await update.message.reply_html(
        "<b>Pro Features</b>\n"
        "• Longer contexts & faster replies\n"
        "• Better code generation & testing stubs\n"
        "• Priority support\n\n"
        f"Visit: {DOMAIN}"
    )

async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    treasury_add(MESSAGE_EARN)
    earned, spent = treasury_get()
    net = earned - spent
    await update.message.reply_text(
        f"📊 Treasury\n"
        f"Earned: ${earned:.4f}\nSpent: ${spent:.4f}\nNet: ${net:.4f}"
    )

async def cmd_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_touch(user.id, user.username)
    treasury_add(MESSAGE_EARN)

    query = " ".join(context.args) if context.args else ""
    if not query:
        await update.message.reply_text("Usage: /ask <your question>")
        return

    await update.message.chat.send_action(constants.ChatAction.TYPING)
    resp = ai_complete(query, system="Be concise, accurate and helpful.", max_tokens=700)
    await update.message.reply_text(resp[:4096])

async def cmd_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_touch(user.id, user.username)
    treasury_add(MESSAGE_EARN)

    spec = " ".join(context.args) if context.args else ""
    if not spec:
        await update.message.reply_text("Usage: /code <what to build>\nExample: /code FastAPI URL shortener with SQLite")
        return
    await update.message.chat.send_action(constants.ChatAction.TYPING)
    code = generate_code(spec)
    # Telegram max message size -> split if needed
    for chunk in split_text(code, 3900):
        await update.message.reply_text(chunk, parse_mode=constants.ParseMode.MARKDOWN)

def split_text(text: str, limit: int):
    buf = []
    cur = []
    total = 0
    for line in text.splitlines(keepends=True):
        if total + len(line) > limit and cur:
            buf.append("".join(cur))
            cur = [line]
            total = len(line)
        else:
            cur.append(line)
            total += len(line)
    if cur:
        buf.append("".join(cur))
    return buf

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    txt = update.message.text.strip() if update.message and update.message.text else ""
    if not txt:
        return
    user_touch(user.id, user.username)
    treasury_add(MESSAGE_EARN)
    await update.message.chat.send_action(constants.ChatAction.TYPING)
    reply = ai_complete(txt, system="Helpful, direct, step-by-step when needed.", max_tokens=700)
    await update.message.reply_text(reply[:4096])

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if data == "ask_ai":
        await query.message.reply_text("Ask me anything with /ask <question> 🤖")
    elif data == "gen_code":
        await query.message.reply_text("Describe what to build using /code <spec> 👨‍💻")
    elif data == "balance":
        earned, spent = treasury_get()
        net = earned - spent
        await query.message.reply_text(f"📊 Earned ${earned:.4f} | Spent ${spent:.4f} | Net ${net:.4f}")
    else:
        await query.message.reply_text("Unknown action.")

# -------------------------
# Flask App (Landing + Health + Monetization)
# -------------------------
app = Flask(__name__)

def set_csp(resp: Response):
    resp.headers["Content-Security-Policy"] = "default-src 'self' https: data: 'unsafe-inline' 'unsafe-eval';"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return resp

@app.before_request
def before_request_log():
    try:
        ip = request.headers.get("X-Forwarded-For", request.remote_addr) or "unknown"
        ref = request.headers.get("Referer", "")
        path = request.path
        conn = db()
        conn.execute("INSERT INTO impressions (ts, ip, path, ref) VALUES (?, ?, ?, ?)",
                     (dt.datetime.utcnow(), ip, path, ref[:500]))
        conn.commit()
        conn.close()

        # Monetize: site visit / ad view
        if path == "/":
            user_add_visit()
            treasury_add(VISIT_EARN)
        if "ad=1" in request.query_string.decode("utf-8", errors="ignore"):
            treasury_add(AD_IMPRESSION_EARN)

    except Exception as e:
        log.warning(f"before_request error: {e}")

@app.after_request
def after(resp):
    return set_csp(resp)

@app.get("/")
def home():
    # Simple landing with a fake ad slot (non-violative placeholder).
    html = f"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Ganesh A.I. – Developer Agent</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin:0; padding:0; background:#0b0d10; color:#e8eef5; }}
header {{ padding:24px; text-align:center; background:linear-gradient(90deg,#0b0d10,#121521); }}
h1 {{ margin:0; font-size:28px; }}
main {{ max-width:960px; margin:20px auto; padding:0 16px 40px; }}
.card {{ background:#141823; border:1px solid #1f2536; border-radius:16px; padding:20px; margin:16px 0; box-shadow:0 10px 30px rgba(0,0,0,.25); }}
.cta {{ display:inline-block; padding:12px 18px; border-radius:12px; background:#2a64ff; color:#fff; text-decoration:none; font-weight:700; }}
.small {{ font-size:13px; opacity:.8 }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:16px; }}
.ad {{ display:block; height:120px; border-radius:12px; background:#0f1320; border:1px dashed #2f3b5f; place-items:center; color:#9fb3ff; text-align:center; font-weight:600; }}
footer {{ text-align:center; padding:20px; opacity:.7 }}
code {{ background:#0f1320; padding:2px 6px; border-radius:6px; }}
</style>
</head>
<body>
<header>
  <h1>Ganesh A.I. – World's Most Powerful Developer Agent</h1>
</header>
<main>
  <div class="card">
    <p>Build apps, generate scripts, and get instant answers like ChatGPT—plus revenue features built-in.</p>
    <p><a class="cta" href="https://t.me/{get_bot_username_safe()}">Open Telegram Bot</a></p>
    <p class="small">Tip: Visiting this page & viewing ad placeholders supports the project.</p>
  </div>

  <div class="grid">
    <div class="card">
      <h3>🔥 Instant AI</h3>
      <p>Ask any question. Use <code>/ask</code> in Telegram.</p>
    </div>
    <div class="card">
      <h3>👨‍💻 Developer Agent</h3>
      <p>Generate production-grade code via <code>/code</code>.</p>
    </div>
    <div class="card">
      <h3>💰 Monetization</h3>
      <p>Visits & ad tags auto-earn into treasury.</p>
    </div>
  </div>

  <div class="card">
    <div class="ad">
      AD SLOT — viewing increments revenue. <a href="?ad=1" style="color:#9fb3ff">Refresh with ?ad=1</a>
    </div>
  </div>

  <div class="card small">
    <b>Webhook URL (optional):</b><br>
    {DOMAIN}/webhook/{SECRET_TOKEN}
  </div>
</main>
<footer>© {dt.datetime.utcnow().year} Ganesh A.I.</footer>
</body>
</html>
    """
    return html

@app.get("/healthz")
def health():
    earned, spent = treasury_get()
    return jsonify(
        ok=True,
        time=dt.datetime.utcnow().isoformat(),
        treasury={"earned": earned, "spent": spent},
    )

# Optional: Telegram webhook endpoint (if you want to switch to webhook mode later)
@app.post(f"/webhook/{SECRET_TOKEN}")
def webhook():
    # Basic header check for Telegram secret token (optional hardening)
    tg_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if tg_secret and tg_secret != SECRET_TOKEN:
        return jsonify({"ok": False, "error": "bad secret"}), 403

    try:
        data = request.get_json(force=True, silent=True) or {}
        # Enqueue for bot to process (only if application started)
        if BOT_APPLICATION is not None:
            BOT_APPLICATION.update_queue.put_nowait(Update.de_json(data, BOT_APPLICATION.bot))
            # Earn per message/update
            treasury_add(MESSAGE_EARN)
        return jsonify({"ok": True})
    except Exception as e:
        log.error(f"Webhook error: {e}")
        return jsonify({"ok": False}), 500

# -------------------------
# Scheduler Jobs
# -------------------------
SCHED = BackgroundScheduler()

def job_heartbeat():
    earned, spent = treasury_get()
    log.info(f"Heartbeat: treasury earned={earned:.4f} spent={spent:.4f}")
    log_event("INFO", f"heartbeat earned={earned:.4f} spent={spent:.4f}")

def job_daily_note():
    if not ADMIN_USER_ID or BOT_APPLICATION is None:
        return
    try:
        earned, spent = treasury_get()
        net = earned - spent
        text = f"💡 Daily Summary: Earned ${earned:.4f} | Spent ${spent:.4f} | Net ${net:.4f}"
        BOT_APPLICATION.bot.send_message(chat_id=int(ADMIN_USER_ID), text=text)
    except Exception as e:
        log.warning(f"Daily note failed: {e}")

# -------------------------
# Telegram App bootstrap
# -------------------------
BOT_APPLICATION: Application | None = None

def get_bot_username_safe() -> str:
    try:
        if BOT_APPLICATION:
            me = BOT_APPLICATION.bot.get_me()
            return me.username or "YourBot"
    except Exception:
        pass
    return "YourBot"

async def setup_bot_commands(app: Application):
    cmds = [
        BotCommand("start", "Welcome message"),
        BotCommand("help", "How to use"),
        BotCommand("ask", "Ask AI instantly"),
        BotCommand("code", "Generate app/script code"),
        BotCommand("balance", "Show revenue & usage"),
        BotCommand("pro", "Pro features"),
    ]
    await app.bot.set_my_commands(cmds)

def run_bot_polling():
    """
    Runs PTB in polling mode on a dedicated asyncio loop (thread).
    Flask keeps the port open for Render. This is simplest + robust.
    """
    async def _run():
        global BOT_APPLICATION
        application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

        # Handlers
        application.add_handler(CommandHandler("start", cmd_start))
        application.add_handler(CommandHandler("help", cmd_help))
        application.add_handler(CommandHandler("pro", cmd_pro))
        application.add_handler(CommandHandler("balance", cmd_balance))
        application.add_handler(CommandHandler("ask", cmd_ask))
        application.add_handler(CommandHandler("code", cmd_code))
        application.add_handler(CallbackQueryHandler(on_callback))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

        await setup_bot_commands(application)

        BOT_APPLICATION = application
        log.info("Telegram handlers registered.")
        log.info("Mode: POLLING")
        try:
            await application.initialize()
            await application.start()
            await application.updater.start_polling(drop_pending_updates=True)
            await application.updater.wait()
        finally:
            await application.stop()
            await application.shutdown()

    import asyncio
    asyncio.run(_run())

def maybe_set_webhook():
    """
    Use this only if you want Telegram to push updates to your server.
    Since we're already polling and keeping Flask port open, webhook is optional.
    If you prefer webhook, set env TELEGRAM_USE_WEBHOOK=1.
    """
    use_webhook = os.getenv("TELEGRAM_USE_WEBHOOK", "0") == "1"
    if not use_webhook:
        log.info("Skipping webhook setup (using polling).")
        return
    try:
        from telegram.request import HTTPXRequest
        req = HTTPXRequest(connect_timeout=20, read_timeout=20, write_timeout=20)
        from telegram import Bot
        bot = Bot(token=TELEGRAM_BOT_TOKEN, request=req)
        webhook_url = f"{DOMAIN}/webhook/{SECRET_TOKEN}"
        bot.delete_webhook(drop_pending_updates=True)
        ok = bot.set_webhook(url=webhook_url, secret_token=SECRET_TOKEN)
        if ok:
            log.info(f"Webhook set to {webhook_url}")
        else:
            log.warning("Failed to set webhook (Telegram returned false).")
    except Exception as e:
        log.warning(f"Webhook setup failed: {e}")

# -------------------------
# Main entry
# -------------------------
def main():
    log.info("Starting...")
    db_init()

    # Scheduler
    try:
        SCHED.add_job(job_heartbeat, "interval", minutes=60, id="heartbeat", replace_existing=True)
        SCHED.add_job(job_daily_note, "cron", hour=18, minute=0, id="daily_note", replace_existing=True)
        SCHED.start()
    except Exception as e:
        log.warning(f"Scheduler failed to start: {e}")

    # Optional webhook setup (we still run polling unless TELEGRAM_USE_WEBHOOK=1)
    maybe_set_webhook()

    # Start Telegram polling in background thread
    t = threading.Thread(target=run_bot_polling, name="polling-thread", daemon=True)
    t.start()

    # Start Flask (bind to PORT for Render)
    log.info(f"Flask serving on 0.0.0.0:{PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
