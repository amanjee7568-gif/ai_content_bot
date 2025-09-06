#!/usr/bin/env python3
# main.py — Full working AI Content Creator Telegram Bot
# Requirements: see requirements.txt (openai==1.42.0, httpx==0.27.2, httpcore==1.0.4, python-telegram-bot==21.6, ...)
# ENV (must set in Render / hosting):
#   BOT_TOKEN, OPENAI_API_KEY, ADMIN_ID (numeric), WEBHOOK_URL (optional)
# optional: BUSINESS_NAME, BUSINESS_EMAIL, SUPPORT_USERNAME, UPI_ID

import os
import sys
import logging
import sqlite3
import random
import tempfile
import subprocess
from datetime import datetime, timedelta
from typing import Optional

# -------------------- Optional runtime installs (best-effort) --------------------
def _ensure(pkg):
    try:
        __import__(pkg)
    except Exception:
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", pkg])
        except Exception:
            pass

# -------------------- Core libs --------------------
import requests
from gtts import gTTS

# OpenAI new SDK (ensure openai==1.42.0 in requirements)
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

# Video / audio helpers (moviepy optional)
try:
    from moviepy.editor import ColorClip, CompositeVideoClip, AudioFileClip, TextClip
    MOVIEPY_AVAILABLE = True
except Exception:
    MOVIEPY_AVAILABLE = False

# yt-dlp optional
try:
    import yt_dlp
except Exception:
    yt_dlp = None

# Scheduler (APScheduler)
from apscheduler.schedulers.background import BackgroundScheduler

# -------------------- Logging --------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# -------------------- Config from ENV --------------------
# Keep variable names compatible with your previous deploys
BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")  # accept both
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")  # e.g. https://ai-content-bot-xxxxx.onrender.com
PORT = int(os.getenv("PORT", os.getenv("RENDER_PORT", "10000")))

BUSINESS_NAME = os.getenv("BUSINESS_NAME", "AI Content Agency")
BUSINESS_EMAIL = os.getenv("BUSINESS_EMAIL", "")
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "@support")
UPI_ID = os.getenv("UPI_ID", "")

if not BOT_TOKEN or not OPENAI_API_KEY:
    logger.error("ENV MISSING: BOT_TOKEN and OPENAI_API_KEY must be set.")
    # do not exit immediately; keep running for inspection (but many features will fail)

# Initialize OpenAI client - NOTE: no proxies argument, uses new SDK
try:
    openai_client = OpenAI(api_key=OPENAI_API_KEY)
except Exception as e:
    logger.exception("Failed to initialize OpenAI client: %s", e)
    openai_client = None

# -------------------- Simple SQLite DB for users/premium --------------------
DB_FILE = os.getenv("DB_FILE", "bot_data.db")

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
    CREATE TABLE IF NOT EXISTS payments (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER,
      amount REAL,
      gateway TEXT,
      status TEXT,
      ts TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)
    conn.commit()
    conn.close()

def add_user(user_id: int, first_name: str = "", username: str = ""):
    try:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("INSERT OR IGNORE INTO users (user_id, first_name, username) VALUES (?, ?, ?)",
                    (user_id, first_name, username))
        conn.commit()
    except Exception:
        logger.exception("add_user error")
    finally:
        try: conn.close()
        except: pass

def set_premium(user_id: int, days: int = 30):
    expiry = (datetime.utcnow() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
    cur.execute("UPDATE users SET is_premium=1, expiry=? WHERE user_id=?", (expiry, user_id))
    conn.commit()
    conn.close()

def remove_premium(user_id: int):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("UPDATE users SET is_premium=0, expiry=NULL WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()

def is_premium(user_id: int) -> bool:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT is_premium, expiry FROM users WHERE user_id=?", (user_id,))
    r = cur.fetchone()
    conn.close()
    if not r:
        return False
    active, expiry = r
    if not active:
        return False
    if expiry:
        try:
            exp_dt = datetime.strptime(expiry, "%Y-%m-%d %H:%M:%S")
            if datetime.utcnow() > exp_dt:
                remove_premium(user_id)
                return False
        except Exception:
            remove_premium(user_id)
            return False
    return True

def all_users():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users")
    rows = [r[0] for r in cur.fetchall()]
    conn.close()
    return rows

def stats():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*), SUM(is_premium) FROM users")
    row = cur.fetchone()
    conn.close()
    if not row:
        return 0, 0
    total, prem = row
    return int(total or 0), int(prem or 0)

# -------------------- Ads rotation --------------------
ADS = [
    "🔥 Sponsored: Try our premium AI tools!",
    "💡 Learn more — business solutions available.",
    "📢 Promote here — contact admin."
]
def ad():
    return random.choice(ADS)

# -------------------- Persistent Reply Keyboard (Main menu + Back) --------------------
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

# -------------------- OpenAI helpers (new SDK) --------------------
def ai_chat(prompt: str, system: Optional[str] = None, max_tokens: int = 400) -> str:
    if openai_client is None:
        return "⚠️ OpenAI client not initialized."
    try:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            max_tokens=max_tokens,
            temperature=0.8,
        )
        # resp may be object-like or dict-like; handle both
        try:
            return resp.choices[0].message.content.strip()
        except Exception:
            # dict-like fallback
            return resp.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
    except Exception as e:
        logger.exception("OpenAI chat error")
        return f"⚠️ OpenAI error: {e}"

