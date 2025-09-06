import os
import re
import json
import time
import uuid
import base64
import random
import string
import sqlite3 as sqlite
import datetime as dt
import threading
from dataclasses import dataclass
from typing import Optional, Dict, Any, List, Tuple

import requests
from dotenv import load_dotenv
from flask import Flask, request, jsonify, make_response, redirect, url_for, render_template_string, session, abort

# Telegram v21.x
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# Optional OpenAI (auto-fallback to HF if not present/configured)
try:
    from openai import OpenAI
except Exception:
    OpenAI = None


# =========================
# Env & Config
# =========================
load_dotenv()

APP_NAME = os.getenv("APP_NAME", "Ganesh A.I.")
BRAND_DOMAIN = os.getenv("BRAND_DOMAIN", "https://brand.page/Ganeshagamingworld").rstrip("/")
HUGGINGFACE_API_URL = os.getenv("HUGGINGFACE_API_URL", "").strip()
HUGGINGFACE_API_TOKEN = os.getenv("HUGGINGFACE_API_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()  # if key exists
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
FLASK_SECRET = os.getenv("FLASK_SECRET", os.urandom(24).hex())
PORT = int(os.getenv("PORT", "10000"))
HOST = os.getenv("HOST", "0.0.0.0")
VISIT_PAY_RATE = float(os.getenv("VISIT_PAY_RATE", "0.001"))  # USD (or points) per unique visit hit
COOKIE_VISIT_KEY = "ganesh_visit_token"
COOKIE_VISIT_TTL = int(os.getenv("COOKIE_VISIT_TTL", "1800"))  # 30 mins

ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "admin123")

ENABLE_OPENAI = bool(OPENAI_API_KEY)
ENABLE_HF = bool(HUGGINGFACE_API_URL and HUGGINGFACE_API_TOKEN)

# =========================
# Flask App
# =========================
app = Flask(__name__)
app.secret_key = FLASK_SECRET


# =========================
# Database
# =========================
DB_PATH = os.getenv("DB_PATH", "ganesh_ai.db")

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_user_id TEXT,
    username TEXT,
    first_name TEXT,
    last_name TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    direction TEXT CHECK(direction IN ('in','out')),
    content TEXT,
    tokens INTEGER DEFAULT 0,
    model TEXT,
    cost REAL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS earnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT,       -- 'visit','ads','affiliate','premium','donation','telegram'
    amount REAL,       -- USD or points
    note TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    value REAL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    level TEXT,
    message TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

def db() -> sqlite.Connection:
    conn = sqlite.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite.Row
    return conn

def ensure_db():
    conn = db()
    with conn:
        # Python 3.12 deprecation note: default adapter warning is ok for SQLite
        conn.executescript(SCHEMA)
    conn.close()

ensure_db()


# =========================
# Utilities
# =========================
def log(level: str, message: str):
    try:
        conn = db()
        with conn:
            conn.execute("INSERT INTO logs(level, message) VALUES (?,?)", (level, message[:4000]))
    except Exception as e:
        print("LOG FAIL:", e)

def metric(name: str, value: float = 1.0):
    try:
        conn = db()
        with conn:
            conn.execute("INSERT INTO metrics(name, value) VALUES (?,?)", (name, value))
    except Exception as e:
        log("ERROR", f"metric fail {name}: {e}")

def earn(source: str, amount: float, note: str = ""):
    try:
        conn = db()
        with conn:
            conn.execute("INSERT INTO earnings(source, amount, note) VALUES (?,?,?)", (source, amount, note[:500]))
    except Exception as e:
        log("ERROR", f"earn fail {source}: {e}")

