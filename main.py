import os
import json
import time
import csv
import threading
import asyncio
import traceback
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, request, jsonify, session, redirect, url_for, render_template_string, make_response, abort

# =============== Logging Helper ===============
def log(level, msg):
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} | {level:<5} | Ganesh A.I. | {msg}", flush=True)

# =============== ENV & Flags ===============
APP_NAME = os.getenv("APP_NAME", "Ganesh A.I.")
PUBLIC_URL = os.getenv("PUBLIC_URL", "https://ai-content-bot.example.com")
BRAND_URL = os.getenv("BRAND_URL", "https://brand.page/Ganeshagamingworld")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
HF_PROXY = os.getenv("HF_PROXY", "1").strip() in ("1", "true", "True")
HF_API_URL = os.getenv("HF_API_URL", "http://127.0.0.1:3000")  # optional
SECRET_KEY = os.getenv("SECRET_KEY", "change_this_secret")
PORT = int(os.getenv("PORT", "10000"))
HOST = "0.0.0.0"

# Admin creds (as requested)
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "admin123")

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_POLLING = os.getenv("TELEGRAM_POLLING", "1").strip() in ("1", "true", "True")

# Faucet / Credits
DAILY_FAUCET = int(os.getenv("DAILY_FAUCET", "25"))  # credits per day
NEW_USER_CREDITS = int(os.getenv("NEW_USER_CREDITS", "50"))

# =============== Flask App ===============
app = Flask(__name__)
app.secret_key = SECRET_KEY

# =============== SQLite DB ===============
import sqlite3
DB_FILE = os.getenv("DB_FILE", "data.db")

