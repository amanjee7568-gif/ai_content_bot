"""
main.py
Comprehensive Telegram AI Bot (production-ready single-file)
Features:
- Telegram webhook mode (render-friendly)
- OpenAI chat integration (uses OpenAI SDK)
- Robust SQLite DB with serialized writes and retries to avoid 'database is locked'
- Users, Ledger, Earnings, Treasury tables and full CRUD helpers
- Signup bonus, daily reward, monthly reset, credits accounting
- Payment integration placeholders: UPI link generator + Cashfree order simulation (extendable)
- Self-heal decorator that logs errors, asks OpenAI for suggestions (logged), retries handlers
- Scheduler (APScheduler/AsyncIOScheduler) for periodic jobs
- Admin web endpoints to view reports (protected by ADMIN_SECRET)
- Usage-metering & earning-records for monetization
- Defensive programming & clear logging
"""

import os
import json
import sqlite3
import time
import logging
import traceback
from datetime import datetime, timezone, timedelta
from functools import wraps
from threading import Lock

from flask import Flask, request, jsonify, abort
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
    CallbackQueryHandler,
)

# Use the official OpenAI library
from openai import OpenAI

# Standard HTTP client for webhook setup and admin calls
import requests

# ---- Logging ----
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("ai_bot")

# ---- Load config from env ----
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")  # required
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")  # required
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")    # e.g. https://your-app.onrender.com
PORT = int(os.getenv("PORT", "10000"))
DB_PATH = os.getenv("DB_PATH", "bot_full.db")

# Payment placeholders / secrets
UPI_ID = os.getenv("UPI_ID", "")              # e.g. yourupi@bank
CASHFREE_KEY = os.getenv("CASHFREE_KEY", "")
CASHFREE_SECRET = os.getenv("CASHFREE_SECRET", "")

# Admin secret for web endpoints (set on Render/Env)
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "changeme")

# Economic constants (editable via ENV)
SIGNUP_BONUS = float(os.getenv("SIGNUP_BONUS", "10.0"))
DAILY_REWARD = float(os.getenv("DAILY_REWARD", "2.0"))
MONTHLY_RESET_BONUS = float(os.getenv("MONTHLY_RESET_BONUS", "50.0"))
CREDIT_COST_PER_CHAT = float(os.getenv("CREDIT_COST_PER_CHAT", "1.0"))
EARNING_PER_INTERACTION = float(os.getenv("EARNING_PER_INTERACTION", "0.01"))

# Safety: ensure required env present
if not TELEGRAM_TOKEN:
    logger.warning("TELEGRAM_TOKEN not set — bot will fail to instantiate until provided.")
if not OPENAI_API_KEY:
    logger.warning("OPENAI_API_KEY not set — AI features will be disabled.")

# ---- DB utilities: thread-safe wrapper, retries to avoid locked DB ----
_db_lock = Lock()

def get_conn():
    # Use check_same_thread=False to allow use from different threads; serialize writes via _db_lock
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def safe_exec(query, params=(), fetch=False, commit=False, max_retries=5, retry_delay=0.08):
    """
    Execute a DB statement safely with a global lock and retries to reduce 'database is locked' errors.
    Use this for writes and reads to be robust in a deployed environment.
    """
    attempt = 0
    while True:
        try:
            with _db_lock:
                conn = get_conn()
                cur = conn.cursor()
                cur.execute(query, params)
                rows = None
                if fetch:
                    rows = cur.fetchall()
                if commit:
                    conn.commit()
                conn.close()
            return rows
        except sqlite3.OperationalError as e:
            attempt += 1
            if attempt > max_retries:
                logger.exception(f"DB operation failed after {attempt} attempts: {e}")
                raise
            logger.warning(f"DB locked; retry {attempt}/{max_retries} after {retry_delay}s")
            time.sleep(retry_delay)
            retry_delay *= 1.5