def get_stats() -> Dict[str, Any]:
    conn = db()
    cur = conn.cursor()
    out = {}
    out["users"] = cur.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    out["messages"] = cur.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"]
    out["earnings"] = cur.execute("SELECT IFNULL(SUM(amount),0) s FROM earnings").fetchone()["s"] or 0.0
    out["last_10_logs"] = [dict(r) for r in cur.execute("SELECT * FROM logs ORDER BY id DESC LIMIT 10").fetchall()]
    out["last_10_earnings"] = [dict(r) for r in cur.execute("SELECT * FROM earnings ORDER BY id DESC LIMIT 10").fetchall()]
    out["last_10_msgs"] = [dict(r) for r in cur.execute("SELECT * FROM messages ORDER BY id DESC LIMIT 10").fetchall()]
    return out

def upsert_user_by_tg(update: Update) -> int:
    """Create/find user row by telegram user info; return user_id."""
    if not update or not update.effective_user:
        return 0
    u = update.effective_user
    tg_user_id = str(u.id)
    conn = db()
    cur = conn.cursor()
    row = cur.execute("SELECT id FROM users WHERE tg_user_id=?", (tg_user_id,)).fetchone()
    if row:
        return row["id"]
    with conn:
        cur.execute(
            "INSERT INTO users (tg_user_id, username, first_name, last_name) VALUES (?,?,?,?)",
            (tg_user_id, u.username, u.first_name, u.last_name),
        )
    return cur.lastrowid

def record_message(user_id: int, direction: str, content: str, tokens: int = 0, model: str = "", cost: float = 0.0):
    try:
        conn = db()
        with conn:
            conn.execute(
                "INSERT INTO messages(user_id, direction, content, tokens, model, cost) VALUES (?,?,?,?,?,?)",
                (user_id, direction, content[:8000], tokens, model, cost),
            )
    except Exception as e:
        log("ERROR", f"record_message fail: {e}")

# =========================
# AI Core
# =========================
@dataclass
class AIResponse:
    text: str
    model: str
    tokens: int = 0
    cost: float = 0.0

# OpenAI client (optional)
_oai_client = None
if ENABLE_OPENAI and OpenAI is not None:
    try:
        _oai_client = OpenAI(api_key=OPENAI_API_KEY)
        log("INFO", "OpenAI client initialized.")
    except Exception as e:
        log("ERROR", f"OpenAI init error: {e}")
        _oai_client = None

def _openai_chat(prompt: str, system: str = "You are a helpful AI assistant.") -> Optional[AIResponse]:
    if not _oai_client:
        return None
    try:
        resp = _oai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
        )
        text = resp.choices[0].message.content or ""
        usage = getattr(resp, "usage", None)
        tokens = (usage.total_tokens if usage else 0) if hasattr(usage, "total_tokens") else 0
        # Rough cost estimation can be added if needed
        return AIResponse(text=text.strip(), model=OPENAI_MODEL, tokens=tokens, cost=0.0)
    except Exception as e:
        log("ERROR", f"OpenAI chat error: {e}")
        return None

def _hf_generate(prompt: str, max_new_tokens: int = 512, temperature: float = 0.7) -> Optional[AIResponse]:
    if not ENABLE_HF:
        return None
    try:
        headers = {
            "Authorization": f"Bearer {HUGGINGFACE_API_TOKEN}",
            "Content-Type": "application/json",
        }
        payload = {
            "inputs": prompt,
            "parameters": {"max_new_tokens": max_new_tokens, "temperature": temperature},
            "options": {"wait_for_model": True}
        }
        r = requests.post(HUGGINGFACE_API_URL, headers=headers, data=json.dumps(payload), timeout=60)
        if r.status_code == 200:
            data = r.json()
            # HF inference endpoints vary; handle common shapes
            if isinstance(data, list) and data and isinstance(data[0], dict) and "generated_text" in data[0]:
                out = data[0]["generated_text"]
            elif isinstance(data, dict) and "generated_text" in data:
                out = data["generated_text"]
            elif isinstance(data, dict) and "choices" in data and data["choices"]:
                out = data["choices"][0].get("text", "")
            else:
                out = str(data)
            return AIResponse(text=out.strip(), model="huggingface", tokens=0, cost=0.0)
        else:
            log("ERROR", f"HF bad status {r.status_code}: {r.text[:300]}")
            return None
    except Exception as e:
        log("ERROR", f"HF generate error: {e}")
        return None

