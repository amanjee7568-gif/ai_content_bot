# main.py
# ─────────────────────────────────────────────────────────────────────────────
# Ganesh A.I. – Single-file production app:
# - Flask web (index + /api/generate + admin login + admin dashboard)
# - Telegram bot (python-telegram-bot v21, polling in background thread)
# - OpenAI responses (openai>=1.42)
# - SQLite logs
# - gTTS optional (toggle via USE_TTS)
# - No "Updater.start_polling was never awaited" error (no Updater used)
# - Gunicorn compatible (app = Flask(...) at bottom)
# ─────────────────────────────────────────────────────────────────────────────

import os
import json
import sqlite3
import threading
import traceback
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from flask import (
    Flask, request, jsonify, redirect, url_for, session,
    render_template_string, send_from_directory
)

from dotenv import load_dotenv
from openai import OpenAI

# Telegram v21
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)

# Optional TTS
try:
    from gtts import gTTS
    USE_TTS_AVAILABLE = True
except Exception:
    USE_TTS_AVAILABLE = False

# ─────────────────────────────────────────────────────────────────────────────
# ENV & CONFIG
# ─────────────────────────────────────────────────────────────────────────────
load_dotenv()

APP_NAME           = os.getenv("APP_NAME", "Ganesh A.I.")
PUBLIC_URL         = os.getenv("PUBLIC_URL", "https://ai-content-bot.example.com")
BRAND_PAGE         = os.getenv("BRAND_PAGE", "https://brand.page/Ganeshagamingworld")

OPENAI_API_KEY     = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL       = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_ENABLED   = os.getenv("TELEGRAM_ENABLED", "true").lower() == "true"

# Admin panel credentials
ADMIN_USER         = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS         = os.getenv("ADMIN_PASS", "change-me")

# Flask session secret
FLASK_SECRET       = os.getenv("FLASK_SECRET", "super-secret-key-change-this")

# Optional features
USE_TTS            = os.getenv("USE_TTS", "false").lower() == "true" and USE_TTS_AVAILABLE

# DB path
DB_PATH            = os.getenv("DB_PATH", "app.db")

# Logging level
LOG_LEVEL          = os.getenv("LOG_LEVEL", "INFO").upper()

# Port (Render/Gunicorn picks $PORT)
PORT               = int(os.getenv("PORT", "10000"))

# ─────────────────────────────────────────────────────────────────────────────
# UTILITIES / LOGGING
# ─────────────────────────────────────────────────────────────────────────────
def now_utc():
    return datetime.now(timezone.utc)

def ts():
    return now_utc().strftime("%Y-%m-%d %H:%M:%S")

def log(level, *parts, **kv):
    level = level.upper()
    if level not in ("DEBUG", "INFO", "WARN", "ERROR"):
        level = "INFO"
    if {"DEBUG":0,"INFO":1,"WARN":2,"ERROR":3}[level] < {"DEBUG":0,"INFO":1,"WARN":2,"ERROR":3}[LOG_LEVEL]:
        return
    msg = " ".join(str(p) for p in parts)
    if kv:
        msg += " | " + json.dumps(kv, ensure_ascii=False)
    print(f"{ts()} | {level:5s} | {APP_NAME} | {msg}", flush=True)