def openai_image(prompt: str) -> Optional[str]:
    if openai_client is None:
        return None
    try:
        res = openai_client.images.generate(prompt=prompt, size="512x512")
        if hasattr(res, "data") and len(res.data) > 0:
            return getattr(res.data[0], "url", None) or res.data[0].get("url")
        if isinstance(res, dict):
            return res.get("data", [{}])[0].get("url")
        return None
    except Exception as e:
        logger.exception("OpenAI image error")
        return None

# -------------------- Media helpers --------------------
def create_video_from_text(text: str, out_path: str = "output.mp4", duration: int = 8) -> Optional[str]:
    """
    If moviepy available, produce simple vertical video with text + TTS audio.
    Else fallback: produce mp3 TTS file path.
    """
    try:
        if not MOVIEPY_AVAILABLE:
            tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
            tts = gTTS(text=text, lang="hi")
            tts.save(tmp.name)
            return tmp.name

        # create TTS audio
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
    except Exception as e:
        logger.exception("create_video error")
        return None

def download_youtube(url: str) -> Optional[str]:
    if not yt_dlp:
        logger.warning("yt-dlp not installed")
        return None
    try:
        tmpdir = tempfile.gettempdir()
        outtmpl = os.path.join(tmpdir, "ai_bot_yt.%(ext)s")
        ydl_opts = {
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/mp4/best",
            "outtmpl": outtmpl,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            return filename
    except Exception as e:
        logger.exception("yt-dlp error")
        return None

# -------------------- Telegram Handlers --------------------
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    add_user(user.id, user.first_name or "", user.username or "")
    text = (f"👋 नमस्ते *{user.first_name or 'User'}*!\n\n"
            f"Welcome to *{BUSINESS_NAME}* — AI Content Creator Bot.\nChoose from menu below.")
    try:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=main_menu_markup())
        await update.message.reply_text(ad())
    except Exception:
        logger.exception("start_handler reply failed")

async def ping_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Bot is online.", reply_markup=main_menu_markup())

# Central router for persistent keyboard
async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    user = update.effective_user
    add_user(user.id, user.first_name or "", user.username or "")

    # Menu options
    if text == "🤖 AI Chat":
        await update.message.reply_text("✍️ Send your question (or /ai <text>)", reply_markup=back_markup())
        context.user_data["awaiting"] = "ai"
        return
    if text == "🎙 Voice Reply":
        await update.message.reply_text("🎤 Send text to convert to voice (or /voice <text>)", reply_markup=back_markup())
        context.user_data["awaiting"] = "voice"
        return
    if text == "🎬 Create Video":
        await update.message.reply_text("🎬 Send a topic/text to create a short vertical video (or /create <text>)", reply_markup=back_markup())
        context.user_data["awaiting"] = "create"
        return
    if text == "🖼 AI Image":
        await update.message.reply_text("🖼 Send an image description (or /image <desc>)", reply_markup=back_markup())
        context.user_data["awaiting"] = "image"
        return
    if text == "📥 YouTube Download":
        await update.message.reply_text("📥 Send a YouTube link (or /yt <url>)", reply_markup=back_markup())
        context.user_data["awaiting"] = "yt"
        return
    if text == "⭐ Premium":
        await show_premium(update, context)
        return
    if text == "🆘 Support":
        await update.message.reply_text("Send your message and it will be forwarded to admin.", reply_markup=back_markup())
        context.user_data["awaiting"] = "support"
        return
    if text == "🔗 Contact":
        await update.message.reply_text(f"Contact: {SUPPORT_USERNAME}\nEmail: {BUSINESS_EMAIL}", reply_markup=main_menu_markup())
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
        elif awaiting == "support":
            await handle_support_text(update, context)
        context.user_data.pop("awaiting", None)
        return

    # fallback: non-command text -> AI chat
    if update.message.text and not update.message.text.startswith("/"):
        await handle_ai_text(update, context)
        return

    await update.message.reply_text("Unknown option — use the menu.", reply_markup=main_menu_markup())