def smart_generate(prompt: str, system: str = "You are a helpful AI assistant.") -> AIResponse:
    """Try OpenAI then HF as fallback."""
    if ENABLE_OPENAI and _oai_client:
        ans = _openai_chat(prompt, system=system)
        if ans and ans.text:
            return ans
    # fallback HF
    ans = _hf_generate(prompt)
    if ans and ans.text:
        return ans
    # ultimate fallback
    return AIResponse(text="(AI backend unavailable right now. Please try again in a moment.)", model="offline")


def code_prompt(task: str) -> str:
    return f"""You are a senior software engineer. Generate clean, production-ready code for this request:

{task}

Requirements:
- Provide a complete solution in the requested language.
- Add brief inline comments where helpful.
- Avoid unnecessary boilerplate unless needed to run.
"""

def image_prompt(idea: str) -> str:
    return f"""Create an image generation prompt suitable for an advanced text-to-image model.
Describe the scene concisely but vividly.

Idea: {idea}

Output only the final prompt text, nothing else.
"""


# =========================
# Monetization Helpers
# =========================
def unique_visit_credit(req) -> bool:
    """Credit earning for unique visits using a cookie token + TTL."""
    now = int(time.time())
    token = request.cookies.get(COOKIE_VISIT_KEY)
    already_counted = False
    if token:
        # store in memory? We'll record a metric; to keep it simple, rely on TTL cookie.
        already_counted = True
    resp = make_response()
    if not already_counted:
        earn("visit", VISIT_PAY_RATE, note=f"ip={req.remote_addr}")
        metric("visit")
        # set cookie
        tok = base64.urlsafe_b64encode(os.urandom(12)).decode("utf-8").rstrip("=")
        resp.set_cookie(COOKIE_VISIT_KEY, tok, max_age=COOKIE_VISIT_TTL, httponly=True, samesite="Lax")
    return resp


# =========================
# HTML Templates
# =========================
def year_utc():
    try:
        return dt.datetime.now(dt.timezone.utc).year
    except Exception:
        return dt.datetime.utcnow().year  # fallback

BASE_HEAD = """
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{{ title }}</title>
<link rel="icon" href="data:,">
<style>
:root{--bg:#0b0f17;--fg:#e6eefb;--muted:#9fb3d1;--card:#111827;--acc:#7c3aed;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,Cantarell,Noto Sans,sans-serif}
.container{max-width:1000px;margin:0 auto;padding:24px}
nav{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px}
.brand{font-weight:800;letter-spacing:.5px}
a,button{cursor:pointer}
.card{background:var(--card);border:1px solid #1f2937;border-radius:16px;padding:20px;box-shadow:0 10px 30px rgba(0,0,0,.25)}
h1{font-size:28px;margin:.2em 0}
h2{font-size:22px;margin:.2em 0}
small{color:var(--muted)}
.input{width:100%;padding:12px 14px;border-radius:10px;border:1px solid #374151;background:#0f1623;color:var(--fg)}
.btn{padding:10px 14px;border-radius:10px;border:1px solid #334155;background:#0d1421;color:#fff}
.btn.primary{background:linear-gradient(90deg,#7c3aed,#2563eb);border:0}
.row{display:grid;grid-template-columns:1fr;gap:16px}
.ad{background:#0b1220;border:1px dashed #334155;border-radius:14px;padding:14px;text-align:center}
footer{margin-top:36px;color:var(--muted);text-align:center}
.table{width:100%;border-collapse:collapse;font-size:14px}
.table th,.table td{border-bottom:1px solid #223049;padding:8px 6px;text-align:left}
.kpi{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:12px 0}
.kpi .card{padding:14px;text-align:center}
.badge{display:inline-block;padding:4px 8px;border:1px solid #334155;border-radius:999px;font-size:12px;color:var(--muted)}
.header-cta{display:flex;gap:8px;flex-wrap:wrap}
@media (min-width:800px){
 .row{grid-template-columns:1.5fr .8fr}
}
</style>
<!-- Monetization/Ads placeholders (replace with your real tags) -->
<!-- Google AdSense example placeholder -->
<!-- <script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-XXXX" crossorigin="anonymous"></script> -->
<!-- Adsterra/Propeller etc. can be inserted here -->
"""

