import os
import json
import logging
import sqlite3
from datetime import datetime, timezone
from flask import Flask, request
from apscheduler.schedulers.background import BackgroundScheduler
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from openai import OpenAI
import requests

# ---------------- LOGGING ----------------
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------------- ENV ----------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
UPI_ID = os.getenv("UPI_ID", "demo@upi")
CASHFREE_API_KEY = os.getenv("CASHFREE_API_KEY", "test_key")
PORT = int(os.getenv("PORT", 10000))

# ---------------- DB ----------------
DB_FILE = "bot.db"

def get_conn():
    return sqlite3.connect(DB_FILE, timeout=30, check_same_thread=False)

def init_db():
    conn = get_conn(); cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY,
        name TEXT,
        username TEXT,
        created_at TEXT
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        event TEXT,
        amount REAL,
        metadata TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.commit(); conn.close()

# ---------------- UTILS ----------------
SIGNUP_BONUS = 10
DAILY_REWARD = 5

def credit_ledger(user_id, amount, event, metadata=None):
    conn = get_conn(); cur = conn.cursor()
    cur.execute(
        "INSERT INTO ledger (user_id, event, amount, metadata) VALUES (?,?,?,?)",
        (user_id, event, amount, json.dumps(metadata or {}))
    )
    conn.commit(); conn.close()

def add_user(user_id, name, username):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO users (id, name, username, created_at) VALUES (?,?,?,?)",
                (user_id, name, username, datetime.now(timezone.utc).isoformat()))
    conn.commit(); conn.close()
    credit_ledger(user_id, SIGNUP_BONUS, "signup_bonus", {"bonus": SIGNUP_BONUS})

# ---------------- OPENAI ----------------
client = OpenAI(api_key=OPENAI_API_KEY)
logger.info("OpenAI client ready")

def ai_answer(prompt: str) -> str:
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"AI error: {e}")
        return "⚠️ AI error occurred."

# ---------------- PAYMENTS ----------------
def generate_upi_link(amount: float, note="Payment"):
    return f"upi://pay?pa={UPI_ID}&pn=Bot&am={amount}&cu=INR&tn={note}"

def create_cashfree_link(amount: float, order_id: str):
    return f"https://cashfree.com/pay/{order_id}?amount={amount}&key={CASHFREE_API_KEY}"

# ---------------- SELF HEAL ----------------
def self_heal(fn):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            return await fn(update, context)
        except Exception as e:
            suggestion = ai_answer(f"Error in {fn.__name__}: {e}")
            logger.error(f"Self-heal suggestion: {suggestion}")
            conn = get_conn(); cur = conn.cursor()
            cur.execute(
                "INSERT INTO ledger (user_id,event,amount,metadata) VALUES (?,?,?,?)",
                (0,"self_heal",0.0,json.dumps({"fn":fn.__name__,"err":str(e),"suggestion":suggestion}))
            )
            conn.commit(); conn.close()
            await update.message.reply_text("⚠️ Error हुआ, auto-fix चालू है…")
    return wrapper

# ---------------- HANDLERS ----------------
@self_heal
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    add_user(user.id, user.first_name or "", user.username or "")
    await update.message.reply_text(f"👋 Welcome {user.first_name}! Signup bonus credited 🎉")

@self_heal
async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 मैं ChatGPT जैसा AI बॉट हूँ। कुछ भी पूछो!")

@self_heal
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.message.text
    ans = ai_answer(q)
    await update.message.reply_text(ans)

@self_heal
async def pay_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args: 
        await update.message.reply_text("Usage: /pay <amount>")
        return
    amt = float(args[0])
    upi = generate_upi_link(amt)
    cash = create_cashfree_link(amt, f"order{datetime.now().timestamp()}")
    await update.message.reply_text(f"💰 Pay via:\n\nUPI: {upi}\n\nCashfree: {cash}")

# ---------------- REWARDS ----------------
def daily_reward():
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT id FROM users")
    for (uid,) in cur.fetchall():
        credit_ledger(uid, DAILY_REWARD, "daily_reward")
    conn.close()
    logger.info("Daily rewards credited")

def monthly_reset():
    conn = get_conn(); cur = conn.cursor()
    cur.execute("DELETE FROM ledger")
    conn.commit(); conn.close()
    logger.info("Monthly ledger reset")

# ---------------- FLASK ----------------
app = Flask(__name__)
application: Application = None

@app.route("/", methods=["GET","POST"])
def webhook():
    if request.method == "POST":
        update = Update.de_json(request.get_json(force=True), application.bot)
        application.update_queue.put_nowait(update)
        return "ok"
    return "Bot running!"

# ---------------- MAIN ----------------
def main():
    global application
    init_db()

    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start_handler))
    application.add_handler(CommandHandler("help", help_handler))
    application.add_handler(CommandHandler("pay", pay_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    # Scheduler
    sched = BackgroundScheduler()
    sched.add_job(daily_reward, "interval", days=1)
    sched.add_job(monthly_reset, "interval", days=30)
    sched.start()

    # Webhook
    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=TELEGRAM_TOKEN,
        webhook_url=f"{WEBHOOK_URL}/{TELEGRAM_TOKEN}"
    )

if __name__ == "__main__":
    main()
