#!/usr/bin/env python3
# main.py — Professional AI Content Creator Telegram Bot
# Features:
#  - Persistent ReplyKeyboard main menu + Back button
#  - /start, /ping
#  - AI Chat (/ai or direct via "🤖 AI Chat" button) using OpenAI (old SDK 0.27.8)
#  - Voice reply (gTTS) ("🎙 Voice Reply")
#  - Create Video (moviepy if available, else audio fallback) ("🎬 Create Video")
#  - Image generation via OpenAI Images ("🖼 AI Image")
#  - YouTube download via yt-dlp ("📥 YouTube Download")
#  - Premium flow (UPI link) ("⭐ Premium")
#  - Support (forwards message to admin) ("🆘 Support")
#  - Admin panel (/admin) with stats, broadcast, manage premium
#  - Ads rotation appended to many replies
#  - Daily job: premium expiry reminders
#  - Webhook-ready (Render/Gunicorn), uses python-telegram-bot[webhooks]==20.3
#
# ENV vars required:
#   BOT_TOKEN, OPENAI_API_KEY, ADMIN_ID, WEBHOOK_URL
# optional: BUSINESS_NAME, BUSINESS_EMAIL, SUPPORT_USERNAME, UPI_ID

import os
import sys
import logging
import sqlite3
import random
import tempfile
import subprocess
from datetime import datetime, timedelta, time
from typing import Optional

# ensure yt_dlp available at runtime (some hosts skip it)
try:
    import yt_dlp
except Exception:
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "yt-dlp"])
        import yt_dlp
    except Exception:
        yt_dlp = None

# try to import moviepy; fallback if not available
MOVIEPY_AVAILABLE = True
try:
    from moviepy.editor import ColorClip, CompositeVideoClip, AudioFileClip, TextClip
except Exception:
    MOVIEPY_AVAILABLE = False

# other libs
from gtts import gTTS
import openai
import requests

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
    ContextTypes,
    filters,
)

# ---------- Logging ----------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------- Config from ENV ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")  # e.g. https://your-app.onrender.com
PORT = int(os.getenv("PORT", os.getenv("RENDER_PORT", "10000")))

BUSINESS_NAME = os.getenv("BUSINESS_NAME", "AI Content Agency")
BUSINESS_EMAIL = os.getenv("BUSINESS_EMAIL", "")
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "@support")
UPI_ID = os.getenv("UPI_ID", "")

if not BOT_TOKEN or not OPENAI_API_KEY or not WEBHOOK_URL:
    logger.error("Set BOT_TOKEN, OPENAI_API_KEY and WEBHOOK_URL in environment.")
    # don't exit here — allow dev to see message; but bot won't run properly
openai.api_key = OPENAI_API_KEY

# ---------- Database (SQLite) ----------
DB_FILE = "bot_prod.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        is_premium INTEGER DEFAULT 0,
        expiry TEXT
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        amount INTEGER,
        gateway TEXT,
        status TEXT,
        ts TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.commit()
    conn.close()

def add_user(user_id: int):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()

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
    if not r: return False
    active, expiry = r
    if not active: return False
    if expiry:
        try:
            exp_dt = datetime.strptime(expiry, "%Y-%m-%d %H:%M:%S")
            if datetime.utcnow() > exp_dt:
                remove_premium(user_id)
                return False
        except:
            remove_premium(user_id)
            return False
    return True

def get_stats():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*), SUM(is_premium) FROM users")
    total, premium = cur.fetchone()
    conn.close()
    return int(total or 0), int(premium or 0)

def get_premium_users():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT user_id, expiry FROM users WHERE is_premium=1")
    rows = cur.fetchall()
    conn.close()
    return rows

def get_all_users():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users")
    rows = [r[0] for r in cur.fetchall()]
    conn.close()
    return rows

# ---------- Ads ----------
ADS = [
    "🔥 Sponsored: Try our AI Tools — visit our site!",
    "💡 Learn & Earn Online — free webinar today!",
    "📢 Promote here — contact admin."
]
def get_ad(): return random.choice(ADS)

# ---------- Reply Keyboard (persistent) ----------
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