def db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE,
        credits INTEGER DEFAULT 0,
        telegram_chat_id TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS logs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel TEXT,
        level TEXT,
        message TEXT,
        extra JSON,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS settings(
        key TEXT PRIMARY KEY,
        value TEXT
    );

    CREATE TABLE IF NOT EXISTS chats(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user TEXT,
        prompt TEXT,
        response TEXT,
        tokens_in INTEGER DEFAULT 0,
        tokens_out INTEGER DEFAULT 0,
        provider TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    """)
    conn.commit()
    conn.close()

def db_log(channel, level, message, extra=None):
    try:
        conn = db()
        cur = conn.cursor()
        cur.execute("INSERT INTO logs(channel, level, message, extra) VALUES (?,?,?,?)",
                    (channel, level, message, json.dumps(extra or {})))
        conn.commit()
        conn.close()
    except Exception as e:
        log("ERROR", f"db_log failed: {e}")

init_db()

# =============== OpenAI Client ===============
USE_OPENAI = bool(OPENAI_API_KEY)
if USE_OPENAI:
    try:
        from openai import OpenAI
        oai = OpenAI(api_key=OPENAI_API_KEY)
        log("INFO", "OpenAI client initialized.")
    except Exception as e:
        log("ERROR", f"OpenAI init failed: {e}")
        USE_OPENAI = False
else:
    log("INFO", "OpenAI: OFF (no API key)")

# =============== Simple Model Call Abstractions ===============
def call_openai(prompt):
    if not USE_OPENAI:
        return "OpenAI key missing. Please set OPENAI_API_KEY."
    try:
        # lightweight responses
        r = oai.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "system", "content": "You are a helpful assistant."},
                      {"role": "user", "content": prompt}],
            temperature=0.6,
            max_tokens=500
        )
        return r.choices[0].message.content.strip()
    except Exception as e:
        db_log("openai", "ERROR", "openai call failed", {"err": str(e)})
        return f"[OpenAI Error] {e}"

def call_hf_proxy(prompt):
    # Dummy fallback implementation; user’s infra can point HF_API_URL to a TGI server.
    # Here we just echo if proxy is unreachable.
    try:
        import httpx
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(f"{HF_API_URL}/generate", json={"inputs": prompt})
            if resp.status_code == 200:
                data = resp.json()
                # accept both TGI style and generic
                if isinstance(data, dict) and "generated_text" in data:
                    return data["generated_text"]
                if isinstance(data, list) and data and "generated_text" in data[0]:
                    return data[0]["generated_text"]
                return str(data)
            return f"[HF Proxy HTTP {resp.status_code}] {resp.text}"
    except Exception as e:
        db_log("hf", "ERROR", "hf proxy failed", {"err": str(e)})
        return "[HF Proxy Error] (falling back) " + str(e)

def generate_text(prompt, user="web"):
    provider = "openai" if USE_OPENAI else ("hf-proxy" if HF_PROXY else "local-echo")
    if provider == "openai":
        out = call_openai(prompt)
    elif provider == "hf-proxy":
        out = call_hf_proxy(prompt)
    else:
        out = f"(echo) {prompt}"

    # save chat
    try:
        conn = db()
        cur = conn.cursor()
        cur.execute("INSERT INTO chats(user, prompt, response, tokens_in, tokens_out, provider) VALUES (?,?,?,?,?,?)",
                    (user, prompt, out, len(prompt.split()), len(out.split()), provider))
        conn.commit()
        conn.close()
    except Exception as e:
        db_log("db", "ERROR", "save chat failed", {"err": str(e)})
    return out

# =============== Credits Helpers ===============
def get_or_create_user(username):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE username=?", (username,))
    row = cur.fetchone()
    if row:
        conn.close()
        return dict(row)
    # create
    cur.execute("INSERT INTO users(username, credits) VALUES (?,?)", (username, NEW_USER_CREDITS))
    conn.commit()
    cur.execute("SELECT * FROM users WHERE username=?", (username,))
    row = cur.fetchone()
    conn.close()
    return dict(row)

def user_add_credits(username, delta):
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET credits = COALESCE(credits,0)+? WHERE username=?", (delta, username))
    conn.commit()
    conn.close()

def user_has_credits(username, need=1):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT credits FROM users WHERE username=?", (username,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return False
    return (row["credits"] or 0) >= need

def user_consume_credit(username, cost=1):
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET credits = credits - ? WHERE username=? AND credits >= ?",
                (cost, username, cost))
    ok = cur.rowcount > 0
    conn.commit()
    conn.close()
    return ok

# =============== Scheduler (Daily Faucet) ===============
def faucet_job():
    try:
        conn = db()
        cur = conn.cursor()
        cur.execute("UPDATE users SET credits = COALESCE(credits,0) + ?", (DAILY_FAUCET,))
        conn.commit()
        conn.close()
        db_log("scheduler", "INFO", f"Daily faucet +{DAILY_FAUCET} credits added to all users", {})
    except Exception as e:
        db_log("scheduler", "ERROR", "faucet job failed", {"err": str(e)})

def start_scheduler_thread():
    def loop():
        while True:
            try:
                faucet_job()
            except Exception as e:
                log("ERROR", f"Scheduler error: {e}")
            time.sleep(24 * 3600)
    t = threading.Thread(target=loop, daemon=True)
    t.start()

# =============== HTML Templates (inline) ===============
HOME_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>{{ app_name }}</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;margin:0;background:#0b1220;color:#e6edf3}
    header{display:flex;justify-content:space-between;align-items:center;padding:16px 22px;border-bottom:1px solid #1e2a44;background:#0e1628}
    .brand a{color:#9bc1ff;text-decoration:none;font-weight:600}
    .container{max-width:860px;margin:24px auto;padding:0 16px}
    .card{background:#101a33;border:1px solid #1e2a44;border-radius:14px;padding:14px 14px 12px 14px;margin-bottom:14px}
    textarea{width:100%;min-height:110px;background:#0b1220;border:1px solid #21314f;border-radius:10px;padding:12px;color:#e6edf3;resize:vertical}
    button{border:0;border-radius:10px;padding:10px 14px;font-weight:600;cursor:pointer}
    .btn{background:#2f6feb;color:#fff}
    .btn:disabled{opacity:.6;cursor:not-allowed}
    .row{display:flex;gap:10px;flex-wrap:wrap}
    .row>*{flex:1}
    #out{white-space:pre-wrap;line-height:1.45}
    .muted{color:#9aa8c1;font-size:13px}
    .hist{max-height:260px;overflow:auto;border:1px solid #1e2a44;border-radius:10px;padding:8px}
    .chip{display:inline-block;background:#0e1b34;border:1px solid #24426c;color:#b4cfff;border-radius:999px;padding:5px 10px;margin:4px 6px 0 0;font-size:12px;cursor:pointer}
    .topline{display:flex;gap:12px;align-items:center;flex-wrap:wrap}
    .badge{font-size:12px;background:#102040;border:1px solid #1f3a66;border-radius:999px;padding:4px 8px;color:#9bc1ff}
  </style>
</head>
<body>
<header>
  <div class="brand"><a href="{{ brand_url }}" target="_blank">{{ app_name }}</a></div>
  <div><a class="badge" href="/admin">Admin</a></div>
</header>
<div class="container">
  <div class="card">
    <div class="topline">
      <div><strong>Ask anything</strong></div>
      <div class="badge">Model: {{ model }}</div>
      <div class="badge">HF Proxy: {{ 'ON' if hf_proxy else 'OFF' }}</div>
    </div>
    <p class="muted">Type your prompt below and hit Generate. History stays for this page session only.</p>
    <textarea id="in" placeholder="Write your prompt..."></textarea>
    <div class="row" style="margin-top:8px">
      <button id="go" class="btn">Generate</button>
      <button id="clr">Clear</button>
    </div>
    <div id="out" class="card" style="margin-top:12px"></div>
  </div>

  <div class="card">
    <div style="display:flex;justify-content:space-between;align-items:center">
      <strong>Quick History</strong>
      <button id="clearHist">Clear History</button>
    </div>
    <div id="hist" class="hist"></div>
  </div>
</div>

<script>
  const $in = document.getElementById('in');
  const $out = document.getElementById('out');
  const $go = document.getElementById('go');
  const $clr = document.getElementById('clr');
  const $hist = document.getElementById('hist');
  const $clearHist = document.getElementById('clearHist');

  let historyList = JSON.parse(localStorage.getItem('hist')||'[]');

  function renderHist(){
    $hist.innerHTML = '';
    historyList.slice().reverse().forEach(h=>{
      const div = document.createElement('span');
      div.className='chip';
      div.textContent = h.prompt.slice(0,80);
      div.title = 'Click to reuse';
      div.onclick = ()=>{$in.value = h.prompt;};
      $hist.appendChild(div);
    });
  }
  renderHist();

  $clearHist.onclick = ()=>{
    historyList = [];
    localStorage.setItem('hist', JSON.stringify(historyList));
    renderHist();
  };

  $clr.onclick = ()=>{ $in.value=''; $out.textContent=''; };

  $go.onclick = async ()=>{
    const prompt = $in.value.trim();
    if(!prompt){ alert('Enter prompt'); return; }
    $go.disabled = true;
    $out.textContent = 'Generating...';
    try{
      const r = await fetch('/api/generate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({prompt, user:'web'})});
      const j = await r.json();
      if(j.ok){
        $out.textContent = j.data;
        historyList.push({prompt, ts:Date.now()});
        historyList = historyList.slice(-50);
        localStorage.setItem('hist', JSON.stringify(historyList));
        renderHist();
      }else{
        $out.textContent = 'Error: ' + (j.error||'unknown');
      }
    }catch(e){
      $out.textContent = 'Network error: '+e;
    }finally{
      $go.disabled = false;
    }
  };
</script>
</body>
</html>
"""

ADMIN_LOGIN_HTML = """
<!doctype html>
<html><head><meta charset="utf-8"/><title>Admin Login</title>
<style>
 body{font-family:system-ui;margin:0;background:#0b1220;color:#e6edf3}
 .wrap{max-width:420px;margin:8% auto;background:#101a33;border:1px solid #1e2a44;border-radius:14px;padding:18px}
 input{width:100%;padding:10px;border-radius:10px;border:1px solid #2a3c61;background:#0b1220;color:#e6edf3;margin:6px 0}
 button{width:100%;padding:10px;border:0;border-radius:10px;background:#2f6feb;color:#fff;font-weight:700}
 .muted{color:#9aa8c1}
 .err{color:#ff9aa2;margin:6px 0 0 0}
</style></head>
<body>
<div class="wrap">
  <h3>Admin Login</h3>
  <form method="post">
    <input name="username" placeholder="Username" autocomplete="username" />
    <input name="password" type="password" placeholder="Password" autocomplete="current-password" />
    <button>Login</button>
    {% if err %}<div class="err">{{ err }}</div>{% endif %}
    <p class="muted" style="margin-top:12px">Default: admin / admin123 (change in .env)</p>
  </form>
</div>
</body></html>
"""

ADMIN_DASH_HTML = """
<!doctype html>
<html><head><meta charset="utf-8"/><title>Admin</title>
<style>
 body{font-family:system-ui;margin:0;background:#0b1220;color:#e6edf3}
 header{display:flex;justify-content:space-between;align-items:center;padding:16px 22px;border-bottom:1px solid #1e2a44;background:#0e1628}
 .container{max-width:980px;margin:20px auto;padding:0 16px}
 .card{background:#101a33;border:1px solid #1e2a44;border-radius:14px;padding:14px;margin:12px 0}
 input,select{padding:8px;border-radius:8px;border:1px solid #22375e;background:#0b1220;color:#e6edf3}
 button{padding:8px 12px;border-radius:8px;border:0;background:#2f6feb;color:#fff;cursor:pointer}
 table{width:100%;border-collapse:collapse}
 th,td{border-bottom:1px solid #1e2a44;padding:8px;text-align:left;font-size:14px}
 .row{display:flex;gap:10px;flex-wrap:wrap}
 .muted{color:#9aa8c1}
 .badge{font-size:12px;background:#102040;border:1px solid #1f3a66;border-radius:999px;padding:4px 8px;color:#9bc1ff}
 a{color:#9bc1ff}
</style></head>
<body>
<header>
  <div><strong>Admin</strong> <span class="badge">{{ app_name }}</span></div>
  <div>
    <a class="badge" href="/">Home</a>
    <a class="badge" href="/admin/logout">Logout</a>
  </div>
</header>
<div class="container">

  <div class="card">
    <h3>Quick Stats</h3>
    <div class="row">
      <div>Total users: <strong>{{ stats.total_users }}</strong></div>
      <div>Total chats: <strong>{{ stats.total_chats }}</strong></div>
      <div>OpenAI: <strong>{{ 'ON' if use_openai else 'OFF' }}</strong></div>
      <div>HF Proxy: <strong>{{ 'ON' if hf_proxy else 'OFF' }}</strong></div>
      <div>Model: <strong>{{ model }}</strong></div>
    </div>
  </div>

  <div class="card">
    <h3>Users</h3>
    <form class="row" method="post" action="/admin/create_user">
      <input name="username" placeholder="username" required />
      <input name="credits" type="number" placeholder="credits" value="50" />
      <button>Create</button>
    </form>
    <form class="row" style="margin-top:8px" method="post" action="/admin/add_credits">
      <input name="username" placeholder="username" required />
      <input name="delta" type="number" placeholder="+/- credits" value="10" />
      <button>Adjust</button>
    </form>
    <div style="overflow:auto;max-height:320px;margin-top:10px">
      <table>
        <thead><tr><th>Username</th><th>Credits</th><th>Telegram</th><th>Created</th></tr></thead>
        <tbody>
          {% for u in users %}
          <tr>
            <td>{{ u['username'] }}</td>
            <td>{{ u['credits'] }}</td>
            <td>{{ u['telegram_chat_id'] or '' }}</td>
            <td>{{ u['created_at'] }}</td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  </div>

  <div class="card">
    <h3>Recent Logs</h3>
    <div style="overflow:auto;max-height:260px">
      <table>
        <thead><tr><th>When</th><th>Chan</th><th>Level</th><th>Message</th></tr></thead>
        <tbody>
          {% for l in logs %}
          <tr>
            <td>{{ l['created_at'] }}</td>
            <td>{{ l['channel'] }}</td>
            <td>{{ l['level'] }}</td>
            <td>{{ l['message'] }}</td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
    <div class="row" style="margin-top:8px">
      <a class="badge" href="/admin/export_logs">Export CSV</a>
    </div>
  </div>

  <div class="card">
    <h3>Settings</h3>
    <form class="row" method="post" action="/admin/save_settings">
      <input name="key" placeholder="key (e.g. announcement)" />
      <input name="value" placeholder="value" />
      <button>Save</button>
    </form>
    <p class="muted">Stored in SQLite settings table.</p>
  </div>

</div>
</body></html>
"""

# =============== Auth Decorator ===============
def admin_required(f):
    @wraps(f)
    def w(*a, **k):
        if not session.get("admin_ok"):
            return redirect(url_for("admin_login"))
        return f(*a, **k)
    return w

# =============== Routes: Public ===============
@app.route("/")
def home():
    return render_template_string(
        HOME_HTML,
        app_name=APP_NAME,
        brand_url=BRAND_URL,
        model=OPENAI_MODEL,
        hf_proxy=HF_PROXY
    )

@app.route("/api/health")
def api_health():
    return jsonify(ok=True, app=APP_NAME, openai=USE_OPENAI, hf_proxy=HF_PROXY, model=OPENAI_MODEL)

@app.route("/api/generate", methods=["POST"])
def api_generate():
    try:
        data = request.get_json(force=True)
        prompt = (data.get("prompt") or "").strip()
        username = (data.get("user") or "web").strip()
        if not prompt:
            return jsonify(ok=False, error="empty prompt"), 400

        u = get_or_create_user(username)
        if not user_has_credits(username, 1):
            return jsonify(ok=False, error="No credits. Try later."), 402

        ok = user_consume_credit(username, 1)
        if not ok:
            return jsonify(ok=False, error="Credit debit failed"), 500

        out = generate_text(prompt, user=username)
        return jsonify(ok=True, data=out)
    except Exception as e:
        db_log("api", "ERROR", "generate failed", {"err": str(e), "trace": traceback.format_exc()})
        return jsonify(ok=False, error=str(e)), 500

# =============== Routes: Admin ===============
@app.route("/admin")
def admin_root():
    if not session.get("admin_ok"):
        return redirect(url_for("admin_login"))
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    err = None
    if request.method == "POST":
        u = request.form.get("username", "")
        p = request.form.get("password", "")
        # **IMPORTANT**: EXACT match with ENV (user report: "incorrect")
        if u == ADMIN_USER and p == ADMIN_PASS:
            session["admin_ok"] = True
            db_log("admin", "INFO", "login success", {"user": u})
            return redirect(url_for("admin_dashboard"))
        err = "Incorrect username or password"
        db_log("admin", "WARN", "login failed", {"user": u})
    return render_template_string(ADMIN_LOGIN_HTML, err=err)

@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))

