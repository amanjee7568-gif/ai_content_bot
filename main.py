import os
import openai
import tempfile
import yt_dlp
from gtts import gTTS
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# ENV variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL")
PORT = int(os.getenv("PORT", "10000"))

openai.api_key = OPENAI_API_KEY

# -------------------------------
# AI Chat (Text)
# -------------------------------
async def ask_ai(prompt: str) -> str:
    try:
        response = openai.ChatCompletion.create(
            model="gpt-4o-mini",  # या gpt-3.5-turbo
            messages=[{"role": "user", "content": prompt}],
        )
        return response["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"❌ Error: {e}"

# -------------------------------
# Handlers
# -------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 हेलो! मैं AI Super Bot हूँ.\n\n"
        "मुझसे टेक्स्ट, वॉइस, इमेज या यूट्यूब वीडियो मांग सकते हो।\n"
        "Commands:\n"
        "/ai <msg>\n"
        "/voice <msg>\n"
        "/image <desc>\n"
        "/yt <url>"
    )

# --- AI Text Chat ---
async def ai_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args)
    if not query:
        await update.message.reply_text("⚠️ Usage: /ai <your message>")
        return
    reply = await ask_ai(query)
    await update.message.reply_text(reply)

# --- AI Voice Reply ---
async def voice_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args)
    if not query:
        await update.message.reply_text("⚠️ Usage: /voice <your message>")
        return
    reply = await ask_ai(query)
    # convert to audio
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp_file:
        tts = gTTS(reply, lang="hi")
        tts.save(tmp_file.name)
        await update.message.reply_voice(voice=open(tmp_file.name, "rb"))
        os.remove(tmp_file.name)

# --- AI Image Generator ---
async def image_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt = " ".join(context.args)
    if not prompt:
        await update.message.reply_text("⚠️ Usage: /image <description>")
        return
    try:
        response = openai.Image.create(prompt=prompt, size="512x512")
        image_url = response["data"][0]["url"]
        await update.message.reply_photo(photo=image_url, caption=f"🖼️ Generated for: {prompt}")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

# --- YouTube Downloader ---
async def yt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = " ".join(context.args)
    if not url:
        await update.message.reply_text("⚠️ Usage: /yt <youtube_url>")
        return
    await update.message.reply_text("📥 Downloading video... please wait.")

    try:
        ydl_opts = {
            "format": "mp4",
            "outtmpl": "/tmp/video.%(ext)s",
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            file_path = ydl.prepare_filename(info)

        await update.message.reply_video(video=open(file_path, "rb"))
        os.remove(file_path)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

# --- General Text Messages ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_msg = update.message.text
    reply = await ask_ai(user_msg)
    await update.message.reply_text(reply)

# -------------------------------
# Main
# -------------------------------
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ai", ai_command))
    app.add_handler(CommandHandler("voice", voice_command))
    app.add_handler(CommandHandler("image", image_command))
    app.add_handler(CommandHandler("yt", yt_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Webhook mode for Render
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=f"{RENDER_URL}/{BOT_TOKEN}"
    )

if __name__ == "__main__":
    main()