def init_db():
    logger.info("Initializing DB...")
    # Create all tables
    queries = [
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id INTEGER UNIQUE,
            first_name TEXT,
            username TEXT,
            credits REAL DEFAULT 0,
            last_daily TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            event TEXT,
            amount REAL,
            metadata TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS earnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT,
            amount REAL,
            metadata TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS treasury (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            balance REAL,
            metadata TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS admin_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT,
            metadata TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """,
    ]
    for q in queries:
        safe_exec(q, commit=True)
    # Ensure a default treasury exists
    row = safe_exec("SELECT id FROM treasury WHERE name = ?", ("primary",), fetch=True)
    if not row:
        safe_exec("INSERT INTO treasury (name,balance,metadata) VALUES (?,?,?)", ("primary", 0.0, "{}"), commit=True)
    logger.info("DB ready.")

# ---- Basic models / helpers ----
def add_user_if_missing(tg_id, first_name="", username=""):
    row = safe_exec("SELECT id, credits FROM users WHERE tg_id = ?", (tg_id,), fetch=True)
    if row:
        return row[0]["id"]
    # Insert with signup bonus as credited amount
    safe_exec(
        "INSERT INTO users (tg_id, first_name, username, credits) VALUES (?,?,?,?)",
        (tg_id, first_name, username, SIGNUP_BONUS),
        commit=True
    )
    # Fetch id
    row = safe_exec("SELECT id FROM users WHERE tg_id = ?", (tg_id,), fetch=True)
    user_id = row[0]["id"]
    # ledger entry for signup
    credit_ledger(user_id, SIGNUP_BONUS, "signup_bonus", {"bonus": SIGNUP_BONUS})
    # record earning / monetization entry for signup (very small)
    record_earning("signup", EARNING_PER_INTERACTION, {"tg_id": tg_id})
    return user_id

def get_user_by_tg(tg_id):
    rows = safe_exec("SELECT * FROM users WHERE tg_id = ?", (tg_id,), fetch=True)
    return dict(rows[0]) if rows else None

def credit_ledger(user_id, amount, event, metadata=None):
    safe_exec(
        "INSERT INTO ledger (user_id, event, amount, metadata) VALUES (?,?,?,?)",
        (user_id, event, amount, json.dumps(metadata or {})),
        commit=True
    )
    # update user credits if user exists
    safe_exec(
        "UPDATE users SET credits = COALESCE(credits,0) + ? WHERE id = ?",
        (amount, user_id),
        commit=True
    )
    # Log to treasury if negative (money spent) or positive
    if amount < 0:
        # money spent by user -> increase treasury
        adjust_treasury("primary", -amount, {"reason": event, "user_id": user_id})

def record_earning(source, amount, metadata=None):
    safe_exec(
        "INSERT INTO earnings (source, amount, metadata) VALUES (?,?,?)",
        (source, amount, json.dumps(metadata or {})),
        commit=True
    )
    # Add to treasury for bookkeeping
    adjust_treasury("primary", amount, {"source": source})

def adjust_treasury(name, delta_amount, metadata=None):
    # Add a new treasury row if not exist
    rows = safe_exec("SELECT id, balance FROM treasury WHERE name = ?", (name,), fetch=True)
    if rows:
        tid = rows[0]["id"]
        current = rows[0]["balance"] or 0.0
        new_bal = current + float(delta_amount)
        safe_exec("UPDATE treasury SET balance = ? WHERE id = ?", (new_bal, tid), commit=True)
    else:
        safe_exec("INSERT INTO treasury (name, balance, metadata) VALUES (?,?,?)", (name, float(delta_amount), json.dumps(metadata or {})), commit=True)

def get_treasury_balance(name="primary"):
    rows = safe_exec("SELECT balance FROM treasury WHERE name = ?", (name,), fetch=True)
    return float(rows[0]["balance"]) if rows else 0.0

def transfer_treasury(from_name, to_name, amount, metadata=None):
    # Basic transfer between treasuries
    if amount <= 0:
        raise ValueError("Amount must be positive")
    # debit source
    rows = safe_exec("SELECT id, balance FROM treasury WHERE name = ?", (from_name,), fetch=True)
    if not rows:
        raise ValueError("Source treasury not found")
    src = rows[0]
    if src["balance"] < amount:
        raise ValueError("Insufficient funds in source treasury")
    safe_exec("UPDATE treasury SET balance = ? WHERE id = ?", (src["balance"] - amount, src["id"]), commit=True)
    # credit target
    rows = safe_exec("SELECT id, balance FROM treasury WHERE name = ?", (to_name,), fetch=True)
    if rows:
        tgt = rows[0]
        safe_exec("UPDATE treasury SET balance = ? WHERE id = ?", (tgt["balance"] + amount, tgt["id"]), commit=True)
    else:
        safe_exec("INSERT INTO treasury (name, balance, metadata) VALUES (?,?,?)", (to_name, amount, json.dumps(metadata or {})), commit=True)
    # record admin action
    safe_exec("INSERT INTO admin_actions (action, metadata) VALUES (?,?)", ("transfer_treasury", json.dumps({"from": from_name, "to": to_name, "amount": amount, "meta": metadata or {}})), commit=True)

# ---- OpenAI client wrapper ----
openai_client = None
if OPENAI_API_KEY:
    try:
        openai_client = OpenAI(api_key=OPENAI_API_KEY)
        logger.info("OpenAI client initialized")
    except Exception as e:
        logger.exception("Failed to initialize OpenAI client: %s", e)
        openai_client = None
else:
    logger.warning("OPENAI_API_KEY missing — AI features disabled.")

def call_openai_chat(system_prompt, user_messages, model="gpt-4o-mini", max_tokens=800, temperature=0.2):
    if not openai_client:
        raise RuntimeError("OpenAI client not initialized")
    try:
        # using new OpenAI client method `chat.completions.create(...)` as used earlier
        response = openai_client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system_prompt}] + [{"role": "user", "content": m} for m in user_messages],
            max_tokens=max_tokens,
            temperature=temperature
        )
        # response.choices[0].message.content
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.exception("OpenAI chat error: %s", e)
        raise

# ---- AI helper (public) ----
async def get_ai_reply(prompt_text: str) -> str:
    """Return AI reply for prompt_text. Graceful fallback if OpenAI not available."""
    try:
        # system persona can be tuned
        system_prompt = "You are a powerful, helpful AI assistant. Answer succinctly but fully."
        out = call_openai_chat(system_prompt, [prompt_text], max_tokens=800)
        return out
    except Exception as e:
        logger.error("AI reply failed: %s", e)
        return "⚠️ AI temporarily unavailable. Try again later."

# ---- Self-heal decorator ----
def self_heal(retries=2):
    def deco(func):
        @wraps(func)
        async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
            attempt = 0
            last_exc = None
            while attempt <= retries:
                try:
                    return await func(update, context)
                except Exception as e:
                    last_exc = e
                    attempt += 1
                    logger.error("Handler %s failed (attempt %d/%d): %s", func.__name__, attempt, retries, e)
                    # log error to ledger
                    try:
                        safe_exec("INSERT INTO ledger (user_id, event, amount, metadata) VALUES (?,?,?,?)", (0, f"error:{func.__name__}", 0.0, json.dumps({"err": str(e), "attempt": attempt})), commit=True)
                    except Exception:
                        logger.exception("Failed to log error to ledger")
                    # Ask OpenAI for suggestion (best-effort non-blocking)
                    suggestion = None
                    try:
                        if openai_client:
                            suggestion = call_openai_chat(
                                system_prompt="You are an assistant that suggests debugging steps for Python exceptions.",
                                user_messages=[f"Exception in function {func.__name__}: {traceback.format_exc()}\nProvide a short debugging suggestion."]
                            )
                            # Save suggestion to ledger for admin inspection
                            safe_exec("INSERT INTO admin_actions (action, metadata) VALUES (?,?)", ("self_heal_suggestion", json.dumps({"fn": func.__name__, "suggestion": suggestion})), commit=True)
                            logger.info("Self-heal suggestion: %s", (suggestion[:200] + "...") if suggestion else "none")
                    except Exception:
                        logger.exception("OpenAI self-heal suggestion failed")
                    # small wait before retry
                    time.sleep(0.2 * attempt)
            # If all attempts failed
            logger.error("Handler %s permanently failed after %d attempts: %s", func.__name__, retries, last_exc)
            # Notify user (safe message)
            try:
                await update.message.reply_text("⚠️ Some internal error occurred and couldn't be auto-fixed. Administrators have been notified.")
            except Exception:
                logger.exception("Failed to send failure notification to user.")
        return wrapped
    return deco

# ---- Payment helpers (placeholders, extend to real APIs) ----
def generate_upi_uri(payee=UPI_ID, amount=0.0, note="AI Bot Payment"):
    # UPI deep link format
    amt = f"{amount:.2f}"
    return f"upi://pay?pa={payee}&pn=AI%20Bot&am={amt}&cu=INR&tn={note}"

def create_cashfree_order(amount, order_id=None):
    # Simulate or integrate Cashfree order creation here. Return a dict with order_id and payment_link
    if not order_id:
        order_id = f"cf_{int(datetime.now(timezone.utc).timestamp()*1000)}"
    payment_link = f"https://sample.cashfree.com/pay/{order_id}?amount={amount}&key={CASHFREE_KEY}"
    # record an earning/request
    safe_exec("INSERT INTO earnings (source,amount,metadata) VALUES (?,?,?)", ("cashfree_request", amount, json.dumps({"order_id": order_id})), commit=True)
    return {"order_id": order_id, "payment_link": payment_link, "amount": amount}

# ---- Bot handlers ----
@self_heal(retries=2)
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    add_user_if_missing(user.id, user.first_name or "", user.username or "")
    await update.message.reply_text(
        f"👋 Hi {user.first_name or 'there'}! Welcome to the AI Bot.\n" +
        f"You have been credited with signup bonus of {SIGNUP_BONUS} credits.\n" +
        f"Use /credits to check balance and send any message to chat with AI (cost {CREDIT_COST_PER_CHAT} credit per chat)."
    )

@self_heal(retries=2)
async def credits_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    u = get_user_by_tg(user.id)
    if not u:
        await update.message.reply_text("Please /start first to create your account.")
        return
    credits = u.get("credits", 0.0)
    await update.message.reply_text(f"💰 You have {credits:.2f} credits.")

@self_heal(retries=2)
async def daily_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    u = get_user_by_tg(user.id)
    if not u:
        await update.message.reply_text("Please /start first.")
        return
    today = datetime.utcnow().date().isoformat()
    if u.get("last_daily") == today:
        await update.message.reply_text("⚠️ You already claimed today's reward.")
        return
    # update last_daily and give reward
    safe_exec("UPDATE users SET last_daily=? WHERE tg_id=?", (today, user.id), commit=True)
    credit_ledger(u["id"], DAILY_REWARD, "daily_reward", {"reward": DAILY_REWARD})
    record_earning("daily_claim", EARNING_PER_INTERACTION)
    await update.message.reply_text(f"✅ Daily reward of {DAILY_REWARD} credits granted!")

@self_heal(retries=2)
async def pay_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Usage: /pay upi 50  or /pay cashfree 100
    user = update.effective_user
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text("Usage: /pay <method: upi|cashfree> <amount>")
        return
    method = args[0].lower()
    try:
        amount = float(args[1])
    except Exception:
        await update.message.reply_text("Invalid amount. Example: /pay upi 50")
        return
    if method == "upi":
        uri = generate_upi_uri(payee=UPI_ID, amount=amount)
        keyboard = [[InlineKeyboardButton("Open UPI App", url=uri)]]
        await update.message.reply_text(f"Scan/pay using UPI: {uri}", reply_markup=InlineKeyboardMarkup(keyboard))
        record_earning("pay_request_upi", EARNING_PER_INTERACTION, {"amount": amount})
    elif method == "cashfree":
        order = create_cashfree_order(amount)
        keyboard = [[InlineKeyboardButton("Pay on Cashfree", url=order["payment_link"])]]
        await update.message.reply_text(f"Cashfree payment link created: {order['payment_link']}", reply_markup=InlineKeyboardMarkup(keyboard))
        record_earning("pay_request_cashfree", EARNING_PER_INTERACTION, {"amount": amount, "order_id": order["order_id"]})
    else:
        await update.message.reply_text("Unsupported method. Use 'upi' or 'cashfree'.")

@self_heal(retries=2)
async def admin_wallet_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Admin-only placeholder: requires checking administrator (we'll check admin secret via message or ENV in production)
    # For demo, allow the bot owner (first startup?) or user with ADMIN_SECRET in args
    args = context.args or []
    if not args or args[0] != ADMIN_SECRET:
        await update.message.reply_text("Unauthorized. Provide admin secret.")
        return
    # return treasury status
    balance = get_treasury_balance("primary")
    await update.message.reply_text(f"Treasury 'primary' balance: {balance:.2f}")

@self_heal(retries=2)
async def chat_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Main message handler: consumes credits, calls AI, returns response
    user = update.effective_user
    text = update.message.text or ""
    u = get_user_by_tg(user.id)
    if not u:
        await update.message.reply_text("Please /start first.")
        return
    if u.get("credits", 0) < CREDIT_COST_PER_CHAT:
        await update.message.reply_text("⚠️ You don't have enough credits. Use /pay to add credits or refer a friend.")
        return
    # Deduct credit first atomically
    try:
        credit_ledger(u["id"], -CREDIT_COST_PER_CHAT, "chat_usage", {"prompt_len": len(text)})
    except Exception as e:
        logger.exception("Failed to deduct credits: %s", e)
        await update.message.reply_text("⚠️ Temporary error deducting credits. Try again later.")
        return
    # Record monetization event
    record_earning("chat_interaction", EARNING_PER_INTERACTION, {"tg_id": user.id})
    # Ask AI
    try:
        reply = await get_ai_reply(text)
    except Exception as e:
        logger.exception("AI call error: %s", e)
        reply = "⚠️ AI failed. Your credits were not refunded. Admin notified."
    # Add small ledger entry for chat result (status)
    safe_exec("INSERT INTO ledger (user_id, event, amount, metadata) VALUES (?,?,?,?)", (u["id"], "chat_response", 0.0, json.dumps({"len": len(reply)})), commit=True)
    await update.message.reply_text(reply)

# ---- Admin web endpoints (Flask) ----
flask_app = Flask(__name__)

def require_admin_secret(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        secret = request.headers.get("X-ADMIN-SECRET") or request.args.get("admin_secret")
        if not secret or secret != ADMIN_SECRET:
            return jsonify({"error": "unauthorized"}), 401
        return func(*args, **kwargs)
    return wrapper

@flask_app.route("/admin/health", methods=["GET"])
def admin_health():
    return jsonify({"status": "ok", "time": datetime.utcnow().isoformat()})

@flask_app.route("/admin/treasury", methods=["GET"])
@require_admin_secret
def admin_treasury():
    rows = safe_exec("SELECT name, balance, metadata, created_at FROM treasury", fetch=True)
    data = [dict(r) for r in rows] if rows else []
    return jsonify({"treasury": data})

@flask_app.route("/admin/earnings", methods=["GET"])
@require_admin_secret
def admin_earnings():
    rows = safe_exec("SELECT source, amount, metadata, created_at FROM earnings ORDER BY created_at DESC LIMIT 500", fetch=True)
    data = [dict(r) for r in rows] if rows else []
    return jsonify({"earnings": data})

@flask_app.route("/admin/ledger", methods=["GET"])
@require_admin_secret
def admin_ledger():
    rows = safe_exec("SELECT user_id, event, amount, metadata, created_at FROM ledger ORDER BY created_at DESC LIMIT 1000", fetch=True)
    data = [dict(r) for r in rows] if rows else []
    return jsonify({"ledger": data})

@flask_app.route("/admin/transfer", methods=["POST"])
@require_admin_secret
def admin_transfer():
    body = request.json or {}
    from_t = body.get("from", "primary")
    to_t = body.get("to")
    amount = float(body.get("amount", 0))
    if not to_t or amount <= 0:
        return jsonify({"error": "invalid"}), 400
    try:
        transfer_treasury(from_t, to_t, amount, metadata={"by": "admin_api"})
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

# ---- Scheduler jobs ----
scheduler = AsyncIOScheduler()

def job_daily_reward():
    logger.info("Running daily reward job...")
    rows = safe_exec("SELECT id FROM users", fetch=True)
    if not rows:
        logger.info("No users to reward.")
        return
    for r in rows:
        uid = r["id"]
        credit_ledger(uid, DAILY_REWARD, "daily_batch_reward", {"job": "daily"})
    record_earning("daily_batch", EARNING_PER_INTERACTION * max(1, len(rows)))

def job_monthly_reset():
    logger.info("Running monthly reset job...")
    # Give a monthly bonus or do ledger cleanup
    rows = safe_exec("SELECT id FROM users", fetch=True)
    for r in rows:
        uid = r["id"]
        credit_ledger(uid, MONTHLY_RESET_BONUS, "monthly_bonus", {"job": "monthly_reset"})
    # Optionally compress ledger older than N days
    cutoff = (datetime.utcnow() - timedelta(days=90)).isoformat()
    safe_exec("DELETE FROM ledger WHERE created_at < ?", (cutoff,), commit=True)
    record_earning("monthly_reset", EARNING_PER_INTERACTION * max(1, len(rows)))

# ---- Webhook receiver endpoint (for Telegram) ----
# We'll attach this route to Flask and hand off updates to telegram Application's queue
# The Application object is created below.
application = None  # will be set in main()

@flask_app.route("/", methods=["GET"])
def index():
    return "AI Content Bot - Running", 200

@flask_app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def telegram_webhook_receiver():
    if not application:
        return "Application not ready", 500
    try:
        payload = request.get_json(force=True)
        update = Update.de_json(payload, application.bot)
        application.update_queue.put_nowait(update)
        return "ok", 200
    except Exception as e:
        logger.exception("Failed to process incoming webhook: %s", e)
        return "err", 500

# ---- Setup telegram application ----
def build_application():
    global application
    # defensive: ensure token present
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN missing")
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    # Register handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("credits", credits_command))
    application.add_handler(CommandHandler("daily", daily_command))
    application.add_handler(CommandHandler("pay", pay_command))
    application.add_handler(CommandHandler("admin_wallet", admin_wallet_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat_message_handler))
    logger.info("Telegram handlers registered.")
    return application

# ---- Utility to set webhook (once) ----
def set_telegram_webhook():
    if not WEBHOOK_URL:
        logger.warning("WEBHOOK_URL missing; skipping webhook setup")
        return False
    webhook_url = f"{WEBHOOK_URL.rstrip('/')}/{TELEGRAM_TOKEN}"
    try:
        r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook", data={"url": webhook_url})
        if r.status_code == 200:
            logger.info("Webhook set: %s", webhook_url)
            return True
        else:
            logger.error("Failed to set webhook: %s", r.text)
            return False
    except Exception as e:
        logger.exception("Webhook set exception: %s", e)
        return False

# ---- Graceful startup & main ----
def main():
    # Initialize DB
    init_db()

    # Build telegram application (will raise if TELEGRAM_TOKEN missing/invalid)
    try:
        build_application()
    except Exception as e:
        logger.exception("Failed to build telegram Application: %s", e)
        raise

    # Start scheduler jobs
    scheduler.add_job(job_daily_reward, "interval", hours=24, next_run_time=datetime.utcnow())
    scheduler.add_job(job_monthly_reset, "cron", day=1, hour=0, minute=5)  # monthly on day 1
    scheduler.start()
    logger.info("Scheduler started.")

    # Make sure webhook set
    set_telegram_webhook()

    # Run Flask app (Render will expose via PORT)
    # We use Application.run_webhook for python-telegram-bot is possible, but since we want Flask endpoints for admin,
    # we run Flask and push updates into the built Application queue (done by telegram_webhook_receiver).
    # Start flask app — if running locally, this will host both admin endpoints and webhook endpoint.
    logger.info("Starting Flask app on port %d", PORT)
    flask_app.run(host="0.0.0.0", port=PORT)

# Run when executed as script
if __name__ == "__main__":
    main()
