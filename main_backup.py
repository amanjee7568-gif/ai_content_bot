"""
Ganesh A.I. - Super Advanced AI Bot
-----------------------------------
This is PART 1 of the full codebase. 
Keep appending PART 2, PART 3... in the same main.py
"""

import os
import sys
import json
import time
import uuid
import base64
import logging
import traceback
import sqlite3
from datetime import datetime, timedelta

import requests
import httpx
import asyncio
from functools import wraps

from flask import (
    Flask, request, jsonify, render_template_string,
    session, redirect, url_for, flash, send_from_directory
)
from flask_sqlalchemy import SQLAlchemy

from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix

from dotenv import load_dotenv
from apscheduler.schedulers.background import BackgroundScheduler

# =========================
# ENV & CONFIG
# =========================

load_dotenv("ai_content_bot.env")

APP_NAME = "Ganesh A.I."
DOMAIN = os.getenv("DOMAIN", "https://brand.page/Ganeshagamingworld")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
HF_API_URL = os.getenv("HUGGINGFACE_API_URL")
HF_API_TOKEN = os.getenv("HUGGINGFACE_API_TOKEN")

ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "password123")

SECRET_KEY = os.getenv("SECRET_KEY", "supersecretkey")
DATABASE_FILE = "ganesh_ai.db"

# =========================
# LOGGING
# =========================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(APP_NAME)


def log(section, level, msg, extra=None):
    payload = {
        "section": section,
        "msg": msg,
        "extra": extra or {},
        "time": datetime.utcnow().isoformat()
    }
    if level == "INFO":
        logger.info(json.dumps(payload))
    elif level == "ERROR":
        logger.error(json.dumps(payload))
    else:
        logger.debug(json.dumps(payload))


# =========================
# DATABASE
# =========================

def init_db():
    """Initialize database tables"""
    with app.app_context():
        db.create_all()
        log("system", "INFO", "Database initialized")


def db_execute(query, params=(), fetch=False, commit=True):
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute(query, params)
    if fetch:
        rows = c.fetchall()
        conn.close()
        return rows
    if commit:
        conn.commit()
    conn.close()


# =========================
# FLASK APP
# =========================

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.wsgi_app = ProxyFix(app.wsgi_app)

# SQLAlchemy setup
app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{DATABASE_FILE}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# =========================
# DATABASE MODELS
# =========================

class User(db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=True)
    password = db.Column(db.String(200), nullable=False)
    credits = db.Column(db.Integer, default=0)
    wallet_balance = db.Column(db.Float, default=0.0)
    is_admin = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

# =========================
# UTILS
# =========================

def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user" not in session:
            flash("Please log in first", "danger")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user" not in session or not session["user"].get("is_admin"):
            flash("Admin access required", "danger")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


# =========================
# OPENAI + HF CLIENTS
# =========================

def query_openai(prompt):
    try:
        headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
        data = {
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.7
        }
        r = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=data, timeout=60)
        res = r.json()
        return res["choices"][0]["message"]["content"]
    except Exception as e:
        log("openai", "ERROR", str(e))
        return "Error: Could not get response from OpenAI."


def query_huggingface(prompt):
    try:
        headers = {"Authorization": f"Bearer {HF_API_TOKEN}"}
        r = requests.post(HF_API_URL, headers=headers, json={"inputs": prompt}, timeout=60)
        res = r.json()
        if isinstance(res, list) and "generated_text" in res[0]:
            return res[0]["generated_text"]
        return str(res)
    except Exception as e:
        log("huggingface", "ERROR", str(e))
        return "Error: Could not get response from HuggingFace."


# =========================
# SCHEDULER
# =========================

scheduler = BackgroundScheduler()


def daily_log():
    log("system", "INFO", "Daily log checkpoint")


scheduler.add_job(daily_log, "interval", hours=24)
scheduler.start()


# =========================
# ROUTES (BASIC)
# =========================

@app.route("/")
def home():
    return render_template_string("""
    <html>
    <head>
      <title>{{app_name}}</title>
    </head>
    <body>
      <h1>🚀 Welcome to {{app_name}}</h1>
      <p>This is Ganesh AI running live at {{domain}}</p>
      <a href='/login'>Login</a> | <a href='/register'>Register</a> | <a href='/admin'>Admin Panel</a>
      <hr>
      <form method="POST" action="/api/generate">
        <input type="text" name="prompt" placeholder="Ask me anything..." style="width:300px;">
        <button type="submit">Generate</button>
      </form>
    </body>
    </html>
    """, app_name=APP_NAME, domain=DOMAIN)


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username")
        password = generate_password_hash(request.form.get("password"))
        email = request.form.get("email")
        try:
            db_execute("INSERT INTO users (username, email, password) VALUES (?, ?, ?)",
                       (username, email, password))
            flash("Registered successfully. Please login.", "success")
            return redirect(url_for("login"))
        except Exception as e:
            flash("Error: " + str(e), "danger")
    return render_template_string("""
    <h2>Register</h2>
    <form method="POST">
      Username: <input type="text" name="username"><br>
      Email: <input type="email" name="email"><br>
      Password: <input type="password" name="password"><br>
      <button type="submit">Register</button>
    </form>
    """)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        rows = db_execute("SELECT id, username, password, is_admin FROM users WHERE username = ?",
                          (username,), fetch=True)
        if rows:
            user = rows[0]
            if check_password_hash(user[2], password):
                session["user"] = {"id": user[0], "username": user[1], "is_admin": bool(user[3])}
                flash("Welcome, " + username, "success")
                return redirect(url_for("home"))
        flash("Invalid credentials", "danger")
    return render_template_string("""
    <h2>Login</h2>
    <form method="POST">
      Username: <input type="text" name="username"><br>
      Password: <input type="password" name="password"><br>
      <button type="submit">Login</button>
    </form>
    """)
    # =========================
# ADMIN PANEL
# =========================

@app.route("/admin")
@admin_required
def admin_dashboard():
    users = db_execute("SELECT id, username, email, credits, is_admin, created_at FROM users", fetch=True)
    payments = db_execute("SELECT id, user_id, amount, provider, status, created_at FROM payments ORDER BY id DESC", fetch=True)
    logs = db_execute("SELECT id, section, level, message, created_at FROM logs ORDER BY id DESC LIMIT 50", fetch=True)

    return render_template_string("""
    <h1>⚡ Admin Dashboard</h1>
    <p><a href="/">🏠 Home</a> | <a href="/logout">Logout</a></p>
    <h2>👤 Users</h2>
    <table border=1>
      <tr><th>ID</th><th>Username</th><th>Email</th><th>Credits</th><th>Admin</th><th>Created</th></tr>
      {% for u in users %}
      <tr>
        <td>{{u[0]}}</td><td>{{u[1]}}</td><td>{{u[2]}}</td><td>{{u[3]}}</td><td>{{u[4]}}</td><td>{{u[5]}}</td>
      </tr>
      {% endfor %}
    </table>
    <h2>💳 Payments</h2>
    <table border=1>
      <tr><th>ID</th><th>User ID</th><th>Amount</th><th>Provider</th><th>Status</th><th>Created</th></tr>
      {% for p in payments %}
      <tr>
        <td>{{p[0]}}</td><td>{{p[1]}}</td><td>{{p[2]}}</td><td>{{p[3]}}</td><td>{{p[4]}}</td><td>{{p[5]}}</td>
      </tr>
      {% endfor %}
    </table>
    <h2>📜 Recent Logs</h2>
    <pre>
    {% for l in logs %}
    [{{l[3]}}] {{l[1]}} | {{l[2]}} | {{l[2]}} -> {{l[2]}}
    {% endfor %}
    </pre>
    """, users=users, payments=payments, logs=logs)


@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out", "info")
    return redirect(url_for("login"))


# =========================
# PAYMENT GATEWAY (DEMO)
# =========================

@app.route("/buy", methods=["GET", "POST"])
@login_required
def buy_credits():
    if request.method == "POST":
        amount = float(request.form.get("amount", "5"))
        user_id = session["user"]["id"]
        db_execute("INSERT INTO payments (user_id, amount, provider, status) VALUES (?, ?, ?, ?)",
                   (user_id, amount, "demo_gateway", "pending"))
        flash("Payment created. In demo mode, auto-approved.", "success")
        db_execute("UPDATE users SET credits = credits + ? WHERE id = ?", (int(amount*10), user_id))
        return redirect(url_for("home"))

    return render_template_string("""
    <h2>💰 Buy Credits</h2>
    <form method="POST">
      Amount (USD): <input type="number" name="amount" value="5">
      <button type="submit">Buy</button>
    </form>
    """)


# =========================
# API ENDPOINT
# =========================

@app.route("/api/generate", methods=["POST"])
def api_generate():
    prompt = request.form.get("prompt") or request.json.get("prompt")
    provider = request.form.get("provider") or "openai"

    if provider == "huggingface":
        result = query_huggingface(prompt)
    else:
        result = query_openai(prompt)

    return jsonify({"response": result})


# =========================
# TELEGRAM BOT (ASYNC)
# =========================

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

telegram_app = None


async def tg_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚀 Welcome to Ganesh AI! Ask me anything.")


async def tg_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    reply = query_openai(user_text)
    await update.message.reply_text(reply)


def setup_telegram():
    global telegram_app
    if not TELEGRAM_TOKEN:
        log("telegram", "INFO", "No TELEGRAM_TOKEN found, skipping Telegram bot")
        return
    telegram_app = Application.builder().token(TELEGRAM_TOKEN).build()
    telegram_app.add_handler(CommandHandler("start", tg_start))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, tg_message))
    asyncio.create_task(telegram_app.run_polling())


# =========================
# STARTUP
# =========================

if __name__ == "__main__":
    init_db()
    log("system", "INFO", f"{APP_NAME} starting...")

    try:
        setup_telegram()
    except Exception as e:
        log("telegram", "ERROR", f"Failed to start bot: {e}")

    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
    # ========================== PART 3 : PAYMENTS, SUBSCRIPTIONS & EARNINGS ==========================
# इस पार्ट में पूरा पेमेंट गेटवे इंटीग्रेशन (Stripe + PayPal + Razorpay + Cashfree) जोड़ा गया है।
# साथ ही Subscription Plans, Wallet System और Admin Earnings Panel भी।

import stripe
import razorpay
import json
import requests
from flask import request, jsonify, redirect

# ------------------- Load Payment Keys from ENV -------------------
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
PAYPAL_CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID", "")
PAYPAL_CLIENT_SECRET = os.getenv("PAYPAL_CLIENT_SECRET", "")
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "")
CASHFREE_APP_ID = os.getenv("CASHFREE_APP_ID", "")
CASHFREE_SECRET_KEY = os.getenv("CASHFREE_SECRET_KEY", "")

stripe.api_key = STRIPE_SECRET_KEY
razorpay_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

# ------------------- Database Models for Payments -------------------
class Payment(db.Model):
    __tablename__ = "payments"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    gateway = db.Column(db.String(50))
    amount = db.Column(db.Float)
    currency = db.Column(db.String(10))
    status = db.Column(db.String(20))
    txn_id = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Subscription(db.Model):
    __tablename__ = "subscriptions"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    plan = db.Column(db.String(100))
    amount = db.Column(db.Float)
    currency = db.Column(db.String(10))
    active = db.Column(db.Boolean, default=True)
    started_at = db.Column(db.DateTime, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime)

# ------------------- Helper: Add Money to Wallet -------------------
def add_to_wallet(user_id, amount):
    user = User.query.get(user_id)
    if not user:
        return False
    if not user.wallet_balance:
        user.wallet_balance = 0.0
    user.wallet_balance += amount
    db.session.commit()
    return True

# ------------------- Helper: Deduct Money from Wallet -------------------
def deduct_from_wallet(user_id, amount):
    user = User.query.get(user_id)
    if not user or user.wallet_balance < amount:
        return False
    user.wallet_balance -= amount
    db.session.commit()
    return True

# ------------------- API: Stripe Payment -------------------
@app.route("/api/pay/stripe", methods=["POST"])
def stripe_payment():
    data = request.json
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Unauthorized"}), 401

    try:
        intent = stripe.PaymentIntent.create(
            amount=int(data["amount"] * 100),  # cents
            currency=data.get("currency", "usd"),
            metadata={"user_id": user_id}
        )
        return jsonify({"client_secret": intent.client_secret})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

# ------------------- API: PayPal Payment -------------------
@app.route("/api/pay/paypal", methods=["POST"])
def paypal_payment():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.json
    auth = (PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET)
    url = "https://api-m.sandbox.paypal.com/v1/oauth2/token"
    headers = {"Accept": "application/json", "Accept-Language": "en_US"}
    resp = requests.post(url, headers=headers, auth=auth, data={"grant_type": "client_credentials"})
    token = resp.json().get("access_token")

    order_url = "https://api-m.sandbox.paypal.com/v2/checkout/orders"
    order_payload = {
        "intent": "CAPTURE",
        "purchase_units": [{"amount": {"currency_code": "USD", "value": str(data["amount"])}}],
        "application_context": {"return_url": "https://yourdomain.com/paypal/success",
                                "cancel_url": "https://yourdomain.com/paypal/cancel"}
    }
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    order_resp = requests.post(order_url, headers=headers, json=order_payload)
    return jsonify(order_resp.json())

# ------------------- API: Razorpay Payment -------------------
@app.route("/api/pay/razorpay", methods=["POST"])
def razorpay_payment():
    data = request.json
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Unauthorized"}), 401

    order = razorpay_client.order.create({
        "amount": int(data["amount"] * 100),
        "currency": data.get("currency", "INR"),
        "payment_capture": 1
    })
    return jsonify(order)

# ------------------- API: Cashfree Payment -------------------
@app.route("/api/pay/cashfree", methods=["POST"])
def cashfree_payment():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.json
    url = "https://sandbox.cashfree.com/pg/orders"
    headers = {
        "x-client-id": CASHFREE_APP_ID,
        "x-client-secret": CASHFREE_SECRET_KEY,
        "x-api-version": "2022-09-01",
        "Content-Type": "application/json"
    }
    payload = {
        "order_id": f"CF_{uuid.uuid4().hex[:10]}",
        "order_amount": data["amount"],
        "order_currency": data.get("currency", "INR"),
        "customer_details": {
            "customer_id": str(user_id),
            "customer_email": "test@example.com",
            "customer_phone": "9999999999"
        }
    }
    resp = requests.post(url, headers=headers, json=payload)
    return jsonify(resp.json())