# ─────────────────────────────────────────────────────────────────────────────
# DB (SQLite)
# ─────────────────────────────────────────────────────────────────────────────
def db_init():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        t TIMESTAMP,
        ch TEXT,        -- channel: web/telegram/system
        level TEXT,
        event TEXT,
        meta TEXT
    )
    """)
    c.execute("""
    CREATE TABLE IF NOT EXISTS queries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        t TIMESTAMP,
        source TEXT,    -- web or telegram
        prompt TEXT,
        response TEXT
    )
    """)
    conn.commit()
    conn.close()

def db_log(ch, level, event, meta=None):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(
            "INSERT INTO logs (t, ch, level, event, meta) VALUES (?, ?, ?, ?, ?)",
            (ts(), ch, level, event, json.dumps(meta or {}, ensure_ascii=False))
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log("ERROR", "db_log failed", err=str(e))

def db_save_query(source, prompt, response):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(
            "INSERT INTO queries (t, source, prompt, response) VALUES (?, ?, ?, ?)",
            (ts(), source, prompt, response)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log("ERROR", "db_save_query failed", err=str(e))

# ─────────────────────────────────────────────────────────────────────────────
# OPENAI CLIENT
# ─────────────────────────────────────────────────────────────────────────────
if not OPENAI_API_KEY:
    log("WARN", "OPENAI_API_KEY missing. /api/generate will fail without it.")

try:
    oai = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
    if oai:
        log("INFO", "OpenAI client initialized.")
except Exception as e:
    oai = None
    log("ERROR", "OpenAI init failed", err=str(e))

async def llm_generate(prompt: str) -> str:
    """
    Generate text using OpenAI chat.completions.
    """
    if not oai:
        return "OpenAI client not configured. Please set OPENAI_API_KEY."
    try:
        resp = oai.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": "You are a helpful assistant for scripts and content."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
            max_tokens=700
        )
        text = resp.choices[0].message.content.strip()
        return text
    except Exception as e:
        log("ERROR", "OpenAI error", err=str(e), trace=traceback.format_exc())
        return f"OpenAI error: {e}"

# ─────────────────────────────────────────────────────────────────────────────
# FLASK APP + TEMPLATES
# ─────────────────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = FLASK_SECRET

INDEX_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>{{ app_name }}</title>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <link rel="icon" href="/favicon.ico">
  <style>
    :root { --bg:#0b1220; --card:#121a2a; --muted:#a7b3c6; --text:#eaf0ff; --brand:#6ca3ff; }
    *{box-sizing:border-box}
    body{margin:0;background:var(--bg);color:var(--text);font-family:Inter,system-ui,-apple-system,Segoe UI,Roboto,Arial}
    .wrap{max-width:880px;margin:40px auto;padding:0 16px}
    .card{background:var(--card);border:1px solid #1d2a44;border-radius:16px;box-shadow:0 10px 30px rgba(0,0,0,.35)}
    header{display:flex;align-items:center;gap:12px}
    header img{width:40px;height:40px}
    .title{font-size:26px;font-weight:700}
    .muted{color:var(--muted)}
    .row{display:grid;grid-template-columns:1fr 1fr;gap:12px}
    .btn{background:var(--brand);color:#04122d;border:none;border-radius:10px;padding:12px 16px;font-weight:700;cursor:pointer}
    .btn:disabled{opacity:.6;cursor:not-allowed}
    input,select,textarea{background:#0c1426;color:var(--text);border:1px solid #1d2a44;border-radius:10px;padding:12px;width:100%}
    textarea{min-height:140px;resize:vertical}
    .out{white-space:pre-wrap;background:#0a0f1a;border-radius:10px;padding:12px;border:1px solid #1d2a44}
    a.brand{color:var(--brand);text-decoration:none}
    .topbar{display:flex;justify-content:space-between;align-items:center;margin-bottom:18px;padding:12px 16px;border-bottom:1px solid #1d2a44}
    .footer{margin:24px 0;color:var(--muted);font-size:13px;text-align:center}
  </style>
</head>
<body>
<div class="wrap">

  <div class="card">
    <div class="topbar">
      <header>
        <img src="https://cdn-icons-png.flaticon.com/512/4712/4712027.png" alt="logo"/>
        <div>
          <div class="title">{{ app_name }}</div>
          <div class="muted">Smart content & script maker</div>
        </div>
      </header>
      <div><a class="brand" href="{{ brand_page }}" target="_blank">Brand Page ↗</a></div>
    </div>

    <div style="padding:16px">
      <div class="row">
        <div>
          <label>Topic / Prompt</label>
          <input id="prompt" placeholder="e.g. PUBG montage video script in Hinglish"/>
        </div>
        <div>
          <label>Preset</label>
          <select id="preset">
            <option value="script">Video Script</option>
            <option value="caption">YouTube Caption</option>
            <option value="ideas">Title Ideas</option>
            <option value="shorts">Shorts Hook</option>
          </select>
        </div>
      </div>

      <div class="row" style="margin-top:12px">
        <div>
          <label>Style / Tone</label>
          <input id="style" placeholder="Energetic, Hinglish, Gen-Z"/>
        </div>
        <div>
          <label>Duration / Length</label>
          <select id="length">
            <option value="short">Short (30-60s)</option>
            <option value="med">Medium (2-5 min)</option>
            <option value="long">Long (8-12 min)</option>
          </select>
        </div>
      </div>

      <div style="margin-top:12px">
        <label>Extra Notes</label>
        <textarea id="notes" placeholder="Anything specific to include?"></textarea>
      </div>

      <div style="display:flex;gap:12px;align-items:center;margin-top:12px">
        <button id="btnGen" class="btn">⚡ Generate</button>
        <button id="btnSearch" class="btn" title="Quick variation">🔎 Search / Remix</button>
        <span id="status" class="muted"></span>
      </div>

      <div id="output" class="out" style="margin-top:16px;min-height:120px"></div>
    </div>
  </div>

  <div class="footer">
    © {{ year }} · <a class="brand" href="{{ public_url }}" target="_blank">{{ public_url }}</a>
    · <a class="brand" href="/admin">Admin</a>
  </div>

</div>

<script>
async function postJSON(url, data) {
  const r = await fetch(url, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(data)
  });
  return await r.json();
}

function buildPrompt() {
  const topic  = document.getElementById("prompt").value.trim();
  const preset = document.getElementById("preset").value;
  const style  = document.getElementById("style").value.trim();
  const length = document.getElementById("length").value;
  const notes  = document.getElementById("notes").value.trim();

  let sys = "";
  if (preset === "script") {
    sys = "Create a tight, timestamped YouTube script with scene beats, VO lines, and B-roll suggestions.";
  } else if (preset === "caption") {
    sys = "Write a catchy YouTube description + 10 SEO tags.";
  } else if (preset === "ideas") {
    sys = "Give 15 viral, SEO-friendly video title ideas.";
  } else if (preset === "shorts") {
    sys = "Write 5 ultra-hooky shorts scripts (<=20s) with punchy lines.";
  }

  return `${sys}\n\nTopic: ${topic}\nStyle: ${style || "Hinglish"}\nLength: ${length}\nNotes: ${notes}`;
}

async function generate() {
  const status = document.getElementById("status");
  const out    = document.getElementById("output");
  const btn    = document.getElementById("btnGen");
  btn.disabled = true;
  status.textContent = "Generating…";
  out.textContent = "";

  const prompt = buildPrompt();
  try {
    const res = await postJSON("/api/generate", { prompt });
    if (res.ok) {
      out.textContent = res.text || "(empty)";
    } else {
      out.textContent = "Error: " + (res.error || "unknown");
    }
  } catch (e) {
    out.textContent = "Network error: " + e;
  } finally {
    btn.disabled = false;
    status.textContent = "";
  }
}

async function remix() {
  const prompt = document.getElementById("prompt");
  if (!prompt.value.trim()) {
    prompt.value = "Gaming highlights video with funny commentary";
  } else {
    prompt.value = prompt.value + " (make it funnier, faster-paced)";
  }
  await generate();
}

document.getElementById("btnGen").addEventListener("click", generate);
document.getElementById("btnSearch").addEventListener("click", remix);
</script>
</body>
</html>
"""

