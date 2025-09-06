# main.py
# =============================================================================
# Ganesh A.I. – Mega Expanded Single-File App
# Web App + Admin Panel + Telegram Bot (Polling) + OpenAI + HF Proxy + SQLite
# =============================================================================
# Why "mega expanded"?
# - Cleaned, production-ish structure but very well-commented so you can follow.
# - Works with python-telegram-bot v21.* using Application.run_polling() in a
#   background thread (no deprecated Updater.wait / start_polling awaits).
# - Keeps UI features (search, prompt, results), Admin panel, Logs view, etc.
# - Friendly error handling + health check + configuration via ENV only.
#
# =============================================================================

import os
import re
import io
import sys
import json
import uuid
import time
import queue
import atexit
import sqlite3 as sqlite
import datetime as dt
import threading
import traceback
from dataclasses import dataclass
from typing import Optional, Dict, Any, Tuple, List

from flask import (
    Flask, request, jsonify, render_template_string,
    session, redirect, url_for, abort
)

from dotenv import load_dotenv

# OpenAI SDK v1.x
from openai import OpenAI

# Telegram (v21)
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters
)

# HTTP client (for optional HF proxy ping)
import httpx

# =============================================================================
# 0) ENV + CONFIG
# =============================================================================

# Load .env if present (Render also injects env)
load_dotenv()

def env(key: str, default: Optional[str] = None) -> str:
    v = os.getenv(key, default)
    if v is None:
        return ""
    return v

# ----- Required / Optional ENV Keys -----
ENV = {
    # Branding / URLs
    "APP_NAME": env("APP_NAME", "Ganesh A.I."),
    "PUBLIC_URL": env("PUBLIC_URL", "https://ai-content-bot.example.com"),
    "BRAND_DOMAIN": env("BRAND_DOMAIN", "https://brand.page/Ganeshagamingworld"),

    # Secrets
    "SECRET_KEY": env("SECRET_KEY", "please-change-me"),
    "ADMIN_USERNAME": env("ADMIN_USERNAME", "admin"),
    "ADMIN_PASSWORD": env("ADMIN_PASSWORD", "admin123"),

    # OpenAI
    "OPENAI_API_KEY": env("OPENAI_API_KEY", ""),
    "OPENAI_MODEL": env("OPENAI_MODEL", "gpt-4o-mini"),
    "OPENAI_TIMEOUT": env("OPENAI_TIMEOUT", "60"),

    # HuggingFace Proxy (as per your credentials)
    "HUGGINGFACE_API_URL": env("HUGGINGFACE_API_URL", "https://candyai.com/artificialagents"),
    "HUGGINGFACE_API_TOKEN": env("HUGGINGFACE_API_TOKEN", ""),

    # Telegram
    "TELEGRAM_BOT_TOKEN": env("TELEGRAM_BOT_TOKEN", ""),  # e.g. 8377....:AA...
    "TELEGRAM_POLLING": env("TELEGRAM_POLLING", "true"),  # keep polling by default
    "TELEGRAM_COMMAND_PREFIX": env("TELEGRAM_COMMAND_PREFIX", "/"),

    # Database / Runtime
    "SQLITE_PATH": env("SQLITE_PATH", "app.db"),
    "LOG_LEVEL": env("LOG_LEVEL", "INFO"),
    "PORT": env("PORT", "10000"),
}

# Derived config
APP_NAME = ENV["APP_NAME"]
PORT = int(ENV["PORT"])
OPENAI_TIMEOUT = int(ENV["OPENAI_TIMEOUT"])
USE_TELEGRAM = bool(ENV["TELEGRAM_BOT_TOKEN"])
TELEGRAM_POLLING = ENV["TELEGRAM_POLLING"].lower() == "true"

# =============================================================================
# 1) LOGGING
# =============================================================================

LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}

def log(level: str, msg: str):
    """Simple structured log with level/time/app-name."""
    lvl = level.upper()
    if LEVELS.get(lvl, 20) < LEVELS.get(ENV["LOG_LEVEL"].upper(), 20):
        return
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{now} | {lvl:<5} | {APP_NAME} | {msg}", flush=True)

# =============================================================================
# 2) DATABASE
# =============================================================================

DB_PATH = ENV["SQLITE_PATH"]

def db_connect():
    # Note: SQLite default adapter deprecation in Python 3.12 warnings
    # are harmless for typical usage; acceptable in this small app.
    conn = sqlite.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite.Row
    return conn

