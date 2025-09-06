# main.py
# Single-file Flask + Telegram Webhook app (python-telegram-bot v21)
# Works on Render. No polling/threads. Admin panel + home UI + OpenAI generation.

import os, json, textwrap, asyncio, datetime as dt
from dataclasses import dataclass
from typing import Optional

from flask import Flask, request, redirect, url_for, render_template_string, session, abort, flash
from dotenv import load_dotenv

# --- Load ENV ---
load_dotenv(dotenv_path=os.getenv("DOTENV_PATH") or ".env")

# --------- Config ---------
@dataclass
class Config:
    ADMIN_ID: Optional[str] = os.getenv("ADMIN_ID")
    ADMIN_USER: str = os.getenv("ADMIN_USER", "admin")
    ADMIN_PASS: str = os.getenv("ADMIN_PASS", "password")

    BUSINESS_NAME: str = os.getenv("BUSINESS_NAME", "Ganesh A.I.")
    BUSINESS_EMAIL: str = os.getenv("BUSINESS_EMAIL", "admin@example.com")
    SUPPORT_USERNAME: str = os.getenv("SUPPORT_USERNAME", "@support")

    DOMAIN: str = os.getenv("DOMAIN", "http://localhost:10000").rstrip("/")
    PORT: int = int(os.getenv("PORT", os.getenv("port", "10000")))  # Render sets PORT

    FLASK_SECRET: str = os.getenv("FLASK_SECRET", "change-me")
    SECRET_TOKEN: str = os.getenv("SECRET_TOKEN", "set-a-random-secret")

    OPENAI_API_KEY: Optional[str] = os.getenv("OPENAI_API_KEY")
    OPENAI_MODEL: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    TELEGRAM_BOT_TOKEN: Optional[str] = os.getenv("TELEGRAM_BOT_TOKEN")
    WEBHOOK_URL: str = os.getenv("WEBHOOK_URL", "").rstrip("/")
    TELEGRAM_WEBHOOK_SECRET: str = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")

    ENABLE_SEARCH: str = os.getenv("ENABLE_SEARCH", "1")
    SHOW_TOOLS: str = os.getenv("SHOW_TOOLS", "1")
    DEFAULT_SOURCES: str = os.getenv("DEFAULT_SOURCES", "web")

    # Optional / not strictly used here, but read safely
    CASHFREE_APP_ID: str = os.getenv("CASHFREE_APP_ID", "")
    CASHFREE_SECRET_KEY: str = os.getenv("CASHFREE_SECRET_KEY", "")
    CASHFREE_WEBHOOK_SECRET: str = os.getenv("CASHFREE_WEBHOOK_SECRET", "")
    UPI_ID: str = os.getenv("UPI_ID", "")
    VISIT_PAY_RATE: str = os.getenv("VISIT_PAY_RATE", "0.0")

    HUGGINGFACE_API_URL: str = os.getenv("HUGGINGFACE_API_URL", "")
    HUGGINGFACE_API_TOKEN: str = os.getenv("HUGGINGFACE_API_TOKEN", "")

CFG = Config()

# ---- Safety checks for Webhook config ----
if not CFG.WEBHOOK_URL:
    # Fall back to DOMAIN if WEBHOOK_URL not provided
    CFG.WEBHOOK_URL = CFG.DOMAIN

# --------- Flask App ---------
app = Flask(__name__)
app.secret_key = CFG.FLASK_SECRET

def y():
    # current UTC year (no deprecated utcnow)
    return dt.datetime.now(dt.UTC).year

# --------- OpenAI Client ----------
OPENAI_AVAILABLE = False
try:
    if CFG.OPENAI_API_KEY:
        from openai import OpenAI
        openai_client = OpenAI(api_key=CFG.OPENAI_API_KEY)
        OPENAI_AVAILABLE = True
except Exception as e:
    OPENAI_AVAILABLE = False

# --------- Telegram (v21) ----------
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram import Update, BotCommand

tg_app: Optional[Application] = None
if CFG.TELEGRAM_BOT_TOKEN:
    tg_app = Application.builder().token(CFG.TELEGRAM_BOT_TOKEN).build()
else:
    print("WARN: TELEGRAM_BOT_TOKEN not set. Telegram bot disabled.")

# --- Telegram Handlers ---
WELCOME = (
    "👋 *Welcome to Ganesh A.I. Assistant!*\n"
    "Type your topic or question, and I’ll generate content.\n"
    "Admin panel (web): `/admin` (on site)\n"
)

async def tg_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_markdown_v2(WELCOME)

