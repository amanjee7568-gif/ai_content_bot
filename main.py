# main.py
# ------------------------------------------------------------------------------
# SINGLE-FILE: Telegram AI Superbot (ChatGPT-like) + Economy + Monetization Hooks
# ------------------------------------------------------------------------------
# Features (all in one file):
# • ChatGPT-style AI replies via OpenAI API (/ask and inline chat)
# • Economy system: /balance /work /daily /pay /lb /stats
# • Monetization hooks (CPC/CPM emulation + impressions/click tracking)
# • Admin tools: /broadcast /ban /unban /give /take /shadowmute /stats_all
# • APScheduler jobs: heartbeat + daily recap + auto-backup (sqlite)
# • Safe polling mode for free deployments (no port binding, no rate_limiter extra)
#
# Deploy:
#   • pip install -U python-telegram-bot==21.6 openai==1.42.0 APScheduler==3.10.4 python-dotenv==1.1.1
#   • .env with:
#       TELEGRAM_TOKEN=123456:ABC...
#       OPENAI_API_KEY=sk-...
#       ADMIN_IDS=111111111,222222222
#   • python main.py
#
# Notes:
#   • Uses sqlite (file: data.db). Auto-creates tables.
#   • “Ads” here are *hooks/metrics* to integrate a real ad or affiliate later.
#   • Keeps CPU/RAM light for free tiers. Works in polling mode.
# ------------------------------------------------------------------------------

import asyncio
import logging
import os
import random
import re
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta
from typing import Optional, Tuple

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from openai import OpenAI
from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ChatAction,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ------------------------------------------------------------------------------
# ENV & LOG
# ------------------------------------------------------------------------------

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
ADMIN_IDS = {
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()
}

if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN missing in env")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
log = logging.getLogger("EconomyBot")

# ------------------------------------------------------------------------------
# DB
# ------------------------------------------------------------------------------

DB_PATH = os.getenv("DB_PATH", "data.db")

SCHEMA = """
PRAGMA journal_mode = WAL;
CREATE TABLE IF NOT EXISTS users (
  user_id INTEGER PRIMARY KEY,
  first_name TEXT,
  username TEXT,
  is_banned INTEGER DEFAULT 0,
  shadow_muted INTEGER DEFAULT 0,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  last_seen TEXT
);
CREATE TABLE IF NOT EXISTS wallets (
  user_id INTEGER PRIMARY KEY,
  balance INTEGER DEFAULT 0,
  lifetime_earned INTEGER DEFAULT 0,
  lifetime_spent INTEGER DEFAULT 0,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(user_id) REFERENCES users(user_id)
);
CREATE TABLE IF NOT EXISTS tx (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER,
  delta INTEGER,
  reason TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS cooldowns (
  user_id INTEGER PRIMARY KEY,
  last_work TEXT,
  last_daily TEXT
);
CREATE TABLE IF NOT EXISTS ads_metrics (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER,
  event TEXT,
  value REAL,
  meta TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS prompts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER,
  role TEXT,
  content TEXT,
  tokens INTEGER DEFAULT 0,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS bot_stats (
  key TEXT PRIMARY KEY,
  value TEXT
);
"""

def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, isolation_level=None, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con

def ensure_schema():
    with closing(db()) as con:
        con.executescript(SCHEMA)
    log.info("DB ready (schema ensured).")

ensure_schema()

# ------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------

def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

def now_ts() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"

def human(n: int) -> str:
    return f"{n:,}"

async def safe_reply(update: Update, text: str, **kw):
    try:
        return await update.effective_message.reply_text(text, **kw)
    except Exception as e:
        log.warning(f"reply failed: {e}")

async def send_typing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id, action=ChatAction.TYPING
        )
    except Exception:
        pass

def upsert_user(u):
    with closing(db()) as con:
        con.execute(
            """
            INSERT INTO users(user_id, first_name, username, last_seen)
            VALUES(?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
              first_name=excluded.first_name,
              username=excluded.username,
              last_seen=excluded.last_seen
            """,
            (u.id, u.first_name or "", (u.username or "").lower(), now_ts()),
        )
        con.execute(
            """
            INSERT INTO wallets(user_id, balance, lifetime_earned, lifetime_spent)
            VALUES(?,0,0,0)
            ON CONFLICT(user_id) DO NOTHING
            """,
            (u.id,),
        )
        con.execute(
            "INSERT OR REPLACE INTO bot_stats(key,value) VALUES('last_user_seen',?)",
            (now_ts(),),
        )

