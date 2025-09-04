# Advanced AI Content Creator Telegram Bot
# Features: Premium+expiry, Ads, Multi-format (video/blog/podcast/social),
# admin panel, Cashfree webhook, free daily limits, expiry reminders.

import os
import random
import sqlite3
from datetime import datetime, timedelta
from threading import Thread

from flask import Flask, request, jsonify

import openai
from gtts import gTTS
from moviepy.editor import ColorClip, CompositeVideoClip, AudioFileClip  # no TextClip (fallback safe)
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, ContextTypes, MessageHandler, filters
)

# --------------------- CONFIG ---------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)

BUSINESS_NAME = os.getenv("BUSINESS_NAME", "Your Business")
BUSINESS_EMAIL = os.getenv("BUSINESS_EMAIL", "support@example.com")
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "@support_handle")
UPI_ID = os.getenv("UPI_ID", "yourupi@upi")

CASHFREE_WEBHOOK_SECRET = os.getenv("CASHFREE_WEBHOOK_SECRET", "")  # optional verify
PORT = int(os.getenv("PORT", "5000"))

openai.api_key = OPENAI_API_KEY

# --------------------- FLASK (for payment webhook) ---------------------
app = Flask(__name__)

# --------------------- DATABASE ---------------------
DB = "bot.db"

def db_conn():
    return sqlite3.connect(DB, check_same_thread=False)