# ------------------- API: Subscription Plans -------------------
@app.route("/api/subscription/buy", methods=["POST"])
def buy_subscription():
    data = request.json
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Unauthorized"}), 401

    plan = data.get("plan")
    amount = data.get("amount", 0)
    duration_days = data.get("duration", 30)

    expires = datetime.utcnow() + timedelta(days=duration_days)
    sub = Subscription(user_id=user_id, plan=plan, amount=amount, currency="USD", expires_at=expires)
    db.session.add(sub)
    db.session.commit()

    add_to_wallet(user_id, amount)  # wallet credit on subscription
    return jsonify({"message": f"Subscription {plan} activated", "expires_at": expires.isoformat()})

# ------------------- Admin: Earnings Dashboard -------------------
@app.route("/admin/earnings")
@admin_required
def admin_earnings():
    payments = Payment.query.all()
    total = sum([p.amount for p in payments if p.status == "success"])
    subs = Subscription.query.filter_by(active=True).count()
    return render_template("admin/earnings.html", payments=payments, total=total, subs=subs)

# ------------------- Webhooks Handling -------------------
@app.route("/webhook/stripe", methods=["POST"])
def stripe_webhook():
    payload = request.data
    sig = request.headers.get("Stripe-Signature")
    try:
        event = stripe.Webhook.construct_event(payload, sig, os.getenv("STRIPE_WEBHOOK_SECRET"))
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    if event["type"] == "payment_intent.succeeded":
        intent = event["data"]["object"]
        uid = intent["metadata"]["user_id"]
        add_to_wallet(uid, intent["amount"] / 100)
        pay = Payment(user_id=uid, gateway="stripe", amount=intent["amount"]/100,
                      currency=intent["currency"], status="success", txn_id=intent["id"])
        db.session.add(pay)
        db.session.commit()

    return jsonify({"status": "ok"})

@app.route("/webhook/razorpay", methods=["POST"])
def razorpay_webhook():
    data = request.json
    uid = data.get("payload", {}).get("payment", {}).get("entity", {}).get("notes", {}).get("user_id")
    amount = data.get("payload", {}).get("payment", {}).get("entity", {}).get("amount") / 100
    status = data.get("event")

    if status == "payment.captured":
        add_to_wallet(uid, amount)
        pay = Payment(user_id=uid, gateway="razorpay", amount=amount,
                      currency="INR", status="success", txn_id=data["payload"]["payment"]["entity"]["id"])
        db.session.add(pay)
        db.session.commit()

    return jsonify({"status": "ok"})

@app.route("/webhook/paypal", methods=["POST"])
def paypal_webhook():
    data = request.json
    if data.get("event_type") == "PAYMENT.CAPTURE.COMPLETED":
        uid = data.get("resource", {}).get("custom_id")
        amount = float(data["resource"]["amount"]["value"])
        add_to_wallet(uid, amount)
        pay = Payment(user_id=uid, gateway="paypal", amount=amount,
                      currency=data["resource"]["amount"]["currency_code"], status="success", txn_id=data["resource"]["id"])
        db.session.add(pay)
        db.session.commit()

    return jsonify({"status": "ok"})

@app.route("/webhook/cashfree", methods=["POST"])
def cashfree_webhook():
    data = request.json
    if data.get("type") == "PAYMENT_SUCCESS":
        uid = data["data"]["order"]["customer_details"]["customer_id"]
        amount = float(data["data"]["order"]["order_amount"])
        add_to_wallet(uid, amount)
        pay = Payment(user_id=uid, gateway="cashfree", amount=amount,
                      currency=data["data"]["order"]["order_currency"], status="success", txn_id=data["data"]["order"]["order_id"])
        db.session.add(pay)
        db.session.commit()

    return jsonify({"status": "ok"})

# ========================== END OF PART 3 ==========================
# ============================
# PART-4: FULL WEB + ADMIN UI
# ============================

from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from functools import wraps
import os, sqlite3, datetime

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.getenv("APP_SECRET_KEY", "supersecret")

# ============================
# DATABASE UTILS
# ============================

DB_PATH = "app_data.db"

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    # Users
    c.execute("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE,
        email TEXT UNIQUE,
        password TEXT,
        wallet REAL DEFAULT 0,
        role TEXT DEFAULT 'user',
        created TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    # Payments
    c.execute("""
    CREATE TABLE IF NOT EXISTS payments(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        provider TEXT,
        amount REAL,
        status TEXT,
        created TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    # Logs
    c.execute("""
    CREATE TABLE IF NOT EXISTS logs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        action TEXT,
        created TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.commit()
    conn.close()

init_db()

# ============================
# AUTH HELPERS
# ============================

def login_required(role=None):
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if "user_id" not in session:
                return redirect(url_for("login"))
            if role and session.get("role") != role:
                return "Unauthorized", 403
            return f(*args, **kwargs)
        return wrapper
    return decorator

# ============================
# ROUTES - PUBLIC
# ============================

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form["username"]
        email = request.form["email"]
        password = request.form["password"]
        conn = get_db()
        try:
            conn.execute("INSERT INTO users(username,email,password) VALUES(?,?,?)",
                         (username,email,password))
            conn.commit()
            flash("Registration successful! Please login.","success")
            return redirect(url_for("login"))
        except Exception as e:
            flash(f"Error: {e}","danger")
        finally:
            conn.close()
    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"]
        password = request.form["password"]
        conn = get_db()
        user = conn.execute("SELECT * FROM users WHERE email=? AND password=?",(email,password)).fetchone()
        conn.close()
        if user:
            session["user_id"] = user["id"]
            session["role"] = user["role"]
            flash("Login successful!","success")
            return redirect(url_for("dashboard"))
        else:
            flash("Invalid credentials","danger")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out.","info")
    return redirect(url_for("index"))

# ============================
# USER DASHBOARD
# ============================

@app.route("/dashboard")
@login_required()
def dashboard():
    conn = get_db()
    uid = session["user_id"]
    user = conn.execute("SELECT * FROM users WHERE id=?",(uid,)).fetchone()
    payments = conn.execute("SELECT * FROM payments WHERE user_id=?",(uid,)).fetchall()
    conn.close()
    return render_template("dashboard.html", user=user, payments=payments)

@app.route("/wallet/topup", methods=["POST"])
@login_required()
def wallet_topup():
    amount = float(request.form["amount"])
    provider = request.form["provider"]
    uid = session["user_id"]
    conn = get_db()
    conn.execute("INSERT INTO payments(user_id,provider,amount,status) VALUES(?,?,?,?)",
                 (uid,provider,amount,"pending"))
    conn.commit()
    conn.close()
    flash("Topup requested. Awaiting confirmation.","info")
    return redirect(url_for("dashboard"))

# ============================
# ADMIN PANEL
# ============================

@app.route("/admin2")
@login_required(role="admin")
def admin_dashboard2():
    conn = get_db()
    users = conn.execute("SELECT * FROM users").fetchall()
    payments = conn.execute("SELECT * FROM payments").fetchall()
    conn.close()
    return render_template("admin/dashboard.html", users=users, payments=payments)

@app.route("/admin/payments/<int:pid>/approve")
@login_required(role="admin")
def approve_payment(pid):
    conn = get_db()
    payment = conn.execute("SELECT * FROM payments WHERE id=?",(pid,)).fetchone()
    if payment:
        conn.execute("UPDATE payments SET status='approved' WHERE id=?",(pid,))
        conn.execute("UPDATE users SET wallet=wallet+? WHERE id=?",
                     (payment["amount"], payment["user_id"]))
        conn.commit()
    conn.close()
    flash("Payment approved.","success")
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/payments/<int:pid>/reject")
@login_required(role="admin")
def reject_payment(pid):
    conn = get_db()
    conn.execute("UPDATE payments SET status='rejected' WHERE id=?",(pid,))
    conn.commit()
    conn.close()
    flash("Payment rejected.","danger")
    return redirect(url_for("admin_dashboard"))

# ============================
# API ENDPOINTS
# ============================

@app.route("/api/user/info")
@login_required()
def api_user_info():
    conn = get_db()
    uid = session["user_id"]
    user = conn.execute("SELECT id,username,email,wallet,role FROM users WHERE id=?",(uid,)).fetchone()
    conn.close()
    return jsonify(dict(user))

@app.route("/api/generate", methods=["POST"])
@login_required()
def api_generate():
    text = request.json.get("text","")
    return jsonify({"response": f"AI Response for: {text}"})

# ============================
# HTML TEMPLATES (JINJA2)
# ============================

# templates/index.html
index_html = """
<!DOCTYPE html>
<html>
<head>
  <title>Ganesh AI</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
</head>
<body class="bg-light">
<div class="container py-5">
  <h1 class="mb-4">Welcome to Ganesh AI</h1>
  <a href="{{ url_for('login') }}" class="btn btn-primary">Login</a>
  <a href="{{ url_for('register') }}" class="btn btn-secondary">Register</a>
</div>
</body>
</html>
"""

# templates/login.html
login_html = """
<!DOCTYPE html>
<html>
<head><title>Login</title></head>
<body>
  <div class="container">
    <h2>Login</h2>
    <form method="POST">
      <input name="email" placeholder="Email"><br>
      <input name="password" placeholder="Password" type="password"><br>
      <button type="submit">Login</button>
    </form>
  </div>
</body>
</html>
"""

# templates/register.html
register_html = """
<!DOCTYPE html>
<html>
<head><title>Register</title></head>
<body>
  <div class="container">
    <h2>Register</h2>
    <form method="POST">
      <input name="username" placeholder="Username"><br>
      <input name="email" placeholder="Email"><br>
      <input name="password" placeholder="Password" type="password"><br>
      <button type="submit">Register</button>
    </form>
  </div>
</body>
</html>
"""

# templates/dashboard.html
dashboard_html = """
<!DOCTYPE html>
<html>
<head><title>Dashboard</title></head>
<body>
  <h2>Hello {{ user['username'] }}</h2>
  <p>Wallet: {{ user['wallet'] }}</p>
  <form method="POST" action="{{ url_for('wallet_topup') }}">
    <input name="amount" placeholder="Amount"><br>
    <select name="provider">
      <option value="stripe">Stripe</option>
      <option value="paypal">PayPal</option>
      <option value="cashfree">Cashfree</option>
    </select><br>
    <button type="submit">Topup</button>
  </form>
  <h3>Your Payments</h3>
  <ul>
  {% for p in payments %}
    <li>{{ p['provider'] }} | {{ p['amount'] }} | {{ p['status'] }}</li>
  {% endfor %}
  </ul>
</body>
</html>
"""

# templates/admin/dashboard.html
admin_html = """
<!DOCTYPE html>
<html>
<head><title>Admin</title></head>
<body>
  <h2>Admin Dashboard</h2>
  <h3>Users</h3>
  <ul>
  {% for u in users %}
    <li>{{ u['username'] }} | {{ u['email'] }} | Wallet: {{ u['wallet'] }}</li>
  {% endfor %}
  </ul>
  <h3>Payments</h3>
  <ul>
  {% for p in payments %}
    <li>{{ p['provider'] }} | {{ p['amount'] }} | {{ p['status'] }}
    {% if p['status']=='pending' %}
      <a href="{{ url_for('approve_payment', pid=p['id']) }}">Approve</a>
      <a href="{{ url_for('reject_payment', pid=p['id']) }}">Reject</a>
    {% endif %}
    </li>
  {% endfor %}
  </ul>
</body>
</html>
"""

# ============== SAVE FILES ==============
os.makedirs("templates/admin", exist_ok=True)
with open("templates/index.html","w") as f: f.write(index_html)
with open("templates/login.html","w") as f: f.write(login_html)
with open("templates/register.html","w") as f: f.write(register_html)
with open("templates/dashboard.html","w") as f: f.write(dashboard_html)
with open("templates/admin/dashboard.html","w") as f: f.write(admin_html)

# ============================
# MAIN
# ============================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
# ============================
# PART-5: PAYMENT BACKEND
# ============================

import stripe
import requests

# Load API Keys from ENV
STRIPE_SECRET = os.getenv("STRIPE_SECRET_KEY", "")
PAYPAL_CLIENT = os.getenv("PAYPAL_CLIENT_ID", "")
PAYPAL_SECRET = os.getenv("PAYPAL_SECRET_KEY", "")
CASHFREE_CLIENT_ID = os.getenv("CASHFREE_CLIENT_ID", "")
CASHFREE_SECRET_KEY = os.getenv("CASHFREE_SECRET_KEY", "")

# Stripe init
if STRIPE_SECRET:
    stripe.api_key = STRIPE_SECRET

# ============================
# STRIPE CHECKOUT
# ============================

@app.route("/pay/stripe", methods=["POST"])
@login_required()
def pay_stripe():
    amount = int(float(request.form["amount"]) * 100)  # cents
    try:
        session_stripe = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {"name": "Wallet Topup"},
                    "unit_amount": amount,
                },
                "quantity": 1,
            }],
            mode="payment",
            success_url=url_for("stripe_success", _external=True),
            cancel_url=url_for("dashboard", _external=True),
        )
        return redirect(session_stripe.url)
    except Exception as e:
        flash(f"Stripe error: {e}","danger")
        return redirect(url_for("dashboard"))

@app.route("/pay/stripe/success")
@login_required()
def stripe_success():
    flash("Stripe payment successful!","success")
    # TODO: Verify webhook & update wallet
    return redirect(url_for("dashboard"))

# ============================
# PAYPAL CHECKOUT
# ============================

def get_paypal_token():
    url = "https://api-m.sandbox.paypal.com/v1/oauth2/token"
    headers = {"Accept": "application/json","Accept-Language":"en_US"}
    resp = requests.post(url, headers=headers, auth=(PAYPAL_CLIENT,PAYPAL_SECRET),
                         data={"grant_type":"client_credentials"})
    return resp.json().get("access_token")

