#!/usr/bin/env python3
"""
Production-ready ChatGPT-like Telegram Bot (main.py)
Features:
 - Multi-turn conversation (history persisted in SQLite)
 - Token-aware history trimming using tiktoken (fallback heuristic available)
 - Moderation checks using OpenAI Moderation
 - Rate limiting (simple per-user checks in SQLite)
 - Embeddings/RAG stub (stores embeddings in SQLite, uses OpenAI embeddings)
 - OpenAI new SDK (from openai import OpenAI) — avoids 'proxies' error
 - Webhook (Render) or polling fallback
 - yt-dlp fallback handling, TTS, basic video creation (moviepy)
"""

import os
import sys
import logging
import sqlite3
import random
import tempfile
import traceback
import time
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

import requests
from gtts import gTTS

# OpenAI new SDK
from openai import OpenAI

# Telegram
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# Optional libs
try:
    import tiktoken
    TIKTOKEN_AVAILABLE = True
except Exception:
    TIKTOKEN_AVAILABLE = False

try:
    from moviepy.editor import ColorClip, CompositeVideoClip, AudioFileClip, TextClip
    MOVIEPY_AVAILABLE = True
except Exception:
    MOVIEPY_AVAILABLE = False

try:
    import yt_dlp
except Exception:
    yt_dlp = None

from apscheduler.schedulers.background import BackgroundScheduler

# ----------------- Logging -----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("chatgpt_bot")

# ----------------- Config (ENV) -----------------
BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")  # e.g. https://your-app.onrender.com
PORT = int(os.getenv("PORT", os.getenv("RENDER_PORT", "10000")))
DB_FILE = os.getenv("DB_FILE", "bot_data.db")
BUSINESS_NAME = os.getenv("BUSINESS_NAME", "AI Assistant")
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "@support")

if not BOT_TOKEN:
    logger.error("BOT_TOKEN env var missing.")
if not OPENAI_API_KEY:
    logger.error("OPENAI_API_KEY env var missing — AI features will not work.")

# ----------------- OpenAI client -----------------
openai_client: Optional[OpenAI] = None
try:
    if OPENAI_API_KEY:
        openai_client = OpenAI(api_key=OPENAI_API_KEY)
        logger.info("OpenAI client initialized.")
except Exception:
    logger.exception("Failed to initialize OpenAI client")
    openai_client = None

# ----------------- Token counting -----------------
if TIKTOKEN_AVAILABLE:
    try:
        ENCODER = tiktoken.get_encoding("cl100k_base")
    except Exception:
        ENCODER = None
        logger.warning("tiktoken: failed to get encoding, will fallback to heuristic.")
else:
    ENCODER = None

