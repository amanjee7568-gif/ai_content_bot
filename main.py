#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Monster AI Module — Single File (Telegram + Flask + Monetization)
Author: You
Python: 3.11+ (PTB v21+)

Features:
- OpenAI chat + HF fallback
- Dev Agent (code gen/fix)
- TTS (gTTS), YT download (yt-dlp)
- Image gen (OpenAI), placeholder fallback
- Monetization: ad slots (web), affiliate links, visit earnings, analytics
- SQLite persistence
- APScheduler jobs
- Render/Railway friendly (PORT)
"""

import os, io, re, json, time, uuid, base64, sqlite3, logging, textwrap, asyncio, datetime as dt
from dataclasses import dataclass
from typing import Optional, Dict, Any, Tuple, List

# ---- Third-party
from dotenv import load_dotenv
from flask import Flask, request, jsonify, send_file, make_response, redirect
from flask import render_template_string
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import httpx
from gtts import gTTS

# Telegram PTB v21
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# Optional libs
try:
    from openai import OpenAI
    HAVE_OPENAI = True
except Exception:
    HAVE_OPENAI = False

try:
    import yt_dlp
    HAVE_YTDLP = True
except Exception:
    HAVE_YTDLP = False

# ---- Setup
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
log = logging.getLogger("EconomyBot")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
OPENAI_API_KEY     = os.getenv("OPENAI_API_KEY", "").strip()
HF_URL             = os.getenv("HUGGINGFACE_API_URL", "").strip()
HF_TOKEN           = os.getenv("HUGGINGFACE_API_TOKEN", "").strip()
BASE_URL           = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
ADMIN_USER_ID      = int(os.getenv("ADMIN_USER_ID", "0") or 0)
AFFILIATE_TAG      = os.getenv("AFFILIATE_TAG", "mytag")
PORT               = int(os.getenv("PORT", "8000"))

DB_PATH = os.getenv("DB_PATH", "monster.db")

# ---- DB
def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def ensure_schema():
    con = db()
    cur = con.cursor()
    cur.executescript("""
    PRAGMA journal_mode=WAL;

    CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_seen TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS sessions(
        id TEXT PRIMARY KEY,
        user_id INTEGER,
        channel TEXT,           -- 'tg' or 'web'
        created TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_active TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(user_id)
    );

    CREATE TABLE IF NOT EXISTS logs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        session_id TEXT,
        kind TEXT,              -- 'chat','code','tts','yt','image','visit','callback'
        input TEXT,
        output TEXT,
        tokens_in INTEGER DEFAULT 0,
        tokens_out INTEGER DEFAULT 0,
        ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS earnings(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT,
        source TEXT,            -- 'ad','affiliate','visit','pro'
        amount_cents INTEGER DEFAULT 0,
        meta TEXT,
        ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS affiliates(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        raw_url TEXT,
        tagged_url TEXT,
        created TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)
    con.commit()
    con.close()

ensure_schema()
log.info("DB ready (schema ensured).")

# ---- OpenAI & HF clients
client_oa: Optional[OpenAI] = None
if HAVE_OPENAI and OPENAI_API_KEY:
    try:
        client_oa = OpenAI(api_key=OPENAI_API_KEY)
        log.info("OpenAI client initialized.")
    except Exception as e:
        log.warning(f"OpenAI init failed: {e}")

HAVE_HF = bool(HF_URL and HF_TOKEN)

# ---- Utils
def now_ts() -> str:
    return dt.datetime.utcnow().isoformat() + "Z"

def new_session(user_id: Optional[int], channel: str) -> str:
    sid = uuid.uuid4().hex
    con = db(); cur = con.cursor()
    cur.execute("INSERT INTO sessions(id,user_id,channel,last_active) VALUES(?,?,?,CURRENT_TIMESTAMP)", (sid, user_id, channel))
    con.commit(); con.close()
    return sid

def touch_session(session_id: str):
    con = db(); cur = con.cursor()
    cur.execute("UPDATE sessions SET last_active=CURRENT_TIMESTAMP WHERE id=?", (session_id,))
    con.commit(); con.close()

def log_event(user_id: Optional[int], session_id: Optional[str], kind: str, inp: str, out: str, ti=0, to=0):
    con = db(); cur = con.cursor()
    cur.execute("INSERT INTO logs(user_id,session_id,kind,input,output,tokens_in,tokens_out) VALUES(?,?,?,?,?,?,?)",
                (user_id, session_id, kind, inp[:2000], out[:2000], ti, to))
    con.commit(); con.close()

def add_user(user_id: int, username: str = ""):
    con = db(); cur = con.cursor()
    cur.execute("INSERT INTO users(user_id,username,first_seen,last_seen) VALUES(?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP) "
                "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, last_seen=CURRENT_TIMESTAMP",
                (user_id, username))
    con.commit(); con.close()

def record_earning(session_id: str, source: str, cents: int, meta: Dict[str, Any]):
    con = db(); cur = con.cursor()
    cur.execute("INSERT INTO earnings(session_id,source,amount_cents,meta) VALUES(?,?,?,?)",
                (session_id, source, cents, json.dumps(meta)[:1000]))
    con.commit(); con.close()

def make_affiliate(url: str) -> str:
    # very basic example: append ?tag=AFFILIATE_TAG
    sep = "&" if "?" in url else "?"
    tagged = f"{url}{sep}tag={AFFILIATE_TAG}"
    con = db(); cur = con.cursor()
    cur.execute("INSERT INTO affiliates(raw_url,tagged_url) VALUES(?,?)", (url, tagged))
    con.commit(); con.close()
    return tagged

# ---- AI Core
SYSTEM_PROMPT = (
    "You are Monster, a super-capable AI assistant and developer agent. "
    "Be concise, accurate, and pragmatic. When asked for code, produce complete runnable snippets. "
    "If a task requires steps (plan -> code -> tests), output them clearly."
)

async def ai_chat(prompt: str) -> str:
    """OpenAI chat with HF fallback."""
    # Try OpenAI (Responses API style)
    if client_oa:
        try:
            resp = client_oa.responses.create(
                model="gpt-4o-mini",  # economical; change to your best available
                input=[{"role":"system","content":SYSTEM_PROMPT},
                       {"role":"user","content":prompt}],
                temperature=0.2,
            )
            txt = resp.output_text or ""
            if txt.strip():
                return txt.strip()
        except Exception as e:
            log.warning(f"OpenAI fail: {e}")

    # HuggingFace fallback (simple)
    if HAVE_HF:
        try:
            headers = {"Authorization": f"Bearer {HF_TOKEN}"}
            payload = {
                "inputs": f"System: {SYSTEM_PROMPT}\nUser: {prompt}\nAssistant:",
                "parameters": {"max_new_tokens": 600, "temperature": 0.3},
                "options": {"wait_for_model": True}
            }
            async with httpx.AsyncClient(timeout=60) as s:
                r = await s.post(HF_URL, headers=headers, json=payload)
                r.raise_for_status()
                data = r.json()
                # HF responses vary by model; try a few shapes
                if isinstance(data, list) and data:
                    txt = data[0].get("generated_text","")
                else:
                    txt = data.get("generated_text","") if isinstance(data, dict) else ""
                return (txt or "Sorry, model returned empty.").strip()
        except Exception as e:
            log.warning(f"HF fail: {e}")

    return "AI backend is not available right now. Please try again later."

async def dev_agent(task: str) -> str:
    """Developer agent: plan + code + tests."""
    prompt = f"""
Act as a senior software engineer. For the task below:
1) Brief Plan
2) Key Files (with paths) and complete code blocks
3) Quick test instructions

Task:
{task}
"""
    return await ai_chat(prompt)

async def ai_image(prompt: str) -> Tuple[str, Optional[bytes]]:
    """
    Returns (message, image_bytes or None).
    """
    if client_oa:
        try:
            img = client_oa.images.generate(model="gpt-image-1", prompt=prompt, size="1024x1024")
            b64 = img.data[0].b64_json
            return ("Image generated with OpenAI.", base64.b64decode(b64))
        except Exception as e:
            log.warning(f"img gen fail: {e}")
    # fallback
    return ("Image service unavailable; showing placeholder.", None)

def synthesize_tts(text: str) -> bytes:
    tts = gTTS(text=text, lang="en")
    buf = io.BytesIO()
    tts.write_to_fp(buf)
    buf.seek(0)
    return buf.read()

def download_youtube(url: str) -> Tuple[str, bytes]:
    if not HAVE_YTDLP:
        raise RuntimeError("yt-dlp not installed in this environment.")
    ydl_opts = {
        "format": "mp4",
        "outtmpl": "-",
        "quiet": True,
        "noplaylist": True,
    }
    # We'll download to buffer by using a file then read; yt-dlp doesn't write to stdout reliably for mp4
    tmp = f"/tmp/{uuid.uuid4().hex}.mp4"
    ydl_opts["outtmpl"] = tmp
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])
    with open(tmp, "rb") as f:
        data = f.read()
    os.remove(tmp)
    return ("video.mp4", data)

