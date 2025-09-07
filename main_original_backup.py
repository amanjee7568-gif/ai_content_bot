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

load_dotenv(".env")

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