def db_init():
    conn = db_connect()
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              telegram_id TEXT UNIQUE,
              username TEXT,
              created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS logs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              source TEXT,          -- 'web' | 'admin' | 'telegram'
              level TEXT,           -- 'INFO', 'ERROR'
              message TEXT,
              meta TEXT,            -- JSON
              created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS contents (
              id TEXT PRIMARY KEY,  -- uuid
              user_source TEXT,     -- 'web' | 'admin' | 'telegram'
              input_prompt TEXT,
              output_text TEXT,
              created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
        """)
    conn.close()

def db_log(source: str, level: str, message: str, meta: Optional[dict] = None):
    try:
        conn = db_connect()
        with conn:
            conn.execute(
                "INSERT INTO logs (source, level, message, meta) VALUES (?, ?, ?, ?)",
                (source, level, message, json.dumps(meta or {}))
            )
    except Exception as e:
        log("ERROR", f"db_log failed: {e}")

def db_save_content(user_source: str, prompt: str, output: str) -> str:
    _id = str(uuid.uuid4())
    try:
        conn = db_connect()
        with conn:
            conn.execute(
                "INSERT INTO contents (id, user_source, input_prompt, output_text) VALUES (?, ?, ?, ?)",
                (_id, user_source, prompt, output)
            )
        return _id
    except Exception as e:
        log("ERROR", f"db_save_content failed: {e}")
        return _id

def db_recent_logs(limit: int = 100) -> List[sqlite.Row]:
    try:
        conn = db_connect()
        cur = conn.execute("SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,))
        rows = cur.fetchall()
        conn.close()
        return rows
    except Exception as e:
        log("ERROR", f"db_recent_logs failed: {e}")
        return []

def db_recent_contents(limit: int = 50) -> List[sqlite.Row]:
    try:
        conn = db_connect()
        cur = conn.execute("SELECT * FROM contents ORDER BY created_at DESC LIMIT ?", (limit,))
        rows = cur.fetchall()
        conn.close()
        return rows
    except Exception as e:
        log("ERROR", f"db_recent_contents failed: {e}")
        return []

# Initialize DB
db_init()

# =============================================================================
# 3) OPENAI + HF CLIENTS
# =============================================================================

OPENAI_AVAILABLE = bool(ENV["OPENAI_API_KEY"])
HF_AVAILABLE = bool(ENV["HUGGINGFACE_API_URL"] and ENV["HUGGINGFACE_API_TOKEN"])

client: Optional[OpenAI] = None
if OPENAI_AVAILABLE:
    try:
        client = OpenAI(api_key=ENV["OPENAI_API_KEY"])
        log("INFO", "OpenAI client initialized.")
    except Exception as e:
        log("ERROR", f"OpenAI init failed: {e}")

def call_openai(prompt: str, sys_prompt: Optional[str] = None) -> str:
    """Text generation using Chat Completions (Responses API wrapper)."""
    if not client:
        raise RuntimeError("OpenAI client not configured")
    try:
        msgs = []
        if sys_prompt:
            msgs.append({"role": "system", "content": sys_prompt})
        msgs.append({"role": "user", "content": prompt})

        resp = client.chat.completions.create(
            model=ENV["OPENAI_MODEL"],
            messages=msgs,
            temperature=0.7,
            max_tokens=800,
            timeout=OPENAI_TIMEOUT,
        )
        out = (resp.choices[0].message.content or "").strip()
        return out
    except Exception as e:
        raise RuntimeError(f"OpenAI error: {e}")

def call_hf_proxy(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Optional call to your Hugging Face proxy endpoint (if you decide to use it)."""
    if not HF_AVAILABLE:
        raise RuntimeError("HF proxy not configured")
    headers = {
        "Authorization": f"Bearer {ENV['HUGGINGFACE_API_TOKEN']}",
        "Content-Type": "application/json"
    }
    try:
        r = httpx.post(
            ENV["HUGGINGFACE_API_URL"],
            headers=headers,
            json=payload,
            timeout=60
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        raise RuntimeError(f"HF proxy error: {e}")

# =============================================================================
# 4) PROMPT PRESETS
# =============================================================================

DEFAULT_SYS_PROMPT = (
    "You are Ganesh A.I., a helpful content assistant that writes crisp, "
    "SEO-friendly, original content. Keep answers structured with headings "
    "and bullet points when useful."
)

PRESETS = {
    "yt_script": "Write a YouTube video script on: {topic}\nTone: engaging, fast-paced\nInclude: hook, key points, CTA.",
    "yt_description": "Write a YouTube description for the video titled: {topic}\nInclude: summary, keywords, hashtags.",
    "insta_captions": "Write 5 concise Instagram captions about: {topic}\nStyle: trendy + emoji.\nAdd 8-12 relevant hashtags.",
    "blog_outline": "Create a detailed blog outline on: {topic}\nInclude H2/H3s and bullet points.",
    "ad_copy": "Write 3 Facebook ad copies for: {topic}\nEach: 2-3 sentences, strong CTA.",
}

# =============================================================================
# 5) FLASK APP
# =============================================================================

app = Flask(__name__)
app.secret_key = ENV["SECRET_KEY"]

# --------------------- HTML Templates (Jinja in-code) ------------------------

BASE_CSS = """
:root{
  --bg:#0b1020; --card:#121833; --text:#e9ecff; --muted:#b6b9d8; --acc:#7aa2ff;
  --ok:#20c997; --warn:#ffb020; --err:#ff6b6b;
  --bord: rgba(255,255,255,0.08);
}
*{box-sizing:border-box}
body{margin:0;font:15px/1.5 ui-sans-serif,system-ui,Segoe UI,Roboto;color:var(--text);background:radial-gradient(80% 120% at 50% -10%,#182048 0%,#0b1020 60%) fixed}
a{color:var(--acc);text-decoration:none}
.container{max-width:1100px;margin:32px auto;padding:0 16px}
.nav{display:flex;gap:18px;align-items:center;justify-content:space-between}
.brand{display:flex;gap:10px;align-items:center}
.brand .logo{width:36px;height:36px;border-radius:12px;background:linear-gradient(135deg,#7aa2ff, #53f3c3)}
.card{background:var(--card);border:1px solid var(--bord);border-radius:18px;padding:18px;box-shadow:0 20px 60px rgba(0,0,0,.25)}
.grid{display:grid;gap:18px}
.grid-2{grid-template-columns:1.2fr .8fr}
input,textarea,select,button{width:100%;padding:12px 14px;border-radius:12px;border:1px solid var(--bord);background:#0d1430;color:var(--text)}
button{cursor:pointer;background:linear-gradient(135deg,#7aa2ff,#53f3c3);border:none;font-weight:600}
button.secondary{background:#0d1430}
.badge{font-size:12px;padding:4px 8px;border-radius:999px;border:1px solid var(--bord);color:var(--muted)}
.small{color:var(--muted);font-size:13px}
hr{border:none;border-top:1px solid var(--bord);margin:14px 0}
.kv{display:grid;grid-template-columns:160px 1fr;gap:8px;align-items:center}
.logline{border-left:3px solid var(--bord);padding-left:10px;margin:8px 0;color:#c7ccff}
.logline.INFO{border-color:#3b82f6}
.logline.ERROR{border-color:#ef4444}
footer{margin:24px 0 12px;color:#9aa0c2;text-align:center}
.code{font-family:ui-monospace, SFMono-Regular, Menlo, Consolas; background:#0a0f24;border:1px solid var(--bord);border-radius:12px;padding:12px;white-space:pre-wrap}
"""

INDEX_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{{ app_name }} — AI Content Studio</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>{{ css }}</style>
</head>
<body>
  <div class="container">
    <div class="nav">
      <div class="brand">
        <div class="logo"></div>
        <div>
          <div style="font-weight:700">{{ app_name }}</div>
          <div class="small">Create scripts, captions & more.</div>
        </div>
      </div>
      <div class="small">
        <a href="{{ brand_domain }}" target="_blank">Brand Page</a> •
        <a href="/admin">Admin</a>
      </div>
    </div>

    <div class="grid grid-2" style="margin-top:18px">
      <div class="card">
        <div style="display:flex;gap:8px;align-items:center;margin-bottom:10px">
          <div class="badge">Home</div>
          <div class="small">Search + Generate</div>
        </div>

        <label>Search Topic</label>
        <input id="search" placeholder="e.g. Budget gaming PC under ₹50k, GTA 6 news..." />

        <div style="display:grid;grid-template-columns:1fr 200px; gap:12px; margin-top:10px">
          <input id="prompt" placeholder="What do you want to generate?" value="Write a YouTube script on this topic." />
          <select id="preset">
            <option value="">-- Presets --</option>
            <option value="yt_script">YouTube Script</option>
            <option value="yt_description">YouTube Description</option>
            <option value="insta_captions">Instagram Captions</option>
            <option value="blog_outline">Blog Outline</option>
            <option value="ad_copy">Ad Copy</option>
          </select>
        </div>

        <div style="display:flex;gap:12px;margin-top:12px">
          <button id="btnGen">Generate</button>
          <button id="btnClear" class="secondary">Clear</button>
        </div>

        <hr>
        <div id="result" class="card" style="background:#0a0f24"></div>
      </div>

      <div class="card">
        <div style="display:flex;gap:8px;align-items:center;margin-bottom:10px">
          <div class="badge">Status</div>
          <div class="small">Runtime & Config</div>
        </div>

        <div class="kv small">
          <div>Model</div><div>{{ model }}</div>
          <div>OpenAI</div><div>{{ 'ON' if openai_on else 'OFF' }}</div>
          <div>HF Proxy</div><div>{{ 'ON' if hf_on else 'OFF' }}</div>
          <div>Telegram</div><div>{{ 'ON' if telegram_on else 'OFF' }}</div>
          <div>Public URL</div><div><a href="{{ public_url }}" target="_blank">{{ public_url }}</a></div>
        </div>

        <hr>
        <div class="small">Recent Logs</div>
        <div id="logs">
          {% for row in logs %}
            <div class="logline {{ row['level'] }}"><b>[{{ row['source'] }}]</b> {{ row['message'] }}</div>
          {% endfor %}
        </div>
      </div>
    </div>

    <footer>© {{ year }} {{ app_name }}</footer>
  </div>

<script>
const $ = (q)=>document.querySelector(q);
const result = $("#result");
const logs = $("#logs");

$("#btnClear").onclick = () => {
  $("#search").value = "";
  $("#prompt").value = "";
  result.innerHTML = "";
};

$("#preset").onchange = () => {
  const v = $("#preset").value;
  const topic = $("#search").value.trim() || "your topic";
  const presets = {{ presets_json | safe }};
  if (v && presets[v]) {
    $("#prompt").value = presets[v].replace("{topic}", topic);
  }
};

$("#btnGen").onclick = async () => {
  const topic = $("#search").value.trim();
  const prompt = $("#prompt").value.trim();
  if (!topic && !prompt) {
    alert("Enter a topic or a prompt.");
    return;
  }
  result.innerHTML = "<div class='small'>Generating...</div>";

  try {
    const r = await fetch("/api/generate", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ topic, prompt })
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || "Failed");
    const out = j.output || "";
    result.innerHTML = "<div class='code' style='white-space:pre-wrap'>" + out.replace(/</g,'&lt;') + "</div>";
  } catch (e) {
    result.innerHTML = "<div class='logline ERROR'>"+ e.message +"</div>";
  }
};
</script>
</body>
</html>
"""

ADMIN_LOGIN_HTML = """
<!doctype html>
<html><head><meta charset="utf-8"><title>Admin Login</title>
<style>{{ css }}</style></head>
<body>
<div class="container">
  <div class="nav">
    <div class="brand">
      <div class="logo"></div>
      <div>
        <div style="font-weight:700">{{ app_name }}</div>
        <div class="small">Admin Panel</div>
      </div>
    </div>
    <div class="small"><a href="/">Home</a></div>
  </div>

  <div class="card" style="max-width:520px;margin:24px auto">
    <div class="badge">Login</div><br>
    {% if error %}<div class="logline ERROR">{{ error }}</div>{% endif %}
    <form method="post">
      <label>Username</label>
      <input name="username" placeholder="admin">
      <label>Password</label>
      <input name="password" type="password" placeholder="••••••••">
      <div style="display:flex;gap:10px;margin-top:12px">
        <button type="submit">Sign in</button>
        <a class="small" href="{{ brand_domain }}" target="_blank">Brand Page</a>
      </div>
    </form>
  </div>
  <footer>© {{ year }} {{ app_name }}</footer>