@app.route("/admin/dashboard")
@admin_required
def admin_dashboard():
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(1) AS n FROM users")
    total_users = cur.fetchone()["n"]
    cur.execute("SELECT COUNT(1) AS n FROM chats")
    total_chats = cur.fetchone()["n"]
    cur.execute("SELECT username, credits, telegram_chat_id, created_at FROM users ORDER BY id DESC LIMIT 100")
    users = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT created_at, channel, level, message FROM logs ORDER BY id DESC LIMIT 100")
    logs = [dict(r) for r in cur.fetchall()]
    conn.close()
    return render_template_string(ADMIN_DASH_HTML,
                                  app_name=APP_NAME,
                                  stats={"total_users": total_users, "total_chats": total_chats},
                                  users=users, logs=logs,
                                  use_openai=USE_OPENAI, hf_proxy=HF_PROXY, model=OPENAI_MODEL)

@app.route("/admin/create_user", methods=["POST"])
@admin_required
def admin_create_user():
    username = request.form.get("username","").strip()
    credits = int(request.form.get("credits","0") or "0")
    if not username:
        return redirect(url_for("admin_dashboard"))
    try:
        conn = db()
        cur = conn.cursor()
        cur.execute("INSERT INTO users(username, credits) VALUES (?,?)", (username, credits))
        conn.commit()
        conn.close()
        db_log("admin", "INFO", "user created", {"u": username, "c": credits})
    except Exception as e:
        db_log("admin", "ERROR", "create_user failed", {"err": str(e)})
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/add_credits", methods=["POST"])
@admin_required
def admin_add_credits():
    username = request.form.get("username","").strip()
    delta = int(request.form.get("delta","0") or "0")
    if username and delta:
        try:
            user_add_credits(username, delta)
            db_log("admin", "INFO", "credits adjusted", {"u": username, "delta": delta})
        except Exception as e:
            db_log("admin", "ERROR", "add_credits failed", {"err": str(e)})
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/save_settings", methods=["POST"])
@admin_required
def admin_save_settings():
    k = (request.form.get("key") or "").strip()
    v = (request.form.get("value") or "").strip()
    if not k:
        return redirect(url_for("admin_dashboard"))
    try:
        conn = db()
        cur = conn.cursor()
        cur.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v))
        conn.commit()
        conn.close()
        db_log("admin", "INFO", "setting saved", {"k": k})
    except Exception as e:
        db_log("admin", "ERROR", "save_settings failed", {"err": str(e)})
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/export_logs")
@admin_required
def admin_export_logs():
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT created_at, channel, level, message, extra FROM logs ORDER BY id DESC LIMIT 5000")
    rows = cur.fetchall()
    conn.close()

    si = []
    si.append(["created_at","channel","level","message","extra"])
    for r in rows:
        si.append([r["created_at"], r["channel"], r["level"], r["message"], r["extra"]])

    out = []
    w = csv.writer(out := [])
    # small trick to avoid io imports; we will build CSV manually
    # but python CSV writer expects file-like; do a simple join instead:
    # We'll just build rows by ourselves:
    csv_lines = []
    csv_lines.append("created_at,channel,level,message,extra")
    for r in rows:
        def esc(x):
            s = (x or "")
            s = str(s).replace('"','""')
            return f'"{s}"'
        csv_lines.append(",".join([esc(r["created_at"]), esc(r["channel"]), esc(r["level"]), esc(r["message"]), esc(r["extra"])]))
    body = "\n".join(csv_lines)

    resp = make_response(body)
    resp.headers["Content-Type"] = "text/csv"
    resp.headers["Content-Disposition"] = "attachment; filename=logs.csv"
    return resp