INDEX_HTML = BASE_HEAD + """
<body>
<div class="container">
  <nav>
    <div class="brand">{{ brand }}</div>
    <div class="header-cta">
      <a class="btn" href="{{ brand_domain }}" target="_blank">Brand</a>
      <a class="btn" href="/admin/login">Admin</a>
      <a class="btn primary" href="#play">Try AI</a>
    </div>
  </nav>

  <div class="card">
    <h1>⚡ {{ brand }} — Ultra AI Assistant</h1>
    <p>Ask anything. Get instant, high-quality answers. Code, scripts, app ideas, marketing copies, summaries — all in one.</p>
    <div class="badge">Monetized • Visit credits active</div>
  </div>

  <div class="row" id="play" style="margin-top:16px">
    <div class="card">
      <h2>Chat</h2>
      <form method="post" action="/api/ask" onsubmit="event.preventDefault(); askAI();">
        <textarea class="input" id="prompt" name="prompt" rows="5" placeholder="Type your question, e.g., 'Build a Telegram bot that greets users...'"></textarea>
        <div style="display:flex;gap:8px;margin-top:8px">
          <button class="btn primary" id="askBtn" type="submit">Ask</button>
          <button class="btn" type="button" onclick="fillCode()">Generate Code</button>
          <button class="btn" type="button" onclick="fillImage()">Image Prompt</button>
        </div>
      </form>
      <div id="ans" style="white-space:pre-wrap;margin-top:12px"></div>
    </div>

    <div class="card">
      <h2>Monetization</h2>
      <div class="ad">Ad Slot #1 (replace with your ad tag)</div>
      <div class="ad">Ad Slot #2 (replace with your ad tag)</div>
      <div class="ad">Affiliate: <a href="#" onclick="alert('Replace with your affiliate URL');return false;">Top AI Courses</a></div>
      <small>Every unique visit gives you a micro-earning. Integrate your ad networks to scale.</small>
    </div>
  </div>

  <footer>© {{ year }} Ganesh A.I. • <a href="/health" style="color:#9fb3d1">health</a></footer>
</div>

<script>
async function askAI(){
  const btn = document.getElementById('askBtn');
  const ans = document.getElementById('ans');
  const prompt = document.getElementById('prompt').value.trim();
  if(!prompt){ ans.textContent="Please type something."; return; }
  btn.disabled = true; ans.textContent = "Thinking…";
  try{
    const r = await fetch('/api/ask', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({prompt})
    });
    const j = await r.json();
    ans.textContent = j.text || "(no output)";
  }catch(e){
    ans.textContent = "Error: "+e;
  }finally{
    btn.disabled = false;
  }
}
function fillCode(){ document.getElementById('prompt').value = "Create a FastAPI service with a /sum endpoint and Dockerfile."; }
function fillImage(){ document.getElementById('prompt').value = "A cinematic cyberpunk street at night, neon rain, reflective puddles, ultra-detailed, 35mm, volumetric lighting."; }
</script>
</body>
"""

ADMIN_LOGIN_HTML = BASE_HEAD + """
<body>
<div class="container">
  <nav>
    <div class="brand">{{ brand }}</div>
    <div class="header-cta">
      <a class="btn" href="/">Home</a>
    </div>
  </nav>

  <div class="card" style="max-width:460px;margin:0 auto">
    <h2>Admin Login</h2>
    {% if error %}<div class="ad" style="border-color:#7c3aed;color:#eab308">{{ error }}</div>{% endif %}
    <form method="post">
      <label>Username</label>
      <input class="input" name="u" placeholder="username"/>
      <label>Password</label>
      <input class="input" type="password" name="p" placeholder="password"/>
      <button class="btn primary" style="margin-top:10px">Login</button>
    </form>
  </div>
</div>
</body>
"""