# ---------- OpenAI helpers (old SDK 0.27.8 compatible) ----------
def ai_chat(prompt: str, max_tokens: int = 350) -> str:
    try:
        resp = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=0.75,
        )
        return resp["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.exception("OpenAI chat error")
        return f"⚠️ OpenAI Error: {e}"

def image_generate(prompt: str) -> Optional[str]:
    try:
        res = openai.Image.create(prompt=prompt, size="512x512")
        return res["data"][0]["url"]
    except Exception as e:
        logger.exception("OpenAI image error")
        return None

# ---------- Media helpers ----------
def create_video_from_text(text: str, out_path: str = "output.mp4", duration: int = 8) -> Optional[str]:
    # If moviepy available - make a simple vertical video with TTS
    if not MOVIEPY_AVAILABLE:
        # fallback to TTS audio
        audio_file = "fallback_audio.mp3"
        tts = gTTS(text=text, lang="hi")
        tts.save(audio_file)
        return audio_file
    try:
        # create TTS audio
        audio_file = "audio_tts.mp3"
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
        # cleanup
        try: os.remove(audio_file)
        except: pass
        return out_path
    except Exception as e:
        logger.exception("Video create error")
        return None

def download_youtube(url: str) -> Optional[str]:
    if not yt_dlp:
        logger.warning("yt_dlp not available")
        return None
    tmpdir = tempfile.gettempdir()
    outtmpl = os.path.join(tmpdir, "ai_bot_yt.%(ext)s")
    opts = {
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/mp4/best",
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 1,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            return filename
    except Exception as e:
        logger.exception("yt_dlp error")
        return None

# ---------- Telegram Handlers ----------
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    add_user(user.id)
    welcome = (
        f"👋 नमस्ते *{user.first_name or 'User'}*!\n\n"
        f"Welcome to *{BUSINESS_NAME}* — professional AI content bot.\n"
        f"Use the menu below to choose a feature.\n\n"
        f"📧 {BUSINESS_EMAIL if BUSINESS_EMAIL else ''}"
    )
    await update.message.reply_text(welcome, parse_mode="Markdown", reply_markup=main_menu_markup())
    await update.message.reply_text(get_ad())

async def ping_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Bot is alive. Webhook OK.", reply_markup=main_menu_markup())

# Helper to send ad appended text
async def reply_with_ad(update: Update, text: str):
    await update.message.reply_text(f"{text}\n\n{get_ad()}", reply_markup=main_menu_markup())

# Main menu button router (persistent keyboard)
async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    user = update.effective_user
    add_user(user.id)

    # Main actions
    if text == "🤖 AI Chat":
        await update.message.reply_text("✍️ Send me your question (or use /ai <text>)", reply_markup=back_markup())
        # set a small state: store expecting type in user_data
        context.user_data["awaiting"] = "ai"
        return
    if text == "🎙 Voice Reply":
        await update.message.reply_text("🎤 Send the text for voice response (or use /voice <text>)", reply_markup=back_markup())
        context.user_data["awaiting"] = "voice"
        return
    if text == "🎬 Create Video":
        await update.message.reply_text("🎬 Send the topic/text to generate short video (or /create <text>)", reply_markup=back_markup())
        context.user_data["awaiting"] = "create"
        return
    if text == "🖼 AI Image":
        await update.message.reply_text("🖼 Send an image description (or /image <desc>)", reply_markup=back_markup())
        context.user_data["awaiting"] = "image"
        return
    if text == "📥 YouTube Download":
        await update.message.reply_text("📥 Send YouTube link (or /yt <url>)", reply_markup=back_markup())
        context.user_data["awaiting"] = "yt"
        return
    if text == "⭐ Premium":
        # show inline pay buttons + contact
        if not UPI_ID:
            await update.message.reply_text("Payment not configured. Contact admin.", reply_markup=main_menu_markup())
            return
        amount = "199"
        upi_uri = f"upi://pay?pa={UPI_ID}&pn={BUSINESS_NAME}&cu=INR&am={amount}"
        kb = [[InlineKeyboardButton("💳 Pay via UPI", url=upi_uri), InlineKeyboardButton("📞 Contact Support", url=f"https://t.me/{SUPPORT_USERNAME.lstrip('@')}")]]
        await update.message.reply_text(f"⭐ Premium — ₹{amount}/30 days\nAfter payment, send screenshot to support for activation.", reply_markup=main_menu_markup())
        await update.message.reply_text("Choose payment method:", reply_markup=InlineKeyboardMarkup(kb))
        return
    if text == "🆘 Support":
        await update.message.reply_text("Send your message and it will be forwarded to support/admin.", reply_markup=back_markup())
        context.user_data["awaiting"] = "support"
        return
    if text == "🔗 Contact":
        await update.message.reply_text(f"Contact: {SUPPORT_USERNAME}\nEmail: {BUSINESS_EMAIL}", reply_markup=main_menu_markup())
        return
    if text == "🔙 Back to Menu":
        context.user_data.pop("awaiting", None)
        await update.message.reply_text("Back to main menu.", reply_markup=main_menu_markup())
        return

    # if not recognized, check for awaiting state
    awaiting = context.user_data.get("awaiting")
    if awaiting:
        # handle based on awaiting
        if awaiting == "ai":
            await handle_ai_text(update, context)
            context.user_data.pop("awaiting", None)
            return
        if awaiting == "voice":
            await handle_voice_text(update, context)
            context.user_data.pop("awaiting", None)
            return
        if awaiting == "create":
            await handle_create_text(update, context)
            context.user_data.pop("awaiting", None)
            return
        if awaiting == "image":
            await handle_image_text(update, context)
            context.user_data.pop("awaiting", None)
            return
        if awaiting == "yt":
            await handle_yt_text(update, context)
            context.user_data.pop("awaiting", None)
            return
        if awaiting == "support":
            await handle_support_text(update, context)
            context.user_data.pop("awaiting", None)
            return

    # fallback to AI chat if plain text and not command
    if update.message.text and not update.message.text.startswith("/"):
        # treat as AI chat
        await handle_ai_text(update, context)
        return

    # else unknown
    await update.message.reply_text("Unknown option. Use the menu.", reply_markup=main_menu_markup())

# ---------- Per-feature implementations (used by router and commands) ----------
async def handle_ai_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt = update.message.text or ""
    if not prompt:
        await update.message.reply_text("Send text for AI.", reply_markup=main_menu_markup()); return
    await update.message.reply_text("⌛ Generating AI response...")  # quick ack
    reply = ai_chat(prompt, max_tokens=400)
    await update.message.reply_text(reply + "\n\n" + get_ad(), reply_markup=main_menu_markup())

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
        await update.message.reply_voice(voice=open(tmp.name, "rb"), caption=get_ad())
    except Exception as e:
        logger.exception("Voice error")
        await update.message.reply_text("⚠️ Voice generation failed: " + str(e))
    finally:
        try: os.remove(tmp.name)
        except: pass

async def handle_create_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topic = update.message.text or ""
    if not topic:
        await update.message.reply_text("Send topic/text to create short script and media.", reply_markup=main_menu_markup()); return
    await update.message.reply_text("🎬 Creating script + media... (may take time)")
    script = ai_chat(f"Write a short, engaging vertical video script for: {topic}", max_tokens=450)
    # create media
    media_path = create_video_from_text(script, out_path="ai_generated.mp4", duration=10)
    if not media_path:
        # fallback: show script and audio if created
        await update.message.reply_text("⚠️ Video creation not available. Here is the script:\n\n" + script + "\n\n" + get_ad(), reply_markup=main_menu_markup())
        return
    # send media (if mp3 send audio, if mp4 send video)
    try:
        if media_path.lower().endswith(".mp3"):
            await update.message.reply_audio(audio=open(media_path, "rb"), caption=script + "\n\n" + get_ad())
        else:
            # size limits apply - try sending; if too big, send message with note
            await update.message.reply_video(video=open(media_path, "rb"), caption=script + "\n\n" + get_ad())
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
    url = image_generate(prompt)
    if url:
        try:
            await update.message.reply_photo(photo=url, caption=f"Image for: {prompt}\n\n{get_ad()}", reply_markup=main_menu_markup())
        except Exception:
            # if direct URL fails, download and send file
            try:
                r = requests.get(url, stream=True, timeout=30)
                tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
                with open(tmp.name, "wb") as f:
                    for chunk in r.iter_content(1024):
                        f.write(chunk)
                await update.message.reply_photo(photo=open(tmp.name, "rb"), caption=f"Image for: {prompt}\n\n{get_ad()}", reply_markup=main_menu_markup())
            except Exception as e:
                logger.exception("image send fallback")
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
    await update.message.reply_text("📥 Downloading video — this may take a while and large videos may fail to send due to Telegram limits.")
    path = download_youtube(url)
    if not path:
        await update.message.reply_text("⚠️ Download failed or yt-dlp not available.", reply_markup=main_menu_markup()); return
    try:
        await update.message.reply_video(video=open(path, "rb"), caption=get_ad(), reply_markup=main_menu_markup())
    except Exception as e:
        logger.exception("yt send error")
        await update.message.reply_text(f"⚠️ Could not send video: {e}\nFile saved at: {path}", reply_markup=main_menu_markup())
    finally:
        try: os.remove(path)
        except: pass

async def handle_support_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    user = update.effective_user
    msg = f"📩 Support request from @{user.username or 'N/A'} (ID:{user.id}):\n\n{text}"
    try:
        await context.bot.send_message(chat_id=ADMIN_ID, text=msg)
        await update.message.reply_text("✅ Sent to admin.", reply_markup=main_menu_markup())
    except Exception as e:
        logger.exception("support forward error")
        await update.message.reply_text("⚠️ Could not forward to admin: " + str(e), reply_markup=main_menu_markup())

# ---------- Commands (direct) ----------
async def cmd_ai(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /ai <text>", reply_markup=main_menu_markup()); return
    prompt = " ".join(context.args)
    await update.message.reply_text("⌛ Generating AI response...")
    reply = ai_chat(prompt, max_tokens=400)
    await update.message.reply_text(reply + "\n\n" + get_ad(), reply_markup=main_menu_markup())

async def cmd_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /voice <text>", reply_markup=main_menu_markup()); return
    prompt = " ".join(context.args)
    await update.message.reply_text("🔉 Generating voice...")
    reply = ai_chat(prompt, max_tokens=300)
    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    try:
        tts = gTTS(text=reply, lang="hi")
        tts.save(tmp.name)
        await update.message.reply_voice(voice=open(tmp.name, "rb"), caption=get_ad(), reply_markup=main_menu_markup())
    except Exception as e:
        logger.exception("cmd_voice")
        await update.message.reply_text("⚠️ Voice error: " + str(e), reply_markup=main_menu_markup())
    finally:
        try: os.remove(tmp.name)
        except: pass

async def cmd_create(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /create <text>", reply_markup=main_menu_markup()); return
    topic = " ".join(context.args)
    update.message.reply_text("🎬 Creating script + media... (may take time)")
    script = ai_chat(f"Write a short, engaging vertical video script for: {topic}", max_tokens=450)
    out = create_video_from_text(script, out_path="ai_out.mp4", duration=10)
    if not out:
        await update.message.reply_text("⚠️ Could not create video. Here is the script:\n\n" + script, reply_markup=main_menu_markup()); return
    try:
        if out.endswith(".mp3"):
            await update.message.reply_audio(audio=open(out, "rb"), caption=script + "\n\n" + get_ad(), reply_markup=main_menu_markup())
        else:
            await update.message.reply_video(video=open(out, "rb"), caption=script + "\n\n" + get_ad(), reply_markup=main_menu_markup())
    except Exception as e:
        logger.exception("cmd_create")
        await update.message.reply_text("⚠️ Sending media failed: " + str(e), reply_markup=main_menu_markup())
    finally:
        try: os.remove(out)
        except: pass

async def cmd_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /image <description>", reply_markup=main_menu_markup()); return
    prompt = " ".join(context.args)
    await update.message.reply_text("🖼 Generating image...")
    url = image_generate(prompt)
    if url:
        await update.message.reply_photo(photo=url, caption=f"Generated for: {prompt}\n\n{get_ad()}", reply_markup=main_menu_markup())
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
        await update.message.reply_video(video=open(path, "rb"), caption=get_ad(), reply_markup=main_menu_markup())
    except Exception as e:
        logger.exception("cmd_yt")
        await update.message.reply_text("⚠️ Could not send video: " + str(e), reply_markup=main_menu_markup())
    finally:
        try: os.remove(path)
        except: pass

async def cmd_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    add_user(uid)
    if not UPI_ID:
        await update.message.reply_text("Payment not configured. Contact admin.", reply_markup=main_menu_markup()); return
    amount = "199"
    upi_uri = f"upi://pay?pa={UPI_ID}&pn={BUSINESS_NAME}&cu=INR&am={amount}"
    kb = [[InlineKeyboardButton("💳 Pay via UPI", url=upi_uri)]]
    await update.message.reply_text(f"⭐ Premium {amount}/30 days\nAfter payment, send screenshot to support/admin to activate.", reply_markup=main_menu_markup())
    await update.message.reply_text("Choose payment method:", reply_markup=InlineKeyboardMarkup(kb))

async def cmd_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Send your message and it will be forwarded to support/admin.", reply_markup=back_markup())
    context.user_data["awaiting"] = "support"

# ---------- Admin Commands ----------
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Unauthorized.")
        return
    kb = [
        [InlineKeyboardButton("👥 Users Stats", callback_data="admin_stats"), InlineKeyboardButton("💎 Manage Premium", callback_data="admin_premium")],
        [InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast"), InlineKeyboardButton("🔙 Back", callback_data="admin_back")]
    ]
    await update.message.reply_text("Admin Panel:", reply_markup=InlineKeyboardMarkup(kb))

# Callback query handler for admin buttons
from telegram.ext import CallbackQueryHandler
async def admin_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    if data == "admin_stats":
        total, prem = get_stats()
        await q.edit_message_text(f"Users: {total}\nPremium: {prem}")
    elif data == "admin_premium":
        await q.edit_message_text("Use /addpremium <user_id> <days> or /removepremium <user_id>")
    elif data == "admin_broadcast":
        await q.edit_message_text("Use /broadcast <message>")
    else:
        await q.edit_message_text("Back to admin menu.")

async def addpremium_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /addpremium <user_id> <days>")
        return
    uid = int(context.args[0]); days = int(context.args[1])
    set_premium(uid, days)
    await update.message.reply_text(f"✅ Premium set for {uid} for {days} days.")

async def removepremium_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("Usage: /removepremium <user_id>")
        return
    uid = int(context.args[0])
    remove_premium(uid)
    await update.message.reply_text(f"❌ Removed premium for {uid}.")

async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    msg = " ".join(context.args)
    users = get_all_users()
    sent = 0
    for u in users:
        try:
            await context.bot.send_message(u, f"📢 {msg}")
            sent += 1
        except:
            pass
    await update.message.reply_text(f"Broadcast sent to {sent} users.")

# ---------- Daily job for expiry reminders ----------
async def daily_job(context: ContextTypes.DEFAULT_TYPE):
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
        # reminder 1 day before
        if 0 <= (exp_dt - now).days <= 1:
            try:
                await context.bot.send_message(uid, "⏰ Reminder: Your Premium expires in ~1 day. Renew via /premium")
            except:
                pass
        # if expired, remove and notify
        if now > exp_dt:
            remove_premium(uid)
            try:
                await context.bot.send_message(uid, "⚠️ Your Premium has expired.")
            except:
                pass
    conn.close()

# ---------- Setup & run ----------
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    # Command handlers
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("ping", ping_handler))

    app.add_handler(CommandHandler("ai", cmd_ai))
    app.add_handler(CommandHandler("voice", cmd_voice))
    app.add_handler(CommandHandler("create", cmd_create))
    app.add_handler(CommandHandler("image", cmd_image))
    app.add_handler(CommandHandler("yt", cmd_yt))
    app.add_handler(CommandHandler("premium", cmd_premium))
    app.add_handler(CommandHandler("support", cmd_support))

    # admin
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CallbackQueryHandler(admin_cb))
    app.add_handler(CommandHandler("addpremium", addpremium_cmd))
    app.add_handler(CommandHandler("removepremium", removepremium_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))

    # menu router: handle persistent keyboard presses & general text fallback
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_router))

    # jobs
    # run daily at 09:00 UTC
    app.job_queue.run_daily(daily_job, time=time(hour=9, minute=0, second=0))

    # run webhook
    logger.info("Starting webhook on port %s", PORT)
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=BOT_TOKEN,
        webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}"
    )

if __name__ == "__main__":
    main()