def user_ban_status(uid: int) -> Tuple[bool, bool]:
    with closing(db()) as con:
        r = con.execute("SELECT is_banned, shadow_muted FROM users WHERE user_id=?",(uid,)).fetchone()
        if not r: return (False, False)
        return (bool(r["is_banned"]), bool(r["shadow_muted"]))

def wallet_get(uid: int) -> Tuple[int,int,int]:
    with closing(db()) as con:
        r = con.execute("SELECT balance,lifetime_earned,lifetime_spent FROM wallets WHERE user_id=?",(uid,)).fetchone()
        if not r: return (0,0,0)
        return (int(r["balance"]), int(r["lifetime_earned"]), int(r["lifetime_spent"]))

def wallet_add(uid: int, delta: int, reason: str):
    with closing(db()) as con:
        con.execute("INSERT INTO tx(user_id,delta,reason) VALUES(?,?,?)",(uid,delta,reason))
        if delta>=0:
            con.execute(
                "UPDATE wallets SET balance=balance+?, lifetime_earned=lifetime_earned+?, updated_at=CURRENT_TIMESTAMP WHERE user_id=?",
                (delta, delta, uid),
            )
        else:
            con.execute(
                "UPDATE wallets SET balance=balance+?, lifetime_spent=lifetime_spent+?, updated_at=CURRENT_TIMESTAMP WHERE user_id=?",
                (delta, -delta, uid),
            )

def cooldown_ok(uid: int, kind: str, seconds: int) -> Tuple[bool,int]:
    col = "last_work" if kind=="work" else "last_daily"
    with closing(db()) as con:
        r = con.execute(f"SELECT {col} FROM cooldowns WHERE user_id=?",(uid,)).fetchone()
        last = None
        if r and r[col]:
            try: last = datetime.fromisoformat(r[col].replace("Z",""))
            except: last = None
        if not last: return True, 0
        elapsed = int((datetime.utcnow()-last).total_seconds())
        if elapsed >= seconds: return True, 0
        return False, seconds - elapsed

def cooldown_mark(uid: int, kind: str):
    col = "last_work" if kind=="work" else "last_daily"
    with closing(db()) as con:
        con.execute(
            f"""
            INSERT INTO cooldowns(user_id,{col}) VALUES(?,?)
            ON CONFLICT(user_id) DO UPDATE SET {col}=excluded.{col}
            """,
            (uid, now_ts()),
        )

def ads_track(uid: int, event: str, value: float=0.0, meta: str=""):
    with closing(db()) as con:
        con.execute(
            "INSERT INTO ads_metrics(user_id,event,value,meta) VALUES(?,?,?,?)",
            (uid, event, float(value), meta),
        )

def ads_snapshot(uid: Optional[int]=None) -> dict:
    # naive monetization model: CPM 0.50$ per 1000 impressions, CPC 0.05$ per click-equivalent
    with closing(db()) as con:
        q_user = " WHERE user_id=?" if uid else ""
        params = (uid,) if uid else ()
        row = con.execute(
            f"""
            SELECT
              SUM(CASE WHEN event='impression' THEN 1 ELSE 0 END) AS imp,
              SUM(CASE WHEN event='click' THEN 1 ELSE 0 END) AS clk,
              SUM(CASE WHEN event='chat' THEN 1 ELSE 0 END) AS chats,
              SUM(CASE WHEN event='cmd' THEN 1 ELSE 0 END) AS cmds
            FROM ads_metrics{q_user}
            """,
            params,
        ).fetchone()
    imp = int(row["imp"] or 0)
    clk = int(row["clk"] or 0)
    chats = int(row["chats"] or 0)
    cmds = int(row["cmds"] or 0)
    cpm = 0.50
    cpc = 0.05
    revenue = (imp/1000.0)*cpm + clk*cpc
    return {"impressions": imp, "clicks": clk, "chats": chats, "commands": cmds, "est_usd": round(revenue, 4)}