def init_db():
    c = db_conn()
    cur = c.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY,
        is_premium INTEGER DEFAULT 0,
        expiry TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS usage(
        user_id INTEGER,
        date TEXT,
        videos INTEGER DEFAULT 0,
        blogs INTEGER DEFAULT 0,
        podcasts INTEGER DEFAULT 0,
        posts INTEGER DEFAULT 0,
        PRIMARY KEY(user_id, date)
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS payments(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        amount INTEGER,
        gateway TEXT,
        status TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.commit(); c.close()

def add_user(user_id: int):
    c = db_conn(); cur = c.cursor()
    cur.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (user_id,))
    c.commit(); c.close()

def set_premium(user_id: int, days: int = 30):
    expiry = (datetime.utcnow() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    c = db_conn(); cur = c.cursor()
    cur.execute("UPDATE users SET is_premium=1, expiry=? WHERE user_id=?", (expiry, user_id))
    c.commit(); c.close()

def remove_premium(user_id: int):
    c = db_conn(); cur = c.cursor()
    cur.execute("UPDATE users SET is_premium=0, expiry=NULL WHERE user_id=?", (user_id,))
    c.commit(); c.close()

def is_premium_active(user_id: int) -> bool:
    c = db_conn(); cur = c.cursor()
    cur.execute("SELECT is_premium, expiry FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone(); c.close()
    if not row: return False
    active, expiry = row
    if not active: return False
    if not expiry: return False
    try:
        if datetime.utcnow() > datetime.strptime(expiry, "%Y-%m-%d %H:%M:%S"):
            remove_premium(user_id)
            return False
    except:  # bad date format
        remove_premium(user_id)
        return False
    return True

def premium_expiry_str(user_id: int) -> str:
    c = db_conn(); cur = c.cursor()
    cur.execute("SELECT expiry FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone(); c.close()
    return row[0] if row and row[0] else "—"

def usage_get(user_id: int):
    today = datetime.utcnow().strftime("%Y-%m-%d")
    c = db_conn(); cur = c.cursor()
    cur.execute("INSERT OR IGNORE INTO usage(user_id, date) VALUES(?,?)", (user_id, today))
    c.commit()
    cur.execute("SELECT videos, blogs, podcasts, posts FROM usage WHERE user_id=? AND date=?", (user_id, today))
    row = cur.fetchone()
    c.close()
    return {"videos": row[0], "blogs": row[1], "podcasts": row[2], "posts": row[3]}

def usage_inc(user_id: int, key: str):
    today = datetime.utcnow().strftime("%Y-%m-%d")
    c = db_conn(); cur = c.cursor()
    cur.execute(f"UPDATE usage SET {key} = COALESCE({key},0)+1 WHERE user_id=? AND date=?", (user_id, today))
    c.commit(); c.close()

# --------------------- ADS ---------------------
ADS = [
    "🔥 Sponsored: Boost your business with AI!",
    "💡 Learn & Earn Online — free webinar today!",
    "📢 Promote here — DM @your_ad_handle"
]
def ad(): return random.choice(ADS)

# --------------------- CONTENT HELPERS ---------------------
FREE_LIMITS = dict(videos=3, blogs=2, podcasts=1, posts=5)  # per day (free users)

def check_and_consume_quota(user_id: int, key: str, is_premium: bool) -> (bool, str):
    if is_premium:  # unlimited (or set higher soft limits)
        return True, ""
    current = usage_get(user_id)
    if current[key] >= FREE_LIMITS[key]:
        return False, f"❗ Free limit reached for today ({key}). Upgrade: /premium"
    usage_inc(user_id, key)
    return True, ""

async def gen_script(prompt: str, tokens: int = 250) -> str:
    # Using legacy Completion API per requirements pin
    resp = openai.Completion.create(
        engine="text-davinci-003",
        prompt=prompt,
        max_tokens=tokens,
        temperature=0.8
    )
    return resp.choices[0].text.strip()

def create_video_from_text(text: str, out_path: str, watermark: str | None):
    # TTS
    tts = gTTS(text=text, lang="en")
    voice_path = "voice.mp3"
    tts.save(voice_path)

    # Use audio duration to size video
    audio = AudioFileClip(voice_path)
    duration = max(8, int(audio.duration) + 2)

    # Solid background clip
    bg = ColorClip(size=(720, 1280), color=(20, 20, 20), duration=duration)

    # (Safe fallback) Without TextClip to avoid ImageMagick dependency on Render.
    video = bg.set_audio(audio)

    # Optional lightweight watermark by extending last second with silent overlay? (skip heavy ops)
    # Keep simple to maximize compatibility.

    video.write_videofile(out_path, fps=24, audio_codec="aac", codec="libx264")
    return out_path

# --------------------- TELEGRAM COMMANDS ---------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    add_user(uid)
    kb = [
        [InlineKeyboardButton("🎬 Create Video", callback_data="menu_video"),
         InlineKeyboardButton("📝 Blog Post", callback_data="menu_blog")],
        [InlineKeyboardButton("🎙️ Podcast Audio", callback_data="menu_podcast"),
         InlineKeyboardButton("📣 Social Post", callback_data="menu_social")],
        [InlineKeyboardButton("⭐ Premium", callback_data="menu_premium"),
         InlineKeyboardButton("🆘 Support", callback_data="menu_support")]
    ]
    await update.message.reply_text(
        f"🤖 *AI Content Creator Bot*\n"
        f"Welcome!\n\n"
        f"• Free users get limited daily usage.\n"
        f"• Premium = longer videos, better quality, no limits.\n\n"
        f"Premium status: {'ACTIVE ✅ (till ' + premium_expiry_str(uid) + ')' if is_premium_active(uid) else 'INACTIVE ❌'}\n"
        f"Commands: /video /blog /podcast /social /premium /support\n",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    await update.message.reply_text(ad())

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Commands:\n"
        "/video <topic>\n/blog <topic>\n/podcast <topic>\n/social <topic>\n"
        "/premium\n/support"
    )

# ---- Generators
async def video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; add_user(uid)
    is_prem = is_premium_active(uid)
    if not context.args:
        await update.message.reply_text("Usage: `/video your topic`", parse_mode="Markdown"); return
    ok, msg = check_and_consume_quota(uid, "videos", is_prem)
    if not ok: await update.message.reply_text(msg); return

    topic = " ".join(context.args)
    await update.message.reply_text("🎥 Generating script + video... please wait…")

    try:
        tokens = 500 if is_prem else 220
        script = await gen_script(
            f"Write a short, engaging vertical video narration for TikTok/Reels on: {topic}. "
            f"Keep it concise and high-energy. End with a call-to-action.",
            tokens=tokens
        )
        out = "ai_video.mp4"
        create_video_from_text(script, out, watermark=None if is_prem else "@YourBot")
        cap = ("🎬 *Your AI Video is ready!*\n\n" +
               ("" if is_prem else "Made with @YourBot (free)\n") +
               f"Script:\n{script}\n\n" + ad())
        await update.message.reply_video(video=open(out, "rb"), caption=cap, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Video error: {e}")

async def blog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; add_user(uid)
    is_prem = is_premium_active(uid)
    if not context.args:
        await update.message.reply_text("Usage: `/blog your topic`", parse_mode="Markdown"); return
    ok, msg = check_and_consume_quota(uid, "blogs", is_prem)
    if not ok: await update.message.reply_text(msg); return

    topic = " ".join(context.args)
    await update.message.reply_text("📝 Writing blog…")
    try:
        tokens = 900 if is_prem else 400
        draft = await gen_script(
            f"Write an SEO-friendly blog post on: {topic}. "
            f"Include an engaging intro, 3-5 subheadings, bullet points, and a conclusion.",
            tokens=tokens
        )
        await update.message.reply_text(f"📝 *Blog Draft:*\n{draft}\n\n{ad()}", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Blog error: {e}")

async def podcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; add_user(uid)
    is_prem = is_premium_active(uid)
    if not context.args:
        await update.message.reply_text("Usage: `/podcast your topic`", parse_mode="Markdown"); return
    ok, msg = check_and_consume_quota(uid, "podcasts", is_prem)
    if not ok: await update.message.reply_text(msg); return

    topic = " ".join(context.args)
    await update.message.reply_text("🎙️ Producing podcast audio…")
    try:
        script = await gen_script(
            f"Write a 2-4 minute podcast monologue on: {topic}. Conversational, insightful, friendly tone.",
            tokens=500 if is_prem else 280
        )
        tts = gTTS(text=script, lang="en"); path = "podcast.mp3"; tts.save(path)
        cap = "🎧 Your AI podcast is ready!\n\n" + ("" if is_prem else "Made with @YourBot (free)\n") + ad()
        await update.message.reply_audio(audio=open(path, "rb"), caption=cap)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Podcast error: {e}")

async def social(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; add_user(uid)
    is_prem = is_premium_active(uid)
    if not context.args:
        await update.message.reply_text("Usage: `/social your topic`", parse_mode="Markdown"); return
    ok, msg = check_and_consume_quota(uid, "posts", is_prem)
    if not ok: await update.message.reply_text(msg); return

    topic = " ".join(context.args)
    await update.message.reply_text("📣 Crafting social post…")
    try:
        post = await gen_script(
            f"Write a catchy multi-platform social media post (Twitter/Instagram/LinkedIn) for: {topic}. "
            f"Provide 3 variants with hashtags.",
            tokens=220 if is_prem else 140
        )
        await update.message.reply_text(f"📣 *Social Post Ideas:*\n{post}\n\n{ad()}", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Social error: {e}")

# ---- Premium & Support
async def premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; add_user(uid)
    kb = [[InlineKeyboardButton("💳 Pay via UPI (₹199/30d)",
                                url=f"upi://pay?pa={UPI_ID}&pn={BUSINESS_NAME}&cu=INR&am=199")]]
    text = ("⭐ *Premium Benefits*\n"
            "• Longer & better videos\n• Higher token limits\n• Daily limits removed\n• No promotional footer\n\n"
            "Price: ₹199 / 30 days\n"
            "_After payment, premium will be activated automatically (or DM your txn screenshot to support if delayed)._")
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
    await update.message.reply_text(ad())

async def support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🛠 Support: {SUPPORT_USERNAME}\nEmail: {BUSINESS_EMAIL}")

# --------------------- ADMIN ---------------------
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    c = db_conn(); cur = c.cursor()
    cur.execute("SELECT COUNT(*), SUM(is_premium) FROM users")
    total, prem = cur.fetchone()
    cur.execute("SELECT COUNT(*) FROM usage WHERE date=?", (datetime.utcnow().strftime("%Y-%m-%d"),))
    today_rows = cur.fetchone()[0]
    c.close()
    await update.message.reply_text(
        f"👥 Users: {total or 0}\n⭐ Premium: {prem or 0}\n📊 Usage rows today: {today_rows}"
    )

async def addpremium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /addpremium <user_id> <days>"); return
    uid = int(context.args[0]); days = int(context.args[1])
    set_premium(uid, days)
    await update.message.reply_text(f"✅ Premium set for {uid} ({days} days).")

async def removepremium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    if not context.args:
        await update.message.reply_text("Usage: /removepremium <user_id>"); return
    uid = int(context.args[0]); remove_premium(uid)
    await update.message.reply_text(f"❌ Premium removed for {uid}.")

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    if not context.args:
        await update.message.reply_text("Usage: /broadcast <message>"); return
    msg = " ".join(context.args)
    c = db_conn(); cur = c.cursor(); cur.execute("SELECT user_id FROM users"); rows = cur.fetchall(); c.close()
    sent = 0
    for (uid,) in rows:
        try:
            await context.bot.send_message(uid, f"📢 {msg}")
            sent += 1
        except: pass
    await update.message.reply_text(f"📨 Broadcast sent to {sent} users.")

# --------------------- CASHFREE WEBHOOK ---------------------
@app.route("/webhook", methods=["POST"])
def cashfree_webhook():
    data = request.get_json(silent=True) or {}
    try:
        status = data.get("data", {}).get("order", {}).get("order_status")
        customer_id = data.get("data", {}).get("customer_details", {}).get("customer_id")
        amount = int(float(data.get("data", {}).get("order", {}).get("order_amount", 0)))

        if status == "PAID" and customer_id:
            tg_user_id = int(customer_id)
            set_premium(tg_user_id, 30)
            c = db_conn(); cur = c.cursor()
            cur.execute("INSERT INTO payments(user_id, amount, gateway, status) VALUES(?,?,?,?)",
                        (tg_user_id, amount, "cashfree", "PAID"))
            c.commit(); c.close()
            return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True})

# --------------------- SCHEDULER: daily expiry check & reminders ---------------------
async def daily_job(context: ContextTypes.DEFAULT_TYPE):
    c = db_conn(); cur = c.cursor()
    cur.execute("SELECT user_id, expiry FROM users WHERE is_premium=1 AND expiry IS NOT NULL")
    rows = cur.fetchall(); c.close()
    now = datetime.utcnow()
    for uid, exp in rows:
        try:
            expiry = datetime.strptime(exp, "%Y-%m-%d %H:%M:%S")
        except:
            continue
        # 1-day reminder
        if 0 <= (expiry - now).days <= 1:
            try:
                await context.bot.send_message(uid,
                    "⏰ Reminder: Your Premium expires in ~1 day. Renew now via /premium to keep benefits.")
            except: pass
        # auto-remove after expiry
        if now > expiry:
            remove_premium(uid)
            try:
                await context.bot.send_message(uid,
                    "⚠️ Your Premium has expired. Renew anytime via /premium.")
            except: pass

# --------------------- MAIN ---------------------
def run_flask():
    app.run(host="0.0.0.0", port=PORT)

def main():
    init_db()
    application = Application.builder().token(BOT_TOKEN).build()

    # Commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("video", video))
    application.add_handler(CommandHandler("blog", blog))
    application.add_handler(CommandHandler("podcast", podcast))
    application.add_handler(CommandHandler("social", social))
    application.add_handler(CommandHandler("premium", premium))
    application.add_handler(CommandHandler("support", support))

    # Admin
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("broadcast", broadcast))
    application.add_handler(CommandHandler("addpremium", addpremium))
    application.add_handler(CommandHandler("removepremium", removepremium))

    # Jobs: run daily at 09:00 UTC
    application.job_queue.run_daily(daily_job, time=datetime.utcnow().time().replace(hour=9, minute=0, second=0, microsecond=0))

    # Start Flask (webhook) and Bot polling together
    Thread(target=run_flask, daemon=True).start()
    print("🤖 Bot is running…")
    application.run_polling()

if __name__ == "__main__":
    main()