def count_tokens(text: str) -> int:
    """
    Return approximate number of tokens for the text.
    Uses tiktoken if available, else uses heuristic (chars/4).
    """
    if ENCODER:
        try:
            return len(ENCODER.encode(text))
        except Exception:
            pass
    # fallback heuristic
    return max(1, len(text) // 4)

# ----------------- DB init -----------------
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
      user_id INTEGER PRIMARY KEY,
      first_name TEXT,
      username TEXT,
      is_premium INTEGER DEFAULT 0,
      expiry TEXT
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS conversations (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER,
      role TEXT,
      content TEXT,
      ts TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS embeddings (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER,
      content TEXT,
      embedding_blob BLOB,
      ts TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS requests_log (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER,
      endpoint TEXT,
      ts TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)
    conn.commit()
    conn.close()

# ----------------- persistence helpers -----------------
def save_message(user_id: int, role: str, content: str):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("INSERT INTO conversations (user_id, role, content) VALUES (?, ?, ?)", (user_id, role, content))
    conn.commit()
    conn.close()

def recent_messages(user_id: int, limit: int = 50) -> List[Dict[str, Any]]:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT role, content, ts FROM conversations WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit))
    rows = cur.fetchall()
    conn.close()
    # return chronological order
    out = [{"role": r[0], "content": r[1], "ts": r[2]} for r in reversed(rows)]
    return out

def log_request(user_id: int, endpoint: str):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("INSERT INTO requests_log (user_id, endpoint) VALUES (?, ?)", (user_id, endpoint))
    conn.commit()
    conn.close()

def count_requests_in_window(user_id: int, seconds: int = 60) -> int:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM requests_log WHERE user_id=? AND ts > datetime('now', ?)", (user_id, f'-{seconds} seconds'))
    r = cur.fetchone()[0]
    conn.close()
    return r

# ----------------- RAG / Embeddings stub -----------------
def store_embedding(user_id: int, content: str, embedding: List[float]):
    # store embedding as bytes (simple) — for production use vector DB
    import json
    blob = json.dumps(embedding).encode("utf-8")
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("INSERT INTO embeddings (user_id, content, embedding_blob) VALUES (?, ?, ?)", (user_id, content, blob))
    conn.commit()
    conn.close()

def get_similar_embeddings(user_id: int, query_embedding: List[float], top_k: int = 3):
    # naive similarity: load all, compute dot product — ok for small scale only
    import json, math
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT id, content, embedding_blob FROM embeddings WHERE user_id=? ORDER BY id DESC LIMIT 200", (user_id,))
    rows = cur.fetchall()
    conn.close()
    scored = []
    for rid, content, blob in rows:
        try:
            vec = json.loads(blob.decode("utf-8"))
            # cosine similarity
            dot = sum(a*b for a,b in zip(vec, query_embedding))
            norm_q = math.sqrt(sum(a*a for a in query_embedding))
            norm_v = math.sqrt(sum(a*a for a in vec))
            if norm_q > 0 and norm_v > 0:
                score = dot / (norm_q * norm_v)
            else:
                score = 0.0
            scored.append((score, content))
        except Exception:
            continue
    scored.sort(reverse=True, key=lambda x: x[0])
    return [c for s,c in scored[:top_k]]

# ----------------- Moderation -----------------
def check_moderation(text: str) -> bool:
    """
    Returns True if text is allowed, False if it violates moderation.
    """
    if openai_client is None:
        return True
    try:
        res = openai_client.moderations.create(input=text)
        # SDK may return object-like; try both
        if hasattr(res, "results"):
            return not getattr(res.results[0], "flagged", False)
        if isinstance(res, dict):
            return not res.get("results", [{}])[0].get("flagged", False)
    except Exception:
        logger.exception("moderation API failed; defaulting to allow")
        return True

# ----------------- OpenAI chat wrapper -----------------
DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful, safe, and concise assistant. Follow user's intent, but refuse illegal or dangerous instructions."
)

def build_message_payload(user_id: int, user_text: str, token_budget: int = 3000) -> List[Dict[str, str]]:
    """
    Build messages (system + recent history + user) trimmed by token_budget.
    """
    messages = [{"role": "system", "content": DEFAULT_SYSTEM_PROMPT}]
    history = recent_messages(user_id, limit=80)  # last 80 messages
    # include RAG: get embeddings for user_text and query local store (best-effort)
    try:
        if openai_client is not None:
            emb_res = openai_client.embeddings.create(model="text-embedding-3-small", input=user_text)
            q_emb = emb_res.data[0].embedding
            relevant = get_similar_embeddings(user_id, q_emb, top_k=3)
            if relevant:
                rag_text = "\n\n".join([f"- {r}" for r in relevant])
                messages.append({"role": "system", "content": f"Relevant past notes:\n{rag_text}"})
    except Exception:
        # don't fail the whole flow for embeddings
        logger.debug("embeddings/RAG step failed or skipped")

    # now append history messages while respecting token budget
    used_tokens = 0
    # count system tokens
    used_tokens += sum(count_tokens(m["content"]) for m in messages)
    # go through history from oldest to newest
    for item in history:
        t = item["content"]
        tks = count_tokens(t) + 4
        if used_tokens + tks + count_tokens(user_text) > token_budget:
            # skip older messages if budget exceeded
            continue
        messages.append({"role": item["role"], "content": t})
        used_tokens += tks

    messages.append({"role": "user", "content": user_text})
    return messages

def ask_model(messages: List[Dict[str,str]], model: str = "gpt-4o-mini", max_tokens: int = 800, temperature: float = 0.7) -> str:
    if openai_client is None:
        return "⚠️ OpenAI not configured."
    try:
        resp = openai_client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        # parse response
        try:
            return resp.choices[0].message.content.strip()
        except Exception:
            return resp.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
    except Exception:
        logger.exception("ask_model failed")
        return "⚠️ OpenAI request failed. Try again later."