# Feature implementations
async def handle_ai_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt = update.message.text or ""
    if not prompt:
        await update.message.reply_text("Send text for AI.", reply_markup=main_menu_markup()); return
    await update.message.reply_text("⌛ Generating AI response...")
    reply = ai_chat(prompt, system="You are a helpful assistant.", max_tokens=400)
    await update.message.reply_text(reply + "\n\n" + ad(), reply_markup=main_menu_markup())

async def handle_voice_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    if not text:
        await update.message.reply_text("Send text to convert to voice.", reply_markup=main_menu_markup()); return
    await update.message.reply_text("🔉 Generating voice...")
    reply = ai_chat(text, max_tokens=300)
    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    try:
        tts = gTTS(text=reply, lang="hi")
        tts.save(tmp.name)
        await update.message.reply_voice(voice=open(tmp.name, "rb"), caption=ad(), reply_markup=main_menu_markup())
    except Exception as e:
        logger.exception("voice error")
        await update.message.reply_text("⚠️ Voice generation failed: " + str(e), reply_markup=main_menu_markup())
    finally:
        try: os.remove(tmp.name)
        except: pass

async def handle_create_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topic = update.message.text or ""
    if not topic:
        await update.message.reply_text("Send topic/text to create script+media.", reply_markup=main_menu_markup()); return
    await update.message.reply_text("🎬 Creating script + media...")
    script = ai_chat(f"Write a short, engaging vertical video script for: {topic}", max_tokens=450)
    media_path = create_video_from_text(script, out_path=tempfile.gettempdir() + "/ai_out.mp4", duration=10)
    if not media_path:
        await update.message.reply_text("⚠️ Video creation not available. Here is the script:\n\n" + script + "\n\n" + ad(), reply_markup=main_menu_markup())
        return
    try:
        if media_path.lower().endswith(".mp3"):
            await update.message.reply_audio(audio=open(media_path, "rb"), caption=script + "\n\n" + ad(), reply_markup=main_menu_markup())
        else:
            await update.message.reply_video(video=open(media_path, "rb"), caption=script + "\n\n" + ad(), reply_markup=main_menu_markup())
    except Exception as e:
        logger.exception("send media error")
        await update.message.reply_text("⚠️ Could not send media: " + str(e) + "\n\nHere is script:\n" + script, reply_markup=main_menu_markup())
    finally:
        try: os.remove(media_path)
        except: pass

async def handle_image_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt = update.message.text or ""
    if not prompt:
        await update.message.reply_text("Send a description to generate an image.", reply_markup=main_menu_markup()); return
    await update.message.reply_text("🖼 Generating image... Please wait.")
    url = openai_image(prompt)
    if url:
        try:
            await update.message.reply_photo(photo=url, caption=f"Image for: {prompt}\n\n{ad()}", reply_markup=main_menu_markup())
        except Exception:
            try:
                r = requests.get(url, stream=True, timeout=30)
                tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
                with open(tmp.name, "wb") as f:
                    for chunk in r.iter_content(1024):
                        f.write(chunk)
                await update.message.reply_photo(photo=open(tmp.name, "rb"), caption=f"Image for: {prompt}\n\n{ad()}", reply_markup=main_menu_markup())
            except Exception as e:
                logger.exception("image fallback error")
                await update.message.reply_text("⚠️ Could not send image: " + str(e), reply_markup=main_menu_markup())
            finally:
                try: os.remove(tmp.name)
                except: pass
    else:
        await update.message.reply_text("⚠️ Image generation failed.", reply_markup=main_menu_markup())

async def handle_yt_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text or ""
    if not url:
        await update.message.reply_text("Send a YouTube URL.", reply_markup=main_menu_markup()); return
    await update.message.reply_text("📥 Downloading video — may take a while.")
    path = download_youtube(url)
    if not path:
        await update.message.reply_text("⚠️ Download failed or yt-dlp unavailable.", reply_markup=main_menu_markup()); return
    try:
        await update.message.reply_video(video=open(path, "rb"), caption=ad(), reply_markup=main_menu_markup())
    except Exception as e:
        logger.exception("yt send error")
        await update.message.reply_text("⚠️ Could not send video: " + str(e) + f"\nSaved at {path}", reply_markup=main_menu_markup())
    finally:
        try: os.remove(path)
        except: pass