ADMIN_DASH_HTML = BASE_HEAD + """
<body>
<div class="container">
  <nav>
    <div class="brand">{{ brand }}</div>
    <div class="header-cta">
      <span class="badge">Admin</span>
      <a class="btn" href="/">Home</a>
      <a class="btn" href="/admin/logout">Logout</a>
    </div>
  </nav>

  <div class="kpi">
    <div class="card"><div style="font-size:12px;color:#9fb3d1">Users</div><div style="font-size:24px">{{ users }}</div></div>
    <div class="card"><div style="font-size:12px;color:#9fb3d1">Messages</div><div style="font-size:24px">{{ messages }}</div></div>
    <div class="card"><div style="font-size:12px;color:#9fb3d1">Earnings</div><div style="font-size:24px">${{ "%.4f"|format(earnings) }}</div></div>
  </div>

  <div class="row" style="margin-top:10px">
    <div class="card">
      <h2>Recent Messages</h2>
      <table class="table">
        <tr><th>ID</th><th>User</th><th>Dir</th><th>Tokens</th><th>Model</th><th>At</th></tr>
        {% for m in last_10_msgs %}
          <tr>
            <td>{{ m.id }}</td>
            <td>{{ m.user_id }}</td>
            <td>{{ m.direction }}</td>
            <td>{{ m.tokens }}</td>
            <td>{{ m.model }}</td>
            <td>{{ m.created_at }}</td>
          </tr>
        {% endfor %}
      </table>
    </div>

    <div class="card">
      <h2>Recent Earnings</h2>
      <table class="table">
        <tr><th>ID</th><th>Source</th><th>Amount</th><th>Note</th><th>At</th></tr>
        {% for e in last_10_earnings %}
          <tr>
            <td>{{ e.id }}</td>
            <td>{{ e.source }}</td>
            <td>${{ "%.6f"|format(e.amount) }}</td>
            <td>{{ e.note }}</td>
            <td>{{ e.created_at }}</td>
          </tr>
        {% endfor %}
      </table>
      <form method="post" action="/admin/mock-earning" style="margin-top:8px">
        <div style="display:flex;gap:6px;align-items:center">
          <input class="input" name="amount" placeholder="0.10"/>
          <input class="input" name="source" placeholder="test"/>
          <button class="btn">+ Add earning</button>
        </div>
      </form>
    </div>
  </div>

  <div class="card" style="margin-top:12px">
    <h2>Logs</h2>
    <table class="table">
      <tr><th>ID</th><th>Level</th><th>Message</th><th>At</th></tr>
      {% for l in last_10_logs %}
        <tr>
          <td>{{ l.id }}</td>
          <td>{{ l.level }}</td>
          <td style="max-width:560px;white-space:pre-wrap">{{ l.message }}</td>
          <td>{{ l.created_at }}</td>
        </tr>
      {% endfor %}
    </table>
  </div>

</div>
</body>
"""

# =========================
# Flask Routes
# =========================
@app.before_request
def _track_visit():
    """Credit per unique visit (cookie)."""
    try:
        if request.endpoint in ("static",):
            return
        # only for GET on primary pages
        if request.method == "GET" and request.path in ("/", "/index.html"):
            resp = unique_visit_credit(request)
            if isinstance(resp, type(make_response())):
                # merged response with index render; handled in index()
                pass
    except Exception as e:
        log("ERROR", f"visit track error: {e}")

@app.route("/", methods=["GET"])
def index():
    # build response + maybe set cookie if new visit
    base = render_template_string(
        INDEX_HTML,
        title=f"{APP_NAME} — Ultra AI",
        brand=APP_NAME,
        brand_domain=BRAND_DOMAIN,
        year=year_utc(),
    )
    resp = unique_visit_credit(request)
    if resp and hasattr(resp, "set_cookie"):
        resp.response = [base]
        resp.headers["Content-Type"] = "text/html; charset=utf-8"
        return resp
    return base

