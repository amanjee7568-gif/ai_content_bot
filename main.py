# main.py
# Telegram AI SuperBot — single-file, polling mode, free-tier friendly
# Features:
# - ChatGPT-style chat (/ask), dev agent (/code, /app, /explain, /fix, /tests)
# - File scaffolds returned as documents
# - Monetization: treasury points, referrals, sponsors/ads rotation, premium upsell, donations
# - Hourly heartbeat with APScheduler
# - Admin: /stats /broadcast /add_sponsor /list_sponsors /del_sponsor /set_premium_link /set_referral_payout
# - Safe fallbacks for rate limiter & webhook conflicts
# - SQLite persistence (users, treasury, messages, sponsors, referrals)

import os
import re
import io
import json
import time
import sqlite3
import textwrap
import logging
from datetime import datetime, timedelta
from typing import Optional, Tuple

from dotenv import load_dotenv

# Telegram
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
)
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes,
    filters, AIORateLimiter
)

# Scheduler
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# OpenAI (>=1.0)
from openai import OpenAI

# --------- Bootstrap ----------
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))
PREMIUM_LINK = os.getenv("PREMIUM_LINK", "").strip()
REFERRAL_PAYOUT = float(os.getenv("REFERRAL_PAYOUT", "1.0"))
WELCOME_BONUS = float(os.getenv("WELCOME_BONUS", "0.5"))
DB_PATH = os.getenv("DB_PATH", "bot.db")

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
)
log = logging.getLogger("EconomyBot")

if not TELEGRAM_TOKEN:
    raise SystemExit("TELEGRAM_TOKEN missing")
if not OPENAI_API_KEY:
    log.warning("OPENAI_API_KEY missing: AI features will be disabled.")