ADMIN_LOGIN_HTML = """
<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Admin Login</title>
<style>
  body{background:#0b1220;font-family:Inter,system-ui;display:flex;align-items:center;justify-content:center;height:100vh;color:#eaf0ff}
  .card{background:#121a2a;border:1px solid #1d2a44;border-radius:16px;padding:24px;min-width:320px}
  input{width:100%;padding:12px;border-radius:10px;border:1px solid #1d2a44;background:#0c1426;color:#eaf0ff;margin-top:8px}
  .btn{width:100%;padding:12px;border-radius:10px;border:none;background:#6ca3ff;color:#04122d;font-weight:700;margin-top:12px}
  .muted{color:#a7b3c6}
</style>
</head><body>
  <div class="card">
    <h2>Admin Login</h2>
    <form method="post">
      <label>Username</label>
      <input name="username" autofocus/>
      <label style="margin-top:6px">Password</label>
      <input name="password" type="password"/>
      <button class="btn" type="submit">Login</button>
    </form>
    {% if error %}<div class="muted" style="margin-top:10px;color:#ff9b9b">{{ error }}</div>{% endif %}
  </div>
</body></html>
"""

ADMIN_DASHBOARD_HTML = """
<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Admin · {{ app_name }}</title>
<style>
  body{background:#0b1220;color:#eaf0ff;font-family:Inter,system-ui}
  .wrap{max-width:980px;margin:30px auto;padding:0 16px}
  .card{background:#121a2a;border:1px solid #1d2a44;border-radius:16px;padding:16px}
  table{width:100%;border-collapse:collapse}
  th,td{border-bottom:1px solid #1d2a44;padding:8px;text-align:left;font-size:14px}
  .top{display:flex;justify-content:space-between;align-items:center}
  a.btn{background:#6ca3ff;color:#04122d;text-decoration:none;padding:8px 12px;border-radius:8px}
  .muted{color:#a7b3c6}
</style>
</head><body>
  <div class="wrap">
    <div class="top">
      <h2>Admin · {{ app_name }}</h2>
      <div>
        <a class="btn" href="/">Home</a>
        <a class="btn" href="/admin/logout">Logout</a>
      </div>
    </div>

    <div class="card" style="margin-top:16px">
      <h3>Latest Queries</h3>
      <table>
        <thead><tr><th>Time</th><th>Source</th><th>Prompt</th><th>Response (first 120 chars)</th></tr></thead>
        <tbody>
        {% for q in queries %}
          <tr>
            <td class="muted">{{ q.t }}</td>
            <td>{{ q.source }}</td>
            <td>{{ q.prompt[:80] }}</td>
            <td class="muted">{{ (q.response or "")[:120] }}</td>
          </tr>
        {% endfor %}
        </tbody>
      </table>
    </div>

    <div class="card" style="margin-top:16px">
      <h3>Logs</h3>
      <table>
        <thead><tr><th>Time</th><th>Ch</th><th>Level</th><th>Event</th><th>Meta</th></tr></thead>
        <tbody>
        {% for l in logs %}
          <tr>
            <td class="muted">{{ l.t }}</td>
            <td>{{ l.ch }}</td>
            <td>{{ l.level }}</td>
            <td>{{ l.event }}</td>
            <td class="muted">{{ l.meta }}</td>
          </tr>
        {% endfor %}
        </tbody>
      </table>
    </div>
  </div>
</body></html>
"""