# ------------------------------------------------------------------------------
# OpenAI
# ------------------------------------------------------------------------------

client: Optional[OpenAI] = None
if OPENAI_API_KEY:
    client = OpenAI(api_key=OPENAI_API_KEY)
    log.info("OpenAI client initialized.")
else:
    log.warning("OPENAI_API_KEY missing; AI features will be OFF.")

DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")  # cost-effective, fast

async def ai_answer(prompt: str, sys: str="You are a concise, highly-capable assistant.") -> str:
    if not client:
        return "⚠️ AI disabled (missing OPENAI_API_KEY)."
    try:
        rs = client.responses.create(
            model=DEFAULT_MODEL,
            input=[{"role":"system","content":sys},{"role":"user","content":prompt}],
            temperature=0.4,
            max_output_tokens=600,
        )
        out = rs.output_text
        return out.strip() if out else "…"
    except Exception as e:
        log.exception("OpenAI error")
        return f"⚠️ OpenAI error: {e}"

# ------------------------------------------------------------------------------
# UI bits
# ------------------------------------------------------------------------------

def home_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💡 Ask AI", callback_data="nav:ask"),
            InlineKeyboardButton("💰 Balance", callback_data="nav:bal"),
        ],
        [
            InlineKeyboardButton("🛠 Work", callback_data="do:work"),
            InlineKeyboardButton("🎁 Daily", callback_data="do:daily"),
        ],
        [
            InlineKeyboardButton("📈 Earn Stats", callback_data="nav:earn"),
            InlineKeyboardButton("🧠 Pro Tips", callback_data="nav:help"),
        ],
    ])

HELP_TEXT = (
    "🤖 *World’s Smartest AI Bot*\n\n"
    "Chat like ChatGPT — code, debug, write content, translate, brainstorm, *instantly*.\n\n"
    "Commands:\n"
    "• /ask <prompt> — Ask the AI\n"
    "• /balance — Check coins\n"
    "• /work — Quick earn (1 min cooldown)\n"
    "• /daily — Claim daily bonus (24h)\n"
    "• /pay <@user|id> <amount> — Send coins\n"
    "• /lb — Leaderboard\n"
    "• /stats — Your earning & monetization snapshot\n\n"
    "_Admins:_ /broadcast /ban /unban /give /take /shadowmute /stats_all\n"
)

WELCOME = (
    "👋 *Welcome!*\n\n"
    "I’m your all-in-one AI assistant + economy bot. Ask me anything or use the buttons below.\n"
    "Tip: The more you use the bot, the more coins you earn. Activity also contributes to monetization which keeps this service free."
)

# ------------------------------------------------------------------------------
# Handlers
# ------------------------------------------------------------------------------

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u)
    ads_track(u.id, "impression", meta="start")
    await safe_reply(update, WELCOME, parse_mode=ParseMode.MARKDOWN, reply_markup=home_kb())

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u)
    ads_track(u.id, "cmd", meta="help")
    await safe_reply(update, HELP_TEXT, parse_mode=ParseMode.MARKDOWN, reply_markup=home_kb())

async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u)
    ads_track(u.id, "cmd", meta="balance")
    bal, le, ls = wallet_get(u.id)
    await safe_reply(update, f"💰 Balance: *{human(bal)}* coins\nEarned: {human(le)} | Spent: {human(ls)}", parse_mode=ParseMode.MARKDOWN)

async def work_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u)
    banned, shadow = user_ban_status(u.id)
    if banned:
        return
    ok, wait = cooldown_ok(u.id, "work", seconds=60)
    if not ok:
        return await safe_reply(update, f"⏳ Work cooldown: {wait}s")
    earnings = random.randint(3, 12)
    wallet_add(u.id, earnings, "work")
    cooldown_mark(u.id, "work")
    ads_track(u.id, "cmd", meta="work")
    ads_track(u.id, "chat", value=1)
    await safe_reply(update, f"🛠 You worked and earned +{earnings} coins!")