@app.route("/health")
def health():
    return jsonify({"ok": True, "time": int(time.time())})

@app.route("/api/ask", methods=["POST"])
def api_ask():
    data = request.get_json(force=True, silent=True) or {}
    prompt = (data.get("prompt") or "").strip()
    if not prompt:
        return jsonify({"ok": False, "error": "empty prompt"}), 400
    metric("ask_api")
    sys = "You are Ganesh A.I., the most helpful multilingual assistant. Keep answers concise but complete."
    ai = smart_generate(prompt, system=sys)
    # Record as anonymous user_id 0 (web)
    record_message(0, "in", prompt)
    record_message(0, "out", ai.text, tokens=ai.tokens, model=ai.model, cost=ai.cost)
    # small earning per interaction (optional)
    earn("web_chat", 0.0002, "api ask")
    return jsonify({"ok": True, "text": ai.text, "model": ai.model})

# simple pixel endpoints for affiliate/cpa postback simulations
@app.route("/pixel/hit")
def pixel_hit():
    metric("pixel_hit")
    earn("pixel", 0.0001, note=f"q={dict(request.args)}")
    return ("", 204)

# ========== Admin ==========
def _is_admin() -> bool:
    return bool(session.get("admin_ok") is True)

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    err = None
    if request.method == "POST":
        u = request.form.get("u", "")
        p = request.form.get("p", "")
        if u == ADMIN_USER and p == ADMIN_PASS:
            session["admin_ok"] = True
            return redirect("/admin/dashboard")
        else:
            err = "Invalid credentials"
    return render_template_string(ADMIN_LOGIN_HTML, title=f"{APP_NAME} Admin", brand=APP_NAME, error=err)

@app.route("/admin/logout")
def admin_logout():
    session.pop("admin_ok", None)
    return redirect("/")

@app.route("/admin/dashboard")
def admin_dashboard():
    if not _is_admin():
        return redirect("/admin/login")
    s = get_stats()
    return render_template_string(
        ADMIN_DASH_HTML,
        title=f"{APP_NAME} Admin",
        brand=APP_NAME,
        users=s["users"],
        messages=s["messages"],
        earnings=s["earnings"],
        last_10_logs=s["last_10_logs"],
        last_10_earnings=s["last_10_earnings"],
        last_10_msgs=s["last_10_msgs"],
    )

@app.route("/admin/mock-earning", methods=["POST"])
def admin_mock_earning():
    if not _is_admin():
        return redirect("/admin/login")
    try:
        amt = float(request.form.get("amount", "0") or "0")
        src = (request.form.get("source", "test") or "test")[:32]
        earn(src, amt, "manual")
    except Exception as e:
        log("ERROR", f"mock earning err: {e}")
    return redirect("/admin/dashboard")