# ─────────────────────────────────────────────────────────────────────────────
# AUTH DECORATOR
# ─────────────────────────────────────────────────────────────────────────────
def require_admin(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("admin_ok"):
            return redirect(url_for("admin_login"))
        return fn(*args, **kwargs)
    return wrapper

# ─────────────────────────────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/")
def home():
    return render_template_string(INDEX_HTML,
                                  app_name=APP_NAME,
                                  public_url=PUBLIC_URL,
                                  brand_page=BRAND_PAGE,
                                  year=datetime.now().year)

@app.get("/favicon.ico")
def favicon():
    # Optional local favicon support (returns 204 if none)
    static_dir = Path("static")
    ico = static_dir / "favicon.ico"
    if ico.exists():
        return send_from_directory(str(static_dir), "favicon.ico")
    return ("", 204)

@app.post("/api/generate")
def api_generate():
    data = request.get_json(force=True, silent=True) or {}
    prompt = (data.get("prompt") or "").strip()
    if not prompt:
        return jsonify(ok=False, error="Empty prompt"), 400

    db_log("web", "INFO", "generate", {"prompt": prompt})

    # Run LLM sync via async helper (use a tiny loop)
    try:
        import asyncio
        text = asyncio.run(llm_generate(prompt))
    except RuntimeError:
        # If we're already in an event loop (rare in gunicorn workers), use new loop
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        text = loop.run_until_complete(llm_generate(prompt))

    db_save_query("web", prompt, text)
    return jsonify(ok=True, text=text)

# ── Admin
@app.get("/admin")
@require_admin
def admin():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT t, source, prompt, response FROM queries ORDER BY id DESC LIMIT 40")
    queries = c.fetchall()
    c.execute("SELECT t, ch, level, event, meta FROM logs ORDER BY id DESC LIMIT 60")
    logs = c.fetchall()
    conn.close()
    return render_template_string(ADMIN_DASHBOARD_HTML,
                                  app_name=APP_NAME,
                                  queries=queries,
                                  logs=logs)

@app.get("/admin/login")
def admin_login():
    return render_template_string(ADMIN_LOGIN_HTML, error=None)

@app.post("/admin/login")
def admin_login_post():
    username = request.form.get("username", "")
    password = request.form.get("password", "")
    if username == ADMIN_USER and password == ADMIN_PASS:
        session["admin_ok"] = True
        db_log("web", "INFO", "admin_login_ok", {"user": username})
        return redirect(url_for("admin"))
    db_log("web", "WARN", "admin_login_fail", {"user": username})
    return render_template_string(ADMIN_LOGIN_HTML, error="गलत username/password")

@app.get("/admin/logout")
def admin_logout():
    session.pop("admin_ok", None)
    return redirect(url_for("admin_login"))

# ─────────────────────────────────────────────────────────────────────────────
# TELEGRAM BOT (Polling, v21 API)
# ─────────────────────────────────────────────────────────────────────────────
telegram_app = None
telegram_thread = None

async def tg_cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Namaste! Main Ganesh A.I. hoon 👋\n"
        "Bas apna topic ya idea bhejo, main script/caption bana dunga."
    )