async def daily_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u)
    ok, wait = cooldown_ok(u.id, "daily", seconds=24*3600)
    if not ok:
        # humanize hours
        hrs = round(wait/3600, 2)
        return await safe_reply(update, f"⏳ Daily already claimed. Try again in ~{hrs}h.")
    amount = random.randint(50, 120)
    wallet_add(u.id, amount, "daily")
    cooldown_mark(u.id, "daily")
    ads_track(u.id, "cmd", meta="daily")
    await safe_reply(update, f"🎁 Daily bonus: +{amount} coins!")

async def pay_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u)
    args = context.args
    if len(args) < 2:
        return await safe_reply(update, "Usage: /pay <@user|id> <amount>")
    target_str = args[0]
    amount_str = args[1]
    try:
        amt = int(amount_str)
        if amt <= 0:
            raise ValueError
    except:
        return await safe_reply(update, "Amount must be a positive integer.")
    # resolve id
    m = re.match(r"@?([A-Za-z0-9_]{5,})", target_str)
    target_id = None
    if m:
        handle = m.group(1).lower()
        with closing(db()) as con:
            r = con.execute("SELECT user_id FROM users WHERE username=?", (handle,)).fetchone()
            if r: target_id = int(r["user_id"])
    if not target_id and target_str.isdigit():
        target_id = int(target_str)
    if not target_id:
        return await safe_reply(update, "User not found in DB. The user must have used the bot at least once.")
    # transfer
    bal, *_ = wallet_get(u.id)
    if bal < amt:
        return await safe_reply(update, "Insufficient balance.")
    wallet_add(u.id, -amt, "transfer_out")
    wallet_add(target_id, +amt, "transfer_in")
    await safe_reply(update, f"✅ Sent {amt} coins to {target_str}")

async def lb_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with closing(db()) as con:
        rows = con.execute(
            """
            SELECT w.user_id, w.balance, u.username, u.first_name
            FROM wallets w JOIN users u ON u.user_id=w.user_id
            ORDER BY w.balance DESC LIMIT 10
            """
        ).fetchall()
    lines = ["🏆 *Top 10 Leaderboard*"]
    for i, r in enumerate(rows, start=1):
        name = f"@{r['username']}" if r["username"] else (r["first_name"] or r["user_id"])
        lines.append(f"{i}. {name} — {human(r['balance'])}💰")
    await safe_reply(update, "\n".join(lines), parse_mode=ParseMode.MARKDOWN)

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    snap = ads_snapshot(u.id)
    bal, le, ls = wallet_get(u.id)
    await safe_reply(
        update,
        "📊 *Your Stats*\n"
        f"Impressions: {human(snap['impressions'])}\n"
        f"Clicks: {human(snap['clicks'])}\n"
        f"Commands: {human(snap['commands'])}\n"
        f"Chats: {human(snap['chats'])}\n"
        f"Est. Revenue Contribution: ${snap['est_usd']}\n\n"
        f"Wallet: {human(bal)} (earned {human(le)} / spent {human(ls)})",
        parse_mode=ParseMode.MARKDOWN,
    )

# --- Admin --------------------------------------------------------------------

async def admin_guard(update: Update) -> Optional[int]:
    u = update.effective_user
    if not is_admin(u.id):
        await safe_reply(update, "⛔ Admins only.")
        return None
    return u.id

async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_guard(update): return
    text = " ".join(context.args) or "Admin broadcast."
    await safe_reply(update, f"📣 Broadcasting…")
    # iterate users (simple)
    with closing(db()) as con:
        ids = [int(r["user_id"]) for r in con.execute("SELECT user_id FROM users").fetchall()]
    sent = 0
    for uid in ids:
        try:
            await context.bot.send_message(uid, f"📣 *Broadcast:*\n{text}", parse_mode=ParseMode.MARKDOWN)
            sent += 1
            await asyncio.sleep(0.02)
        except Exception:
            pass
    await safe_reply(update, f"✅ Broadcast sent to {sent} users.")

async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_guard(update): return
    if not context.args: return await safe_reply(update, "Usage: /ban <user_id>")
    uid = int(context.args[0])
    with closing(db()) as con:
        con.execute("UPDATE users SET is_banned=1 WHERE user_id=?", (uid,))
    await safe_reply(update, f"✅ Banned {uid}")