</div>
</body></html>
"""

ADMIN_HTML = """
<!doctype html>
<html><head><meta charset="utf-8"><title>Admin — {{ app_name }}</title>
<style>{{ css }}</style></head>
<body>
<div class="container">
  <div class="nav">
    <div class="brand">
      <div class="logo"></div>
      <div>
        <div style="font-weight:700">{{ app_name }}</div>
        <div class="small">Admin Panel</div>
      </div>
    </div>
    <div class="small"><a href="/">Home</a> • <a href="/admin/logout">Logout</a></div>
  </div>

  <div class="grid">
    <div class="card">
      <div class="badge">Quick Generate</div>
      <div class="small">Create content directly from admin.</div>
      <div style="display:grid;grid-template-columns:1fr 220px; gap:12px; margin-top:10px">
        <input id="topic" placeholder="Topic e.g. PUBG montage script">
        <select id="preset">
          <option value="">-- Presets --</option>
          {% for k in presets.keys() %}
           <option value="{{ k }}">{{ k }}</option>
          {% endfor %}
        </select>
      </div>
      <label style="margin-top:10px">Prompt</label>
      <textarea id="prompt" rows="6" placeholder="Or write a custom instruction..."></textarea>
      <div style="display:flex; gap:10px; margin-top:10px">
        <button id="btnGen">Generate</button>
        <button class="secondary" id="btnClear">Clear</button>
      </div>
      <hr>
      <div id="out" class="code"></div>
    </div>

    <div class="card">
      <div class="badge">System</div>
      <div class="small">Status, Logs, Tools</div>
      <div class="kv small">
        <div>Model</div><div>{{ model }}</div>
        <div>OpenAI</div><div>{{ 'ON' if openai_on else 'OFF' }}</div>
        <div>HF Proxy</div><div>{{ 'ON' if hf_on else 'OFF' }}</div>
        <div>Telegram</div><div>{{ 'ON' if telegram_on else 'OFF' }}</div>
        <div>Public URL</div><div><a href="{{ public_url }}" target="_blank">{{ public_url }}</a></div>
      </div>
      <hr>
      <div class="small">Actions</div>
      <div style="display:grid; grid-template-columns:1fr 1fr; gap:10px">
        <button id="btnPing" class="secondary">Ping HF Proxy</button>
        <button id="btnSendTG" class="secondary">Send Test Telegram</button>
      </div>

      <hr>
      <div class="small">Recent Logs</div>
      <div id="logs">{% for row in logs %}
        <div class="logline {{ row['level'] }}"><b>[{{ row['source'] }}]</b> {{ row['message'] }}</div>
      {% endfor %}</div>
    </div>

    <div class="card">
      <div class="badge">Recent Contents</div>
      <div class="small">Last 20 items</div>
      <div id="contents">
        {% for c in contents %}
          <div class="logline INFO"><b>{{ c['user_source'] }}</b> — {{ c['input_prompt'][:100] }}...
          <div class="small" style="opacity:.8;margin-top:6px">{{ c['output_text'][:200] }}...</div></div>
        {% endfor %}
      </div>
    </div>
  </div>

  <footer>© {{ year }} {{ app_name }}</footer>