async def tg_cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/start – greeting\n"
        "Just send any topic to generate a script.\n"
        "Example: 'BGMI montage with roast-style commentary'"
    )

async def tg_on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        return
    db_log("telegram", "INFO", "msg", {"from": update.effective_user.id, "text": text})
    await update.message.chat.send_action("typing")
    try:
        reply = await llm_generate(f"Telegram user asked:\n\n{text}")
    except Exception as e:
        reply = f"Error: {e}"
    db_save_query("telegram", text, reply)
    await update.message.reply_text(reply[:4000])

def start_telegram_polling():
    global telegram_app
    if not TELEGRAM_ENABLED or not TELEGRAM_BOT_TOKEN:
        log("INFO", "Telegram disabled or token missing; skipping bot start.")
        return

    try:
        telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        telegram_app.add_handler(CommandHandler("start", tg_cmd_start))
        telegram_app.add_handler(CommandHandler("help", tg_cmd_help))
        telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, tg_on_text))

        def runner():
            try:
                log("INFO", "Telegram handlers registered. Mode: POLLING")
                db_log("telegram", "INFO", "polling_start", {})
                # run_polling is a blocking convenience method (v21) – NOT a coroutine.
                telegram_app.run_polling(close_loop=False, allowed_updates=Update.ALL_TYPES)
            except Exception as e:
                db_log("telegram", "ERROR", "polling crashed", {"err": str(e), "trace": traceback.format_exc()})
                log("ERROR", "telegram polling crashed", err=str(e))
        th = threading.Thread(target=runner, daemon=True)
        th.start()
        return th
    except Exception as e:
        db_log("telegram", "ERROR", "init_failed", {"err": str(e), "trace": traceback.format_exc()})
        log("ERROR", "Telegram init failed", err=str(e))

# ─────────────────────────────────────────────────────────────────────────────
# APP STARTUP
# ─────────────────────────────────────────────────────────────────────────────
def startup():
    db_init()
    log("INFO", "App:", APP_NAME)
    log("INFO", "Public URL:", PUBLIC_URL)
    log("INFO", "Brand Page:", BRAND_PAGE)
    log("INFO", "OpenAI:", "ON" if oai else "OFF", model=OPENAI_MODEL)
    log("INFO", "Telegram:", "ON" if TELEGRAM_ENABLED else "OFF")
    if TELEGRAM_ENABLED and TELEGRAM_BOT_TOKEN:
        global telegram_thread
        telegram_thread = start_telegram_polling()

# ─────────────────────────────────────────────────────────────────────────────
# MAIN (Gunicorn will import `app`; local run uses flask dev server)
# ─────────────────────────────────────────────────────────────────────────────
startup()

if __name__ == "__main__":
    # Dev server only (Render uses gunicorn via Procfile)
    log("INFO", "Starting Flask dev server…", port=PORT)
    app.run(host="0.0.0.0", port=PORT)