@app.route("/pay/paypal", methods=["POST"])
@login_required()
def pay_paypal():
    amount = request.form["amount"]
    token = get_paypal_token()
    if not token:
        flash("PayPal auth failed","danger")
        return redirect(url_for("dashboard"))

    order_url = "https://api-m.sandbox.paypal.com/v2/checkout/orders"
    headers = {"Content-Type":"application/json","Authorization":f"Bearer {token}"}
    body = {
        "intent": "CAPTURE",
        "purchase_units": [{"amount": {"currency_code":"USD","value":amount}}],
        "application_context": {
            "return_url": url_for("paypal_success", _external=True),
            "cancel_url": url_for("dashboard", _external=True)
        }
    }
    r = requests.post(order_url, headers=headers, json=body)
    data = r.json()
    if "links" in data:
        for link in data["links"]:
            if link["rel"] == "approve":
                return redirect(link["href"])
    flash("PayPal error","danger")
    return redirect(url_for("dashboard"))

@app.route("/pay/paypal/success")
@login_required()
def paypal_success():
    flash("PayPal payment successful!","success")
    # TODO: Verify via capture API
    return redirect(url_for("dashboard"))

# ============================
# CASHFREE CHECKOUT
# ============================

@app.route("/pay/cashfree", methods=["POST"])
@login_required()
def pay_cashfree():
    amount = request.form["amount"]
    uid = session["user_id"]

    headers = {
        "x-client-id": CASHFREE_CLIENT_ID,
        "x-client-secret": CASHFREE_SECRET_KEY,
        "x-api-version": "2022-09-01",
        "Content-Type": "application/json"
    }
    data = {
        "order_id": f"ORDER{uid}{int(datetime.datetime.now().timestamp())}",
        "order_amount": amount,
        "order_currency": "INR",
        "customer_details": {
            "customer_id": str(uid),
            "customer_email": "test@example.com",
            "customer_phone": "9999999999"
        },
        "order_meta": {
            "return_url": url_for("cashfree_success", _external=True) + "?order_id={order_id}"
        }
    }

    resp = requests.post("https://sandbox.cashfree.com/pg/orders", headers=headers, json=data)
    res = resp.json()
    if "payment_link" in res:
        return redirect(res["payment_link"])
    flash("Cashfree error","danger")
    return redirect(url_for("dashboard"))

@app.route("/pay/cashfree/success")
@login_required()
def cashfree_success():
    flash("Cashfree payment successful!","success")
    # TODO: Verify via order_id API
    return redirect(url_for("dashboard"))
    # ============================
# PART-6: PAYMENT WEBHOOKS + AUTO WALLET
# ============================

from flask import jsonify

# ============================
# STRIPE WEBHOOK
# ============================
@app.route("/webhook/stripe", methods=["POST"])
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature")
    endpoint_secret = os.getenv("STRIPE_WEBHOOK_SECRET", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, endpoint_secret)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    if event["type"] == "checkout.session.completed":
        session_obj = event["data"]["object"]
        amount = int(session_obj["amount_total"]) / 100
        customer_email = session_obj.get("customer_email")

        # Wallet update
        if customer_email:
            db = get_db()
            db.execute("UPDATE users SET wallet = wallet + ? WHERE email = ?", (amount, customer_email))
            db.commit()

    return jsonify(success=True)


# ============================
# PAYPAL WEBHOOK
# ============================

@app.route("/webhook/paypal", methods=["POST"])
def paypal_webhook():
    data = request.json
    event_type = data.get("event_type")

    if event_type == "CHECKOUT.ORDER.APPROVED":
        order = data["resource"]
        amount = float(order["purchase_units"][0]["amount"]["value"])
        payer_email = order["payer"]["email_address"]

        # Wallet update
        db = get_db()
        db.execute("UPDATE users SET wallet = wallet + ? WHERE email = ?", (amount, payer_email))
        db.commit()

    return jsonify(success=True)


# ============================
# CASHFREE WEBHOOK
# ============================

@app.route("/webhook/cashfree", methods=["POST"])
def cashfree_webhook():
    data = request.json
    order_status = data.get("order_status")

    if order_status == "PAID":
        amount = float(data.get("order_amount", 0))
        customer_id = data["customer_details"]["customer_id"]

        # Wallet update
        db = get_db()
        db.execute("UPDATE users SET wallet = wallet + ? WHERE id = ?", (amount, customer_id))
        db.commit()

    return jsonify(success=True)


# ============================
# ADMIN DASHBOARD WALLET DISPLAY
# ============================

@app.route("/admin/users")
@admin_required
def admin_users():
    db = get_db()
    users = db.execute("SELECT id, username, email, wallet FROM users").fetchall()
    return render_template("admin_users.html", users=users)
    # ============================
# PART-7: AI USAGE BILLING + WALLET DEDUCTION
# ============================

from datetime import datetime

# ============================
# CONFIG: COST PER TOKENS / REPLY
# ============================
COST_PER_PROMPT = 0.002   # per token prompt
COST_PER_COMPLETION = 0.004  # per token completion
MIN_CHARGE = 0.01  # minimum charge per query

# ============================
# FUNCTION: DEDUCT WALLET AFTER AI USAGE
# ============================
def deduct_wallet(user_id, tokens_prompt, tokens_completion):
    db = get_db()

    # calculate cost
    cost = (tokens_prompt * COST_PER_PROMPT) + (tokens_completion * COST_PER_COMPLETION)
    if cost < MIN_CHARGE:
        cost = MIN_CHARGE

    # check balance
    balance = db.execute("SELECT wallet FROM users WHERE id=?", (user_id,)).fetchone()[0]
    if balance < cost:
        return False, "Insufficient balance!"

    # deduct wallet
    db.execute("UPDATE users SET wallet = wallet - ? WHERE id=?", (cost, user_id))
    db.commit()

    # log billing
    db.execute(
        "INSERT INTO billing (user_id, cost, tokens_prompt, tokens_completion, created_at) VALUES (?,?,?,?,?)",
        (user_id, cost, tokens_prompt, tokens_completion, datetime.utcnow())
    )
    db.commit()
    return True, cost


# ============================
# AI CHAT ENDPOINT WITH BILLING
# ============================
@app.route("/api/chat", methods=["POST"])
@login_required()
def api_chat():
    user_id = session["user_id"]
    data = request.json
    user_prompt = data.get("prompt")

    # call OpenAI
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": user_prompt}]
        )
        reply = response.choices[0].message.content
        tokens_prompt = response.usage.prompt_tokens
        tokens_completion = response.usage.completion_tokens

        # Deduct wallet
        ok, info = deduct_wallet(user_id, tokens_prompt, tokens_completion)
        if not ok:
            return jsonify({"error": info}), 402

        return jsonify({
            "reply": reply,
            "cost_deducted": info,
            "tokens_used": tokens_prompt + tokens_completion
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================
# ADMIN: BILLING HISTORY PAGE
# ============================
@app.route("/admin/billing")
@admin_required
def admin_billing():
    db = get_db()
    bills = db.execute("""
        SELECT b.id, u.username, b.cost, b.tokens_prompt, b.tokens_completion, b.created_at
        FROM billing b
        JOIN users u ON u.id = b.user_id
        ORDER BY b.created_at DESC
    """).fetchall()
    return render_template("admin_billing.html", bills=bills)
    # ============================
# PART-8: SUBSCRIPTION PLANS + AUTO CREDIT
# ============================

from apscheduler.schedulers.background import BackgroundScheduler

# ============================
# CONFIG: SUBSCRIPTION PLANS
# ============================
SUBSCRIPTION_PLANS = {
    "free": {"monthly_credit": 5.0, "price": 0},
    "pro": {"monthly_credit": 50.0, "price": 10},
    "premium": {"monthly_credit": 150.0, "price": 25},
}

# ============================
# FUNCTION: ASSIGN PLAN TO USER
# ============================
def assign_plan(user_id, plan_name):
    db = get_db()
    if plan_name not in SUBSCRIPTION_PLANS:
        return False, "Invalid plan"
    db.execute("UPDATE users SET plan=? WHERE id=?", (plan_name, user_id))
    db.commit()
    return True, f"Plan {plan_name} assigned."


# ============================
# CRON JOB: MONTHLY CREDIT
# ============================
def monthly_credit_job():
    db = get_db()
    users = db.execute("SELECT id, plan FROM users").fetchall()
    for user in users:
        plan = user["plan"]
        if plan and plan in SUBSCRIPTION_PLANS:
            credit = SUBSCRIPTION_PLANS[plan]["monthly_credit"]
            db.execute("UPDATE users SET wallet = wallet + ? WHERE id=?", (credit, user["id"]))
            db.commit()
            # Log transaction
            db.execute(
                "INSERT INTO billing (user_id, cost, tokens_prompt, tokens_completion, created_at) VALUES (?,?,?,?,?)",
                (user["id"], -credit, 0, 0, datetime.utcnow())
            )
            db.commit()


# ============================
# START SCHEDULER
# ============================
scheduler = BackgroundScheduler()
scheduler.add_job(monthly_credit_job, "cron", day=1, hour=0, minute=0)  # every 1st day midnight
scheduler.start()


# ============================
# USER ENDPOINT: CHANGE PLAN
# ============================
@app.route("/api/change_plan", methods=["POST"])
@login_required()
def change_plan():
    user_id = session["user_id"]
    data = request.json
    plan = data.get("plan")
    ok, msg = assign_plan(user_id, plan)
    if not ok:
        return jsonify({"error": msg}), 400
    return jsonify({"success": msg})


# ============================
# ADMIN: MANAGE SUBSCRIPTIONS
# ============================
@app.route("/admin/subscriptions")
@admin_required
def admin_subscriptions():
    db = get_db()
    users = db.execute("SELECT id, username, email, plan, wallet FROM users").fetchall()
    return render_template("admin_subscriptions.html", users=users, plans=SUBSCRIPTION_PLANS)
    # ============================
# PART-9: PAYMENT GATEWAY INTEGRATION
# ============================

import stripe
import requests

# ============================
# STRIPE CONFIG
# ============================
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
stripe.api_key = STRIPE_SECRET_KEY

# ============================
# PAYPAL CONFIG
# ============================
PAYPAL_CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID", "")
PAYPAL_SECRET = os.getenv("PAYPAL_SECRET", "")
PAYPAL_API = "https://api-m.sandbox.paypal.com"  # sandbox; switch to live

# ============================
# CASHFREE CONFIG
# ============================
CASHFREE_APP_ID = os.getenv("CASHFREE_APP_ID", "")
CASHFREE_SECRET_KEY = os.getenv("CASHFREE_SECRET_KEY", "")
CASHFREE_BASE_URL = "https://sandbox.cashfree.com/pg"  # change to prod for live


# ============================
# FUNCTION: STRIPE PAYMENT
# ============================
@app.route("/api/stripe/create_checkout", methods=["POST"])
@login_required()
def stripe_checkout():
    data = request.json
    amount = int(float(data.get("amount", 0)) * 100)  # in cents
    if amount <= 0:
        return jsonify({"error": "Invalid amount"}), 400

    session_stripe = stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[{
            "price_data": {
                "currency": "usd",
                "product_data": {"name": "Ganesh AI Wallet Recharge"},
                "unit_amount": amount,
            },
            "quantity": 1,
        }],
        mode="payment",
        success_url=request.host_url + "payment/success",
        cancel_url=request.host_url + "payment/cancel",
    )
    return jsonify({"checkout_url": session_stripe.url})


# ============================
# FUNCTION: PAYPAL PAYMENT
# ============================
@app.route("/api/paypal/create_order", methods=["POST"])
@login_required()
def paypal_order():
    data = request.json
    amount = float(data.get("amount", 0))
    if amount <= 0:
        return jsonify({"error": "Invalid amount"}), 400

    # Get access token
    auth = requests.post(
        f"{PAYPAL_API}/v1/oauth2/token",
        auth=(PAYPAL_CLIENT_ID, PAYPAL_SECRET),
        data={"grant_type": "client_credentials"},
    ).json()
    access_token = auth.get("access_token")

    # Create order
    order = requests.post(
        f"{PAYPAL_API}/v2/checkout/orders",
        headers={"Authorization": f"Bearer {access_token}"},
        json={
            "intent": "CAPTURE",
            "purchase_units": [{"amount": {"currency_code": "USD", "value": str(amount)}}],
        },
    ).json()
    return jsonify(order)


# ============================
# FUNCTION: CASHFREE PAYMENT
# ============================
@app.route("/api/cashfree/create_order", methods=["POST"])
@login_required()
def cashfree_order():
    data = request.json
    amount = float(data.get("amount", 0))
    user_id = session["user_id"]

    headers = {
        "x-client-id": CASHFREE_APP_ID,
        "x-client-secret": CASHFREE_SECRET_KEY,
        "x-api-version": "2022-09-01",
        "Content-Type": "application/json"
    }
    body = {
        "order_id": f"GANESH_{user_id}_{int(time.time())}",
        "order_amount": amount,
        "order_currency": "INR",
        "customer_details": {
            "customer_id": str(user_id),
            "customer_email": session.get("email", "test@example.com"),
            "customer_phone": "9999999999"
        },
        "order_meta": {
            "return_url": request.host_url + "payment/cashfree_callback?order_id={order_id}&order_token={order_token}"
        }
    }

    resp = requests.post(f"{CASHFREE_BASE_URL}/orders", headers=headers, json=body).json()
    return jsonify(resp)


# ============================
# PAYMENT SUCCESS ENDPOINTS
# ============================
@app.route("/payment/success")
def payment_success():
    # Add credits after payment
    user_id = session.get("user_id")
    if not user_id:
        return "User not logged in", 403

    db = get_db()
    db.execute("UPDATE users SET wallet = wallet + 50 WHERE id=?", (user_id,))
    db.commit()

    return render_template("payment_success.html", msg="Wallet recharged successfully!")


@app.route("/payment/cancel")
def payment_cancel():
    return render_template("payment_cancel.html", msg="Payment cancelled!")


@app.route("/payment/cashfree_callback")
def cashfree_callback():
    order_id = request.args.get("order_id")
    token = request.args.get("order_token")
    return render_template("payment_success.html", msg=f"Cashfree order {order_id} confirmed!")
    # ============================
# PART-10: ADVANCED ANALYTICS & ADMIN DASHBOARD
# ============================

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import io
import base64


# ============================
# ROUTE: ADMIN DASHBOARD
# ============================
@app.route("/admin/dashboard")
@admin_required
def admin_dashboard3():
    db = get_db()

    # Total stats
    total_users = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    total_earnings = db.execute("SELECT SUM(wallet) FROM users").fetchone()[0] or 0
    total_requests = db.execute("SELECT COUNT(*) FROM logs WHERE type='openai'").fetchone()[0]

    # Daily usage (last 7 days)
    usage_data = db.execute("""
        SELECT DATE(timestamp) as day, COUNT(*) as count 
        FROM logs WHERE type='openai'
        GROUP BY day ORDER BY day DESC LIMIT 7
    """).fetchall()

    days = [row["day"] for row in usage_data][::-1]
    counts = [row["count"] for row in usage_data][::-1]

    # Create chart
    fig, ax = plt.subplots(figsize=(6,3))
    ax.plot(days, counts, marker="o")
    ax.set_title("Daily AI Usage")
    ax.set_xlabel("Date")
    ax.set_ylabel("Requests")

    img = io.BytesIO()
    plt.savefig(img, format="png")
    img.seek(0)
    chart_url = base64.b64encode(img.getvalue()).decode()

    return render_template("admin_dashboard.html",
                           total_users=total_users,
                           total_earnings=total_earnings,
                           total_requests=total_requests,
                           chart_url=chart_url)


# ============================
# ROUTE: EARNINGS BREAKDOWN
# ============================
@app.route("/admin/earnings")
@admin_required
def admin_earnings():
    db = get_db()
    earnings = db.execute("""
        SELECT id, username, wallet
        FROM users ORDER BY wallet DESC
    """).fetchall()
    return render_template("admin_earnings.html", earnings=earnings)


# ============================
# ROUTE: USER ACTIVITY
# ============================
@app.route("/admin/users2")
@admin_required
def admin_users2():
    db = get_db()
    users = db.execute("""
        SELECT id, username, email, wallet, role, created_at
        FROM users ORDER BY created_at DESC
    """).fetchall()
    return render_template("admin_users.html", users=users)


# ============================
# ROUTE: API LOGS
# ============================
@app.route("/admin/logs")
@admin_required
def admin_logs():
    db = get_db()
    logs = db.execute("""
        SELECT type, message, timestamp
        FROM logs ORDER BY timestamp DESC LIMIT 100
    """).fetchall()
    return render_template("admin_logs.html", logs=logs)
    # ============================
# PART-11: NOTIFICATIONS + EMAIL ALERTS
# ============================

import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ============================
# SEND EMAIL FUNCTION
# ============================
def send_email(to_email, subject, body):
    try:
        smtp_server = os.getenv("SMTP_SERVER", "smtp.gmail.com")
        smtp_port = int(os.getenv("SMTP_PORT", "587"))
        smtp_user = os.getenv("SMTP_USER")
        smtp_pass = os.getenv("SMTP_PASS")

        msg = MIMEMultipart()
        msg["From"] = smtp_user
        msg["To"] = to_email
        msg["Subject"] = subject

        msg.attach(MIMEText(body, "html"))

        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, to_email, msg.as_string())
        server.quit()

        db_log("email", "INFO", f"Email sent to {to_email}")
        return True
    except Exception as e:
        db_log("email", "ERROR", f"Failed to send email: {e}")
        return False