async def tg_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Send me a topic. I’ll draft content using OpenAI.")

async def tg_echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = (update.message.text or "").strip()
    if not user_text:
        await update.message.reply_text("Send some text to generate content.")
        return
    draft = await ai_generate(user_text)
    await update.message.reply_text(draft[:4000] or "No content generated.")

if tg_app:
    tg_app.add_handler(CommandHandler("start", tg_start))
    tg_app.add_handler(CommandHandler("help", tg_help))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, tg_echo))

# --- Webhook boot sequence (initialize+start+set_webhook) ---
async def _boot_telegram():
    if not tg_app:
        return
    await tg_app.initialize()
    await tg_app.start()
    # set commands
    try:
        await tg_app.bot.set_my_commands([
            BotCommand("start", "Start the bot"),
            BotCommand("help", "How to use"),
        ])
    except Exception:
        pass
    # set webhook
    secret = CFG.TELEGRAM_WEBHOOK_SECRET or CFG.SECRET_TOKEN
    url = CFG.WEBHOOK_URL.rstrip("/") + f"/telegram/webhook/{secret}"
    await tg_app.bot.set_webhook(url=url, allowed_updates=["message","callback_query"])

# Run boot once on startup
if tg_app:
    try:
        asyncio.run(_boot_telegram())
        print("Telegram webhook set.")
    except RuntimeError:
        # If an event loop is already running (rare on WSGI), schedule it differently.
        loop = asyncio.new_event_loop()
        loop.run_until_complete(_boot_telegram())
        loop.close()

# ---------- HTML Templates ----------
BASE_CSS = """
<style>
:root { --bg:#0b0f17; --card:#121826; --muted:#94a3b8; --txt:#e2e8f0; --acc:#60a5fa; }
*{box-sizing:border-box}
body{margin:0;font-family:Inter,system-ui,Arial;background:linear-gradient(180deg,#0b0f17,#0f172a);}
.wrap{max-width:1040px;margin:0 auto;padding:24px;}
nav{display:flex;justify-content:space-between;align-items:center;margin-bottom:18px}
.brand{font-weight:700;color:#fff;font-size:20px;letter-spacing:.2px}
.badge{color:#a3e635;background:#1d2538;border-radius:999px;padding:6px 10px;font-size:12px}
.card{background:var(--card);border:1px solid #1f2937;border-radius:16px;padding:16px;box-shadow:0 10px 24px rgba(0,0,0,.25)}
.row{display:grid;grid-template-columns:1fr 420px; gap:16px}
textarea,input,button{width:100%;border-radius:12px;border:1px solid #23314d;background:#0f172a;color:var(--txt);padding:12px;font-size:14px;outline:none}
button{background:linear-gradient(90deg,#2563eb,#7c3aed);border:none;font-weight:700;cursor:pointer}
button:disabled{opacity:.5;cursor:not-allowed}
h1{color:#fff;font-size:22px;margin:0 0 8px}
label{display:block;color:var(--muted);font-size:12px;margin:6px 0}
pre.output{white-space:pre-wrap;background:#0a0f1d;border:1px dashed #223; padding:12px;border-radius:12px;color:#cbd5e1;min-height:120px}
.kv{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
.kv span{background:#0b1222;border:1px solid #1e293b;border-radius:8px;padding:6px 10px;color:#9fb3c8;font-size:12px}
footer{color:#64748b;text-align:center;margin-top:32px}
a, a:visited{color:#93c5fd;text-decoration:none}
.formline{display:flex;gap:8px}
hr{border:none;border-top:1px solid #1f2937;margin:16px 0}
.notice{color:#a5b4fc;font-size:12px}
</style>
"""