</div>

<script>
const $ = (q)=>document.querySelector(q);
const out = $("#out");

$("#btnClear").onclick = () => { $("#topic").value=""; $("#prompt").value=""; out.textContent=""; };
$("#preset").onchange = () => {
  const v = $("#preset").value;
  const topic = $("#topic").value.trim() || "your topic";
  const presets = {{ presets_json | safe }};
  if (v && presets[v]) $("#prompt").value = presets[v].replace("{topic}", topic);
};

$("#btnGen").onclick = async () => {
  const topic = $("#topic").value.trim();
  const prompt = $("#prompt").value.trim();
  if (!topic && !prompt) { alert("Enter topic or prompt"); return; }
  out.textContent = "Generating...";
  const r = await fetch("/api/admin/generate", {
    method:"POST",
    headers:{"Content-Type":"application/json"},
    body: JSON.stringify({topic, prompt})
  });
  const j = await r.json();
  if (!r.ok) { out.textContent = "Error: "+(j.error||"Failed"); return; }
  out.textContent = j.output || "";
};

$("#btnPing").onclick = async () => {
  const r = await fetch("/api/admin/ping-hf", {method:"POST"});
  const j = await r.json();
  alert(j.ok ? "HF OK: "+j.detail : "HF Error: "+j.error);
};