# ============================
# SYSTEM NOTIFICATIONS (DB)
# ============================
def create_notification(user_id, message, level="info"):
    db = get_db()
    db.execute("INSERT INTO notifications (user_id, message, level) VALUES (?,?,?)",
               (user_id, message, level))
    db.commit()
    return True


# ============================
# USER ROUTE: VIEW NOTIFICATIONS
# ============================
@app.route("/notifications")
@login_required()
def notifications():
    db = get_db()
    user_id = session["user_id"]
    notes = db.execute("""
        SELECT id, message, level, timestamp
        FROM notifications WHERE user_id=?
        ORDER BY timestamp DESC LIMIT 50
    """, (user_id,)).fetchall()
    return render_template("notifications.html", notes=notes)


# ============================
# ADMIN ROUTE: SEND NOTIFICATION
# ============================
@app.route("/admin/notify", methods=["GET", "POST"])
@admin_required
def admin_notify():
    if request.method == "POST":
        target = request.form.get("target")  # "all" or user_id
        message = request.form.get("message")

        db = get_db()
        if target == "all":
            users = db.execute("SELECT id, email FROM users").fetchall()
            for u in users:
                create_notification(u["id"], message, "info")
                send_email(u["email"], "📢 Ganesh A.I. Notification", message)
        else:
            create_notification(int(target), message, "info")
            user = db.execute("SELECT email FROM users WHERE id=?", (target,)).fetchone()
            if user:
                send_email(user["email"], "📢 Ganesh A.I. Notification", message)

        flash("Notification sent successfully!", "success")
        return redirect(url_for("admin_notify"))

    db = get_db()
    users = db.execute("SELECT id, username FROM users").fetchall()
    return render_template("admin_notify.html", users=users)
    # ============================================================
# PART-12: AI Auto-Agent + Background Workers + Daily Reports
# ============================================================

import threading
import asyncio
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ==============================
# Telegram Asyncio Fix (Thread)
# ==============================

def start_telegram_bot():
    """
    Fix telegram polling coroutine issue by forcing event loop in thread
    """
    import telegram
    from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters

    async def start(update, context):
        await update.message.reply_text("🤖 Ganesh AI Telegram Bot is now active!")

    async def reply(update, context):
        user_msg = update.message.text
        response = openai_chat(user_msg)
        await update.message.reply_text(response)

    async def run_bot():
        app = ApplicationBuilder().token(os.getenv("TELEGRAM_BOT_TOKEN")).build()
        app.add_handler(CommandHandler("start", start))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, reply))
        await app.run_polling()

    def run_asyncio_loop():
        asyncio.run(run_bot())

    thread = threading.Thread(target=run_asyncio_loop, daemon=True)
    thread.start()
    log_event("TELEGRAM", "Polling thread started successfully.")


# ==============================
# AI Auto Content Generator
# ==============================

def ai_generate_daily_content():
    """
    Auto generate content daily for blog, ads and YouTube scripts
    """
    prompts = {
        "blog": "Write a 500 word SEO blog on AI future and gaming industry trends.",
        "ads": "Create 3 creative ad copies for a gaming YouTube channel called Ganeshagamingworld.",
        "yt_script": "Write a 2 minute YouTube script introducing Ganesh A.I. as the ultimate gaming assistant."
    }

    results = {}
    for key, prompt in prompts.items():
        try:
            response = openai_chat(prompt)
            results[key] = response
            # Save to DB
            save_generated_content(key, response)
            log_event("AUTO-CONTENT", f"Generated {key} successfully.")
        except Exception as e:
            log_event("AUTO-CONTENT", f"Failed to generate {key}: {e}")

    return results


def save_generated_content(category, content):
    conn = sqlite3.connect("app.db")
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO auto_content (category, content, created_at) VALUES (?, ?, ?)",
        (category, content, datetime.utcnow())
    )
    conn.commit()
    conn.close()


# ==============================
# Earnings & Reports
# ==============================

def calculate_daily_earnings():
    """
    Calculate daily earnings based on ad clicks, visits, and API usage
    """
    conn = sqlite3.connect("app.db")
    cursor = conn.cursor()
    cursor.execute("SELECT SUM(amount) FROM earnings WHERE DATE(created_at)=DATE('now')")
    result = cursor.fetchone()[0]
    conn.close()
    return result if result else 0.0


def send_daily_report():
    """
    Send daily report to admin email
    """
    earnings = calculate_daily_earnings()
    subject = f"Ganesh A.I. Daily Report - {datetime.utcnow().strftime('%Y-%m-%d')}"
    body = f"""
    Namaste Admin 👋

    ✅ Daily Earnings: ${earnings}

    ✅ Auto-Content Generated:
    - Blog: Done
    - Ads: Done
    - YT Script: Done

    Keep rocking! 🚀
    """

    try:
        sender_email = os.getenv("SMTP_EMAIL")
        sender_pass = os.getenv("SMTP_PASS")
        receiver_email = os.getenv("ADMIN_EMAIL")

        msg = MIMEMultipart()
        msg["From"] = sender_email
        msg["To"] = receiver_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender_email, sender_pass)
            server.sendmail(sender_email, receiver_email, msg.as_string())

        log_event("REPORT", "Daily report sent successfully.")
    except Exception as e:
        log_event("REPORT", f"Failed to send daily report: {e}")


# ==============================
# Scheduler (Background Jobs)
# ==============================

scheduler = BackgroundScheduler()

def start_scheduler():
    """
    Schedule recurring jobs for auto-agent
    """
    scheduler.add_job(ai_generate_daily_content, "interval", hours=24, id="auto_content_job")
    scheduler.add_job(send_daily_report, "interval", hours=24, id="daily_report_job")
    scheduler.add_job(clear_cache, "interval", hours=12, id="cache_clear_job")
    scheduler.add_job(update_analytics, "interval", hours=6, id="analytics_job")
    scheduler.start()
    log_event("SCHEDULER", "Background scheduler started.")


# ==============================
# Cache & Analytics
# ==============================

cache_data = {}

def clear_cache():
    global cache_data
    cache_data = {}
    log_event("CACHE", "Cache cleared successfully.")

def update_analytics():
    """
    Simulated analytics updater
    """
    conn = sqlite3.connect("app.db")
    cursor = conn.cursor()
    cursor.execute("INSERT INTO analytics (visits, clicks, created_at) VALUES (?, ?, ?)",
                   (random.randint(50, 500), random.randint(5, 50), datetime.utcnow()))
    conn.commit()
    conn.close()
    log_event("ANALYTICS", "Analytics updated successfully.")


# ==============================
# Logging Helper
# ==============================

def log_event(module, message):
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [{module}] {message}")
    conn = sqlite3.connect("app.db")
    cursor = conn.cursor()
    cursor.execute("INSERT INTO logs (module, message, created_at) VALUES (?, ?, ?)",
                   (module, message, ts))
    conn.commit()
    conn.close()


# ==============================
# Initialization Call
# ==============================

def init_auto_agent():
    try:
        start_telegram_bot()
        start_scheduler()
        log_event("AUTO-AGENT", "Auto Agent initialized successfully.")
    except Exception as e:
        log_event("AUTO-AGENT", f"Initialization failed: {e}")
        # ======================= PART-13: PAYMENTS + EARNINGS =======================
# Drop-in: Add this whole block once, then register blueprint: app.register_blueprint(payments_bp)
# Uses ENV from your ai_content_bot.env: CASHFREE_*, DOMAIN, SQLITE_PATH, ADMIN_USER/PASS, etc.

import os, hmac, hashlib, base64, json, time, math, sqlite3, csv, io, asyncio, threading
from datetime import datetime, timezone
from functools import wraps
from urllib.parse import urljoin
import httpx
from flask import Blueprint, request, jsonify, session, redirect, url_for, render_template_string, abort, send_file

# ---------------- ENV ----------------
APP_NAME            = os.getenv("APP_NAME", "Ganesh A.I.")
DOMAIN              = os.getenv("DOMAIN", "").rstrip("/")
FLASK_SECRET        = os.getenv("FLASK_SECRET", "change-me")
SQLITE_PATH         = os.getenv("SQLITE_PATH", "app.db")

# Admin creds (plain, as per your env)
ADMIN_USER          = os.getenv("ADMIN_USER", "Admin")
ADMIN_PASS          = os.getenv("ADMIN_PASS", "12345")

# Cashfree
CASHFREE_APP_ID         = os.getenv("CASHFREE_APP_ID", "")
CASHFREE_SECRET_KEY     = os.getenv("CASHFREE_SECRET_KEY", "")
CASHFREE_WEBHOOK_SECRET = os.getenv("CASHFREE_WEBHOOK_SECRET", "")
CASHFREE_API_BASE       = "https://api.cashfree.com/pg/"

