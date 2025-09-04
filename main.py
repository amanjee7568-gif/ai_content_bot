# ===============================
#  AI Content Creator Telegram Bot (Advanced)
#  Features:
#   ✅ Free/Premium Mode
#   ✅ AI Script + Auto Video Generation
#   ✅ Ads System
#   ✅ Admin Commands (/stats, /broadcast, /addpremium)
#   ✅ Payment Link (UPI)
#   ✅ Support Info
#   ✅ Render Deploy Safe (MoviePy fallback)
# ===============================

import os
import random
import sqlite3
import openai
from gtts import gTTS
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes
from datetime import datetime, timedelta

# -------------------------------
# Environment Variables
# -------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID", "123456789"))
BUSINESS_NAME = os.getenv("BUSINESS_NAME", "Demo Agency")
BUSINESS_EMAIL = os.getenv("BUSINESS_EMAIL", "demo@gmail.com")
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "@support")
UPI_ID = os.getenv("UPI_ID", "demo@upi")

openai.api_key = OPENAI_API_KEY

# -------------------------------
# Flask for Render keep-alive
# -------------------------------
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running on Render 🚀"

# -------------------------------
# Database (SQLite)
# -------------------------------
DB_NAME = "users.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        is_premium INTEGER DEFAULT 0,
        expiry_date TEXT
    )
    """)
    conn.commit()
    conn.close()

def add_user(user_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()

def set_premium(user_id: int, days: int = 30):
    expiry = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET is_premium=?, expiry_date=? WHERE user_id=?", (1, expiry, user_id))
    conn.commit()
    conn.close()

def remove_premium(user_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET is_premium=0, expiry_date=NULL WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()

def check_premium(user_id: int) -> bool:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT is_premium, expiry_date FROM users WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    conn.close()

    if not row:
        return False

    is_premium, expiry_date = row
    if not is_premium:
        return False

    if expiry_date:
        expiry = datetime.strptime(expiry_date, "%Y-%m-%d %H:%M:%S")
        if datetime.now() > expiry:
            remove_premium(user_id)
            return False

    return True

# -------------------------------
# Ads System
# -------------------------------
ads_list = [
    "🔥 Sponsored: Best AI Tools - www.example1.com",
    "💡 Learn & Earn Online - www.example2.com",
    "📢 Join Free Money Making Group - www.example3.com"
]

def get_random_ad():
    return random.choice(ads_list)

# -------------------------------
# Video Generator (with fallback)
# -------------------------------
try:
    from moviepy.editor import ColorClip, CompositeVideoClip, AudioFileClip, TextClip

    def create_video(text, filename="output.mp4"):
        tts = gTTS(text=text, lang="en")
        audio_path = "audio.mp3"
        tts.save(audio_path)

        clip = ColorClip(size=(720, 480), color=(30, 30, 30), duration=10)
        txt_clip = TextClip(text, fontsize=32, color="white", size=(700, None), method="caption")
        txt_clip = txt_clip.set_duration(10).set_position("center")

        video = CompositeVideoClip([clip, txt_clip])
        video = video.set_audio(AudioFileClip(audio_path))
        video.write_videofile(filename, fps=24, codec="libx264", audio_codec="aac")

        return filename

except Exception as e:
    print("⚠️ MoviePy not working, fallback to text-only mode:", e)
    def create_video(text, filename="output.mp4"):
        # Fallback: save audio only
        tts = gTTS(text=text, lang="en")
        tts.save("audio.mp3")
        return None  # No video

# -------------------------------
# Telegram Bot Handlers
# -------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    add_user(user_id)
    text = f"""
🤖 Welcome to *AI Content Creator Bot* 🎬

👉 /create Your text
👉 /premium to unlock premium features
👉 /support for help

🧾 Business: {BUSINESS_NAME}
📧 Email: {BUSINESS_EMAIL}
"""
    await update.message.reply_text(text, parse_mode="Markdown")
    await update.message.reply_text(get_random_ad())

async def create(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    add_user(user_id)
    is_premium = check_premium(user_id)

    if not context.args:
        await update.message.reply_text("✍️ Example:\n`/create My travel vlog intro`", parse_mode="Markdown")
        return

    user_input = " ".join(context.args)
    await update.message.reply_text("⏳ Generating video... Please wait...")

    try:
        tokens = 300 if is_premium else 120
        response = openai.Completion.create(
            engine="text-davinci-003",
            prompt=f"Create a short video script for: {user_input}",
            max_tokens=tokens
        )
        script = response.choices[0].text.strip()

        video_path = create_video(script, "ai_video.mp4")
        if video_path:
            await update.message.reply_video(video=open(video_path, "rb"), caption=f"🎬 {script}\n\n{get_random_ad()}")
        else:
            await update.message.reply_text(f"🎬 Script:\n{script}\n\n(Audio saved only)\n{get_random_ad()}")

    except Exception as e:
        await update.message.reply_text(f"⚠️ Error: {e}")

async def premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    add_user(user_id)
    keyboard = [[InlineKeyboardButton("💳 Pay via UPI", url=f"upi://pay?pa={UPI_ID}&pn={BUSINESS_NAME}&cu=INR&am=199")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = """
🌟 *Premium Features* 🌟

✅ Generate longer videos
✅ High Quality export
✅ No Watermark

💰 Price: 199 INR / 30 days
"""
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=reply_markup)
    await update.message.reply_text(get_random_ad())

async def support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"📞 Contact Support: {SUPPORT_USERNAME}")

# -------------------------------
# Admin Commands
# -------------------------------
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM users")
    total = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM users WHERE is_premium=1")
    premium_count = cursor.fetchone()[0]
    conn.close()
    await update.message.reply_text(f"📊 Stats:\nUsers: {total}\nPremium: {premium_count}")

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    msg = " ".join(context.args)
    if not msg:
        await update.message.reply_text("Usage: /broadcast Your message")
        return
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users")
    users = cursor.fetchall()
    conn.close()
    for (uid,) in users:
        try:
            await context.bot.send_message(chat_id=uid, text=f"📢 {msg}")
        except:
            pass
    await update.message.reply_text("✅ Broadcast sent.")

async def addpremium_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /addpremium user_id days")
        return
    uid = int(context.args[0])
    days = int(context.args[1])
    set_premium(uid, days)
    await update.message.reply_text(f"✅ User {uid} upgraded to premium for {days} days.")

# -------------------------------
# Main
# -------------------------------
def main():
    init_db()
    app_bot = Application.builder().token(BOT_TOKEN).build()

    # User Commands
    app_bot.add_handler(CommandHandler("start", start))
    app_bot.add_handler(CommandHandler("create", create))
    app_bot.add_handler(CommandHandler("premium", premium))
    app_bot.add_handler(CommandHandler("support", support))

    # Admin Commands
    app_bot.add_handler(CommandHandler("stats", stats))
    app_bot.add_handler(CommandHandler("broadcast", broadcast))
    app_bot.add_handler(CommandHandler("addpremium", addpremium_cmd))

    print("🤖 Bot is running...")
    app_bot.run_polling()

if __name__ == "__main__":
    from threading import Thread
    Thread(target=lambda: app.run(host="0.0.0.0", port=5000)).start()
    main()