# ---- Flask Web App
app = Flask(__name__)

BASE_HTML = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Monster AI</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="preconnect" href="https://cdn.jsdelivr.net">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@picocss/pico@2/css/pico.classless.min.css">
<style>
  .hero {padding: 1.5rem; border-radius: 16px; background: #111; color:#eee;}
  .ad-slot{min-height:120px;border:1px dashed #aaa;display:flex;align-items:center;justify-content:center;margin:10px 0;border-radius:12px}
  .mono{font-family: ui-monospace, Menlo, Monaco, Consolas, "Liberation Mono", monospace}
</style>
<!-- Google AdSense (placeholder) -->
<!-- Replace with your client id
<script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-XXXX" crossorigin="anonymous"></script>
-->
</head>
<body>
<main class="container">
  <section class="hero">
    <h2>Monster AI — Developer Agent + Chat</h2>
    <p>Visit earns are counted. Ads & affiliate integrated.</p>
  </section>

  <div class="ad-slot">Ad Slot (header)</div>

  <form id="askform">
    <label>Ask anything</label>
    <textarea name="q" id="q" rows="4" placeholder="Ask Monster..." required></textarea>
    <button type="submit">Ask</button>
  </form>

  <article>
    <h4>Response</h4>
    <pre class="mono" id="resp"></pre>
  </article>

  <div class="grid">
    <div>
      <h5>Dev Agent</h5>
      <textarea id="devtask" rows="4" placeholder="Build me a Flask API with JWT..."></textarea>
      <button id="devbtn">Generate</button>
    </div>
    <div>
      <h5>TTS</h5>
      <textarea id="tts_text" rows="4" placeholder="Text to speak..."></textarea>
      <button id="tts_btn">Synthesize</button>
      <div id="tts_out"></div>
    </div>
  </div>

  <div class="grid">
    <div>
      <h5>Image</h5>
      <input id="img_prompt" placeholder="A cyberpunk city at dawn"/>
      <button id="img_btn">Generate Image</button>
      <div id="img_out"></div>
    </div>
    <div>
      <h5>Affiliate</h5>
      <input id="aff_url" placeholder="https://amazon.in/some-product"/>
      <button id="aff_btn">Make Affiliate Link</button>
      <pre class="mono" id="aff_out"></pre>
    </div>
  </div>

  <div class="ad-slot">Ad Slot (footer)</div>

  <footer>
    <small>© Monster AI</small>
  </footer>
</main>

<script>
const sid = localStorage.getItem("monster_sid") || crypto.randomUUID();
localStorage.setItem("monster_sid", sid);

// Visit earning ping
fetch("/api/visit?sid="+sid).catch(()=>{});

const q = (id)=>document.getElementById(id);

document.getElementById("askform").addEventListener("submit", async (e)=>{
  e.preventDefault();
  const txt = q("q").value.trim();
  q("resp").textContent = "Thinking...";
  const r = await fetch("/api/ask", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({sid, q: txt})});
  const j = await r.json();
  q("resp").textContent = j.answer || j.error || "No response";
});

q("devbtn").onclick = async ()=>{
  const task = q("devtask").value.trim();
  const r = await fetch("/api/dev", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({sid, task})});
  const j = await r.json();
  q("resp").textContent = j.answer || j.error || "No response";
};