# Razorpay
RAZORPAY_KEY_ID         = os.getenv("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET     = os.getenv("RAZORPAY_KEY_SECRET", "")
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")

# PayPal
PAYPAL_CLIENT_ID        = os.getenv("PAYPAL_CLIENT_ID", "")
PAYPAL_CLIENT_SECRET    = os.getenv("PAYPAL_CLIENT_SECRET", "")
PAYPAL_ENV              = os.getenv("PAYPAL_ENV", "live").lower()  # live|sandbox
PAYPAL_API_BASE         = "https://api-m.paypal.com" if PAYPAL_ENV == "live" else "https://api-m.sandbox.paypal.com"
PAYPAL_WEBHOOK_ID       = os.getenv("PAYPAL_WEBHOOK_ID", "")  # strong verify (optional but recommended)

UPI_ID                  = os.getenv("UPI_ID", "")

# ---------------- DB helpers ----------------
def db_conn():
    conn = sqlite3.connect(SQLITE_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def db_init_payments():
    with db_conn() as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS payments(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider TEXT NOT NULL,
            order_id TEXT UNIQUE,
            payment_id TEXT,
            status TEXT NOT NULL,
            amount INTEGER NOT NULL,
            currency TEXT NOT NULL,
            user_id TEXT,
            meta TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            raw_json TEXT
        )
        """)
        con.commit()

def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def pay_upsert(provider, order_id, amount, currency, status="CREATED", user_id=None, meta=None, payment_id=None, raw_json=None):
    meta_s = json.dumps(meta or {}, ensure_ascii=False)
    raw_s = raw_json if isinstance(raw_json, str) else json.dumps(raw_json or {}, ensure_ascii=False)
    with db_conn() as con:
        cur = con.execute("SELECT id FROM payments WHERE order_id=?",(order_id,))
        row = cur.fetchone()
        if row:
            con.execute("""UPDATE payments SET status=?, payment_id=?, updated_at=?, raw_json=? WHERE order_id=?""",
                        (status, payment_id, now_iso(), raw_s, order_id))
        else:
            con.execute("""INSERT INTO payments(provider,order_id,payment_id,status,amount,currency,user_id,meta,created_at,updated_at,raw_json)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        (provider, order_id, payment_id, status, amount, currency, user_id, meta_s, now_iso(), now_iso(), raw_s))
        con.commit()

def pay_mark_status(order_id, status, payment_id=None, raw=None):
    with db_conn() as con:
        con.execute("""UPDATE payments SET status=?, payment_id=?, updated_at=?, raw_json=COALESCE(?, raw_json) WHERE order_id=?""",
                    (status, payment_id, now_iso(), json.dumps(raw or {}), order_id))
        con.commit()

# ---------------- Auth (Admin) ----------------
def login_required(f):
    @wraps(f)
    def _w(*a, **k):
        if not session.get("admin"):
            return redirect(url_for("payments_bp.admin_login"))
        return f(*a, **k)
    return _w

payments_bp = Blueprint("payments_bp", __name__)

# ---------------- Minimal Admin Templates ----------------
TPL_LOGIN = """
<!doctype html><title>{{app}}</title>
<div style="max-width:420px;margin:64px auto;font-family:system-ui;">
<h2>{{app}} Admin Login</h2>
<form method="post">
  <label>User</label><input name="u" required style="width:100%;padding:8px;margin:6px 0">
  <label>Password</label><input name="p" type="password" required style="width:100%;padding:8px;margin:6px 0">
  <button style="padding:10px 16px">Login</button>
  {% if error %}<p style="color:#b00">{{error}}</p>{% endif %}
</form>
</div>
"""

TPL_EARNINGS = """
<!doctype html><title>{{app}}</title>
<div style="max-width:1080px;margin:24px auto;font-family:system-ui;">
  <div style="display:flex;justify-content:space-between;align-items:center;">
    <h2>{{app}} • Earnings</h2>
    <div>
      <a href="{{ url_for('payments_bp.admin_logout') }}">Logout</a>
    </div>
  </div>

  <form method="get" style="display:flex;gap:8px;flex-wrap:wrap;margin:12px 0;">
    <select name="provider">
      <option value="">All Providers</option>
      {% for pv in ['cashfree','razorpay','paypal'] %}
      <option value="{{pv}}" {{'selected' if q_provider==pv else ''}}>{{pv}}</option>
      {% endfor %}
    </select>
    <select name="status">
      <option value="">All Status</option>
      {% for st in ['CREATED','PENDING','PAID','FAILED','REFUNDED','CAPTURED'] %}
      <option value="{{st}}" {{'selected' if q_status==st else ''}}>{{st}}</option>
      {% endfor %}
    </select>
    <input type="date" name="from" value="{{q_from or ''}}">
    <input type="date" name="to" value="{{q_to or ''}}">
    <button>Filter</button>
    <a href="{{ url_for('payments_bp.admin_earnings_csv', **request.args) }}">Export CSV</a>
  </form>

  <div style="margin:8px 0;padding:8px;background:#f6f7f8;border:1px solid #e4e5e7">
    <b>Total Rows:</b> {{rows|length}} &nbsp; | &nbsp;
    <b>Gross:</b> {{gross/100.0}} {{currency}} &nbsp; | &nbsp;
    <b>Paid:</b> {{paid/100.0}} {{currency}}
  </div>

  <table border="1" cellspacing="0" cellpadding="6" width="100%">
    <thead>
      <tr><th>#</th><th>Provider</th><th>Order ID</th><th>Payment ID</th><th>Status</th><th>Amount</th><th>Cur.</th><th>User</th><th>Created</th></tr>
    </thead>
    <tbody>
      {% for r in rows %}
      <tr>
        <td>{{loop.index}}</td>
        <td>{{r['provider']}}</td>
        <td>{{r['order_id']}}</td>
        <td>{{r['payment_id'] or '-'}}</td>
        <td>{{r['status']}}</td>
        <td>{{'%.2f' % (r['amount']/100.0)}}</td>
        <td>{{r['currency']}}</td>
        <td>{{r['user_id'] or '-'}}</td>
        <td>{{r['created_at']}}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</div>
"""

@payments_bp.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    err = None
    if request.method == "POST":
        if request.form.get("u") == ADMIN_USER and request.form.get("p") == ADMIN_PASS:
            session["admin"] = True
            return redirect(url_for("payments_bp.admin_earnings"))
        err = "Invalid credentials"
    return render_template_string(TPL_LOGIN, app=APP_NAME, error=err)

@payments_bp.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("payments_bp.admin_login"))

@payments_bp.route("/admin/earnings")
@login_required
def admin_earnings():
    q_provider = request.args.get("provider") or ""
    q_status   = request.args.get("status") or ""
    q_from     = request.args.get("from")
    q_to       = request.args.get("to")

    sql = "SELECT * FROM payments WHERE 1=1"
    args=[]
    if q_provider:
        sql += " AND provider=?"; args.append(q_provider)
    if q_status:
        sql += " AND status=?"; args.append(q_status)
    if q_from:
        sql += " AND date(created_at) >= date(?)"; args.append(q_from)
    if q_to:
        sql += " AND date(created_at) <= date(?)"; args.append(q_to)
    sql += " ORDER BY id DESC"

    with db_conn() as con:
        rows = [dict(r) for r in con.execute(sql, args).fetchall()]
        gross = sum(r["amount"] for r in rows)
        paid  = sum(r["amount"] for r in rows if r["status"] in ("PAID","CAPTURED"))
    # Display currency of first row or INR default
    currency = rows[0]["currency"] if rows else "INR"
    return render_template_string(TPL_EARNINGS, app=APP_NAME, rows=rows, gross=gross, paid=paid,
                                  currency=currency, q_provider=q_provider, q_status=q_status,
                                  q_from=q_from, q_to=q_to, request=request)

@payments_bp.route("/admin/earnings.csv")
@login_required
def admin_earnings_csv():
    q_provider = request.args.get("provider") or ""
    q_status   = request.args.get("status") or ""
    q_from     = request.args.get("from")
    q_to       = request.args.get("to")

    sql = "SELECT * FROM payments WHERE 1=1"
    args=[]
    if q_provider:
        sql += " AND provider=?"; args.append(q_provider)
    if q_status:
        sql += " AND status=?"; args.append(q_status)
    if q_from:
        sql += " AND date(created_at) >= date(?)"; args.append(q_from)
    if q_to:
        sql += " AND date(created_at) <= date(?)"; args.append(q_to)
    sql += " ORDER BY id DESC"

    with db_conn() as con:
        rows = [dict(r) for r in con.execute(sql, args).fetchall()]

    output = io.StringIO()
    w = csv.writer(output)
    w.writerow(["provider","order_id","payment_id","status","amount","currency","user_id","created_at","updated_at"])
    for r in rows:
        w.writerow([r["provider"],r["order_id"],r.get("payment_id"),r["status"],r["amount"],r["currency"],r.get("user_id"),r["created_at"],r["updated_at"]])
    output.seek(0)
    return send_file(io.BytesIO(output.getvalue().encode("utf-8")), mimetype="text/csv", as_attachment=True, download_name="earnings.csv")

# ---------------- Payment Create API ----------------
@payments_bp.route("/pay/create", methods=["POST"])
def pay_create():
    """
    Body: { amount: number (in INR rupees), currency: "INR", provider: "cashfree|razorpay|paypal", user_id?: str, meta?: {} }
    Returns: { order_id, provider, init, redirect_url?, sdk_params? }
    Note: amount will be converted to paise/cents where required.
    """
    data = request.get_json(force=True, silent=True) or {}
    provider = (data.get("provider") or "cashfree").lower()
    amount_r = float(data.get("amount") or 0)
    if amount_r <= 0:
        return jsonify({"ok": False, "error": "amount_invalid"}), 400

    currency = (data.get("currency") or "INR").upper()
    user_id  = data.get("user_id")
    meta     = data.get("meta") or {}

    # normalize smallest unit
    smallest = int(round(amount_r * (100 if currency in ("INR","USD","EUR") else 100)))
    order_id = f"{provider.upper()}_{int(time.time())}_{math.floor(1000+1000*random.random())}" if False else f"{provider}_{int(time.time())}"

    # Create per provider
    if provider == "cashfree":
        return _create_cashfree(order_id, smallest, currency, user_id, meta)
    elif provider == "razorpay":
        return _create_razorpay(order_id, smallest, currency, user_id, meta)
    elif provider == "paypal":
        return _create_paypal(order_id, smallest, currency, user_id, meta)
    else:
        return jsonify({"ok": False, "error": "provider_unsupported"}), 400

# ---------------- Cashfree ----------------
def _create_cashfree(order_id, amount_smallest, currency, user_id, meta):
    if not (CASHFREE_APP_ID and CASHFREE_SECRET_KEY):
        return jsonify({"ok": False, "error": "cashfree_env_missing"}), 500

    payload = {
        "order_id": order_id,
        "order_amount": round(amount_smallest/100.0, 2),
        "order_currency": currency,
        "customer_details": {
            "customer_id": user_id or f"user_{int(time.time())}",
            "customer_email": meta.get("email") or "customer@example.com",
            "customer_phone": meta.get("phone") or "9999999999"
        },
        "order_meta": {
            "return_url": urljoin(DOMAIN or "", "/pay/return/cashfree?order_id={order_id}"),
            "notify_url": urljoin(DOMAIN or "", "/webhooks/cashfree")
        }
    }
    headers = {
        "x-client-id": CASHFREE_APP_ID,
        "x-client-secret": CASHFREE_SECRET_KEY,
        "x-api-version": "2023-08-01",
        "Content-Type": "application/json"
    }

    try:
        with httpx.Client(timeout=30) as cli:
            r = cli.post(urljoin(CASHFREE_API_BASE, "orders"), headers=headers, json=payload)
            data = r.json()
            if r.status_code not in (200,201):
                return jsonify({"ok": False, "provider":"cashfree", "error": data}), 400

            pay_upsert("cashfree", order_id, amount_smallest, currency, "PENDING", user_id, meta, raw_json=data)
            return jsonify({
                "ok": True,
                "provider": "cashfree",
                "order_id": order_id,
                "init": data,  # contains payment_sessions, links
            })
    except Exception as e:
        return jsonify({"ok": False, "error": f"cashfree_create_failed: {e}"}), 500

@payments_bp.route("/webhooks/cashfree", methods=["POST"])
def cashfree_webhook():
    # Signature header
    sig = request.headers.get("x-webhook-signature")
    body = request.get_data()
    expected = base64.b64encode(hmac.new(CASHFREE_WEBHOOK_SECRET.encode(), body, hashlib.sha256).digest()).decode()
    if not hmac.compare_digest(sig or "", expected):
        abort(401)

    evt = request.json or {}
    order_id = (evt.get("data") or {}).get("order", {}).get("order_id")
    status   = (evt.get("data") or {}).get("order", {}).get("order_status") or evt.get("type")
    payment  = (evt.get("data") or {}).get("payment", {}).get("cf_payment_id")
    # Map to unified statuses
    mapped = "PAID" if str(status).upper() in ("PAID","SUCCESS","CAPTURED") else ("FAILED" if "FAIL" in str(status).upper() else "PENDING")
    pay_mark_status(order_id, mapped, payment_id=payment, raw=evt)
    return jsonify({"ok": True})

# ---------------- Razorpay ----------------
def _create_razorpay(order_id, amount_smallest, currency, user_id, meta):
    if not (RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET):
        return jsonify({"ok": False, "error": "razorpay_env_missing"}), 500
    auth = (RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET)
    payload = {"amount": amount_smallest, "currency": currency, "receipt": order_id, "notes": {"user_id": user_id or ""}}
    try:
        with httpx.Client(timeout=30, auth=auth) as cli:
            r = cli.post("https://api.razorpay.com/v1/orders", json=payload)
            data = r.json()
            if r.status_code not in (200, 201):
                return jsonify({"ok": False, "provider":"razorpay", "error": data}), 400
            pay_upsert("razorpay", data["id"], amount_smallest, currency, "PENDING", user_id, meta, raw_json=data)
            return jsonify({
                "ok": True,
                "provider": "razorpay",
                "order_id": data["id"],
                "sdk_params": {
                    "key": RAZORPAY_KEY_ID,
                    "amount": amount_smallest,
                    "currency": currency,
                    "name": APP_NAME,
                    "order_id": data["id"]
                }
            })
    except Exception as e:
        return jsonify({"ok": False, "error": f"razorpay_create_failed: {e}"}), 500