# =============== Telegram Bot (PTB v21) ===============
PTB_AVAILABLE = False
try:
    from telegram import Update
    from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
    PTB_AVAILABLE = True
except Exception as e:
    db_log("telegram", "WARN", "PTB import failed", {"err": str(e)})

TELEGRAM_READY = PTB_AVAILABLE and bool(TELEGRAM_BOT_TOKEN)

async def tg_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    username = f"tg:{chat_id}"
    get_or_create_user(username)
    await update.message.reply_text(f"Hi! You are registered as {username}. You have credits to use /ask <prompt>.")

async def tg_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Use /ask <your prompt>")

async def tg_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    username = f"tg:{chat_id}"
    get_or_create_user(username)
    prompt = " ".join(context.args).strip()
    if not prompt:
        await update.message.reply_text("Send: /ask your question")
        return
    if not user_has_credits(username, 1):
        await update.message.reply_text("No credits. Try later.")
        return
    if not user_consume_credit(username, 1):
        await update.message.reply_text("Debit failed, try again.")
        return
    out = generate_text(prompt, user=username)
    await update.message.reply_text(out[:4000])

def start_telegram_polling():
    if not TELEGRAM_READY:
        log("INFO", "Telegram: OFF (missing token or PTB)")
        return

    async def _run():
        try:
            app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
            app.add_handler(CommandHandler("start", tg_start))
            app.add_handler(CommandHandler("help", tg_help))
            app.add_handler(CommandHandler("ask", tg_ask))
            app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, tg_help))
            log("INFO", "Telegram handlers registered. Mode: POLLING")
            db_log("telegram", "INFO", "handlers registered", {})
            await app.run_polling(close_loop=False)  # single call; await properly
        except Exception as e:
            db_log("telegram", "ERROR", "polling crashed", {"err": str(e), "trace": traceback.format_exc()})

    # run in a dedicated thread with its own event loop
    def thread_target():
        try:
            asyncio.run(_run())
        except Exception as e:
            db_log("telegram", "ERROR", "thread crash", {"err": str(e)})

    t = threading.Thread(target=thread_target, daemon=True)
    t.start()

# =============== Startup Logs ===============
def print_boot_banner():
    log("INFO", "OpenAI client initialized." if USE_OPENAI else "OpenAI disabled.")
    log("INFO", f"App: {APP_NAME}")
    log("INFO", f"Public URL: {PUBLIC_URL}")
    log("INFO", f"Brand Page: {BRAND_URL}")
    log("INFO", f"OpenAI: {'ON' if USE_OPENAI else 'OFF'} ({OPENAI_MODEL})")
    log("INFO", f"HF Proxy: {'ON' if HF_PROXY else 'OFF'}")
    log("INFO", f"Telegram: {'ON' if TELEGRAM_READY else 'OFF'}; Polling: {TELEGRAM_POLLING}")

# =============== Main ===============
if __name__ == "__main__":
    print_boot_banner()
    # start scheduler
    start_scheduler_thread()

    # start telegram polling in background (fixed: no 'never awaited' now)
    if TELEGRAM_POLLING:
        start_telegram_polling()

    # serve Flask
    # Render binds to PORT env; dev run is fine too
    app.run(host=HOST, port=PORT, debug=False)