# ----------------- Media helpers (same as your previous) -----------------
def create_video_from_text(text: str, out_path: str = "output.mp4", duration: int = 8) -> Optional[str]:
    try:
        if not MOVIEPY_AVAILABLE:
            tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
            tts = gTTS(text=text, lang="hi")
            tts.save(tmp.name)
            return tmp.name
        audio_file = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False).name
        tts = gTTS(text=text, lang="hi")
        tts.save(audio_file)
        audio = AudioFileClip(audio_file)
        dur = max(duration, int(audio.duration) + 1)
        bg = ColorClip(size=(720, 1280), color=(18, 18, 18), duration=dur)
        try:
            txt = TextClip(text, fontsize=36, color="white", size=(680, None), method="caption")
            txt = txt.set_duration(dur).set_position(("center", "center"))
            video = CompositeVideoClip([bg, txt])
        except Exception:
            video = bg
        video = video.set_audio(audio)
        video.write_videofile(out_path, fps=24, codec="libx264", audio_codec="aac", verbose=False, logger=None)
        try: os.remove(audio_file)
        except: pass
        return out_path
    except Exception:
        logger.exception("create_video error")
        return None

def download_youtube(url: str) -> Optional[str]:
    if not yt_dlp:
        logger.warning("yt-dlp not installed")
        return None
    tmpdir = tempfile.gettempdir()
    outtmpl = os.path.join(tmpdir, "ai_bot_yt.%(ext)s")
    ydl_opts = {
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/mp4/best",
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            # check common extensions
            if os.path.exists(filename):
                return filename
            for ext in ("mp4", "mkv", "webm", "m4a"):
                path = filename.rsplit(".", 1)[0] + "." + ext
                if os.path.exists(path):
                    return path
            return filename
    except Exception:
        logger.exception("download_youtube error")
        return None

# ----------------- UX (menu) -----------------
ADS = [
    "🔥 Try the premium AI features!",
    "💡 Business solutions available.",
    "📢 Contact admin to promote here."
]

def ad():
    return random.choice(ADS)

MAIN_MENU = [
    [KeyboardButton("🤖 AI Chat"), KeyboardButton("🎙 Voice Reply")],
    [KeyboardButton("🎬 Create Video"), KeyboardButton("🖼 AI Image")],
    [KeyboardButton("📥 YouTube Download"), KeyboardButton("⭐ Premium")],
    [KeyboardButton("🆘 Support"), KeyboardButton("🔗 Contact")]
]
BACK_BUTTON = [[KeyboardButton("🔙 Back to Menu")]]

def main_menu_markup():
    return ReplyKeyboardMarkup(MAIN_MENU, resize_keyboard=True)

def back_markup():
    return ReplyKeyboardMarkup(BACK_BUTTON, resize_keyboard=True, one_time_keyboard=True)

# ----------------- Telegram Handlers -----------------
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    add_user(user.id, user.first_name or "", user.username or "")
    text = f"👋 नमस्ते {user.first_name or 'User'}! मैं आपका AI assistant हूँ. पूछिए कुछ भी."
    try:
        await update.message.reply_text(text, reply_markup=main_menu_markup())
        await update.message.reply_text(ad())
    except Exception:
        logger.exception("start_handler failed")

async def ping_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Bot is online.", reply_markup=main_menu_markup())

# Core multi-turn chat handler
async def handle_ai_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("Send some text to chat.", reply_markup=main_menu_markup())
        return

    # Rate limit check
    reqs = count_requests_in_window(user_id, seconds=30)
    if reqs > 10 and not is_premium(user_id):
        await update.message.reply_text("You're sending messages too fast. Please wait a bit.", reply_markup=main_menu_markup())
        return
    log_request(user_id, "chat")

    # Moderation
    if not check_moderation(text):
        await update.message.reply_text("Sorry — your message violates policy and cannot be answered.", reply_markup=main_menu_markup())
        return

    await update.message.reply_text("⌛ Thinking...")

    # Save user message
    save_message(user_id, "user", text)

    # build messages with trimmed history
    messages = build_message_payload(user_id, text, token_budget=3000)
    # ask model
    reply = ask_model(messages, model="gpt-4o-mini", max_tokens=800, temperature=0.7)

    # save assistant reply
    save_message(user_id, "assistant", reply)

    # optionally store embedding of the user message for RAG later
    try:
        if openai_client:
            emb = openai_client.embeddings.create(model="text-embedding-3-small", input=text)
            vector = emb.data[0].embedding
            store_embedding(user_id, text, vector)
    except Exception:
        logger.debug("embedding store failed (non-fatal)")

    await update.message.reply_text(reply + "\n\n" + ad(), reply_markup=main_menu_markup())

# Router for persistent keyboard
async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    user = update.effective_user
    add_user(user.id, user.first_name or "", user.username or "")

    # menu actions
    if text == "🤖 AI Chat":
        await update.message.reply_text("Send your question.", reply_markup=back_markup())
        context.user_data["awaiting"] = "ai"
        return
    if text == "🎙 Voice Reply":
        await update.message.reply_text("Send text to convert to voice.", reply_markup=back_markup())
        context.user_data["awaiting"] = "voice"
        return
    if text == "🎬 Create Video":
        await update.message.reply_text("Send topic/text for a short vertical video.", reply_markup=back_markup())
        context.user_data["awaiting"] = "create"
        return
    if text == "🖼 AI Image":
        await update.message.reply_text("Send image description.", reply_markup=back_markup())
        context.user_data["awaiting"] = "image"
        return
    if text == "📥 YouTube Download":
        await update.message.reply_text("Send a YouTube link.", reply_markup=back_markup())
        context.user_data["awaiting"] = "yt"
        return
    if text == "⭐ Premium":
        await update.message.reply_text("Premium info...", reply_markup=main_menu_markup())
        return
    if text == "🔙 Back to Menu":
        context.user_data.pop("awaiting", None)
        await update.message.reply_text("Back to main menu.", reply_markup=main_menu_markup())
        return

    awaiting = context.user_data.get("awaiting")
    if awaiting:
        if awaiting == "ai":
            await handle_ai_text(update, context)
        elif awaiting == "voice":
            await handle_voice_text(update, context)
        elif awaiting == "create":
            await handle_create_text(update, context)
        elif awaiting == "image":
            await handle_image_text(update, context)
        elif awaiting == "yt":
            await handle_yt_text(update, context)
        context.user_data.pop("awaiting", None)
        return

    # fallback: if plain text (not command), treat as chat
    if update.message.text and not update.message.text.startswith("/"):
        await handle_ai_text(update, context)
        return

    await update.message.reply_text("Unknown option — use the menu.", reply_markup=main_menu_markup())

# Other handlers (voice, create, image, yt) mirror previous implementations
async def handle_voice_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    if not text:
        await update.message.reply_text("Send text to convert to voice.", reply_markup=main_menu_markup()); return
    await update.message.reply_text("🔉 Generating voice...")
    reply = ai_chat(text, max_tokens=300) if 'ai_chat' in globals() else ask_model(build_message_payload(update.effective_user.id, text))
    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    try:
        tts = gTTS(text=reply, lang="hi")
        tts.save(tmp.name)
        await update.message.reply_voice(voice=open(tmp.name, "rb"), caption=ad(), reply_markup=main_menu_markup())
    except Exception:
        logger.exception("voice generation failed")
        await update.message.reply_text("⚠️ Voice generation failed.", reply_markup=main_menu_markup())
    finally:
        try: os.remove(tmp.name)
        except: pass

async def handle_create_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topic = update.message.text or ""
    if not topic:
        await update.message.reply_text("Send topic/text.", reply_markup=main_menu_markup()); return
    await update.message.reply_text("🎬 Creating script + media...")
    script = ask_model(build_message_payload(update.effective_user.id, f"Write a short vertical video script for: {topic}"))
    media_path = create_video_from_text(script, out_path=tempfile.gettempdir() + "/ai_out.mp4", duration=10)
    if not media_path:
        await update.message.reply_text("⚠️ Video creation not available. Here is the script:\n\n" + script, reply_markup=main_menu_markup()); return
    try:
        if media_path.lower().endswith(".mp3"):
            await update.message.reply_audio(audio=open(media_path, "rb"), caption=script + "\n\n" + ad(), reply_markup=main_menu_markup())
        else:
            await update.message.reply_video(video=open(media_path, "rb"), caption=script + "\n\n" + ad(), reply_markup=main_menu_markup())
    except Exception:
        logger.exception("send media error")
        await update.message.reply_text("⚠️ Could not send media.", reply_markup=main_menu_markup())
    finally:
        try: os.remove(media_path)
        except: pass

async def handle_image_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt = update.message.text or ""
    if not prompt:
        await update.message.reply_text("Send a description.", reply_markup=main_menu_markup()); return
    await update.message.reply_text("🖼 Generating image... Please wait.")
    try:
        url = openai_image(prompt) if 'openai_image' in globals() else None
    except Exception:
        url = None
    if url:
        try:
            await update.message.reply_photo(photo=url, caption=f"Image for: {prompt}\n\n{ad()}", reply_markup=main_menu_markup())
            return
        except Exception:
            logger.exception("sending image by url failed, will try download fallback")
    # fallback
    await update.message.reply_text("⚠️ Image generation failed or unavailable.", reply_markup=main_menu_markup())

async def handle_yt_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text or ""
    if not url:
        await update.message.reply_text("Send a YouTube URL.", reply_markup=main_menu_markup()); return
    await update.message.reply_text("📥 Downloading video — may take a while.")
    path = download_youtube(url)
    if not path:
        await update.message.reply_text("⚠️ Download failed or yt-dlp unavailable. Here's the link: " + url, reply_markup=main_menu_markup())
        return
    try:
        await update.message.reply_video(video=open(path, "rb"), caption=ad(), reply_markup=main_menu_markup())
    except Exception:
        logger.exception("send video failed")
        await update.message.reply_text("⚠️ Could not send video. File saved at: " + path, reply_markup=main_menu_markup())
    finally:
        try: os.remove(path)
        except: pass

# Admin & direct command handlers
async def cmd_ai(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /ai <text>"); return
    prompt = " ".join(context.args)
    await update.message.reply_text("⌛ Generating AI response...")
    # moderation
    if not check_moderation(prompt):
        await update.message.reply_text("Message violates policy.")
        return
    # save and respond
    save_message(update.effective_user.id, "user", prompt)
    messages = build_message_payload(update.effective_user.id, prompt)
    resp = ask_model(messages)
    save_message(update.effective_user.id, "assistant", resp)
    await update.message.reply_text(resp)

async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("PONG")

# Admin commands (broadcast, premium management) omitted for brevity — you can reuse earlier implementations

# ----------------- Scheduler daily job -----------------
def daily_check(app: Application):
    logger.info("Running daily premium check.")
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT user_id, expiry FROM users WHERE is_premium=1 AND expiry IS NOT NULL")
    rows = cur.fetchall()
    now = datetime.utcnow()
    for uid, expiry in rows:
        try:
            exp_dt = datetime.strptime(expiry, "%Y-%m-%d %H:%M:%S")
        except Exception:
            remove_premium(uid)
            continue
        if 0 <= (exp_dt - now).days <= 1:
            try:
                app.bot.send_message(uid, "⏰ Your premium expires soon.")
            except Exception:
                pass
        if now > exp_dt:
            remove_premium(uid)
            try:
                app.bot.send_message(uid, "⚠️ Your premium has expired.")
            except Exception:
                pass
    conn.close()

# ----------------- App run -----------------
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    # Handlers
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("ping", ping_handler))
    app.add_handler(CommandHandler("ai", cmd_ai))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_router))

    # Scheduler
    sched = BackgroundScheduler()
    sched.add_job(lambda: daily_check(app), "cron", hour=9, minute=0)
    sched.start()
    logger.info("Scheduler started.")

    # Run webhook or polling
    try:
        if WEBHOOK_URL:
            logger.info("Starting webhook on port %s", PORT)
            app.run_webhook(
                listen="0.0.0.0",
                port=PORT,
                url_path=BOT_TOKEN,
                webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}"
            )
        else:
            logger.info("Starting polling (no WEBHOOK_URL)")
            app.run_polling()
    except Exception:
        logger.exception("Failed to start Application: %s", traceback.format_exc())

if __name__ == "__main__":
    main()