HOME_TMPL = f"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>{CFG.BUSINESS_NAME}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
{BASE_CSS}
</head>
<body>
<div class="wrap">
  <nav>
    <div class="brand">{CFG.BUSINESS_NAME}</div>
    <div class="kv">
      <span>Admin: <a href="/admin">/admin</a></span>
      <span>Support: {CFG.SUPPORT_USERNAME}</span>
    </div>
  </nav>

  <div class="row">
    <div class="card">
      <h1>Generate AI Content</h1>
      <form method="post" action="/generate">
        {% if env.ENABLE_SEARCH == '1' %}
        <div class="formline">
          <input type="text" name="topic" placeholder="Topic / Query" value="{{ topic or '' }}" required>
          <button type="submit">Generate</button>
        </div>
        {% else %}
        <label>Topic</label>
        <input type="text" name="topic" placeholder="Topic / Query" value="{{ topic or '' }}" required>
        <button type="submit" style="margin-top:10px">Generate</button>
        {% endif %}
        <label>Extra instructions (optional)</label>
        <textarea name="instructions" rows="4" placeholder="Tone, target audience, outlines etc.">{{ instructions or '' }}</textarea>
      </form>
      <hr>
      <label>Output</label>
      <pre class="output">{{ output or '— your content will appear here —' }}</pre>
      <div class="kv">
        {% if env.SHOW_TOOLS == '1' %}
        <span>Model: {{ model }}</span>
        <span>Sources: {{ env.DEFAULT_SOURCES }}</span>
        {% endif %}
        <span>Webhook: /telegram/webhook/{{ env.TELEGRAM_WEBHOOK_SECRET or env.SECRET_TOKEN }}</span>
      </div>
    </div>

    <div class="card">
      <h1>How it works</h1>
      <div class="notice">
        • Uses OpenAI ({{ model }})<br>
        • Telegram bot via webhook (no polling)<br>
        • Admin panel at <code>/admin</code><br>
        • Flags: ENABLE_SEARCH={{ env.ENABLE_SEARCH }}, SHOW_TOOLS={{ env.SHOW_TOOLS }}
      </div>
      <hr>
      <div class="kv">
        <span>Email: {CFG.BUSINESS_EMAIL}</span>
        <span>UPI: {CFG.UPI_ID or '—'}</span>
      </div>
    </div>
  </div>

  <footer>© {y()} {CFG.BUSINESS_NAME}</footer>
</div>
</body>
</html>
"""

ADMIN_TMPL = f"""
<!doctype html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">{BASE_CSS}<title>Admin · {CFG.BUSINESS_NAME}</title></head>
<body>
<div class="wrap">
  <nav>
    <div class="brand">Admin · {CFG.BUSINESS_NAME}</div>
    <div class="kv"><span><a href="/">Home</a></span><span><a href="/logout">Logout</a></span></div>
  </nav>

  <div class="card">
    <h1>Environment</h1>
    <div class="kv">
      <span>ENABLE_SEARCH={{ env.ENABLE_SEARCH }}</span>
      <span>SHOW_TOOLS={{ env.SHOW_TOOLS }}</span>
      <span>DEFAULT_SOURCES={{ env.DEFAULT_SOURCES }}</span>
      <span>MODEL={{ model }}</span>
      <span>WEBHOOK_URL={{ env.WEBHOOK_URL }}</span>
    </div>
    <hr>
    <form method="post" action="/admin/actions">
      <button name="action" value="set_webhook">Re-set Telegram Webhook</button>
    </form>
    <hr>
    <div class="notice">
      Webhook endpoint:
      <code>{{ env.WEBHOOK_URL }}/telegram/webhook/{{ env.TELEGRAM_WEBHOOK_SECRET or env.SECRET_TOKEN }}</code>
    </div>
  </div>

  <div class="card">
    <h1>Quick Generate (admin)</h1>
    <form method="post" action="/generate">
      <label>Topic</label>
      <input type="text" name="topic" placeholder="e.g., YouTube script on AI news" required>
      <label>Instructions</label>
      <textarea name="instructions" rows="4" placeholder="Tone, target audience, bullets, CTA, etc."></textarea>
      <button type="submit" style="margin-top:8px">Generate</button>
    </form>
  </div>

  <footer>© {y()} {CFG.BUSINESS_NAME}</footer>
</div>
</body>
</html>
"""

LOGIN_TMPL = f"""
<!doctype html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">{BASE_CSS}<title>Login · {CFG.BUSINESS_NAME}</title></head>
<body>
<div class="wrap">
  <nav><div class="brand">{CFG.BUSINESS_NAME} Admin</div></nav>
  <div class="card">
    <h1>Admin Login</h1>
    <form method="post">
      <label>Username</label>
      <input name="username" required>
      <label>Password</label>
      <input name="password" type="password" required>
      <button type="submit" style="margin-top:8px">Login</button>
    </form>
    {% with msgs = get_flashed_messages() %}
      {% if msgs %}<hr><div class="notice">{{ msgs[0] }}</div>{% endif %}
    {% endwith %}
  </div>
  <footer>© {y()} {CFG.BUSINESS_NAME}</footer>