async def handle_support_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    user = update.effective_user
    msg = f"📩 Support from @{user.username or 'N/A'} (ID:{user.id}):\n\n{text}"
    try:
        await context.bot.send_message(chat_id=ADMIN_ID, text=msg)
        await update.message.reply_text("✅ Sent to admin.", reply_markup=main_menu_markup())
    except Exception as e:
        logger.exception("support forward error")
        await update.message.reply_text("⚠️ Could not forward to admin: " + str(e), reply_markup=main_menu_markup())

# ---------- Direct command handlers ----------
async def cmd_ai(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /ai <text>", reply_markup=main_menu_markup()); return
    prompt = " ".join(context.args)
    await update.message.reply_text("⌛ Generating AI response...")
    reply = ai_chat(prompt)
    await update.message.reply_text(reply + "\n\n" + ad(), reply_markup=main_menu_markup())

async def cmd_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /voice <text>", reply_markup=main_menu_markup()); return
    prompt = " ".join(context.args)
    await update.message.reply_text("🔉 Generating voice...")
    reply = ai_chat(prompt)
    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    try:
        tts = gTTS(text=reply, lang="hi")
        tts.save(tmp.name)
        await update.message.reply_voice(voice=open(tmp.name, "rb"), caption=ad(), reply_markup=main_menu_markup())
    except Exception as e:
        logger.exception("cmd_voice error")
        await update.message.reply_text("⚠️ Voice error: " + str(e), reply_markup=main_menu_markup())
    finally:
        try: os.remove(tmp.name)
        except: pass

async def cmd_create(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /create <text>", reply_markup=main_menu_markup()); return
    topic = " ".join(context.args)
    await update.message.reply_text("🎬 Creating script + media...")
    script = ai_chat(f"Write a short, engaging vertical video script for: {topic}")
    out = create_video_from_text(script, out_path=tempfile.gettempdir() + "/ai_create.mp4", duration=10)
    if not out:
        await update.message.reply_text("⚠️ Could not create media. Here is script:\n\n" + script, reply_markup=main_menu_markup()); return
    try:
        if out.endswith(".mp3"):
            await update.message.reply_audio(audio=open(out, "rb"), caption=script + "\n\n" + ad(), reply_markup=main_menu_markup())
        else:
            await update.message.reply_video(video=open(out, "rb"), caption=script + "\n\n" + ad(), reply_markup=main_menu_markup())
    except Exception as e:
        logger.exception("cmd_create send error")
        await update.message.reply_text("⚠️ Sending media failed: " + str(e), reply_markup=main_menu_markup())
    finally:
        try: os.remove(out)
        except: pass

async def cmd_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /image <description>", reply_markup=main_menu_markup()); return
    prompt = " ".join(context.args)
    await update.message.reply_text("🖼 Generating image...")
    url = openai_image(prompt)
    if url:
        await update.message.reply_photo(photo=url, caption=f"Generated for: {prompt}\n\n{ad()}", reply_markup=main_menu_markup())
    else:
        await update.message.reply_text("⚠️ Image generation failed.", reply_markup=main_menu_markup())

async def cmd_yt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /yt <youtube_url>", reply_markup=main_menu_markup()); return
    url = context.args[0]
    await update.message.reply_text("📥 Downloading video... may take time.")
    path = download_youtube(url)
    if not path:
        await update.message.reply_text("⚠️ Download failed.", reply_markup=main_menu_markup()); return
    try:
        await update.message.reply_video(video=open(path, "rb"), caption=ad(), reply_markup=main_menu_markup())
    except Exception as e:
        logger.exception("cmd_yt send error")
        await update.message.reply_text("⚠️ Could not send video: " + str(e), reply_markup=main_menu_markup())
    finally:
        try: os.remove(path)
        except: pass

async def cmd_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    add_user(uid, update.effective_user.first_name or "", update.effective_user.username or "")
    if not UPI_ID:
        await update.message.reply_text("Payment not configured. Contact admin.", reply_markup=main_menu_markup()); return
    amount = "199"
    upi_uri = f"upi://pay?pa={UPI_ID}&pn={BUSINESS_NAME}&cu=INR&am={amount}"
    kb = [[InlineKeyboardButton("💳 Pay via UPI", url=upi_uri)]]
    await update.message.reply_text(f"⭐ Premium — ₹{amount}/30 days\nAfter payment, send screenshot to support to activate.", reply_markup=main_menu_markup())
    await update.message.reply_text("Choose payment method:", reply_markup=InlineKeyboardMarkup(kb))

# -------------------- Admin --------------------
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Unauthorized.")
        return
    kb = [
        [InlineKeyboardButton("👥 Users Stats", callback_data="admin_stats"), InlineKeyboardButton("💎 Manage Premium", callback_data="admin_premium")],
        [InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast"), InlineKeyboardButton("🔙 Back", callback_data="admin_back")]
    ]
    await update.message.reply_text("Admin Panel:", reply_markup=InlineKeyboardMarkup(kb))

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "admin_stats":
        total, prem = stats()
        await q.edit_message_text(f"Users: {total}\nPremium: {prem}")
    elif q.data == "admin_premium":
        await q.edit_message_text("Use /addpremium <user_id> <days> or /removepremium <user_id>")
    elif q.data == "admin_broadcast":
        await q.edit_message_text("Use /broadcast <message>")
    else:
        await q.edit_message_text("Admin menu.")

async def addpremium_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /addpremium <user_id> <days>")
        return
    uid = int(context.args[0]); days = int(context.args[1])
    set_premium(uid, days)
    await update.message.reply_text(f"✅ Set premium for {uid} for {days} days.")

async def removepremium_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    uid = int(context.args[0])
    remove_premium(uid)
    await update.message.reply_text(f"❌ Removed premium for {uid}.")

async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    if not context.args:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    msg = " ".join(context.args)
    users = all_users()
    sent = 0
    for u in users:
        try:
            await context.bot.send_message(u, f"📢 {msg}")
            sent += 1
        except Exception:
            pass
    await update.message.reply_text(f"Broadcast sent to {sent} users.")

# -------------------- Daily premium check job (APScheduler) --------------------
def daily_check(app: Application):
    logger.info("Running daily premium check job.")
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT user_id, expiry FROM users WHERE is_premium=1 AND expiry IS NOT NULL")
    rows = cur.fetchall()
    now = datetime.utcnow()
    for uid, expiry in rows:
        try:
            exp_dt = datetime.strptime(expiry, "%Y-%m-%d %H:%M:%S")
        except:
            remove_premium(uid)
            continue
        # 1 day reminder
        if 0 <= (exp_dt - now).days <= 1:
            try:
                app.bot.send_message(uid, "⏰ Reminder: Your premium expires soon. Renew with /premium")
            except Exception:
                pass
        if now > exp_dt:
            remove_premium(uid)
            try:
                app.bot.send_message(uid, "⚠️ Your premium has expired.")
            except Exception:
                pass
    conn.close()

# -------------------- App setup & run --------------------
def main():
    init_db()
    # Build application
    app = Application.builder().token(BOT_TOKEN).build()

    # Commands & handlers
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("ping", ping_handler))

    app.add_handler(CommandHandler("ai", cmd_ai))
    app.add_handler(CommandHandler("voice", cmd_voice))
    app.add_handler(CommandHandler("create", cmd_create))
    app.add_handler(CommandHandler("image", cmd_image))
    app.add_handler(CommandHandler("yt", cmd_yt))
    app.add_handler(CommandHandler("premium", cmd_premium))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("addpremium", addpremium_cmd))
    app.add_handler(CommandHandler("removepremium", removepremium_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))

    app.add_handler(CallbackQueryHandler(admin_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_router))

    # Start APScheduler and schedule daily_check with app reference
    sched = BackgroundScheduler()
    sched.add_job(lambda: daily_check(app), "cron", hour=9, minute=0)
    sched.start()
    logger.info("Scheduler started.")

    # Webhook mode (Render) — uses BOT_TOKEN as URL path for security if WEBHOOK_URL set
    try:
        if WEBHOOK_URL:
            logger.info("Starting webhook on port %s", PORT)
            # Run webhook with the given port and URL
            app.run_webhook(
                listen="0.0.0.0",
                port=PORT,
                url_path=BOT_TOKEN,
                webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}"
            )
        else:
            logger.info("Starting polling (WEBHOOK_URL not set).")
            app.run_polling()
    except Exception as e:
        logger.exception("Failed to start Telegram application: %s", e)

if __name__ == "__main__":
    main()