$("#btnSendTG").onclick = async () => {
  const text = prompt("Enter test message for Telegram:");
  if (!text) return;
  const r = await fetch("/api/admin/send-telegram", {
    method:"POST",
    headers:{"Content-Type":"application/json"},
    body: JSON.stringify({ text })
  });
  const j = await r.json();
  alert(j.ok ? "Sent!" : "Error: "+j.error);
};
</script>
</body></html>
"""

# --------------------- Helpers ---------------------

def is_admin() -> bool:
    return session.get("admin_logged", False) is True

def require_admin():
    if not is_admin():
        abort(403, description="Admin only")

# --------------------- Routes ----------------------

@app.get("/")
def index():
    logs = db_recent_logs(20)
    return render_template_string(
        INDEX_HTML,
        css=BASE_CSS,
        app_name=APP_NAME,
        brand_domain=ENV["BRAND_DOMAIN"],
        public_url=ENV["PUBLIC_URL"],
        model=ENV["OPENAI_MODEL"],
        openai_on=OPENAI_AVAILABLE,
        hf_on=HF_AVAILABLE,
        telegram_on=USE_TELEGRAM,
        year=dt.datetime.now(dt.UTC).year if hasattr(dt, "UTC") else dt.datetime.utcnow().year,
        logs=logs,
        presets_json=json.dumps(PRESETS),
    )

@app.get("/healthz")
def healthz():
    status = {
        "app": APP_NAME,
        "time": dt.datetime.now().isoformat(),
        "openai": OPENAI_AVAILABLE,
        "hf_proxy": HF_AVAILABLE,
        "telegram": USE_TELEGRAM,
        "db": True
    }
    return jsonify(status)

# ----- Auth -----

@app.get("/admin/login")
def admin_login_get():
    if is_admin():
        return redirect(url_for("admin_home"))
    return render_template_string(
        ADMIN_LOGIN_HTML,
        css=BASE_CSS, app_name=APP_NAME, brand_domain=ENV["BRAND_DOMAIN"],
        year=dt.datetime.now(dt.UTC).year if hasattr(dt, "UTC") else dt.datetime.utcnow().year,
        error=None
    )

@app.post("/admin/login")
def admin_login_post():
    u = request.form.get("username","").strip()
    p = request.form.get("password","")
    if u == ENV["ADMIN_USERNAME"] and p == ENV["ADMIN_PASSWORD"]:
        session["admin_logged"] = True
        db_log("web", "INFO", "Admin logged in", {"user": u})
        return redirect(url_for("admin_home"))
    db_log("web", "WARN", "Admin login failed", {"user": u})
    return render_template_string(
        ADMIN_LOGIN_HTML,
        css=BASE_CSS, app_name=APP_NAME, brand_domain=ENV["BRAND_DOMAIN"],
        year=dt.datetime.now(dt.UTC).year if hasattr(dt, "UTC") else dt.datetime.utcnow().year,
        error="Invalid credentials"
    )

@app.get("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login_get"))

@app.get("/admin")
def admin_home():
    if not is_admin():
        return redirect(url_for("admin_login_get"))
    logs = db_recent_logs(40)
    contents = db_recent_contents(20)
    return render_template_string(
        ADMIN_HTML,
        css=BASE_CSS,
        app_name=APP_NAME,
        public_url=ENV["PUBLIC_URL"],
        model=ENV["OPENAI_MODEL"],
        openai_on=OPENAI_AVAILABLE,
        hf_on=HF_AVAILABLE,
        telegram_on=USE_TELEGRAM,
        year=dt.datetime.now(dt.UTC).year if hasattr(dt, "UTC") else dt.datetime.utcnow().year,
        logs=logs,
        contents=contents,
        presets=PRESETS,
        presets_json=json.dumps(PRESETS),
    )

# ----- APIs -----

@app.post("/api/generate")
def api_generate():
    data = request.get_json(force=True, silent=True) or {}
    topic = (data.get("topic") or "").strip()
    prompt = (data.get("prompt") or "").strip()

    if not (topic or prompt):
        return jsonify({"error":"topic or prompt required"}), 400

    # Expand preset if user has typed only topic with default prompt
    final_prompt = prompt or PRESETS["yt_script"].replace("{topic}", topic)
    try:
        output = call_openai(final_prompt, DEFAULT_SYS_PROMPT) if OPENAI_AVAILABLE else f"(OpenAI OFF)\n{final_prompt}"
        db_log("web", "INFO", "Generated content", {"len": len(output)})
        _id = db_save_content("web", final_prompt, output)
        return jsonify({"ok":True, "id":_id, "output":output})
    except Exception as e:
        db_log("web", "ERROR", "Generate failed", {"err": str(e)})
        return jsonify({"error": str(e)}), 500

@app.post("/api/admin/generate")
def api_admin_generate():
    if not is_admin():
        return jsonify({"error": "forbidden"}), 403
    data = request.get_json(force=True, silent=True) or {}
    topic = (data.get("topic") or "").strip()
    prompt = (data.get("prompt") or "").strip()
    if not (topic or prompt):
        return jsonify({"error":"topic or prompt required"}), 400
    final_prompt = prompt or PRESETS["yt_script"].replace("{topic}", topic)
    try:
        output = call_openai(final_prompt, DEFAULT_SYS_PROMPT) if OPENAI_AVAILABLE else f"(OpenAI OFF)\n{final_prompt}"
        db_log("admin", "INFO", "Admin generated", {"len": len(output)})
        _id = db_save_content("admin", final_prompt, output)
        return jsonify({"ok":True, "id":_id, "output":output})
    except Exception as e:
        db_log("admin", "ERROR", "Admin generate failed", {"err": str(e)})
        return jsonify({"error": str(e)}), 500

@app.post("/api/admin/ping-hf")
def api_admin_ping_hf():
    if not is_admin():
        return jsonify({"error": "forbidden"}), 403
    if not HF_AVAILABLE:
        return jsonify({"ok": False, "error": "HF proxy not configured"})
    try:
        resp = call_hf_proxy({"ping":"ok"})
        return jsonify({"ok": True, "detail": str(resp)[:200]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.post("/api/admin/send-telegram")
def api_admin_send_telegram():
    if not is_admin():
        return jsonify({"error":"forbidden"}), 403
    if not USE_TELEGRAM:
        return jsonify({"error":"telegram not configured"}), 400
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("text") or "").strip() or "Test message from Admin Panel."
    # We can't send to a user unless we know chat_id.
    # So we store last_chat_id from recent telegram activity (see handler).
    cid = tg_last_chat_id()
    if not cid:
        return jsonify({"error": "No chat_id yet. Send a message to bot first."}), 400
    try:
        tg_send_message(cid, f"🔔 Admin test: {text}")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# =============================================================================
# 6) TELEGRAM BOT (Polling Thread, PTB v21)
# =============================================================================

_TELEGRAM_APPLICATION: Optional[Application] = None
_TELEGRAM_THREAD: Optional[threading.Thread] = None
_LAST_CHAT_ID_PATH = ".last_chat_id"

def tg_store_chat_id(chat_id: int):
    try:
        with open(_LAST_CHAT_ID_PATH, "w", encoding="utf-8") as f:
            f.write(str(chat_id))
    except Exception as e:
        log("ERROR", f"store chat id failed: {e}")

def tg_last_chat_id() -> Optional[int]:
    try:
        if not os.path.exists(_LAST_CHAT_ID_PATH):
            return None
        with open(_LAST_CHAT_ID_PATH, "r", encoding="utf-8") as f:
            s = f.read().strip()
            return int(s) if s else None
    except Exception:
        return None

def tg_send_message(chat_id: int, text: str):
    """Thread-safe helper using application.bot directly."""
    if not _TELEGRAM_APPLICATION:
        raise RuntimeError("Telegram app not ready")
    bot = _TELEGRAM_APPLICATION.bot
    return bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)

async def tg_cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id if update.effective_chat else None
    if cid: tg_store_chat_id(cid)
    user = update.effective_user
    db_log("telegram", "INFO", "Start cmd", {"user": user.username if user else None})
    msg = (
        f"👋 Welcome to <b>{APP_NAME}</b>!\n"
        f"Send me a topic or use commands:\n"
        f"<code>{ENV['TELEGRAM_COMMAND_PREFIX']}script</code> – YouTube script\n"
        f"<code>{ENV['TELEGRAM_COMMAND_PREFIX']}desc</code> – YouTube description\n"
        f"<code>{ENV['TELEGRAM_COMMAND_PREFIX']}insta</code> – Instagram captions\n"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def tg_cmd_script(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id if update.effective_chat else None
    if cid: tg_store_chat_id(cid)
    text = " ".join(context.args) if context.args else (update.message.text or "")
    topic = text.replace("/script","").strip()
    if not topic:
        await update.message.reply_text("Send: /script your topic")
        return
    prompt = PRESETS["yt_script"].replace("{topic}", topic)
    try:
        out = call_openai(prompt, DEFAULT_SYS_PROMPT) if OPENAI_AVAILABLE else f"(OpenAI OFF)\n{prompt}"
        db_save_content("telegram", prompt, out)
        await update.message.reply_text(out[:4096])
    except Exception as e:
        db_log("telegram", "ERROR", "script failed", {"err": str(e)})
        await update.message.reply_text(f"Error: {e}")

async def tg_cmd_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id if update.effective_chat else None
    if cid: tg_store_chat_id(cid)
    topic = " ".join(context.args) if context.args else (update.message.text or "")
    topic = topic.replace("/desc","").strip()
    if not topic:
        await update.message.reply_text("Send: /desc video title/topic")
        return
    prompt = PRESETS["yt_description"].replace("{topic}", topic)
    try:
        out = call_openai(prompt, DEFAULT_SYS_PROMPT) if OPENAI_AVAILABLE else f"(OpenAI OFF)\n{prompt}"
        db_save_content("telegram", prompt, out)
        await update.message.reply_text(out[:4096])
    except Exception as e:
        db_log("telegram", "ERROR", "desc failed", {"err": str(e)})
        await update.message.reply_text(f"Error: {e}")

async def tg_cmd_insta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id if update.effective_chat else None
    if cid: tg_store_chat_id(cid)
    topic = " ".join(context.args) if context.args else (update.message.text or "")
    topic = topic.replace("/insta","").strip()
    if not topic:
        await update.message.reply_text("Send: /insta topic")
        return
    prompt = PRESETS["insta_captions"].replace("{topic}", topic)
    try:
        out = call_openai(prompt, DEFAULT_SYS_PROMPT) if OPENAI_AVAILABLE else f"(OpenAI OFF)\n{prompt}"
        db_save_content("telegram", prompt, out)
        await update.message.reply_text(out[:4096])
    except Exception as e:
        db_log("telegram", "ERROR", "insta failed", {"err": str(e)})
        await update.message.reply_text(f"Error: {e}")

async def tg_echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fallback: when user just sends a topic, we generate a script."""
    cid = update.effective_chat.id if update.effective_chat else None
    if cid: tg_store_chat_id(cid)
    text = (update.message.text or "").strip()
    if not text:
        return
    prompt = PRESETS["yt_script"].replace("{topic}", text)
    try:
        out = call_openai(prompt, DEFAULT_SYS_PROMPT) if OPENAI_AVAILABLE else f"(OpenAI OFF)\n{prompt}"
        db_save_content("telegram", prompt, out)
        await update.message.reply_text(out[:4096])
    except Exception as e:
        db_log("telegram", "ERROR", "echo failed", {"err": str(e)})
        await update.message.reply_text(f"Error: {e}")

