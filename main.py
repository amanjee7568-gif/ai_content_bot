import os
import logging
import requests
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from openai import OpenAI
from dotenv import load_dotenv

# Load .env
load_dotenv()

# Credentials
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
BUSINESS_NAME = os.getenv("BUSINESS_NAME", "My Business")
CASHFREE_APP_ID = os.getenv("CASHFREE_APP_ID")
CASHFREE_SECRET_KEY = os.getenv("CASHFREE_SECRET_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")

# Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# OpenAI Client
client = OpenAI(api_key=OPENAI_API_KEY)

# ---------------- HANDLERS ---------------- #

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"👋 Welcome to {BUSINESS_NAME} Bot!\n\n"
        f"📌 Commands:\n"
        f"/ai <text> → AI से चैट\n"
        f"/yt <url> → YouTube वीडियो डाउनलोड\n"
        f"/pay <amount> → पेमेंट लिंक बनाएं\n"
        f"/support <msg> → सपोर्ट टीम से संपर्क\n"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🆘 Help Menu:\n"
        "/ai <text>\n"
        "/yt <youtube_url>\n"
        "/pay <amount>\n"
        "/support <your message>"
    )

# ---------- AI CHAT ----------
async def ai_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /ai <your question>")
        return

    user_msg = " ".join(context.args)
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": user_msg}],
        )
        reply = response.choices[0].message.content
        await update.message.reply_text(reply)
    except Exception as e:
        await update.message.reply_text("⚠️ AI Error: " + str(e))

# ---------- YOUTUBE DOWNLOAD ----------
async def yt_download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /yt <youtube_url>")
        return

    url = context.args[0]
    try:
        api = f"https://yt1sapi.vercel.app/api?url={url}"
        res = requests.get(api).json()
        video_url = res.get("download_url")

        if video_url:
            await update.message.reply_video(video_url, caption="🎬 Here's your video")
        else:
            await update.message.reply_text("⚠️ Download failed, try another link.")
    except Exception as e:
        await update.message.reply_text("⚠️ Error: " + str(e))

# ---------- PAYMENT ----------
async def create_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /pay <amount>")
        return

    amount = context.args[0]
    try:
        payment_url = f"https://payments.cashfree.com/{CASHFREE_APP_ID}?amount={amount}"
        await update.message.reply_text(f"💳 Pay here securely: {payment_url}")
    except Exception as e:
        await update.message.reply_text("⚠️ Payment error: " + str(e))

# ---------- SUPPORT ----------
async def support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /support <your message>")
        return

    user = update.message.from_user
    user_msg = " ".join(context.args)

    msg = (
        f"📩 Support Request:\n"
        f"👤 User: @{user.username or 'N/A'}\n"
        f"🆔 ID: {user.id}\n"
        f"💬 Message: {user_msg}"
    )

    try:
        await context.bot.send_message(ADMIN_ID, msg)
        await update.message.reply_text("✅ आपका मैसेज एडमिन तक भेज दिया गया है।")
    except Exception as e:
        await update.message.reply_text("⚠️ Support error: " + str(e))

# ---------- FALLBACK ----------
async def fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❓ Unknown command. Type /help for commands.")

# ---------------- MAIN APP ---------------- #
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("ai", ai_chat))
    app.add_handler(CommandHandler("yt", yt_download))
    app.add_handler(CommandHandler("pay", create_payment))
    app.add_handler(CommandHandler("support", support))

    # Fallback
    app.add_handler(MessageHandler(filters.COMMAND, fallback))

    # Render Webhook
    port = int(os.environ.get("PORT", 10000))
    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=BOT_TOKEN,
        webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}",
    )

if __name__ == "__main__":
    main()
