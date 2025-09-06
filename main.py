#!/usr/bin/env python3
# compressed final main.py — ChatGPT-like Telegram bot with payments & self-healing
import os, sys, time, json, sqlite3, tempfile, traceback, logging, requests
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any, Callable
from functools import wraps

# OpenAI new SDK
from openai import OpenAI

# Telegram
from telegram import Update, KeyboardButton, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# Flask for webhook + extra endpoints
from flask import Flask, request, jsonify

# Optional heavy libs
try:
    import yt_dlp
except Exception:
    yt_dlp = None
try:
    from gtts import gTTS
except Exception:
    gTTS = None
try:
    from moviepy.editor import ColorClip, CompositeVideoClip, AudioFileClip, TextClip
    MOVIEPY_AVAILABLE = True
except Exception:
    MOVIEPY_AVAILABLE = False

from apscheduler.schedulers.background import BackgroundScheduler

# ---- logging ----
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("final_singlefile_bot")

# ---- ENV / config ----
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")  # e.g. https://app.onrender.com
PORT = int(os.getenv("PORT", os.getenv("RENDER_PORT", "10000")))
DB_FILE = os.getenv("DB_FILE", "bot_prod.db")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
UPI_ID = os.getenv("UPI_ID", "")  # e.g. merchant@bank
CASHFREE_APP_ID = os.getenv("CASHFREE_APP_ID", "")
CASHFREE_SECRET = os.getenv("CASHFREE_SECRET", "")

# monetization defaults
AD_RATE_PER_VISIT = float(os.getenv("AD_RATE_PER_VISIT", "0.01"))
SIGNUP_BONUS = float(os.getenv("SIGNUP_BONUS", "0.05"))
QUERY_CHARGE = float(os.getenv("QUERY_CHARGE", "0.002"))
FREE_QUOTA = int(os.getenv("FREE_QUOTA", "20"))

if not BOT_TOKEN or not OPENAI_API_KEY:
    logger.error("BOT_TOKEN and OPENAI_API_KEY must be set in env. Exiting.")
    sys.exit(1)

# ---- init clients ----
openai_client = None
try:
    openai_client = OpenAI(api_key=OPENAI_API_KEY)
    logger.info("OpenAI client ready")
except Exception:
    logger.exception("OpenAI init failed")
    openai_client = None

# ---- DB helpers ----
def get_conn():
    return sqlite3.connect(DB_FILE, check_same_thread=False)