# --------- DB ----------
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    con = db()
    cur = con.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        joined_at TEXT,
        ref_code TEXT UNIQUE,
        referred_by TEXT
    );

    CREATE TABLE IF NOT EXISTS treasury (
        user_id INTEGER PRIMARY KEY,
        balance REAL DEFAULT 0,
        total_earned REAL DEFAULT 0,
        total_spent REAL DEFAULT 0,
        updated_at TEXT
    );

    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        kind TEXT,
        content TEXT,
        created_at TEXT
    );

    CREATE TABLE IF NOT EXISTS sponsors (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT,
        url TEXT,
        weight INTEGER DEFAULT 1,
        active INTEGER DEFAULT 1,
        created_at TEXT
    );

    CREATE TABLE IF NOT EXISTS referrals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        referrer_code TEXT,
        referee_user_id INTEGER,
        reward REAL,
        created_at TEXT
    );
    """)
    con.commit()
    con.close()

def _now():
    return datetime.utcnow().isoformat()

def get_or_create_user(u) -> Tuple[sqlite3.Row, bool]:
    con = db()
    cur = con.cursor()
    cur.execute("SELECT * FROM users WHERE user_id=?", (u.id,))
    row = cur.fetchone()
    created = False
    if not row:
        ref_code = f"R{u.id}"
        cur.execute(
            "INSERT INTO users (user_id, username, first_name, joined_at, ref_code) VALUES (?,?,?,?,?)",
            (u.id, u.username or "", u.first_name or "", _now(), ref_code)
        )
        cur.execute(
            "INSERT INTO treasury (user_id, balance, total_earned, total_spent, updated_at) VALUES (?,?,?,?,?)",
            (u.id, 0.0, 0.0, 0.0, _now())
        )
        con.commit()
        cur.execute("SELECT * FROM users WHERE user_id=?", (u.id,))
        row = cur.fetchone()
        created = True
    con.close()
    return row, created

def credit(user_id: int, amt: float, reason: str = ""):
    con = db(); cur = con.cursor()
    cur.execute("SELECT balance,total_earned FROM treasury WHERE user_id=?", (user_id,))
    t = cur.fetchone()
    if not t:
        cur.execute("INSERT INTO treasury (user_id,balance,total_earned,total_spent,updated_at) VALUES (?,?,?,?,?)",
                    (user_id, amt, amt, 0.0, _now()))
    else:
        balance = float(t["balance"]) + amt
        total_earned = float(t["total_earned"]) + amt
        cur.execute("UPDATE treasury SET balance=?, total_earned=?, updated_at=? WHERE user_id=?",
                    (balance, total_earned, _now(), user_id))
    con.commit()
    con.close()
    log.info(f"Treasury credit {user_id}: +{amt} | {reason}")

def debit(user_id: int, amt: float, reason: str = "") -> bool:
    con = db(); cur = con.cursor()
    cur.execute("SELECT balance,total_spent FROM treasury WHERE user_id=?", (user_id,))
    t = cur.fetchone()
    if not t or float(t["balance"]) < amt:
        con.close(); return False
    balance = float(t["balance"]) - amt
    total_spent = float(t["total_spent"]) + amt
    cur.execute("UPDATE treasury SET balance=?, total_spent=?, updated_at=? WHERE user_id=?",
                (balance, total_spent, _now(), user_id))
    con.commit(); con.close()
    log.info(f"Treasury debit {user_id}: -{amt} | {reason}")
    return True

def get_balance(user_id: int) -> float:
    con = db(); cur = con.cursor()
    cur.execute("SELECT balance FROM treasury WHERE user_id=?", (user_id,))
    t = cur.fetchone()
    con.close()
    return float(t["balance"]) if t else 0.0

def add_message(user_id: int, kind: str, content: str):
    con = db(); cur = con.cursor()
    cur.execute("INSERT INTO messages (user_id,kind,content,created_at) VALUES (?,?,?,?)",
                (user_id, kind, content[:4000], _now()))
    con.commit(); con.close()

def add_sponsor(title: str, url: str, weight: int = 1):
    con = db(); cur = con.cursor()
    cur.execute("INSERT INTO sponsors (title,url,weight,active,created_at) VALUES (?,?,?,?,?)",
                (title.strip(), url.strip(), max(1, int(weight)), 1, _now()))
    con.commit(); con.close()

def pick_sponsor() -> Optional[sqlite3.Row]:
    con = db(); cur = con.cursor()
    cur.execute("SELECT * FROM sponsors WHERE active=1")
    rows = cur.fetchall()
    if not rows:
        con.close(); return None
    # weight-based simple picker
    pool = []
    for r in rows:
        pool += [r]*int(r["weight"])
    chosen = pool[int(time.time()) % len(pool)]
    con.close()
    return chosen

def list_sponsors():
    con = db(); cur = con.cursor()
    cur.execute("SELECT * FROM sponsors ORDER BY id DESC")
    rows = cur.fetchall()
    con.close()
    return rows

def del_sponsor(sid: int) -> bool:
    con = db(); cur = con.cursor()
    cur.execute("DELETE FROM sponsors WHERE id=?", (sid,))
    ok = cur.rowcount > 0
    con.commit(); con.close()
    return ok

# --------- OpenAI ----------
def get_openai_client() -> Optional[OpenAI]:
    if not OPENAI_API_KEY:
        return None
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        log.info("OpenAI client initialized.")
        return client
    except Exception as e:
        log.error(f"OpenAI init failed: {e}")
        return None

oa_client = get_openai_client()

async def ai_complete(prompt: str, sys: str = "You are a helpful AI assistant. Be concise, accurate.") -> str:
    if not oa_client:
        return "⚠️ OpenAI key not set. Admin needs to configure OPENAI_API_KEY."
    try:
        # gpt-4o-mini for speed/cost. You can change to 'o4-mini' or 'gpt-4o' if enabled.
        resp = oa_client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.4,
            messages=[
                {"role":"system","content":sys},
                {"role":"user","content":prompt}
            ]
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        log.exception("OpenAI error")
        return f"❌ OpenAI error: {e}"

# --------- Monetization helpers ----------
def referral_attach_if_any(args_text: str, user_row: sqlite3.Row):
    # /start R123 or /start ref=R123
    code = ""
    m = re.search(r"\b(R\d+)\b", args_text or "")
    if m:
        code = m.group(1)
    m2 = re.search(r"ref=([A-Za-z0-9_]+)", args_text or "")
    if m2:
        code = m2.group(1)

    if not code:
        return None

    # Attach only if not already
    con = db(); cur = con.cursor()
    cur.execute("SELECT referred_by FROM users WHERE user_id=?", (user_row["user_id"],))
    rb = cur.fetchone()
    if rb and (rb["referred_by"] or "") != "":
        con.close(); return None

    # Don't allow self-ref
    if code == user_row["ref_code"]:
        con.close(); return None

    cur.execute("UPDATE users SET referred_by=? WHERE user_id=?", (code, user_row["user_id"]))
    con.commit(); con.close()
    # reward referrer now
    reward_referrer(code, user_row["user_id"])
    return code

def reward_referrer(ref_code: str, new_user_id: int):
    # Find referrer user_id from ref_code
    con = db(); cur = con.cursor()
    cur.execute("SELECT user_id FROM users WHERE ref_code=?", (ref_code,))
    r = cur.fetchone()
    if not r:
        con.close(); return
    referrer_id = int(r["user_id"])
    credit(referrer_id, REFERRAL_PAYOUT, reason=f"Referral reward for {new_user_id}")
    cur.execute("INSERT INTO referrals (referrer_code, referee_user_id, reward, created_at) VALUES (?,?,?,?)",
                (ref_code, new_user_id, REFERRAL_PAYOUT, _now()))
    con.commit(); con.close()

# --------- Bot Replies ----------
WELCOME_TEXT = """\
👋 *Welcome to SuperBot* — दुनिया का शक्तिशाली AI डेवलपर असिस्टेंट!