q("tts_btn").onclick = async ()=>{
  const t = q("tts_text").value.trim();
  const r = await fetch("/api/tts", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({sid, text: t})});
  const blob = await r.blob();
  const url = URL.createObjectURL(blob);
  q("tts_out").innerHTML = '<audio controls src="'+url+'"></audio>';
};

q("img_btn").onclick = async ()=>{
  const p = q("img_prompt").value.trim();
  const r = await fetch("/api/image", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({sid, prompt: p})});
  const ct = r.headers.get("Content-Type");
  if (ct && ct.startsWith("image/")){
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    q("img_out").innerHTML = '<img style="max-width:100%" src="'+url+'"/>';
  } else {
    const j = await r.json();
    q("img_out").textContent = j.message || "No image";
  }
};

q("aff_btn").onclick = async ()=>{
  const u = q("aff_url").value.trim();
  const r = await fetch("/api/aff", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({url: u})});
  const j = await r.json();
  q("aff_out").textContent = j.tagged_url || j.error || "";
};
</script>
</body>
</html>
"""

@app.route("/")
def home():
    return render_template_string(BASE_HTML)

@app.route("/health")
def health():
    return jsonify({"ok": True, "ts": now_ts()})

@app.post("/api/ask")
async def api_ask():
    data = request.get_json(force=True, silent=True) or {}
    sid = data.get("sid") or new_session(None, "web")
    q = (data.get("q") or "").strip()
    touch_session(sid)
    if not q:
        return jsonify({"error":"empty question"}), 400
    ans = await ai_chat(q)
    log_event(None, sid, "chat", q, ans)
    # Monetization: per-answer earning hook (tiny)
    record_earning(sid, "pro", cents=1, meta={"reason":"answer"})
    return jsonify({"answer": ans, "sid": sid})

@app.post("/api/dev")
async def api_dev():
    data = request.get_json(force=True, silent=True) or {}
    sid = data.get("sid") or new_session(None, "web")
    task = (data.get("task") or "").strip()
    touch_session(sid)
    if not task:
        return jsonify({"error":"empty task"}), 400
    ans = await dev_agent(task)
    log_event(None, sid, "code", task, ans)
    record_earning(sid, "pro", cents=2, meta={"reason":"dev"})
    return jsonify({"answer": ans})

@app.post("/api/tts")
def api_tts():
    data = request.get_json(force=True, silent=True) or {}
    sid = data.get("sid") or new_session(None, "web")
    text = (data.get("text") or "").strip()
    touch_session(sid)
    if not text:
        return jsonify({"error":"empty text"}), 400
    audio = synthesize_tts(text)
    log_event(None, sid, "tts", text, "[audio]")
    record_earning(sid, "pro", cents=1, meta={"reason":"tts"})
    return send_file(io.BytesIO(audio), mimetype="audio/mpeg", as_attachment=False, download_name="speech.mp3")

@app.post("/api/image")
async def api_image():
    data = request.get_json(force=True, silent=True) or {}
    sid = data.get("sid") or new_session(None, "web")
    prompt = (data.get("prompt") or "").strip()
    touch_session(sid)
    if not prompt:
        return jsonify({"error":"empty prompt"}), 400
    msg, img = await ai_image(prompt)
    if img:
        log_event(None, sid, "image", prompt, "[image]")
        record_earning(sid, "pro", cents=2, meta={"reason":"image"})
        return send_file(io.BytesIO(img), mimetype="image/png", as_attachment=False, download_name="image.png")
    else:
        log_event(None, sid, "image", prompt, msg)
        return jsonify({"message": msg})

@app.post("/api/aff")
def api_aff():
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error":"empty url"}), 400
    tagged = make_affiliate(url)
    return jsonify({"tagged_url": tagged})

@app.get("/api/visit")
def api_visit():
    sid = request.args.get("sid") or new_session(None, "web")
    touch_session(sid)
    log_event(None, sid, "visit", "landing", "ok")
    # Visit earning (tiny)
    record_earning(sid, "visit", cents=1, meta={"path": request.path, "ua": request.headers.get("User-Agent","")[:120]})
    return jsonify({"ok": True, "sid": sid})

# ---- Telegram Bot

RATE_LIMITER = None
try:
    from telegram.ext import AIORateLimiter
    RATE_LIMITER = AIORateLimiter(max_retries=3)
    log.info("Rate limiter enabled.")
except Exception:
    log.warning("Rate limiter not available. Proceeding without it.")

def kb_main():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧠 Dev Agent", callback_data="menu_dev"),
         InlineKeyboardButton("🖼️ Image", callback_data="menu_img")],
        [InlineKeyboardButton("🔊 TTS", callback_data="menu_tts"),
         InlineKeyboardButton("⬇️ YT", callback_data="menu_yt")]
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    add_user(u.id, u.username or "")
    sid = new_session(u.id, "tg")
    text = ("Monster AI ready!\n"
            "Use /ask <question>\n"
            "/dev <task>\n"
            "/tts <text>\n"
            "/yt <url>\n"
            "/img <prompt>\n")
    await update.effective_chat.send_message(text, reply_markup=kb_main())
    log_event(u.id, sid, "visit", "start", "ok")
    record_earning(sid, "visit", 1, {"via":"tg_start"})

async def ask_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user; add_user(u.id, u.username or "")
    sid = new_session(u.id, "tg")
    q = " ".join(context.args) if context.args else (update.message.text or "").replace("/ask","",1).strip()
    if not q:
        await update.message.reply_text("Send like: /ask how to center a div?")
        return
    await update.message.reply_text("Thinking…")
    ans = await ai_chat(q)
    await update.message.reply_text(ans[:4000])
    log_event(u.id, sid, "chat", q, ans)

async def dev_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user; add_user(u.id, u.username or "")
    sid = new_session(u.id, "tg")
    task = " ".join(context.args) if context.args else (update.message.text or "").replace("/dev","",1).strip()
    if not task:
        await update.message.reply_text("Send like: /dev build a todo app with FastAPI and SQLite")
        return
    await update.message.reply_text("Engineering…")
    ans = await dev_agent(task)
    await update.message.reply_text(ans[:4000], disable_web_page_preview=True)
    log_event(u.id, sid, "code", task, ans)

async def tts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user; add_user(u.id, u.username or "")
    sid = new_session(u.id, "tg")
    text = " ".join(context.args) if context.args else (update.message.text or "").replace("/tts","",1).strip()
    if not text:
        await update.message.reply_text("Send like: /tts welcome to monster!")
        return
    audio = synthesize_tts(text)
    await update.message.reply_voice(voice=audio)
    log_event(u.id, sid, "tts", text, "[voice]")

async def yt_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user; add_user(u.id, u.username or "")
    sid = new_session(u.id, "tg")
    url = " ".join(context.args) if context.args else (update.message.text or "").replace("/yt","",1).strip()
    if not url:
        await update.message.reply_text("Send like: /yt https://youtube.com/watch?v=...")
        return
    try:
        if not HAVE_YTDLP:
            await update.message.reply_text("yt-dlp unavailable in this environment.")
            return
        await update.message.reply_text("Downloading…")
        name, data = await asyncio.get_running_loop().run_in_executor(None, download_youtube, url)
        bio = io.BytesIO(data); bio.name = name
        await update.message.reply_document(document=bio)
        log_event(u.id, sid, "yt", url, name)
    except Exception as e:
        await update.message.reply_text(f"Failed: {e}")

async def img_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user; add_user(u.id, u.username or "")
    sid = new_session(u.id, "tg")
    prompt = " ".join(context.args) if context.args else (update.message.text or "").replace("/img","",1).strip()
    if not prompt:
        await update.message.reply_text("Send like: /img a cozy cabin in snow")
        return
    msg, img = await ai_image(prompt)
    if img:
        await update.message.reply_photo(photo=img, caption="Here you go!")
    else:
        await update.message.reply_text(msg)
    log_event(u.id, sid, "image", prompt, msg if not img else "[image]")

async def inline_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    u = update.effective_user; add_user(u.id, u.username or "")
    sid = new_session(u.id, "tg")
    data = query.data
    if data == "menu_dev":
        await query.edit_message_text("Send /dev <your task>", reply_markup=kb_main())
    elif data == "menu_img":
        await query.edit_message_text("Send /img <prompt>", reply_markup=kb_main())
    elif data == "menu_tts":
        await query.edit_message_text("Send /tts <text>", reply_markup=kb_main())
    elif data == "menu_yt":
        await query.edit_message_text("Send /yt <url>", reply_markup=kb_main())
    else:
        await query.edit_message_text("Unknown", reply_markup=kb_main())
    log_event(u.id, sid, "callback", data, "ok")

# ---- Scheduler jobs
scheduler = AsyncIOScheduler()

def job_heartbeat():
    # summarize last hour logs & add tiny earning to simulate ads
    con = db(); cur = con.cursor()
    cur.execute("SELECT count(*) c FROM logs WHERE ts >= datetime('now','-1 hour')")
    c = cur.fetchone()["c"]
    con.close()
    sid = new_session(None, "system")
    record_earning(sid, "ad", cents=max(0, min(5, c//10)), meta={"reason":"hourly_traffic"})
    log.info(f"Heartbeat: logs_last_hour={c}")

scheduler.add_job(job_heartbeat, "interval", minutes=60, id="heartbeat")

# ---- Bootstrap App + Bot
def build_application() -> Application:
    b = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN)
    if RATE_LIMITER:
        b = b.rate_limiter(RATE_LIMITER)
    app = b.build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ask", ask_cmd))
    app.add_handler(CommandHandler("dev", dev_cmd))
    app.add_handler(CommandHandler("tts", tts_cmd))
    app.add_handler(CommandHandler("yt", yt_cmd))
    app.add_handler(CommandHandler("img", img_cmd))
    app.add_handler(CallbackQueryHandler(inline_cb))
    # Generic text -> /ask
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, ask_cmd))
    return app

async def run_all():
    # Start scheduler
    scheduler.start()
    # Start Telegram
    if not TELEGRAM_BOT_TOKEN:
        log.warning("TELEGRAM_BOT_TOKEN missing — Telegram bot will not start.")
    else:
        application = build_application()
        asyncio.create_task(application.initialize())
        asyncio.create_task(application.start())
        asyncio.create_task(application.updater.start_polling())
        log.info("Telegram bot started (polling).")

    # Start Flask (ASGI via hypercorn/uvicorn would be ideal; here use built-in in a thread)
    # Use waitress? Keep simple: run in a thread via asyncio.to_thread + Werkzeug dev server is blocking.
    # We'll spin a thread for Flask using WSGI server from Werkzeug.
    import threading
    def _run_flask():
        app.run(host="0.0.0.0", port=PORT)
    t = threading.Thread(target=_run_flask, daemon=True)
    t.start()

    # Keep the event loop alive
    while True:
        await asyncio.sleep(3600)

def main():
    if not os.path.exists(DB_PATH):
        ensure_schema()
    log.info("Monster starting…")
    try:
        asyncio.run(run_all())
    except (KeyboardInterrupt, SystemExit):
        pass

if __name__ == "__main__":
    main()