def start_telegram_polling_in_thread():
    global _TELEGRAM_APPLICATION, _TELEGRAM_THREAD
    if not USE_TELEGRAM or not TELEGRAM_POLLING:
        log("INFO", "Telegram polling disabled or token missing.")
        return

    # Build application
    _TELEGRAM_APPLICATION = Application.builder().token(ENV["TELEGRAM_BOT_TOKEN"]).build()

    # Handlers
    _TELEGRAM_APPLICATION.add_handler(CommandHandler("start", tg_cmd_start))
    _TELEGRAM_APPLICATION.add_handler(CommandHandler("script", tg_cmd_script))
    _TELEGRAM_APPLICATION.add_handler(CommandHandler("desc", tg_cmd_desc))
    _TELEGRAM_APPLICATION.add_handler(CommandHandler("insta", tg_cmd_insta))
    _TELEGRAM_APPLICATION.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), tg_echo))

    def _runner():
        try:
            db_log("telegram", "INFO", "Polling thread starting", {})
            # Blocking call (sync), correct for v21
            _TELEGRAM_APPLICATION.run_polling(
                allowed_updates=Update.ALL_TYPES,
                close_loop=False,  # we're in our own thread
                stop_signals=None
            )
            db_log("telegram", "INFO", "Polling stopped", {})
        except Exception as e:
            db_log("telegram", "ERROR", f"polling crashed: {e}", {"trace": traceback.format_exc()})

    _TELEGRAM_THREAD = threading.Thread(target=_runner, name="telegram-polling", daemon=True)
    _TELEGRAM_THREAD.start()
    log("INFO", "Telegram handlers registered. Mode: POLLING")