आप कर सकते हैं:
• /ask — किसी भी सवाल का instant जवाब
• /code — code generate/complete (उदा. `/code python make a fastapi hello world`)
• /app — पूरा app scaffold फाइल के रूप में
• /explain — code समझाओ
• /fix — buggy code ठीक करो
• /tests — unit tests बनाओ
• /balance — अपनी कमाई/treasury देखो
• /ads — स्पॉन्सर/ऑफर देखें (visit=earn)
• /premium — प्रीमियम benefits

💸 Monetization & Rewards
• Referral: अपना code शेयर करो — हर join पर +₹/points
• Visits to sponsors: periodic bonus
• Daily welcome bonus (first time)

Admin tools: /stats /broadcast /add_sponsor /list_sponsors /del_sponsor /set_premium_link /set_referral_payout
"""

PREMIUM_TEXT = """\
✨ *Premium Features*
- Higher rate limits
- Longer context & faster responses
- Priority code-gen & file scaffolds

{link}

Alternatively, donate/sponsor us via /ads. Thanks!
"""

def sponsor_keyboard():
    s = pick_sponsor()
    if not s:
        return None
    kb = [[InlineKeyboardButton(f"🔥 {s['title']}", url=s["url"])]]
    return InlineKeyboardMarkup(kb)

# --------- Handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    row, created = get_or_create_user(u)
    args_text = ""
    if context.args:
        args_text = " ".join(context.args)
    attached = referral_attach_if_any(args_text, row)

    if created:
        # welcome bonus
        credit(u.id, WELCOME_BONUS, "Welcome bonus")

    add_message(u.id, "cmd", "/start " + args_text)

    kb = [
        [InlineKeyboardButton("Ask AI", callback_data="action:ask"),
         InlineKeyboardButton("Generate Code", callback_data="action:code")],
        [InlineKeyboardButton("View Sponsors", callback_data="action:ads"),
         InlineKeyboardButton("Check Balance", callback_data="action:bal")]
    ]
    if row["ref_code"]:
        kb.append([InlineKeyboardButton("Invite & Earn", url=f"https://t.me/{(await context.bot.get_me()).username}?start={row['ref_code']}")])

    await update.message.reply_markdown(
        WELCOME_TEXT + (f"\n🔗 Attached referral: *{attached}*" if attached else ""),
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_markdown(WELCOME_TEXT)

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    bal = get_balance(u.id)
    row, _ = get_or_create_user(u)
    await update.message.reply_text(f"💰 Balance: {bal:.2f}\nReferral code: {row['ref_code']}")

async def premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    link = PREMIUM_LINK or "👉 Admin ने premium link set नहीं किया है। (/set_premium_link)"
    await update.message.reply_markdown(PREMIUM_TEXT.format(link=link))

async def ads(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = sponsor_keyboard()
    if not keyboard:
        await update.message.reply_text("No active sponsors yet. Ask admin to add via /add_sponsor")
        return
    await update.message.reply_text("🎯 *Sponsored Offer* — visit to support us:", parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)

async def ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    q = " ".join(context.args) if context.args else (update.message.text or "")
    if not q.strip():
        await update.message.reply_text("Usage: /ask your question")
        return
    add_message(u.id, "ask", q)
    sponsor = sponsor_keyboard()
    res = await ai_complete(q)
    credit(u.id, 0.02, "Engagement reward")
    await update.message.reply_text(res, reply_markup=sponsor)

# ---- Dev Agent Tools ----
DEV_SYSTEM = """You are an expert software engineer and architect. 
Respond with clear, production-grade solutions, include reasoning briefly, and give runnable code where requested.
Prefer minimal dependencies. Be safe and avoid secrets."""

async def code_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    prompt = " ".join(context.args) if context.args else ""
    if not prompt:
        await update.message.reply_text("Example:\n/code python make a FastAPI hello world with /health route")
        return
    add_message(u.id, "code", prompt)
    res = await ai_complete(f"Generate code as requested.\n\nRequest:\n{prompt}", DEV_SYSTEM)
    credit(u.id, 0.05, "Code-gen reward")
    await update.message.reply_markdown_v2(escape_md(res))

async def explain_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    code = extract_code_from_text(update.message.text or "")
    if not code:
        await update.message.reply_text("Send with code block or paste your code after /explain")
        return
    add_message(u.id, "explain", "len="+str(len(code)))
    res = await ai_complete(f"Explain this code step by step and suggest improvements:\n\n{code}", DEV_SYSTEM)
    credit(u.id, 0.03, "Explain reward")
    await update.message.reply_text(res)

async def fix_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    code = extract_code_from_text(update.message.text or "")
    if not code:
        await update.message.reply_text("Send buggy code after /fix")
        return
    add_message(u.id, "fix", "len="+str(len(code)))
    res = await ai_complete(f"Fix the following code. Return a corrected version and a short diff summary:\n\n{code}", DEV_SYSTEM)
    credit(u.id, 0.04, "Fix reward")
    await update.message.reply_markdown_v2(escape_md(res))

async def tests_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    code = extract_code_from_text(update.message.text or "")
    if not code:
        await update.message.reply_text("Send code after /tests to generate unit tests")
        return
    add_message(u.id, "tests", "len="+str(len(code)))
    res = await ai_complete(f"Write robust unit tests for this code. Use pytest where possible:\n\n{code}", DEV_SYSTEM)
    credit(u.id, 0.04, "Tests reward")
    await update.message.reply_markdown_v2(escape_md(res))

async def app_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    spec = " ".join(context.args) if context.args else "python fastapi todo app with CRUD"
    add_message(u.id, "app", spec)
    plan = await ai_complete(f"Create a minimal but production-ready app scaffold as a single file. Include comments.\n\nSpec:\n{spec}", DEV_SYSTEM)
    # return as file
    buf = io.BytesIO(plan.encode("utf-8"))
    buf.name = "app_scaffold.txt"
    credit(u.id, 0.06, "App scaffold reward")
    await update.message.reply_document(document=InputFile(buf), caption="Your app scaffold ✅")

# --------- Admin Handlers ----------
def is_admin(user_id: int) -> bool:
    return ADMIN_USER_ID and user_id == ADMIN_USER_ID

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    con = db(); cur = con.cursor()
    cur.execute("SELECT COUNT(*) c FROM users"); users = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) c FROM messages"); msgs = cur.fetchone()["c"]
    cur.execute("SELECT SUM(balance) s FROM treasury"); bal = cur.fetchone()["s"] or 0
    con.close()
    await update.message.reply_text(f"👥 Users: {users}\n💬 Messages: {msgs}\n💰 Total balances: {bal:.2f}")

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    text = " ".join(context.args) if context.args else ""
    if not text:
        await update.message.reply_text("Usage: /broadcast your message")
        return
    # naive broadcast (be careful with large user base)
    con = db(); cur = con.cursor()
    cur.execute("SELECT user_id FROM users")
    ids = [int(r["user_id"]) for r in cur.fetchall()]
    con.close()
    sent = 0
    for uid in ids:
        try:
            await context.bot.send_message(uid, f"📣 {text}")
            sent += 1
            time.sleep(0.03)  # mild pacing
        except Exception:
            pass
    await update.message.reply_text(f"Broadcast sent to {sent} users.")

async def add_sponsor_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    arg = " ".join(context.args) if context.args else ""
    # format: title | url | weight
    parts = [p.strip() for p in arg.split("|")]
    if len(parts) < 2:
        await update.message.reply_text("Usage: /add_sponsor Title | https://link | [weight]")
        return
    title, url = parts[0], parts[1]
    weight = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 1
    add_sponsor(title, url, weight)
    await update.message.reply_text("Sponsor added ✅")

async def list_sponsors_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    rows = list_sponsors()
    if not rows:
        await update.message.reply_text("No sponsors.")
        return
    lines = [f"{r['id']}. {r['title']} ({r['url']}) w={r['weight']} active={r['active']}" for r in rows]
    await update.message.reply_text("Sponsors:\n" + "\n".join(lines[:1000]))

async def del_sponsor_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /del_sponsor <id>")
        return
    ok = del_sponsor(int(context.args[0]))
    await update.message.reply_text("Deleted ✅" if ok else "Not found.")

async def set_premium_link_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    global PREMIUM_LINK
    link = " ".join(context.args) if context.args else ""
    if not link:
        await update.message.reply_text("Usage: /set_premium_link <url>")
        return
    PREMIUM_LINK = link.strip()
    await update.message.reply_text(f"Premium link set: {PREMIUM_LINK}")

async def set_referral_payout_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    global REFERRAL_PAYOUT
    try:
        REFERRAL_PAYOUT = float(context.args[0])
    except Exception:
        await update.message.reply_text("Usage: /set_referral_payout <amount(float)>")
        return
    await update.message.reply_text(f"Referral payout set: {REFERRAL_PAYOUT}")

# --------- Callback actions (buttons) ----------
async def cb_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    if data == "action:ask":
        await q.message.reply_text("Type: /ask <your question>")
    elif data == "action:code":
        await q.message.reply_text("Type: /code <what to generate>")
    elif data == "action:ads":
        await ads(update, context)
    elif data == "action:bal":
        await balance(update, context)

# --------- Fallback chat (plain text -> /ask) ----------
async def fallback_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # treat plain text as /ask
    context.args = [update.message.text]
    await ask(update, context)

# --------- Scheduler / Heartbeat ----------
async def job_heartbeat(app):
    # small log + occasional sponsor ping to admin
    bal_sum = 0.0
    con = db(); cur = con.cursor()
    cur.execute("SELECT SUM(balance) s FROM treasury"); s = cur.fetchone()["s"]
    if s: bal_sum = float(s)
    con.close()
    log.info(f"Heartbeat: treasury total={bal_sum:.2f}")

# --------- Utilities ----------
def extract_code_from_text(text: str) -> str:
    # try to pick code blocks ```...```
    m = re.findall(r"```(?:[a-zA-Z0-9_+-]+)?\n(.*?)```", text, flags=re.S)
    if m:
        return "\n\n".join(m).strip()
    # fallback: return text after first space
    parts = text.split(maxsplit=1)
    return parts[1] if len(parts) == 2 else ""

def escape_md(s: str) -> str:
    # Telegram MarkdownV2 escaper
    for ch in r"_*[]()~`>#+-=|{}.!":
        s = s.replace(ch, "\\"+ch)
    return s

# --------- Main build ----------
def build_application():
    appb = ApplicationBuilder().token(TELEGRAM_TOKEN)
    try:
        appb = appb.rate_limiter(AIORateLimiter(max_retries=3))
        log.info("Rate limiter enabled.")
    except Exception:
        log.warning("Rate limiter not available. Proceeding without it.")
    application = appb.build()

    # Handlers
    application.add_handler(CommandHandler(["start","help"], start))
    application.add_handler(CommandHandler("balance", balance))
    application.add_handler(CommandHandler("premium", premium))
    application.add_handler(CommandHandler("ads", ads))

    application.add_handler(CommandHandler("ask", ask))
    application.add_handler(CommandHandler("code", code_cmd))
    application.add_handler(CommandHandler("explain", explain_cmd))
    application.add_handler(CommandHandler("fix", fix_cmd))
    application.add_handler(CommandHandler("tests", tests_cmd))
    application.add_handler(CommandHandler("app", app_cmd))

    # Admin
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("broadcast", broadcast))
    application.add_handler(CommandHandler("add_sponsor", add_sponsor_cmd))
    application.add_handler(CommandHandler("list_sponsors", list_sponsors_cmd))
    application.add_handler(CommandHandler("del_sponsor", del_sponsor_cmd))
    application.add_handler(CommandHandler("set_premium_link", set_premium_link_cmd))
    application.add_handler(CommandHandler("set_referral_payout", set_referral_payout_cmd))

    # ✅ Fix: use CallbackQueryHandler instead of UpdateType
    application.add_handler(CallbackQueryHandler(cb_action))

    # Fallback plain text
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback_text))

    return application

def main():
    log.info("OpenAI client will be enabled." if OPENAI_API_KEY else "OpenAI client disabled.")
    init_db()
    log.info("DB ready (schema ensured).")

    app = build_application()

    # Force delete webhook (avoid webhook/polling conflict)
    try:
        app.bot.delete_webhook(drop_pending_updates=False)
        log.info("deleteWebhook sent.")
    except Exception:
        pass

    # Scheduler
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(lambda: app.create_task(job_heartbeat(app)), "interval", hours=1, next_run_time=datetime.utcnow()+timedelta(seconds=10))
    scheduler.start()
    log.info("Scheduler started.")

    # Polling mode (Render free tier friendly)
    log.info("Mode: POLLING")
    app.run_polling(drop_pending_updates=False, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