async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_guard(update): return
    if not context.args: return await safe_reply(update, "Usage: /unban <user_id>")
    uid = int(context.args[0])
    with closing(db()) as con:
        con.execute("UPDATE users SET is_banned=0 WHERE user_id=?", (uid,))
    await safe_reply(update, f"✅ Unbanned {uid}")

async def shadowmute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_guard(update): return
    if len(context.args)<2: return await safe_reply(update, "Usage: /shadowmute <user_id> <0|1>")
    uid = int(context.args[0]); flag=int(context.args[1])!=0
    with closing(db()) as con:
        con.execute("UPDATE users SET shadow_muted=? WHERE user_id=?", (1 if flag else 0, uid))
    await safe_reply(update, f"✅ Shadow mute for {uid} = {flag}")

async def give_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_guard(update): return
    if len(context.args)<2: return await safe_reply(update,"Usage: /give <user_id> <amount>")
    uid=int(context.args[0]); amt=int(context.args[1])
    wallet_add(uid, amt, "admin_grant")
    await safe_reply(update, f"✅ Gave {amt} to {uid}")

async def take_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_guard(update): return
    if len(context.args)<2: return await safe_reply(update,"Usage: /take <user_id> <amount>")
    uid=int(context.args[0]); amt=int(context.args[1])
    wallet_add(uid, -amt, "admin_take")
    await safe_reply(update, f"✅ Took {amt} from {uid}")

async def stats_all_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_guard(update): return
    snap = ads_snapshot(None)
    with closing(db()) as con:
        totals = con.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        coins = con.execute("SELECT SUM(balance) AS s FROM wallets").fetchone()["s"] or 0
    await safe_reply(
        update,
        "📊 *Global Stats*\n"
        f"Users: {human(totals)}\n"
        f"Coins in circulation: {human(int(coins))}\n"
        f"Impressions: {human(snap['impressions'])} | Clicks: {human(snap['clicks'])}\n"
        f"Commands: {human(snap['commands'])} | Chats: {human(snap['chats'])}\n"
        f"Est. Revenue: ${snap['est_usd']}",
        parse_mode=ParseMode.MARKDOWN,
    )

# --- AI -----------------------------------------------------------------------

async def ask_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u)
    banned, shadow = user_ban_status(u.id)
    if banned:
        return
    prompt = " ".join(context.args).strip()
    if not prompt:
        return await safe_reply(update, "Usage: /ask <your question>")
    ads_track(u.id, "cmd", meta="ask")
    await send_typing(update, context)
    reply = await ai_answer(prompt)
    # economy micro-reward per AI use
    reward = random.randint(1, 4)
    wallet_add(u.id, reward, "ai_chat_reward")
    ads_track(u.id, "chat", value=1, meta="ask")
    # shadow mute silently discards outward response
    if not shadow:
        await safe_reply(update, f"🧠 *AI:*\n{reply}\n\n`+{reward} coins`", parse_mode=ParseMode.MARKDOWN)

# Inline “chatgpt-style” replies to any text (DMs only)
async def text_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    u = update.effective_user
    upsert_user(u)
    banned, shadow = user_ban_status(u.id)
    if banned:
        return
    text = (update.effective_message.text or "").strip()
    if not text:
        return
    ads_track(u.id, "chat", value=1, meta="free_text")
    await send_typing(update, context)
    reply = await ai_answer(text)
    reward = 1
    wallet_add(u.id, reward, "ai_chat_reward")
    if not shadow:
        await safe_reply(update, reply)

# --- Buttons ------------------------------------------------------------------

async def cb_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    u = update.effective_user
    upsert_user(u)
    data = q.data or ""
    if data == "nav:ask":
        ads_track(u.id, "click", meta="nav_ask")
        await q.message.reply_text("🧠 Send me your question now (or use /ask).")
    elif data == "nav:bal":
        ads_track(u.id, "click", meta="nav_bal")
        bal, le, ls = wallet_get(u.id)
        await q.message.reply_text(f"💰 Balance: {human(bal)} | Earned: {human(le)} | Spent: {human(ls)}")
    elif data == "do:work":
        await work_cmd(update, context)
    elif data == "do:daily":
        await daily_cmd(update, context)
    elif data == "nav:earn":
        s = ads_snapshot(u.id)
        await q.message.reply_text(
            f"📈 Your activity\nImpressions: {human(s['impressions'])}\nClicks: {human(s['clicks'])}\nEst. $: {s['est_usd']}"
        )
    elif data == "nav:help":
        await q.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)