# =========================
# Telegram Bot Handlers
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    metric("tg_start")
    user_id = upsert_user_by_tg(update)
    text = (
        f"👋 Welcome to *{APP_NAME}*!\n"
        "Ask anything (tech, code, business, marketing, school). I reply instantly.\n\n"
        "Commands:\n"
        "• /help — features list\n"
        "• /code <task> — generate production-ready code\n"
        "• /imagine <idea> — image prompt for TTI models\n"
        "• /stats — usage summary\n\n"
        "Tip: just send a message to chat freely."
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    earn("telegram", 0.0001, "start")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    metric("tg_help")
    text = (
        "🧠 *Features*\n"
        "• Chat like ChatGPT (multilingual)\n"
        "• Code generation `/code`\n"
        "• Image prompt maker `/imagine`\n"
        "• Monetization + Admin Panel (web)\n"
        f"• Web app: {BRAND_DOMAIN}\n"
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = get_stats()
    msg = (
        f"📊 *Stats*\nUsers: {s['users']}\nMessages: {s['messages']}\n"
        f"Earnings: ${s['earnings']:.4f}"
    )
    await update.effective_message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def cmd_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = upsert_user_by_tg(update)
    task = " ".join(context.args) if context.args else ""
    if not task:
        await update.effective_message.reply_text("Usage: `/code your requirement here`", parse_mode=ParseMode.MARKDOWN)
        return
    await update.effective_chat.send_action(ChatAction.TYPING)
    p = code_prompt(task)
    ai = smart_generate(p, system="You are a senior software engineer.")
    record_message(user_id, "in", task)
    record_message(user_id, "out", ai.text, model=ai.model, tokens=ai.tokens, cost=ai.cost)
    earn("telegram", 0.0002, "code")
    await update.effective_message.reply_text(ai.text[:4096])

async def cmd_imagine(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = upsert_user_by_tg(update)
    idea = " ".join(context.args) if context.args else ""
    if not idea:
        await update.effective_message.reply_text("Usage: `/imagine a cute robot in rainforest`", parse_mode=ParseMode.MARKDOWN)
        return
    await update.effective_chat.send_action(ChatAction.TYPING)
    p = image_prompt(idea)
    ai = smart_generate(p, system="You are a world-class prompt engineer for image models.")
    record_message(user_id, "in", idea)
    record_message(user_id, "out", ai.text, model=ai.model, tokens=ai.tokens, cost=ai.cost)
    earn("telegram", 0.0002, "imagine")
    await update.effective_message.reply_text("🖼️ Use this prompt in your favorite image generator:\n\n" + ai.text[:3900])

async def tg_free_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = upsert_user_by_tg(update)
    q = update.effective_message.text or ""
    if not q.strip():
        return
    await update.effective_chat.send_action(ChatAction.TYPING)
    ai = smart_generate(q, system="You are Ganesh A.I., extremely helpful and concise.")
    record_message(user_id, "in", q)
    record_message(user_id, "out", ai.text, model=ai.model, tokens=ai.tokens, cost=ai.cost)
    earn("telegram", 0.00015, "chat")
    await update.effective_message.reply_text(ai.text[:4096])

async def cb_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # currently not used; kept for future inline keyboards
    q = update.callback_query
    if q:
        await q.answer("Working…")
        await q.edit_message_text("Action handled.")


def build_application():
    if not TELEGRAM_BOT_TOKEN:
        log("WARNING", "TELEGRAM_BOT_TOKEN missing; bot will not start.")
        return None
    appb = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    appb.add_handler(CommandHandler("start", cmd_start))
    appb.add_handler(CommandHandler("help", cmd_help))
    appb.add_handler(CommandHandler("stats", cmd_stats))
    appb.add_handler(CommandHandler("code", cmd_code))
    appb.add_handler(CommandHandler("imagine", cmd_imagine))
    appb.add_handler(CallbackQueryHandler(cb_action))
    # free text chat
    appb.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, tg_free_chat))
    return appb


# =========================
# Boot: Run Flask + Telegram
# =========================
def run_telegram_in_thread():
    application = build_application()
    if application is None:
        log("INFO", "Telegram not configured; skipping bot start.")
        return
    # run_polling is blocking; run it in a dedicated thread
    def _runner():
        try:
            # NOTE: run_polling manages its own asyncio loop internally in PTB v21
            application.run_polling(allowed_updates=Update.ALL_TYPES, close_loop=True)
        except Exception as e:
            log("ERROR", f"telegram thread crash: {e}")

    t = threading.Thread(target=_runner, name="tg-polling", daemon=True)
    t.start()
    log("INFO", "Telegram polling thread started.")


def main():
    log("INFO", f"{APP_NAME} booting...")
    # Start Telegram
    run_telegram_in_thread()
    # Start Flask (must bind PORT for Render)
    app.run(host=HOST, port=PORT)

if __name__ == "__main__":
    main()