def stop_telegram():
    global _TELEGRAM_APPLICATION
    try:
        if _TELEGRAM_APPLICATION:
            _TELEGRAM_APPLICATION.stop()
            db_log("telegram", "INFO", "Application.stop() invoked", {})
    except Exception as e:
        log("ERROR", f"Telegram stop error: {e}")

atexit.register(stop_telegram)

# =============================================================================
# 7) APP STARTUP
# =============================================================================

def boot_banner():
    log("INFO", f"App: {APP_NAME}")
    log("INFO", f"Public URL: {ENV['PUBLIC_URL']}")
    log("INFO", f"Brand Page: {ENV['BRAND_DOMAIN']}")
    log("INFO", f"OpenAI: {'ON' if OPENAI_AVAILABLE else 'OFF'} ({ENV['OPENAI_MODEL']})")
    log("INFO", f"HF Proxy: {'ON' if HF_AVAILABLE else 'OFF'}")
    log("INFO", f"Telegram: {'ON' if USE_TELEGRAM else 'OFF'}; Polling: {TELEGRAM_POLLING}")
    db_log("web", "INFO", "App boot", {"openai": OPENAI_AVAILABLE, "telegram": USE_TELEGRAM})

# =============================================================================
# 8) MAIN
# =============================================================================

if __name__ == "__main__":
    boot_banner()
    # Start Telegram polling in background (non-async, correct for v21)
    start_telegram_polling_in_thread()

    # Run Flask
    # Note: Render uses PORT env and forwards traffic. Built-in server is OK for simple apps.
    app.run(host="0.0.0.0", port=PORT, debug=False)