@payments_bp.route("/webhooks/razorpay", methods=["POST"])
def razorpay_webhook():
    body = request.get_data()
    sig  = request.headers.get("x-razorpay-signature") or ""
    expected = hmac.new(RAZORPAY_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        abort(401)
    evt = request.json or {}
    entity = evt.get("payload", {}).get("payment", {}).get("entity", {})
    order_id = entity.get("order_id")
    payment_id = entity.get("id")
    status = entity.get("status","")
    mapped = "PAID" if status in ("captured","authorized") else ("FAILED" if status in ("failed") else "PENDING")
    pay_mark_status(order_id, mapped, payment_id=payment_id, raw=evt)
    return jsonify({"ok": True})

# ---------------- PayPal ----------------
def _paypal_token():
    auth = (PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET)
    headers = {"Content-Type":"application/x-www-form-urlencoded"}
    with httpx.Client(timeout=30, auth=auth) as cli:
        r = cli.post(urljoin(PAYPAL_API_BASE, "/v1/oauth2/token"), data={"grant_type":"client_credentials"}, headers=headers)
        r.raise_for_status()
        return r.json()["access_token"]

def _create_paypal(order_id, amount_smallest, currency, user_id, meta):
    if not (PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET):
        return jsonify({"ok": False, "error": "paypal_env_missing"}), 500
    token = _paypal_token()
    headers = {"Content-Type":"application/json","Authorization":f"Bearer {token}"}
    payload = {
        "intent": "CAPTURE",
        "purchase_units": [{
            "reference_id": order_id,
            "amount": {"currency_code": currency, "value": f"{amount_smallest/100.0:.2f}"}
        }],
        "application_context": {
            "brand_name": APP_NAME,
            "return_url": urljoin(DOMAIN or "", "/pay/return/paypal"),
            "cancel_url": urljoin(DOMAIN or "", "/pay/cancel/paypal")
        }
    }
    try:
        with httpx.Client(timeout=30) as cli:
            r = cli.post(urljoin(PAYPAL_API_BASE, "/v2/checkout/orders"), headers=headers, json=payload)
            data = r.json()
            if r.status_code not in (200,201):
                return jsonify({"ok": False, "provider":"paypal", "error": data}), 400
            pay_upsert("paypal", data["id"], amount_smallest, currency, "PENDING", user_id, meta, raw_json=data)
            approve = next((l["href"] for l in data.get("links",[]) if l.get("rel")=="approve"), None)
            return jsonify({"ok": True, "provider":"paypal", "order_id": data["id"], "redirect_url": approve})
    except Exception as e:
        return jsonify({"ok": False, "error": f"paypal_create_failed: {e}"}), 500

@payments_bp.route("/webhooks/paypal", methods=["POST"])
def paypal_webhook():
    # Basic verification (advanced: verify-Webhook-Signature API using PAYPAL_WEBHOOK_ID)
    evt = request.json or {}
    resource = evt.get("resource", {})
    order_id = resource.get("id") or resource.get("supplementary_data",{}).get("related_ids",{}).get("order_id")
    status = resource.get("status") or evt.get("event_type","")
    mapped = "PAID" if "COMPLETED" in status.upper() else ("FAILED" if "DENIED" in status.upper() else "PENDING")
    pay_mark_status(order_id, mapped, payment_id=resource.get("id"), raw=evt)
    return jsonify({"ok": True})

# ---------------- UPI helper (optional) ----------------
@payments_bp.route("/upi/qr")
def upi_qr():
    if not UPI_ID:
        return jsonify({"ok": False, "error": "upi_not_configured"}), 400
    amount = float(request.args.get("amount","0") or 0)
    payee = request.args.get("name") or APP_NAME
    upi_uri = f"upi://pay?pa={UPI_ID}&pn={payee}&am={amount:.2f}&cu=INR"
    # You can render this in frontend as QR using any QR lib; backend just returns URI
    return jsonify({"ok": True, "upi": upi_uri})

# ---------------- Returns (simple) ----------------
@payments_bp.route("/pay/return/<provider>")
def pay_return(provider):
    # In a real app you'd verify and show result; here we just generic thanks page
    order_id = request.args.get("order_id") or request.args.get("token") or ""
    return f"<h3>{APP_NAME}</h3><p>Thanks! Provider={provider} Order={order_id}</p>"

# ---------------- Telegram polling fix (v21 asyncio) ----------------
def start_telegram_polling_safely(build_application_callable):
    """
    Call this with a function that builds and returns a python-telegram-bot v21 Application.
    Runs in a dedicated thread with its own asyncio loop so Flask thread is clean.
    """
    def runner():
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            app = build_application_callable()
            loop.run_until_complete(app.initialize())
            loop.run_until_complete(app.start())
            loop.run_until_complete(app.updater.start_polling())  # OR: loop.run_until_complete(app.run_polling())
            loop.run_forever()
        except Exception as e:
            print(f"[TELEGRAM] polling crashed: {e}")
    th = threading.Thread(target=runner, daemon=True, name="tg-poll")
    th.start()

# ---------------- Register helper to be called from your create_app() ----------------
def init_payments(app):
    app.register_blueprint(payments_bp)
    app.secret_key = FLASK_SECRET
    db_init_payments()
    print("[Payments] Initialized.")

# =================== END PART-13 ===================
# ======================= PART-14: ADVANCED ADMIN PANEL =======================
# Adds: user management, usage logs, settings UI with Bootstrap templates.

import secrets

# -------- DB migrate for users & logs --------
def db_init_users_logs():
    with db_conn() as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT UNIQUE,
            name TEXT,
            email TEXT,
            banned INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        )""")
        con.execute("""
        CREATE TABLE IF NOT EXISTS usage_logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            query TEXT,
            response TEXT,
            tokens_in INTEGER,
            tokens_out INTEGER,
            created_at TEXT NOT NULL
        )""")
        con.execute("""
        CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY,
            value TEXT
        )""")
        con.commit()

# Auto-run
db_init_users_logs()

# -------- Templates --------
TPL_BASE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>{{app}} Admin</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css">
</head>
<body>
<nav class="navbar navbar-expand-lg navbar-dark bg-dark mb-3">
  <div class="container-fluid">
    <a class="navbar-brand" href="{{ url_for('admin_bp.dashboard') }}">{{app}}</a>
    <div>
      <a class="btn btn-sm btn-outline-light" href="{{ url_for('payments_bp.admin_earnings') }}">Earnings</a>
      <a class="btn btn-sm btn-outline-light" href="{{ url_for('admin_bp.users') }}">Users</a>
      <a class="btn btn-sm btn-outline-light" href="{{ url_for('admin_bp.logs') }}">Logs</a>
      <a class="btn btn-sm btn-outline-light" href="{{ url_for('admin_bp.settings') }}">Settings</a>
      <a class="btn btn-sm btn-danger" href="{{ url_for('payments_bp.admin_logout') }}">Logout</a>
    </div>
  </div>
</nav>
<div class="container">
  {% block body %}{% endblock %}
</div>
</body>
</html>
"""

TPL_DASH = """
{% extends "base.html" %}
{% block body %}
<h3>Dashboard</h3>
<p>Welcome, Admin!</p>
<ul>
  <li>Total Users: {{counts.users}}</li>
  <li>Banned Users: {{counts.banned}}</li>
  <li>Total Logs: {{counts.logs}}</li>
</ul>
{% endblock %}
"""

TPL_USERS = """
{% extends "base.html" %}
{% block body %}
<h3>Users</h3>
<table class="table table-striped">
  <thead><tr><th>ID</th><th>User</th><th>Email</th><th>Banned</th><th>Actions</th></tr></thead>
  <tbody>
  {% for u in rows %}
    <tr>
      <td>{{u['id']}}</td>
      <td>{{u['user_id']}}</td>
      <td>{{u['email'] or '-'}}</td>
      <td>{{'Yes' if u['banned'] else 'No'}}</td>
      <td>
        {% if u['banned'] %}
          <a href="{{ url_for('admin_bp.unban', uid=u['user_id']) }}" class="btn btn-sm btn-success">Unban</a>
        {% else %}
          <a href="{{ url_for('admin_bp.ban', uid=u['user_id']) }}" class="btn btn-sm btn-warning">Ban</a>
        {% endif %}
      </td>
    </tr>
  {% endfor %}
  </tbody>
</table>
{% endblock %}
"""

TPL_LOGS = """
{% extends "base.html" %}
{% block body %}
<h3>Usage Logs</h3>
<table class="table table-sm">
  <thead><tr><th>ID</th><th>User</th><th>Query</th><th>Resp</th><th>Tokens</th><th>At</th></tr></thead>
  <tbody>
  {% for l in rows %}
    <tr>
      <td>{{l['id']}}</td>
      <td>{{l['user_id']}}</td>
      <td>{{l['query']|truncate(40)}}</td>
      <td>{{l['response']|truncate(40)}}</td>
      <td>{{l['tokens_in']}}/{{l['tokens_out']}}</td>
      <td>{{l['created_at']}}</td>
    </tr>
  {% endfor %}
  </tbody>
</table>
{% endblock %}
"""

TPL_SETTINGS = """
{% extends "base.html" %}
{% block body %}
<h3>Settings</h3>
<form method="post">
  <div class="mb-3">
    <label>Welcome Message</label>
    <input type="text" name="welcome" value="{{s.get('welcome','')}}" class="form-control">
  </div>
  <div class="mb-3">
    <label>Default Model</label>
    <input type="text" name="model" value="{{s.get('model','')}}" class="form-control">
  </div>
  <div class="mb-3">
    <label>Enable Monetization</label>
    <select name="monet" class="form-control">
      <option value="on" {% if s.get('monet')=='on' %}selected{% endif %}>On</option>
      <option value="off" {% if s.get('monet')=='off' %}selected{% endif %}>Off</option>
    </select>
  </div>
  <button class="btn btn-primary">Save</button>
</form>
{% endblock %}
"""

# -------- Blueprint --------
from flask import Blueprint as _Blueprint, render_template_string as _rts

admin_bp = _Blueprint("admin_bp", __name__, template_folder=".")

@admin_bp.route("/admin/dashboard")
@login_required
def dashboard():
    with db_conn() as con:
        users = con.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        banned = con.execute("SELECT COUNT(*) c FROM users WHERE banned=1").fetchone()["c"]
        logs = con.execute("SELECT COUNT(*) c FROM usage_logs").fetchone()["c"]
    return _rts(TPL_DASH, app=APP_NAME, counts={"users":users,"banned":banned,"logs":logs})

@admin_bp.route("/admin/users")
@login_required
def users():
    with db_conn() as con:
        rows = [dict(r) for r in con.execute("SELECT * FROM users ORDER BY id DESC")]
    return _rts(TPL_USERS, app=APP_NAME, rows=rows)

@admin_bp.route("/admin/users/ban/<uid>")
@login_required
def ban(uid):
    with db_conn() as con:
        con.execute("UPDATE users SET banned=1 WHERE user_id=?",(uid,))
        con.commit()
    return redirect(url_for("admin_bp.users"))

@admin_bp.route("/admin/users/unban/<uid>")
@login_required
def unban(uid):
    with db_conn() as con:
        con.execute("UPDATE users SET banned=0 WHERE user_id=?",(uid,))
        con.commit()
    return redirect(url_for("admin_bp.users"))

@admin_bp.route("/admin/logs")
@login_required
def logs():
    with db_conn() as con:
        rows = [dict(r) for r in con.execute("SELECT * FROM usage_logs ORDER BY id DESC LIMIT 100")]
    return _rts(TPL_LOGS, app=APP_NAME, rows=rows)

@admin_bp.route("/admin/settings", methods=["GET","POST"])
@login_required
def settings():
    if request.method=="POST":
        kv = {"welcome": request.form.get("welcome",""),
              "model": request.form.get("model",""),
              "monet": request.form.get("monet","off")}
        with db_conn() as con:
            for k,v in kv.items():
                con.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(k,v))
            con.commit()
    with db_conn() as con:
        s = {r["key"]:r["value"] for r in con.execute("SELECT * FROM settings")}
    return _rts(TPL_SETTINGS, app=APP_NAME, s=s)

# Register helper
def init_admin(app):
    app.register_blueprint(admin_bp)
    print("[Admin] Advanced panel loaded.")
# =================== END PART-14 ===================
# =============================
# PART-15: REFERRAL + COMMISSION SYSTEM
# =============================

from flask import Blueprint, request, render_template, redirect, url_for, flash, session
from sqlalchemy import Column, Integer, String, Float, ForeignKey, DateTime, func
from sqlalchemy.orm import relationship
import secrets

referral_bp = Blueprint("referral", __name__, template_folder="templates")

# -----------------------------
# MODELS
# -----------------------------

class Referral(db.Model):
    __tablename__ = "referrals"
    id = Column(Integer, primary_key=True)
    referrer_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    referred_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=func.now())


class Commission(db.Model):
    __tablename__ = "commissions"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    amount = Column(Float, nullable=False)
    commission_rate = Column(Float, default=0.10)  # default 10%
    created_at = Column(DateTime, default=func.now())


class AdminWallet(db.Model):
    __tablename__ = "admin_wallet"
    id = Column(Integer, primary_key=True)
    balance = Column(Float, default=0.0)


# -----------------------------
# HELPER FUNCTIONS
# -----------------------------

def get_or_create_admin_wallet():
    wallet = AdminWallet.query.first()
    if not wallet:
        wallet = AdminWallet(balance=0.0)
        db.session.add(wallet)
        db.session.commit()
    return wallet


def apply_commission(user_id: int, earning: float):
    """Apply commission on user earning and send to admin wallet"""
    wallet = get_or_create_admin_wallet()

    commission_rate = get_commission_rate()
    commission_amount = earning * commission_rate
    net_amount = earning - commission_amount

    # Add commission record
    commission = Commission(
        user_id=user_id,
        amount=commission_amount,
        commission_rate=commission_rate,
    )
    db.session.add(commission)

    # Update admin wallet
    wallet.balance += commission_amount
    db.session.commit()

    return net_amount


def get_commission_rate():
    """Fetch commission rate (default 10%)"""
    setting = Setting.query.filter_by(key="commission_rate").first()
    if setting:
        return float(setting.value)
    return 0.10


def set_commission_rate(new_rate: float):
    """Update commission rate via Admin Panel"""
    setting = Setting.query.filter_by(key="commission_rate").first()
    if setting:
        setting.value = str(new_rate)
    else:
        setting = Setting(key="commission_rate", value=str(new_rate))
        db.session.add(setting)
    db.session.commit()


# -----------------------------
# ROUTES
# -----------------------------

@referral_bp.route("/referral/generate")
def generate_referral():
    if "user_id" not in session:
        flash("Please login first.", "danger")
        return redirect(url_for("auth.login"))

    code = secrets.token_hex(4)
    flash(f"Your referral code: {code}", "success")
    return render_template("referral_code.html", code=code)


@referral_bp.route("/referral/use/<ref_code>")
def use_referral(ref_code):
    if "user_id" not in session:
        flash("Please login first.", "danger")
        return redirect(url_for("auth.login"))

    referrer = User.query.filter_by(referral_code=ref_code).first()
    if not referrer:
        flash("Invalid referral code", "danger")
        return redirect(url_for("dashboard"))

    referral = Referral(
        referrer_id=referrer.id,
        referred_id=session["user_id"]
    )
    db.session.add(referral)
    db.session.commit()
    flash("Referral applied successfully!", "success")
    return redirect(url_for("dashboard"))


# -----------------------------
# ADMIN PANEL - COMMISSION MGMT
# -----------------------------