</div>
</body>
</html>
"""

# ---------- AI generation ----------
async def ai_generate_async(topic: str, instructions: str = "") -> str:
    if not OPENAI_AVAILABLE:
        return "OpenAI API key not configured. Please set OPENAI_API_KEY."
    prompt = textwrap.dedent(f"""
    You are Ganesh A.I. Write a helpful, structured response.

    Topic: {topic}
    Extra instructions: {instructions or "N/A"}

    Requirements:
    - Clear intro, useful bullets, and a short CTA at the end.
    - Keep it concise but actionable.
    """).strip()

    try:
        # OpenAI v1 chat.completions
        resp = openai_client.chat.completions.create(
            model=CFG.OPENAI_MODEL,
            messages=[
                {"role":"system","content":"You are a concise, helpful, multilingual content writer."},
                {"role":"user","content": prompt}
            ],
            temperature=0.7,
            max_tokens=700,
        )
        out = (resp.choices[0].message.content or "").strip()
        return out or "No content generated."
    except Exception as e:
        return f"AI error: {e}"

def ai_generate(topic: str, instructions: str = "") -> str:
    # Helper to run async in sync Flask views
    try:
        return asyncio.run(ai_generate_async(topic, instructions))
    except RuntimeError:
        loop = asyncio.new_event_loop()
        out = loop.run_until_complete(ai_generate_async(topic, instructions))
        loop.close()
        return out

# ---------- Routes ----------
@app.get("/")
def home():
    return render_template_string(
        HOME_TMPL,
        env=CFG.__dict__,
        model=CFG.OPENAI_MODEL,
        topic="",
        instructions="",
        output=""
    )

@app.post("/generate")
def generate():
    topic = (request.form.get("topic") or "").strip()
    instructions = (request.form.get("instructions") or "").strip()
    if not topic:
        flash("Please enter a topic.")
        return redirect(url_for("home"))
    out = ai_generate(topic, instructions)
    return render_template_string(
        HOME_TMPL,
        env=CFG.__dict__,
        model=CFG.OPENAI_MODEL,
        topic=topic,
        instructions=instructions,
        output=out
    )

# --- Admin auth helpers ---
def is_authed() -> bool:
    return session.get("authed") is True

@app.route("/admin", methods=["GET","POST"])
def admin():
    if request.method == "POST":
        u = request.form.get("username","")
        p = request.form.get("password","")
        if u == CFG.ADMIN_USER and p == CFG.ADMIN_PASS:
            session["authed"] = True
            return redirect(url_for("admin"))
        flash("Invalid credentials.")
        return render_template_string(LOGIN_TMPL)
    # GET
    if not is_authed():
        return render_template_string(LOGIN_TMPL)
    return render_template_string(ADMIN_TMPL, env=CFG.__dict__, model=CFG.OPENAI_MODEL)

@app.post("/admin/actions")
def admin_actions():
    if not is_authed():
        abort(403)
    action = request.form.get("action","")
    if action == "set_webhook":
        if not tg_app:
            flash("Telegram not configured.")
            return redirect(url_for("admin"))
        async def _do():
            secret = CFG.TELEGRAM_WEBHOOK_SECRET or CFG.SECRET_TOKEN
            url = CFG.WEBHOOK_URL.rstrip("/") + f"/telegram/webhook/{secret}"
            await tg_app.bot.set_webhook(url=url, allowed_updates=["message","callback_query"])
        try:
            asyncio.run(_do())
            flash("Webhook updated.")
        except RuntimeError:
            loop = asyncio.new_event_loop()
            loop.run_until_complete(_do())
            loop.close()
            flash("Webhook updated.")
    return redirect(url_for("admin"))

@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("admin"))

# --- Telegram webhook endpoint ---
@app.post(f"/telegram/webhook/{CFG.TELEGRAM_WEBHOOK_SECRET or CFG.SECRET_TOKEN}")
def telegram_webhook():
    if not tg_app:
        return "Bot not configured", 200
    if request.headers.get("Content-Type","").startswith("application/json"):
        try:
            data = request.get_json(force=True, silent=False)
            update = Update.de_json(data, tg_app.bot)
            # Run processing synchronously (creates a short-lived loop)
            asyncio.run(tg_app.process_update(update))
            return "OK", 200
        except Exception as e:
            print("Webhook error:", e)
            return "ERR", 200
    abort(415)

@app.get("/healthz")
def health():
    return {"ok": True, "ts": dt.datetime.now(dt.UTC).isoformat(), "app": "Ganesh-AI"}, 200

# --- Entry ---
if __name__ == "__main__":
    print(f"Starting {CFG.BUSINESS_NAME} on port {CFG.PORT} …")
    app.run(host="0.0.0.0", port=CFG.PORT, debug=False)