def init_db():
    conn = get_conn(); cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS users (
      user_id INTEGER PRIMARY KEY, first_name TEXT, username TEXT,
      created_at TEXT, referral_code TEXT, referred_by INTEGER,
      is_premium INTEGER DEFAULT 0, monthly_queries INTEGER DEFAULT 0, wallet REAL DEFAULT 0.0
    );
    CREATE TABLE IF NOT EXISTS conversations (
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, role TEXT, content TEXT, ts TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS ledger (
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, event TEXT, amount REAL, metadata TEXT, ts TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS visits (
      id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, ip TEXT, ua TEXT, ts TEXT DEFAULT CURRENT_TIMESTAMP
    );
    """)
    conn.commit(); conn.close()

# ---- utility + monetization ----
def generate_referral_code(uid:int)->str:
    return f"R{uid}{int(time.time())%10000}"

def credit_ledger(user_id:int, amount:float, event:str, metadata:Dict=None):
    metadata = json.dumps(metadata or {})
    conn = get_conn(); cur = conn.cursor()
    cur.execute("INSERT INTO ledger (user_id,event,amount,metadata) VALUES (?,?,?,?)", (user_id,event,amount,metadata))
    cur.execute("UPDATE users SET wallet = wallet + ? WHERE user_id=?", (amount, user_id))
    conn.commit(); conn.close()
    logger.info("credited %s to %s (%s)", amount, user_id, event)

def debit_ledger(user_id:int, amount:float, event:str, metadata:Dict=None):
    metadata = json.dumps(metadata or {})
    conn = get_conn(); cur = conn.cursor()
    cur.execute("INSERT INTO ledger (user_id,event,amount,metadata) VALUES (?,?,?,?)", (user_id,event,-abs(amount),metadata))
    cur.execute("UPDATE users SET wallet = wallet - ? WHERE user_id=?", (amount, user_id))
    conn.commit(); conn.close()
    logger.info("debited %s from %s (%s)", amount, user_id, event)

def add_user(user_id:int, first_name:str="", username:str="", referred_by:Optional[int]=None):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,))
    if not cur.fetchone():
        code = generate_referral_code(user_id)
        cur.execute("INSERT INTO users (user_id,first_name,username,created_at,referral_code,referred_by) VALUES (?,?,?,?,?,?)",
                    (user_id, first_name, username, datetime.utcnow().isoformat(), code, referred_by))
        credit_ledger(user_id, SIGNUP_BONUS, "signup_bonus", {"bonus": SIGNUP_BONUS})
    conn.commit(); conn.close()

def get_user(user_id:int)->Optional[Dict[str,Any]]:
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT user_id,first_name,username,created_at,referral_code,referred_by,is_premium,monthly_queries,wallet FROM users WHERE user_id=?", (user_id,))
    r = cur.fetchone(); conn.close()
    if not r: return None
    keys = ["user_id","first_name","username","created_at","referral_code","referred_by","is_premium","monthly_queries","wallet"]
    return dict(zip(keys,r))

def record_visit(source:str, ip:str, ua:str):
    conn = get_conn(); cur = conn.cursor(); cur.execute("INSERT INTO visits (source,ip,ua) VALUES (?,?,?)",(source,ip,ua)); conn.commit(); conn.close()
    credit_ledger(0, AD_RATE_PER_VISIT, "visit_earning", {"source":source})

def inc_monthly_query(uid:int):
    conn = get_conn(); cur = conn.cursor(); cur.execute("UPDATE users SET monthly_queries = monthly_queries + 1 WHERE user_id=?", (uid,)); conn.commit(); conn.close()

def charge_for_query(user_id:int)->bool:
    u = get_user(user_id)
    if not u: return False
    if u.get("is_premium"): return True
    if (u.get("monthly_queries",0) < FREE_QUOTA):
        inc_monthly_query(user_id); return True
    if u.get("wallet",0.0) >= QUERY_CHARGE:
        debit_ledger(user_id, QUERY_CHARGE, "query_charge"); return True
    return False

def save_conversation(user_id:int, role:str, content:str):
    conn = get_conn(); cur = conn.cursor(); cur.execute("INSERT INTO conversations (user_id,role,content) VALUES (?,?,?)",(user_id,role,content)); conn.commit(); conn.close()

def get_recent_conversation(user_id:int, limit:int=20):
    conn = get_conn(); cur = conn.cursor(); cur.execute("SELECT role,content,ts FROM conversations WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id,limit)); rows = cur.fetchall(); conn.close()
    return [{"role":r[0],"content":r[1],"ts":r[2]} for r in reversed(rows)]

# ---- AI helpers ----
def build_chat_messages(user_id:int, user_input:str, max_hist:int=12):
    sys_msg = {"role":"system","content":"You are a helpful, honest, safe assistant."}
    hist = get_recent_conversation(user_id, max_hist)
    msgs = [sys_msg] + [{"role":m["role"],"content":m["content"]} for m in hist] + [{"role":"user","content":user_input}]
    return msgs

def ask_openai(user_id:int, prompt:str, model:str="gpt-4o-mini", max_tokens:int=700, temperature:float=0.7)->str:
    if openai_client is None: return "AI unavailable."
    try:
        messages = build_chat_messages(user_id, prompt, max_hist=15)
        resp = openai_client.chat.completions.create(model=model, messages=messages, max_tokens=max_tokens, temperature=temperature)
        try: text = resp.choices[0].message.content.strip()
        except: text = resp.get("choices",[{}])[0].get("message",{}).get("content","").strip()
        return text or "🤖 (empty response)"
    except Exception as e:
        logger.exception("OpenAI call failed")
        return "⚠️ AI error."

# ---- Self-healing decorator ----
def self_heal(max_retries:int=2, retry_delay:float=1.0):
    def deco(fn:Callable):
        @wraps(fn)
        async def wrapper(*args, **kwargs):
            attempt = 0
            while attempt <= max_retries:
                try:
                    return await fn(*args, **kwargs)
                except Exception as exc:
                    attempt += 1
                    tb = traceback.format_exc()
                    logger.error("Exception in %s: %s", fn.__name__, tb)
                    # Ask AI for a concise suggestion to fix (read-only; no code patching)
                    if openai_client:
                        try:
                            prompt = f"Exception in function {fn.__name__}:\n{tb}\nProvide a concise suggestion to fix the issue and a short retry plan. Do not output code longer than 200 chars."
                            resp = openai_client.chat.completions.create(model="gpt-4o-mini", messages=[{"role":"system","content":"You are an expert python engineer."},{"role":"user","content":prompt}], max_tokens=200)
                            suggestion = resp.choices[0].message.content.strip()
                        except Exception:
                            suggestion = "AI diagnosis failed."
                    else:
                        suggestion = "OpenAI not available; cannot self-heal automatically."
                    logger.info("Self-heal suggestion: %s", suggestion)
                    # record into ledger/platform logs (admin can review)
                    conn = get_conn(); cur = conn.cursor(); cur.execute("INSERT INTO ledger (user_id,event,amount,metadata) VALUES (?,?,?,?)",(0,"self_heal_suggestion",0.0,json.dumps({"fn":fn.__name__,"attempt":attempt,"suggestion":suggestion}))); conn.commit(); conn.close()
                    if attempt > max_retries:
                        raise
                    time.sleep(retry_delay * attempt)
        return wrapper
    return deco

# ---- media helpers ----
def tts_save(text:str, lang:str="en")->Optional[str]:
    if gTTS is None: return None
    try:
        f = tempfile.NamedTemporaryFile(delete=False,suffix=".mp3")
        gTTS(text=text, lang=lang).save(f.name)
        return f.name
    except Exception:
        logger.exception("TTS failed"); return None

def download_youtube(url:str)->Optional[str]:
    if yt_dlp is None:
        logger.warning("yt-dlp not installed"); return None
    tmpdir = tempfile.gettempdir()
    outtmpl = os.path.join(tmpdir,"ytbot_%(id)s.%(ext)s")
    opts = {"format":"best","outtmpl":outtmpl,"noplaylist":True,"quiet":True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            fn = ydl.prepare_filename(info)
            if os.path.exists(fn): return fn
            base = fn.rsplit(".",1)[0]
            for ext in ("mp4","mkv","webm","m4a","mp3"):
                p = base+"."+ext
                if os.path.exists(p): return p
            return fn
    except Exception:
        logger.exception("yt download failed"); return None

def create_video_from_text(text:str, out_path:str)->Optional[str]:
    if not MOVIEPY_AVAILABLE:
        logger.warning("moviepy not available"); return None
    audio = tts_save(text)
    if not audio: return None
    try:
        aud = AudioFileClip(audio); dur = max(5,int(aud.duration)+1)
        bg = ColorClip(size=(720,1280), color=(20,20,20), duration=dur)
        try:
            txt = TextClip(text, fontsize=36, color="white", size=(680,None), method="caption").set_duration(dur).set_position(("center","center"))
            video = CompositeVideoClip([bg,txt]).set_audio(aud)
        except Exception:
            video = bg.set_audio(aud)
        video.write_videofile(out_path, fps=24, codec="libx264", audio_codec="aac", verbose=False, logger=None)
        try: os.remove(audio)
        except: pass
        return out_path
    except Exception:
        logger.exception("video creation failed"); return None

# ---- Payments: UPI deep-link + Cashfree (sample) ----
def generate_upi_deeplink(amount:float, note:str="", payee_vpa:Optional[str]=None)->str:
    vpa = payee_vpa or UPI_ID
    # UPI intent/deeplink format
    params = {"pa":vpa,"pn":"Merchant","tn":note or "Payment","am":f"{amount:.2f}","cu":"INR"}
    q = "&".join(f"{k}={requests.utils.quote(str(v))}" for k,v in params.items())
    deeplink = f"upi://pay?{q}"
    return deeplink

def cashfree_create_order(amount:float, order_id:str, customer_phone:str="", customer_email:str=""):
    """
    Create Cashfree order (sample). Requires CASHFREE_APP_ID and CASHFREE_SECRET.
    Use test keys in sandbox. Return payment link / order info or None.
    """
    if not (CASHFREE_APP_ID and CASHFREE_SECRET): return None
    # Acquire token (example for Cashfree new API)
    try:
        token_url = "https://api.cashfree.com/pg/orders"  # NOTE: check Cashfree docs for correct endpoint & headers
        headers = {"accept":"application/json","content-type":"application/json","x-client-id":CASHFREE_APP_ID,"x-client-secret":CASHFREE_SECRET}
        payload = {"customer_details": {"customer_id": str(order_id), "customer_email": customer_email, "customer_phone": customer_phone}, "order_meta": {"return_url": WEBHOOK_URL + "/cashfree_callback"}, "order_amount": amount, "order_id": order_id}
        r = requests.post(token_url, json=payload, headers=headers, timeout=20)
        if r.status_code in (200,201):
            return r.json()
        else:
            logger.error("Cashfree create order failed: %s %s", r.status_code, r.text)
            return None
    except Exception:
        logger.exception("Cashfree request error"); return None

# ---- Flask app + Telegram app setup ----
flask_app = Flask(__name__)
application = Application.builder().token(BOT_TOKEN).build()

# ---- Handlers (decorated with self_heal to attempt retry/diagnose) ----
MAIN_MENU = [[KeyboardButton("🤖 Chat")],[KeyboardButton("🎙 TTS"),KeyboardButton("📥 YouTube")],[KeyboardButton("💳 Payments"),KeyboardButton("🔗 Refer")]]
def main_markup(): return ReplyKeyboardMarkup(MAIN_MENU, resize_keyboard=True)

@self_heal(max_retries=2)
async def start_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    add_user(user.id, user.first_name or "", user.username or None)
    await update.message.reply_text(f"Hello {user.first_name or 'User'}! I'm your AI assistant. Use /chat or type anything.", reply_markup=main_markup())

@self_heal(max_retries=2)
async def chat_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; uid = user.id
    text = " ".join(context.args) if context.args else (update.message.text or "")
    if not text:
        await update.message.reply_text("Send some text to chat.", reply_markup=main_markup()); return
    allowed = charge_for_query(uid)
    if not allowed:
        await update.message.reply_text("Quota exhausted & insufficient wallet. Buy premium or top-up.", reply_markup=main_markup()); return
    save_conversation(uid,"user",text)
    await update.message.reply_text("⌛ Thinking...")
    ans = ask_openai(uid, text)
    save_conversation(uid,"assistant",ans)
    # platform revenue share
    credit_ledger(0, QUERY_CHARGE, "platform_query_revenue", {"from_user":uid})
    await update.message.reply_text(ans, reply_markup=main_markup())

@self_heal(max_retries=2)
async def tts_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args) if context.args else (update.message.text or "")
    if not text:
        await update.message.reply_text("Usage: /tts <text>"); return
    path = tts_save(text)
    if not path:
        await update.message.reply_text("TTS not available."); return
    await update.message.reply_voice(voice=open(path,"rb"))
    try: os.remove(path)
    except: pass

@self_heal(max_retries=2)
async def yt_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    url = " ".join(context.args) if context.args else (update.message.text or "")
    if not url:
        await update.message.reply_text("Usage: /yt <url>"); return
    await update.message.reply_text("Downloading... may take time.")
    path = download_youtube(url)
    if not path:
        await update.message.reply_text("Download failed or yt-dlp not installed."); return
    try:
        if path.lower().endswith((".mp4",".mkv",".webm")):
            await update.message.reply_video(video=open(path,"rb"))
        else:
            await update.message.reply_document(document=open(path,"rb"))
    except Exception:
        logger.exception("send failed")
        await update.message.reply_text("Failed to send file.")
    finally:
        try: os.remove(path)
        except: pass

@self_heal(max_retries=2)
async def pay_upi_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; uid = user.id
    amount = float(context.args[0]) if context.args else 1.0
    link = generate_upi_deeplink(amount, note=f"Payment from {uid}")
    # give user deeplink and QR suggestion
    await update.message.reply_text(f"UPI Payment Link:\n{link}\nIf your device supports UPI intents it will open UPI app. After paying, send proof (screenshot) to activate.")
    # record pending ledger note (platform may credit once proof verified)
    conn = get_conn(); cur = conn.cursor(); cur.execute("INSERT INTO ledger (user_id,event,amount,metadata) VALUES (?,?,?,?)",(uid,"upi_pending",amount,json.dumps({"link":link}))); conn.commit(); conn.close()

@self_heal(max_retries=2)
async def pay_cashfree_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; uid = user.id
    amount = float(context.args[0]) if context.args else 5.0
    order_id = f"CF{uid}{int(time.time())}"
    r = cashfree_create_order(amount, order_id, customer_phone="", customer_email="")
    if not r:
        await update.message.reply_text("Cashfree create order failed. Check keys.") ; return
    # response expected to contain payment link
    link = r.get("payment_link") or r.get("order_link") or r.get("order_url") or r.get("data",{}).get("payment_link")
    await update.message.reply_text(f"Pay here: {link}\nAfter payment Cashfree will callback the webhook (implement callback route if needed).")
    conn = get_conn(); cur = conn.cursor(); cur.execute("INSERT INTO ledger (user_id,event,amount,metadata) VALUES (?,?,?,?)",(uid,"cashfree_pending",amount,json.dumps({"order_id":order_id,"resp":r}))); conn.commit(); conn.close()

@self_heal(max_retries=2)
async def wallet_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; uid = user.id
    u = get_user(uid)
    if not u:
        await update.message.reply_text("Start first with /start"); return
    conn = get_conn(); cur = conn.cursor(); cur.execute("SELECT event,amount,metadata,ts FROM ledger WHERE user_id=? ORDER BY id DESC LIMIT 10",(uid,)); rows = cur.fetchall(); conn.close()
    lines = "\n".join([f"{r[3]} {r[0]} {r[1]}" for r in rows]) or "No recent entries."
    await update.message.reply_text(f"Wallet: {u.get('wallet',0.0):.4f}\nMonthly queries: {u.get('monthly_queries',0)}\n\nRecent:\n{lines}")

@self_heal(max_retries=2)
async def refer_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; uid = user.id
    u = get_user(uid)
    if not u: await update.message.reply_text("Start first /start"); return
    await update.message.reply_text(f"Your referral code: {u.get('referral_code')} — share it. Use /wallet to see referral bonus once processed.")

@self_heal(max_retries=2)
async def admin_report(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: await update.message.reply_text("Unauthorized"); return
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT SUM(amount) FROM ledger WHERE user_id=0"); total = cur.fetchone()[0] or 0.0
    cur.execute("SELECT COUNT(*) FROM users"); ucount = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM visits"); vcount = cur.fetchone()[0]
    conn.close()
    await update.message.reply_text(f"Platform revenue: {total:.4f}\nUsers: {ucount}\nVisits: {vcount}")

# fallback message handler
@self_heal(max_retries=2)
async def fallback_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; uid = user.id
    text = update.message.text or ""
    add_user(uid, user.first_name or "", user.username or None)
    allowed = charge_for_query(uid)
    if not allowed:
        await update.message.reply_text("Quota exceeded and insufficient wallet. Buy premium or top-up."); return
    save_conversation(uid,"user",text)
    await update.message.reply_text("⌛ Thinking...")
    ans = ask_openai(uid, text)
    save_conversation(uid,"assistant",ans)
    credit_ledger(0, QUERY_CHARGE, "platform_query_revenue", {"from_user":uid})
    await update.message.reply_text(ans)

# register handlers
application.add_handler(CommandHandler("start", start_handler))
application.add_handler(CommandHandler("chat", chat_handler))
application.add_handler(CommandHandler("tts", tts_handler))
application.add_handler(CommandHandler("yt", yt_handler))
application.add_handler(CommandHandler("pay_upi", pay_upi_handler))
application.add_handler(CommandHandler("pay_cashfree", pay_cashfree_handler))
application.add_handler(CommandHandler("wallet", wallet_handler))
application.add_handler(CommandHandler("refer", refer_handler))
application.add_handler(CommandHandler("admin_report", admin_report))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback_handler))

# ---- Flask endpoints: visit, cashfree callback, health ----
@flask_app.route("/visit", methods=["GET"])
def visit_endpoint():
    src = request.args.get("source","web")
    ip = request.remote_addr or request.headers.get("X-Forwarded-For","")
    ua = request.headers.get("User-Agent","")
    record_visit(src, ip, ua)
    return jsonify({"ok":True,"credited":AD_RATE_PER_VISIT}), 200

@flask_app.route("/cashfree_callback", methods=["POST"])
def cashfree_callback():
    # Cashfree will call with payment status; validate signatures as per their docs.
    data = request.get_json(force=True)
    # basic sample: expect order_id and status
    order_id = data.get("order_id") or data.get("reference_id") or data.get("orderId")
    status = data.get("order_status") or data.get("status") or data.get("txStatus")
    # find ledger pending entry and mark completed and credit user premium
    try:
        conn = get_conn(); cur = conn.cursor()
        # naive: search ledger metadata for order_id
        cur.execute("SELECT id,user_id,metadata FROM ledger WHERE metadata LIKE ? AND event LIKE ?", (f"%{order_id}%","%pending%"))
        row = cur.fetchone()
        if row:
            lid, uid, meta = row
            if status and status.lower() in ("paid","success","completed"):
                # credit user's wallet or mark premium
                credit_ledger(uid, 0.0, "cashfree_confirm", {"order":order_id,"status":status})
                cur.execute("UPDATE users SET is_premium=1 WHERE user_id=?", (uid,))
                conn.commit()
                conn.close()
                return jsonify({"ok":True,"status":"activated"}), 200
        conn.close()
        return jsonify({"ok":False}), 400
    except Exception:
        logger.exception("cashfree callback error"); return jsonify({"ok":False}), 500

@flask_app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok":True,"time":datetime.utcnow().isoformat()}), 200

# ---- Scheduler jobs ----
scheduler = BackgroundScheduler()
def monthly_reset():
    try:
        conn = get_conn(); cur = conn.cursor(); cur.execute("UPDATE users SET monthly_queries=0"); conn.commit(); conn.close()
        logger.info("Monthly queries reset")
    except Exception:
        logger.exception("monthly_reset failed")
scheduler.add_job(lambda: logger.info("heartbeat"), "interval", minutes=60)
scheduler.add_job(monthly_reset, "cron", day="1", hour=0, minute=5)
scheduler.start()

# ---- prepare DB + platform user ----
init_db()
conn = get_conn(); cur = conn.cursor()
cur.execute("INSERT OR IGNORE INTO users (user_id,first_name,username,created_at,referral_code,is_premium,wallet) VALUES (?,?,?,?,?,?,?)",
            (0,"Platform","platform",datetime.utcnow().isoformat(),"PLATFORM",1,0.0))
conn.commit(); conn.close()
logger.info("DB ready")

# ---- Telegram webhook entry for Flask ----
@flask_app.route(f"/{BOT_TOKEN}", methods=["POST"])
def telegram_webhook():
    try:
        update = Update.de_json(request.get_json(force=True), application.bot)
        application.update_queue.put_nowait(update)
        return "OK",200
    except Exception:
        logger.exception("webhook error"); return "ERR",500

# ---- run ----
def start():
    # If WEBHOOK_URL set, run webhook mode. Else run polling.
    if WEBHOOK_URL:
        logger.info("Running webhook mode on port %s", PORT)
        application.run_webhook(listen="0.0.0.0", port=PORT, url_path=BOT_TOKEN, webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}")
    else:
        logger.info("Running polling mode")
        application.run_polling()

if __name__ == "__main__":
    start()