# ------------------------------------------------------------------------------
# Jobs
# ------------------------------------------------------------------------------

async def job_heartbeat(app: Application):
    s = ads_snapshot(None)
    log.info(f"Heartbeat: users, activity: imp={s['impressions']} clk={s['clicks']} est=${s['est_usd']}")

async def job_daily_recap(app: Application):
    # Send optional recap to admins
    s = ads_snapshot(None)
    with closing(db()) as con:
        ucount = con.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
    msg = (
        "📅 *Daily Recap*\n"
        f"Users: {human(ucount)}\n"
        f"Impressions: {human(s['impressions'])} | Clicks: {human(s['clicks'])}\n"
        f"Commands: {human(s['commands'])} | Chats: {human(s['chats'])}\n"
        f"Est. Revenue: ${s['est_usd']}"
    )
    for aid in ADMIN_IDS:
        try:
            await app.bot.send_message(aid, msg, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            pass

async def job_backup(_app: Application):
    # naive sqlite copy
    try:
        ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        bkp = f"data-{ts}.db"
        import shutil
        shutil.copyfile(DB_PATH, bkp)
        log.info(f"Backup created: {bkp}")
    except Exception as e:
        log.warning(f"Backup failed: {e}")

def schedule_jobs(app: Application):
    sched = AsyncIOScheduler(timezone="UTC")
    # Heartbeat every hour
    sched.add_job(job_heartbeat, "interval", hours=1, args=[app], id="heartbeat", replace_existing=True)
    # Daily recap at 00:00 UTC
    sched.add_job(job_daily_recap, "cron", hour=0, minute=0, args=[app], id="daily_recap", replace_existing=True)
    # Backup every 6 hours
    sched.add_job(job_backup, "interval", hours=6, args=[app], id="backup", replace_existing=True)
    sched.start()
    log.info("JobQueue scheduled.")

# ------------------------------------------------------------------------------
# App Build & Run
# ------------------------------------------------------------------------------

def build_application() -> Application:
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        # NOTE: Avoid AIORateLimiter to keep dependencies simple/free.
        .build()
    )
    # Commands
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("work", work_cmd))
    app.add_handler(CommandHandler("daily", daily_cmd))
    app.add_handler(CommandHandler("pay", pay_cmd))
    app.add_handler(CommandHandler(["lb","leaderboard"], lb_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))

    # Admin
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    app.add_handler(CommandHandler("ban", ban_cmd))
    app.add_handler(CommandHandler("unban", unban_cmd))
    app.add_handler(CommandHandler("shadowmute", shadowmute_cmd))
    app.add_handler(CommandHandler("give", give_cmd))
    app.add_handler(CommandHandler("take", take_cmd))
    app.add_handler(CommandHandler("stats_all", stats_all_cmd))

    # AI
    app.add_handler(CommandHandler("ask", ask_cmd))

    # Buttons
    app.add_handler(CallbackQueryHandler(cb_query))

    # Text chat (private only)
    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE, text_chat))

    return app

async def on_startup(app: Application):
    # turn off any old webhook to avoid 409 conflicts with polling
    try:
        await app.bot.delete_webhook(drop_pending_updates=False)
        log.info("Webhook deleted (picking polling).")
    except Exception:
        pass
    schedule_jobs(app)
    log.info("Bot started in POLLING mode.")

def main():
    log.info("OpenAI client will be %s.", "enabled." if client else "disabled.")
    app = build_application()
    app.post_init = on_startup  # PTB v21: post_init hook is awaited before polling
    # Start polling (no port binding -> works on free render/background-like)
    app.run_polling(close_loop=False, allowed_updates=Update.ALL_TYPES, timeout=30)

if __name__ == "__main__":
    main()