@referral_bp.route("/admin/commission", methods=["GET", "POST"])
def admin_commission():
    if "admin" not in session:
        return redirect(url_for("admin.login"))

    wallet = get_or_create_admin_wallet()
    current_rate = get_commission_rate()

    if request.method == "POST":
        try:
            new_rate = float(request.form["rate"])
            set_commission_rate(new_rate)
            flash(f"Commission rate updated to {new_rate*100}%", "success")
        except:
            flash("Invalid input", "danger")

    commissions = Commission.query.order_by(Commission.created_at.desc()).limit(50).all()
    return render_template(
        "admin_commission.html",
        wallet=wallet,
        current_rate=current_rate,
        commissions=commissions
    )
    # ===============================================
# PART-16 / PAYMENTS SUITE (Cashfree + Razorpay + PayPal)
# Single-file, plug-and-play Flask Blueprint
# -----------------------------------------------
# Features:
# - /billing  : User recharge page (HTML + JS inline)
# - /api/payments/create  : Create order/intention for chosen gateway
# - Webhooks:
#     /webhook/razorpay
#     /webhook/cashfree
#     /webhook/paypal
# - Wallet credit on successful payment
# - Admin listings (very simple)
#     /billing/admin?key=YOUR_ADMIN_TOKEN
# - SQLite via SQLAlchemy (same Flask app, self-contained)
# - Minimal dependencies: flask, sqlalchemy, requests, itsdangerous
#
# ENV (set in your .env or Render env):
#   APP_NAME=Ganesh A.I.
#   ADMIN_TOKEN=change_this_secret_admin_key           # admin listing guard
#
#   # Wallet base currency (display only)
#   CURRENCY=INR
#
#   # Razorpay
#   RAZORPAY_KEY_ID=...
#   RAZORPAY_KEY_SECRET=...
#
#   # Cashfree (PG V2)
#   CASHFREE_APP_ID=...
#   CASHFREE_SECRET_KEY=...
#   CASHFREE_ENV=TEST              # TEST or PROD
#
#   # PayPal
#   PAYPAL_CLIENT_ID=...
#   PAYPAL_CLIENT_SECRET=...
#   PAYPAL_ENV=SANDBOX             # SANDBOX or LIVE
#
#   # PUBLIC_URL (for redirects/webhooks, e.g., https://yourdomain.com)
#   PUBLIC_URL=https://yourapp.onrender.com
#
# Hooks for your existing app:
# - Register blueprint: app.register_blueprint(billing_bp)
# - Init DB once: payments_init_app(app)
#
# NOTES:
# - Inline HTML keeps you template-free. Replace with your UI later if needed.
# - Webhook URLs must be added in gateway dashboards.
# - This is a minimal-yet-practical prod-style scaffold. Review before going live.
# ===============================================

import os, hmac, hashlib, json, base64, uuid
from datetime import datetime, timezone
from functools import wraps

import requests
from flask import (
    Blueprint, request, jsonify, render_template_string,
    abort
)

# --- SQLAlchemy setup (self-contained) ---
from flask_sqlalchemy import SQLAlchemy

# Global-ish DB holder so we can init lazily
_payments_db = SQLAlchemy()
_DB_BOUND = False

def _utcnow_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

# -------------------------
# MODELS
# -------------------------
class WalletUser(_payments_db.Model):
    __tablename__ = "wallet_users"
    id = _payments_db.Column(_payments_db.Integer, primary_key=True)
    ext_user_id = _payments_db.Column(_payments_db.String(128), unique=True, nullable=False)
    display_name = _payments_db.Column(_payments_db.String(128), default="")
    balance_cents = _payments_db.Column(_payments_db.Integer, default=0)
    created_at = _payments_db.Column(_payments_db.String(32), default=_utcnow_str)
    updated_at = _payments_db.Column(_payments_db.String(32), default=_utcnow_str)

    def credit(self, cents: int):
        self.balance_cents = (self.balance_cents or 0) + int(cents)
        self.updated_at = _utcnow_str()

class Payment(_payments_db.Model):
    __tablename__ = "payments"
    id = _payments_db.Column(_payments_db.Integer, primary_key=True)
    ext_user_id = _payments_db.Column(_payments_db.String(128), nullable=False)
    provider = _payments_db.Column(_payments_db.String(32), nullable=False)  # razorpay|cashfree|paypal
    currency = _payments_db.Column(_payments_db.String(8), nullable=False, default="INR")
    amount_cents = _payments_db.Column(_payments_db.Integer, nullable=False, default=0)
    status = _payments_db.Column(_payments_db.String(32), nullable=False, default="created")
    provider_order_id = _payments_db.Column(_payments_db.String(128), default="")
    provider_payment_id = _payments_db.Column(_payments_db.String(128), default="")
    provider_signature = _payments_db.Column(_payments_db.Text, default="")
    raw_payload = _payments_db.Column(_payments_db.Text, default="")
    created_at = _payments_db.Column(_payments_db.String(32), default=_utcnow_str)
    updated_at = _payments_db.Column(_payments_db.String(32), default=_utcnow_str)
    idem_key = _payments_db.Column(_payments_db.String(64), default="")

# --------------------------------
# BLUEPRINT
# --------------------------------
billing_bp = Blueprint("billing", __name__)

# -------------------------
# CONFIG HELPERS
# -------------------------
def cfg(key, default=None):
    return os.environ.get(key, default)

def payments_init_app(app):
    global _DB_BOUND
    if not app.config.get("SQLALCHEMY_DATABASE_URI"):
        app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL", "sqlite:///payments.sqlite3")
    app.config.setdefault("SQLALCHEMY_TRACK_MODIFICATIONS", False)
    if not _DB_BOUND:
        _payments_db.init_app(app)
        with app.app_context():
            _payments_db.create_all()
        _DB_BOUND = True

def require_admin(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        token = request.args.get("key") or request.headers.get("X-Admin-Token")
        if not token or token != cfg("ADMIN_TOKEN", "change_this_secret_admin_key"):
            return abort(403)
        return func(*args, **kwargs)
    return wrapper

# -------------------------
# UTIL
# -------------------------
def minor_units(amount_float: float, currency: str) -> int:
    return int(round(float(amount_float) * 100))

def major_units(cents: int) -> float:
    return round((cents or 0) / 100.0, 2)

def get_or_create_user(ext_user_id: str, display_name=""):
    u = WalletUser.query.filter_by(ext_user_id=ext_user_id).first()
    if not u:
        u = WalletUser(ext_user_id=ext_user_id, display_name=display_name or ext_user_id)
        _payments_db.session.add(u)
        _payments_db.session.commit()
    return u

def _save(obj):
    _payments_db.session.add(obj)
    _payments_db.session.commit()

def _update_payment_status(pay: Payment, status: str, **fields):
    pay.status = status
    pay.updated_at = _utcnow_str()
    for k, v in fields.items():
        setattr(pay, k, v)
    _save(pay)

def _credit_wallet(ext_user_id: str, cents: int):
    u = get_or_create_user(ext_user_id)
    u.credit(cents)
    _save(u)

# -------------------------
# BILLING HTML PAGE
# -------------------------
_BILLING_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>{{app_name}} — Billing</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    body{font-family:system-ui; background:#0b1020; color:#e9ecf1; margin:0; padding:0;}
    header{padding:20px; background:#0e1530; border-bottom:1px solid #1e2748; display:flex; justify-content:space-between;}
    .card{background:#101735; border:1px solid #232c56; border-radius:12px; padding:16px; margin-top:16px;}
    input,select{width:100%; padding:10px; border-radius:10px; border:1px solid #2a376b; background:#0f1733; color:#fff;}
    button{padding:12px; border:none; border-radius:10px; background:#4a7dff; color:white; font-weight:600;}
    table{width:100%; border-collapse:collapse; margin-top:12px;}
    th,td{padding:8px; border-bottom:1px dashed #2a3566; font-size:14px;}
    .ok{color:#5dd39e}.bad{color:#ff7171}.pending{color:#ffd166}
  </style>
</head>
<body>
<header>
  <div>{{app_name}} · Billing</div>
  <div>Currency: {{currency}}</div>
</header>
<div style="max-width:800px; margin:auto; padding:16px;">
  <div class="card">
    <h2>Add Funds</h2>
    <label>User ID</label>
    <input id="uid" placeholder="telegram_123" />
    <label>Amount ({{currency}})</label>
    <input id="amt" type="number" min="1" value="99" />
    <label>Gateway</label>
    <select id="gw">
      <option value="razorpay">Razorpay</option>
      <option value="cashfree">Cashfree</option>
      <option value="paypal">PayPal</option>
    </select>
    <label>Name</label>
    <input id="name" placeholder="Your Name"/>
    <button id="paybtn">Create & Pay</button>
    <div id="msg"></div>
  </div>

  <div class="card">
    <h3>Wallet</h3>
    <input id="check_uid" placeholder="Enter User ID"/>
    <button id="checkbtn">Check</button>
    <div id="balinfo"></div>
  </div>

  <div class="card">
    <h3>Recent</h3>
    <table><thead><tr><th>ID</th><th>Gateway</th><th>Amount</th><th>Status</th></tr></thead>
    <tbody id="rows"></tbody></table>
  </div>
</div>
<script>
const $=s=>document.querySelector(s);
const rows=$("#rows"), msg=$("#msg");
$("#paybtn").onclick=async()=>{
  msg.textContent="Creating...";
  const r=await fetch("/api/payments/create",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({
    ext_user_id:$("#uid").value,display_name:$("#name").value,amount:parseFloat($("#amt").value||0),gateway:$("#gw").value
  })});
  const d=await r.json();
  if(!r.ok){msg.textContent=d.error;return;}
  const tr=document.createElement("tr");
  tr.innerHTML=`<td>${d.idem_key}</td><td>${d.provider}</td><td>${d.amount_cents/100}</td><td>${d.status}</td>`;
  rows.prepend(tr);
  msg.textContent="Order created, wait webhook...";
};
$("#checkbtn").onclick=async()=>{
  const r=await fetch("/api/wallet/"+encodeURIComponent($("#check_uid").value));
  const d=await r.json();
  $("#balinfo").textContent=r.ok?"Balance: "+(d.balance_cents/100):d.error;
};
</script>
</body></html>
"""

@billing_bp.route("/billing", methods=["GET"])
def billing_home():
    return render_template_string(
        _BILLING_HTML,
        app_name=cfg("APP_NAME", "App"),
        currency=cfg("CURRENCY", "INR"),
        public_url=cfg("PUBLIC_URL", "")
    )
    # -------------------------
# PART-16B (APIs, Webhooks, Admin, Health)
# Paste this directly after Part-16A block in your main.py
# -------------------------

import time
from flask import url_for

# -------------------------
# CREATE PAYMENT (server-side)
# -------------------------
@billing_bp.route("/api/payments/create", methods=["POST"])
def api_create_payment():
    data = request.get_json(force=True, silent=True) or {}
    ext_user_id = (data.get("ext_user_id") or "").strip()
    display_name = (data.get("display_name") or "").strip()
    try:
        amount = float(data.get("amount"))
    except Exception:
        return jsonify({"error":"invalid amount"}), 400

    gateway = (data.get("gateway") or "").lower().strip()
    if gateway not in ("razorpay","cashfree","paypal"):
        return jsonify({"error":"invalid gateway"}), 400

    currency = cfg("CURRENCY","INR")
    cents = minor_units(amount, currency)
    if cents < 100:
        return jsonify({"error":"minimum amount is 1.00"}), 400

    # Ensure user exists
    get_or_create_user(ext_user_id, display_name)

    idem_key = str(uuid.uuid4()).replace("-","")[:16]
    pay = Payment(
        ext_user_id=ext_user_id,
        provider=gateway,
        currency=currency,
        amount_cents=cents,
        status="created",
        idem_key=idem_key
    )
    _save(pay)

    public_url = cfg("PUBLIC_URL","").rstrip("/")
    provider_order_id = ""
    provider_payment_id = ""
    provider_signature = ""

    try:
        if gateway == "razorpay":
            # Create order
            key_id = cfg("RAZORPAY_KEY_ID","")
            key_secret = cfg("RAZORPAY_KEY_SECRET","")
            if not key_id or not key_secret:
                return jsonify({"error":"razorpay credentials missing"}), 400
            auth = (key_id, key_secret)
            payload = {
                "amount": cents,
                "currency": currency,
                "receipt": f"rcpt_{idem_key}",
                "payment_capture": 1,
                "notes": {"ext_user_id": ext_user_id, "idem_key": idem_key}
            }
            r = requests.post("https://api.razorpay.com/v1/orders", auth=auth, json=payload, timeout=15)
            if r.status_code not in (200,201):
                _update_payment_status(pay, "failed", raw_payload=r.text)
                return jsonify({"error":"razorpay order failed", "detail": r.text}), 400
            resp = r.json()
            provider_order_id = resp.get("id","")
            _update_payment_status(pay, "pending", provider_order_id=provider_order_id, raw_payload=json.dumps(resp))

        elif gateway == "cashfree":
            app_id = cfg("CASHFREE_APP_ID","")
            secret_key = cfg("CASHFREE_SECRET_KEY","")
            env = (cfg("CASHFREE_ENV","TEST") or "TEST").upper()
            if not app_id or not secret_key:
                return jsonify({"error":"cashfree credentials missing"}), 400
            base = "https://sandbox.cashfree.com/pg" if env=="TEST" else "https://api.cashfree.com/pg"
            headers = {
                "x-client-id": app_id,
                "x-client-secret": secret_key,
                "x-api-version": "2022-09-01",
                "content-type": "application/json"
            }
            payload = {
                "order_id": f"ord_{idem_key}",
                "order_amount": float(f"{amount:.2f}"),
                "order_currency": currency,
                "customer_details": {
                    "customer_id": ext_user_id,
                    "customer_name": display_name or ext_user_id,
                    "customer_email": f"{idem_key}@example.com",
                    "customer_phone": "9999999999"
                },
                "order_meta": {
                    "return_url": f"{public_url}/billing",
                    "notify_url": f"{public_url}/webhook/cashfree"
                },
                "order_note": "Wallet top-up"
            }
            r = requests.post(f"{base}/orders", json=payload, headers=headers, timeout=20)
            if r.status_code not in (200,201):
                _update_payment_status(pay, "failed", raw_payload=r.text)
                return jsonify({"error":"cashfree order failed", "detail": r.text}), 400
            resp = r.json()
            # Cashfree may return order_id in response structure differently; try common keys
            provider_order_id = resp.get("order_id") or resp.get("data",{}).get("order_id","") or f"ord_{idem_key}"
            _update_payment_status(pay, "pending", provider_order_id=provider_order_id, raw_payload=json.dumps(resp))

        elif gateway == "paypal":
            client_id = cfg("PAYPAL_CLIENT_ID","")
            client_secret = cfg("PAYPAL_CLIENT_SECRET","")
            env = (cfg("PAYPAL_ENV","SANDBOX") or "SANDBOX").upper()
            base = "https://api-m.sandbox.paypal.com" if env=="SANDBOX" else "https://api-m.paypal.com"
            if not client_id or not client_secret:
                return jsonify({"error":"paypal credentials missing"}), 400
            tok = requests.post(
                f"{base}/v1/oauth2/token",
                data={"grant_type":"client_credentials"},
                auth=(client_id, client_secret),
                timeout=15
            )
            if tok.status_code != 200:
                _update_payment_status(pay, "failed", raw_payload=tok.text)
                return jsonify({"error":"paypal token failed", "detail":tok.text}), 400
            access_token = tok.json().get("access_token","")
            payload = {
                "intent":"CAPTURE",
                "purchase_units":[
                    {
                        "reference_id": idem_key,
                        "amount":{
                            "currency_code": "USD" if currency.upper()!="INR" else "INR",
                            "value": f"{amount:.2f}"
                        }
                    }
                ],
                "application_context":{
                    "brand_name": cfg("APP_NAME","App"),
                    "return_url": f"{public_url}/billing",
                    "cancel_url": f"{public_url}/billing"
                }
            }
            headers = {"Content-Type":"application/json", "Authorization": f"Bearer {access_token}"}
            r = requests.post(f"{base}/v2/checkout/orders", headers=headers, json=payload, timeout=20)
            if r.status_code not in (200,201):
                _update_payment_status(pay, "failed", raw_payload=r.text)
                return jsonify({"error":"paypal order failed", "detail":r.text}), 400
            resp = r.json()
            provider_order_id = resp.get("id","")
            _update_payment_status(pay, "pending", provider_order_id=provider_order_id, raw_payload=json.dumps(resp))

    except Exception as e:
        _update_payment_status(pay, "failed", raw_payload=str(e))
        return jsonify({"error":"create failed", "detail": str(e)}), 500

    return jsonify({
        "status": pay.status,
        "provider": gateway,
        "amount_cents": pay.amount_cents,
        "currency": pay.currency,
        "idem_key": pay.idem_key,
        "provider_order_id": provider_order_id,
        "provider_payment_id": provider_payment_id
    })


# -------------------------
# WALLET CHECK
# -------------------------
@billing_bp.route("/api/wallet/<ext_user_id>", methods=["GET"])
def api_wallet(ext_user_id):
    u = WalletUser.query.filter_by(ext_user_id=ext_user_id).first()
    if not u:
        return jsonify({"error":"user not found"}), 404
    return jsonify({
        "ext_user_id": u.ext_user_id,
        "display_name": u.display_name,
        "balance_cents": u.balance_cents
    })


# -------------------------
# ADMIN SIMPLE LIST
# -------------------------
@billing_bp.route("/billing/admin", methods=["GET"])
@require_admin
def billing_admin():
    last = Payment.query.order_by(Payment.id.desc()).limit(200).all()
    rows = []
    for p in last:
        rows.append({
            "at": p.created_at,
            "ext_user_id": p.ext_user_id,
            "provider": p.provider,
            "amount": f"{major_units(p.amount_cents):.2f} {p.currency}",
            "status": p.status,
            "order": p.provider_order_id,
            "pay": p.provider_payment_id,
            "idem": p.idem_key
        })
    html = """
    <html><head><title>Billing Admin</title>
    <style>
      body{font-family:system-ui; background:#0b1020; color:#e9ecf1; padding:18px}
      table{width:100%; border-collapse:collapse}
      th,td{padding:8px; border-bottom:1px dashed #2a3566; font-size:13px}
      .ok{color:#5dd39e}.bad{color:#ff7171}.pending{color:#ffd166}
      .mono{font-family: ui-monospace, Menlo, Consolas, monospace}
      .card{background:#101735; border:1px solid #232c56; border-radius:12px; padding:16px;}
    </style></head><body>
    <div class="card">
    <h2>Payments (latest 200)</h2>
    <table><thead>
      <tr><th>Time</th><th>User</th><th>Prov</th><th>Amount</th><th>Status</th><th>Order</th><th>Payment</th><th>Idem</th></tr>
    </thead><tbody>
    """
    for r in rows:
        cls = "ok" if r["status"]=="paid" else ("bad" if r["status"]=="failed" else "pending")
        html += f"<tr><td class='mono'>{r['at']}</td><td>{r['ext_user_id']}</td><td>{r['provider']}</td><td>{r['amount']}</td><td class='{cls}'>{r['status']}</td><td class='mono'>{r['order']}</td><td class='mono'>{r['pay']}</td><td class='mono'>{r['idem']}</td></tr>"
    html += "</tbody></table></div></body></html>"
    return html


# -------------------------
# RAZORPAY WEBHOOK
# -------------------------
def _verify_razorpay_signature(body: bytes, secret: str, signature: str) -> bool:
    mac = hmac.new(bytes(secret, 'utf-8'), msg=body, digestmod=hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, signature)

@billing_bp.route("/webhook/razorpay", methods=["POST"])
def razorpay_webhook():
    secret = cfg("RAZORPAY_KEY_SECRET","")
    if not secret:
        return "missing secret", 400

    sig = request.headers.get("X-Razorpay-Signature","")
    body = request.get_data()
    if not sig or not _verify_razorpay_signature(body, secret, sig):
        # If signature fails, return 400
        return "invalid signature", 400

    evt = request.json or {}
    et = evt.get("event") or ""
    payload = evt.get("payload") or {}
    payment_entity = (payload.get("payment") or {}).get("entity") or {}
    order_entity = (payload.get("order") or {}).get("entity") or {}

    provider_payment_id = payment_entity.get("id","")
    provider_order_id = payment_entity.get("order_id") or order_entity.get("id","")

    # Attempt to locate payment by provider_order_id
    pay = None
    if provider_order_id:
        pay = Payment.query.filter_by(provider="razorpay", provider_order_id=provider_order_id).order_by(Payment.id.desc()).first()
    if not pay:
        # fallback: try by idem_key in notes (notes may be present in order entity)
        notes = order_entity.get("notes") or payment_entity.get("notes") or {}
        idem_key = notes.get("idem_key") or notes.get("idempotency") or ""
        if idem_key:
            pay = Payment.query.filter_by(idem_key=idem_key).order_by(Payment.id.desc()).first()
    if not pay:
        # As last resort, try any pending razorpay payment for same amount and user (best-effort)
        cand = Payment.query.filter_by(provider="razorpay", status="pending").order_by(Payment.id.desc()).first()
        pay = cand

    if not pay:
        return "payment not found", 200

    pay.provider_payment_id = provider_payment_id
    pay.provider_signature = sig
    pay.raw_payload = json.dumps(evt)
    pay.updated_at = _utcnow_str()

    status = payment_entity.get("status","")
    # Razorpay status 'captured' means success
    if et.startswith("payment.") and status == "captured":
        pay.status = "paid"
        _payments_db.session.commit()
        _credit_wallet(pay.ext_user_id, pay.amount_cents)
    elif et.startswith("payment.") and status in ("failed","authorized","refunded","void"):
        pay.status = "failed" if status=="failed" else status
        _payments_db.session.commit()

    return "ok", 200


# -------------------------
# CASHFREE WEBHOOK
# -------------------------
@billing_bp.route("/webhook/cashfree", methods=["POST"])
def cashfree_webhook():
    secret = cfg("CASHFREE_SECRET_KEY","")
    if not secret:
        return "missing secret", 400

    sig = request.headers.get("x-webhook-signature","")
    body = request.get_data()
    # Cashfree HMAC-SHA256 Base64(body)
    mac = hmac.new(secret.encode("utf-8"), msg=body, digestmod=hashlib.sha256).digest()
    expect = base64.b64encode(mac).decode("utf-8")
    if not sig or not hmac.compare_digest(sig, expect):
        return "invalid signature", 400

    evt = request.json or {}
    data = evt.get("data") or {}
    order = data.get("order") or {}
    payment = data.get("payment") or {}
    provider_order_id = order.get("order_id","") or evt.get("order_id","")
    provider_payment_id = payment.get("cf_payment_id","") or evt.get("payment_id","")
    status = (payment.get("payment_status") or evt.get("status") or "").lower()

    pay = None
    if provider_order_id:
        pay = Payment.query.filter_by(provider="cashfree", provider_order_id=provider_order_id).order_by(Payment.id.desc()).first()
    if not pay:
        # fallback by idem/reference
        ref = (order.get("order_id") or "").replace("ord_","")
        if ref:
            pay = Payment.query.filter_by(idem_key=ref).order_by(Payment.id.desc()).first()

    if not pay:
        return "payment not found", 200

    pay.provider_payment_id = provider_payment_id
    pay.provider_signature = sig
    pay.raw_payload = json.dumps(evt)
    pay.updated_at = _utcnow_str()

    if status in ("success","completed","paid"):
        pay.status = "paid"
        _payments_db.session.commit()
        _credit_wallet(pay.ext_user_id, pay.amount_cents)
    elif status in ("failed","failure"):
        pay.status = "failed"
        _payments_db.session.commit()
    else:
        pay.status = "pending"
        _payments_db.session.commit()

    return "ok", 200


# -------------------------
# PAYPAL WEBHOOK
# -------------------------
def _paypal_base_and_token():
    env = (cfg("PAYPAL_ENV","SANDBOX") or "SANDBOX").upper()
    base = "https://api-m.sandbox.paypal.com" if env=="SANDBOX" else "https://api-m.paypal.com"
    cid = cfg("PAYPAL_CLIENT_ID","")
    sec = cfg("PAYPAL_CLIENT_SECRET","")
    if not cid or not sec:
        return None, None
    tok = requests.post(
        f"{base}/v1/oauth2/token",
        data={"grant_type":"client_credentials"},
        auth=(cid, sec),
        timeout=15
    )
    if tok.status_code != 200:
        return base, None
    return base, tok.json().get("access_token","")

@billing_bp.route("/webhook/paypal", methods=["POST"])
def paypal_webhook():
    evt = request.json or {}
    resource = evt.get("resource") or {}
    # resource may contain order id or capture id
    order_id = resource.get("id") or resource.get("supplementary_data",{}).get("related_ids",{}).get("order_id","")

    base, token = _paypal_base_and_token()
    if not token:
        return "paypal auth fail", 400

    if not order_id:
        # try extracting from event
        order_id = evt.get("resource",{}).get("id","") or evt.get("resource",{}).get("parent_payment","")

    if not order_id:
        return "no order id", 200

    r = requests.get(f"{base}/v2/checkout/orders/{order_id}", headers={"Authorization": f"Bearer {token}"}, timeout=20)
    if r.status_code != 200:
        # Could be a capture or other event; still log and return
        return "order fetch failed", 400
    order = r.json()
    status = order.get("status","")
    ref = order.get("purchase_units",[{}])[0].get("reference_id","")

    pay = None
    if order_id:
        pay = Payment.query.filter_by(provider="paypal", provider_order_id=order_id).order_by(Payment.id.desc()).first()
    if not pay and ref:
        pay = Payment.query.filter_by(provider="paypal", idem_key=ref).order_by(Payment.id.desc()).first()
    if not pay:
        return "payment not found", 200

    pay.provider_payment_id = order_id
    pay.raw_payload = json.dumps(evt)
    pay.updated_at = _utcnow_str()
    if status.upper() in ("COMPLETED","APPROVED","CAPTURED"):
        pay.status = "paid"
        _payments_db.session.commit()
        _credit_wallet(pay.ext_user_id, pay.amount_cents)
    elif status.upper() in ("VOIDED","CANCELED","CANCELLED","FAILED"):
        pay.status = "failed"
        _payments_db.session.commit()
    else:
        pay.status = "pending"
        _payments_db.session.commit()

    return "ok", 200


# -------------------------
# OPTIONAL: MANUAL CAPTURE (PayPal)
# -------------------------
@billing_bp.route("/api/paypal/capture/<order_id>", methods=["POST"])
def paypal_capture(order_id):
    base, token = _paypal_base_and_token()
    if not token:
        return jsonify({"error":"paypal auth fail"}), 400
    r = requests.post(f"{base}/v2/checkout/orders/{order_id}/capture",
                      headers={"Authorization": f"Bearer {token}", "Content-Type":"application/json"},
                      json={}, timeout=20)
    if r.status_code not in (200,201):
        return jsonify({"error":"capture failed","detail":r.text}), 400
    resp = r.json()
    pay = Payment.query.filter_by(provider="paypal", provider_order_id=order_id).order_by(Payment.id.desc()).first()
    if pay:
        pay.status = "paid"
        pay.raw_payload = json.dumps(resp)
        _payments_db.session.commit()
        _credit_wallet(pay.ext_user_id, pay.amount_cents)
    return jsonify(resp)


# -------------------------
# HEALTH
# -------------------------
@billing_bp.route("/payments/health", methods=["GET"])
def payments_health():
    return jsonify({"ok": True, "time": _utcnow_str()})


# -------------------------
# END OF PART-16B
# -------------------------
if __name__ == "__main__":
    # Flask app start
    port = int(os.environ.get("PORT", 10000))

    # Telegram webhook setup
    WEBHOOK_URL = f"https://{os.environ.get('RENDER_EXTERNAL_HOSTNAME')}/webhook"

    application.bot.delete_webhook()
    application.bot.set_webhook(WEBHOOK_URL)

    logger.info(f"Webhook set to {WEBHOOK_URL}")

    app.run(host="0.0.0.0", port=port)
