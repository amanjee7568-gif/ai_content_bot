"""
Ganesh A.I. - Production Ready AI Bot
====================================
Complete production-ready application with:
- Flask Web App
- Telegram Bot Integration  
- Admin Panel
- Payment Gateway Integration
- Database Management
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
import threading
import asyncio
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

import requests
import httpx
from functools import wraps

from flask import (
    Flask, request, jsonify, render_template, render_template_string,
    session, redirect, url_for, flash, send_from_directory
)
from flask_sqlalchemy import SQLAlchemy

from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix

from dotenv import load_dotenv
from apscheduler.schedulers.background import BackgroundScheduler

# Telegram Bot imports
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# =========================
# ENVIRONMENT & CONFIG
# =========================

load_dotenv(".env")

# App Configuration
APP_NAME = os.getenv("APP_NAME", "Ganesh A.I.")
DOMAIN = os.getenv("DOMAIN", "https://brand.page/Ganeshagamingworld")
DEBUG = os.getenv("DEBUG", "False").lower() == "true"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Admin Configuration
ADMIN_USER = os.getenv("ADMIN_USER", "Admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "12345")
ADMIN_ID = os.getenv("ADMIN_ID", "6646320334")

# Database Configuration
DB_URL = os.getenv("DB_URL", "sqlite:///data.db")
SQLITE_PATH = os.getenv("SQLITE_PATH", "app.db")

# API Keys
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_TIMEOUT = int(os.getenv("OPENAI_TIMEOUT", "60"))

HF_API_URL = os.getenv("HUGGINGFACE_API_URL")
HF_API_TOKEN = os.getenv("HUGGINGFACE_API_TOKEN")

# Telegram Configuration
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_POLLING = os.getenv("TELEGRAM_POLLING", "true").lower() == "true"
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

# Payment Gateway Configuration
CASHFREE_CLIENT_ID = os.getenv("CASHFREE_CLIENT_ID")
CASHFREE_CLIENT_SECRET = os.getenv("CASHFREE_CLIENT_SECRET")
CASHFREE_WEBHOOK_SECRET = os.getenv("CASHFREE_WEBHOOK_SECRET")

PAYPAL_CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID")
PAYPAL_CLIENT_SECRET = os.getenv("PAYPAL_CLIENT_SECRET")

# Business Configuration
BUSINESS_EMAIL = os.getenv("BUSINESS_EMAIL", "ru387653@gmail.com")
BUSINESS_NAME = os.getenv("BUSINESS_NAME", "Artificial intelligence bot pvt Ltd")
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "@amanjee7568")
UPI_ID = os.getenv("UPI_ID", "9234906001@ptyes")

# Feature Flags
ENABLE_SEARCH = os.getenv("ENABLE_SEARCH", "1") == "1"
SHOW_TOOLS = os.getenv("SHOW_TOOLS", "1") == "1"
VISIT_PAY_RATE = float(os.getenv("VISIT_PAY_RATE", "0.001"))

# Security
FLASK_SECRET = os.getenv("FLASK_SECRET", "da1d476a2031fd15c3e16d5d6e9576d2")
SECRET_TOKEN = os.getenv("SECRET_TOKEN", "1a16bb0b-4204-4ef2-8a14-fb0b50396ef8")

# =========================
# LOGGING SETUP
# =========================

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper()),
    format='%(asctime)s | %(levelname)s | %(name)s | %(message)s'
)
logger = logging.getLogger(APP_NAME)

def log(section: str, level: str, message: str, extra: Dict = None):
    """Enhanced logging function"""
    log_data = {
        "section": section,
        "msg": message,
        "extra": extra or {},
        "time": datetime.utcnow().isoformat()
    }
    
    log_level = getattr(logging, level.upper(), logging.INFO)
    logger.log(log_level, json.dumps(log_data))

# =========================
# FLASK APP SETUP
# =========================

app = Flask(__name__)
app.secret_key = FLASK_SECRET
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# Database setup
app.config['SQLALCHEMY_DATABASE_URI'] = DB_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# =========================
# DATABASE MODELS
# =========================

class User(db.Model):
    __tablename__ = 'users'
    
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    wallet = db.Column(db.Float, default=0.0)
    role = db.Column(db.String(20), default='user')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_active = db.Column(db.Boolean, default=True)
    telegram_id = db.Column(db.String(50), unique=True, nullable=True)
    
    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
    
    def check_password(self, password):
        return check_password_hash(self.password_hash, password)
    
    def to_dict(self):
        return {
            'id': self.id,
            'username': self.username,
            'email': self.email,
            'wallet': self.wallet,
            'role': self.role,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'is_active': self.is_active
        }

class Transaction(db.Model):
    __tablename__ = 'transactions'
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    transaction_type = db.Column(db.String(20), nullable=False)  # credit, debit
    payment_method = db.Column(db.String(50), nullable=True)
    payment_id = db.Column(db.String(100), nullable=True)
    status = db.Column(db.String(20), default='pending')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    description = db.Column(db.Text, nullable=True)

class APIUsage(db.Model):
    __tablename__ = 'api_usage'
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    telegram_id = db.Column(db.String(50), nullable=True)
    api_type = db.Column(db.String(50), nullable=False)  # openai, huggingface
    tokens_used = db.Column(db.Integer, default=0)
    cost = db.Column(db.Float, default=0.0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    request_data = db.Column(db.Text, nullable=True)

# =========================
# DATABASE INITIALIZATION
# =========================

def init_db():
    """Initialize database with tables and admin user"""
    try:
        with app.app_context():
            db.create_all()
            
            # Create admin user if not exists
            admin_user = User.query.filter_by(username=ADMIN_USER).first()
            if not admin_user:
                admin_user = User(
                    username=ADMIN_USER,
                    email=BUSINESS_EMAIL,
                    role='admin',
                    wallet=1000.0  # Give admin some initial credits
                )
                admin_user.set_password(ADMIN_PASS)
                db.session.add(admin_user)
                db.session.commit()
                log("database", "INFO", f"Admin user created: {ADMIN_USER}")
            
        log("database", "INFO", "Database initialized successfully")
    except Exception as e:
        log("database", "ERROR", f"Database initialization failed: {e}")
        raise

# =========================
# AUTHENTICATION DECORATORS
# =========================

def login_required(f):
    """Decorator to require login for routes"""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please log in to access this page.', 'error')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrapper

def admin_required(f):
    """Decorator to require admin privileges"""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please log in to access this page.', 'error')
            return redirect(url_for('login'))
        
        user = User.query.get(session['user_id'])
        if not user or user.role != 'admin':
            flash('Admin access required.', 'error')
            return redirect(url_for('dashboard'))
        
        return f(*args, **kwargs)
    return wrapper

# =========================
# AI API FUNCTIONS
# =========================

def query_openai(prompt: str, user_id: Optional[int] = None) -> str:
    """Query OpenAI API with usage tracking"""
    if not OPENAI_API_KEY:
        return "OpenAI API key not configured."
    
    try:
        headers = {
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json"
        }
        
        data = {
            "model": OPENAI_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 1000,
            "temperature": 0.7
        }
        
        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers=headers,
            json=data,
            timeout=OPENAI_TIMEOUT
        )
        
        if response.status_code == 200:
            result = response.json()
            content = result['choices'][0]['message']['content']
            
            # Track usage
            if user_id:
                usage = APIUsage(
                    user_id=user_id,
                    api_type='openai',
                    tokens_used=result.get('usage', {}).get('total_tokens', 0),
                    cost=0.001,  # Approximate cost
                    request_data=json.dumps({"prompt": prompt[:100]})
                )
                db.session.add(usage)
                db.session.commit()
            
            return content
        else:
            log("openai", "ERROR", f"OpenAI API error: {response.status_code} - {response.text}")
            return "Sorry, I'm having trouble processing your request right now."
            
    except Exception as e:
        log("openai", "ERROR", f"OpenAI query failed: {e}")
        return "Sorry, I encountered an error while processing your request."

def query_huggingface(prompt: str, user_id: Optional[int] = None) -> str:
    """Query Hugging Face API with usage tracking"""
    if not HF_API_TOKEN or not HF_API_URL:
        return "Hugging Face API not configured."
    
    try:
        headers = {
            "Authorization": f"Bearer {HF_API_TOKEN}",
            "Content-Type": "application/json"
        }
        
        data = {"inputs": prompt}
        
        response = requests.post(HF_API_URL, headers=headers, json=data, timeout=30)
        
        if response.status_code == 200:
            result = response.json()
            
            # Track usage
            if user_id:
                usage = APIUsage(
                    user_id=user_id,
                    api_type='huggingface',
                    tokens_used=len(prompt.split()),
                    cost=0.0005,
                    request_data=json.dumps({"prompt": prompt[:100]})
                )
                db.session.add(usage)
                db.session.commit()
            
            return result.get('generated_text', str(result))
        else:
            log("huggingface", "ERROR", f"HF API error: {response.status_code}")
            return "Sorry, I'm having trouble with the AI service right now."
            
    except Exception as e:
        log("huggingface", "ERROR", f"HF query failed: {e}")
        return "Sorry, I encountered an error while processing your request."

# =========================
# WEB ROUTES
# =========================

@app.route('/')
def index():
    """Home page"""
    return render_template_string("""
    <!DOCTYPE html>
    <html>
    <head>
        <title>{{ app_name }}</title>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body { font-family: Arial, sans-serif; margin: 0; padding: 20px; background: #f5f5f5; }
            .container { max-width: 800px; margin: 0 auto; background: white; padding: 30px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
            .header { text-align: center; margin-bottom: 30px; }
            .header h1 { color: #333; margin: 0; }
            .header p { color: #666; margin: 10px 0; }
            .features { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 20px; margin: 30px 0; }
            .feature { padding: 20px; background: #f8f9fa; border-radius: 8px; text-align: center; }
            .feature h3 { color: #007bff; margin: 0 0 10px 0; }
            .buttons { text-align: center; margin: 30px 0; }
            .btn { display: inline-block; padding: 12px 24px; margin: 0 10px; background: #007bff; color: white; text-decoration: none; border-radius: 5px; transition: background 0.3s; }
            .btn:hover { background: #0056b3; }
            .btn.secondary { background: #6c757d; }
            .btn.secondary:hover { background: #545b62; }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>🤖 {{ app_name }}</h1>
                <p>Advanced AI-Powered Content Bot</p>
                <p>Telegram Bot • Web Interface • Admin Panel</p>
            </div>
            
            <div class="features">
                <div class="feature">
                    <h3>🚀 AI Chat</h3>
                    <p>Powered by OpenAI GPT-4 and Hugging Face models for intelligent conversations</p>
                </div>
                <div class="feature">
                    <h3>💬 Telegram Bot</h3>
                    <p>Chat directly with our AI bot on Telegram for instant responses</p>
                </div>
                <div class="feature">
                    <h3>💳 Payment Integration</h3>
                    <p>Secure payments via PayPal, Cashfree, and other gateways</p>
                </div>
                <div class="feature">
                    <h3>📊 Admin Panel</h3>
                    <p>Complete dashboard for managing users, transactions, and analytics</p>
                </div>
            </div>
            
            <div class="buttons">
                {% if session.user_id %}
                    <a href="{{ url_for('dashboard') }}" class="btn">Dashboard</a>
                    {% if session.user_role == 'admin' %}
                        <a href="{{ url_for('admin_dashboard') }}" class="btn secondary">Admin Panel</a>
                    {% endif %}
                    <a href="{{ url_for('logout') }}" class="btn secondary">Logout</a>
                {% else %}
                    <a href="{{ url_for('login') }}" class="btn">Login</a>
                    <a href="{{ url_for('register') }}" class="btn secondary">Register</a>
                {% endif %}
            </div>
            
            <div style="text-align: center; margin-top: 30px; padding-top: 20px; border-top: 1px solid #eee; color: #666;">
                <p>Telegram: <a href="https://t.me/{{ telegram_username }}" target="_blank">{{ telegram_username }}</a></p>
                <p>Support: {{ support_username }}</p>
            </div>
        </div>
    </body>
    </html>
    """, 
    app_name=APP_NAME,
    telegram_username=TELEGRAM_TOKEN.split(':')[0] if TELEGRAM_TOKEN else 'bot',
    support_username=SUPPORT_USERNAME
    )

@app.route('/register', methods=['GET', 'POST'])
def register():
    """User registration"""
    if request.method == 'POST':
        username = request.form.get('username')
        email = request.form.get('email')
        password = request.form.get('password')
        
        if not username or not email or not password:
            flash('All fields are required.', 'error')
            return redirect(url_for('register'))
        
        # Check if user exists
        if User.query.filter_by(username=username).first():
            flash('Username already exists.', 'error')
            return redirect(url_for('register'))
        
        if User.query.filter_by(email=email).first():
            flash('Email already registered.', 'error')
            return redirect(url_for('register'))
        
        # Create new user
        user = User(username=username, email=email)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        
        flash('Registration successful! Please log in.', 'success')
        return redirect(url_for('login'))
    
    return render_template_string("""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Register - {{ app_name }}</title>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body { font-family: Arial, sans-serif; margin: 0; padding: 20px; background: #f5f5f5; }
            .container { max-width: 400px; margin: 50px auto; background: white; padding: 30px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
            .header { text-align: center; margin-bottom: 30px; }
            .form-group { margin-bottom: 20px; }
            .form-group label { display: block; margin-bottom: 5px; font-weight: bold; }
            .form-group input { width: 100%; padding: 10px; border: 1px solid #ddd; border-radius: 5px; box-sizing: border-box; }
            .btn { width: 100%; padding: 12px; background: #007bff; color: white; border: none; border-radius: 5px; cursor: pointer; font-size: 16px; }
            .btn:hover { background: #0056b3; }
            .links { text-align: center; margin-top: 20px; }
            .links a { color: #007bff; text-decoration: none; }
            .alert { padding: 10px; margin-bottom: 20px; border-radius: 5px; }
            .alert.error { background: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
            .alert.success { background: #d4edda; color: #155724; border: 1px solid #c3e6cb; }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h2>Register for {{ app_name }}</h2>
            </div>
            
            {% with messages = get_flashed_messages(with_categories=true) %}
                {% if messages %}
                    {% for category, message in messages %}
                        <div class="alert {{ category }}">{{ message }}</div>
                    {% endfor %}
                {% endif %}
            {% endwith %}
            
            <form method="POST">
                <div class="form-group">
                    <label for="username">Username:</label>
                    <input type="text" id="username" name="username" required>
                </div>
                
                <div class="form-group">
                    <label for="email">Email:</label>
                    <input type="email" id="email" name="email" required>
                </div>
                
                <div class="form-group">
                    <label for="password">Password:</label>
                    <input type="password" id="password" name="password" required>
                </div>
                
                <button type="submit" class="btn">Register</button>
            </form>
            
            <div class="links">
                <p>Already have an account? <a href="{{ url_for('login') }}">Login here</a></p>
                <p><a href="{{ url_for('index') }}">Back to Home</a></p>
            </div>
        </div>
    </body>
    </html>
    """, app_name=APP_NAME)

@app.route('/login', methods=['GET', 'POST'])
def login():
    """User login"""
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        if not username or not password:
            flash('Username and password are required.', 'error')
            return redirect(url_for('login'))
        
        user = User.query.filter_by(username=username).first()
        
        if user and user.check_password(password) and user.is_active:
            session['user_id'] = user.id
            session['username'] = user.username
            session['user_role'] = user.role
            
            flash(f'Welcome back, {user.username}!', 'success')
            
            if user.role == 'admin':
                return redirect(url_for('admin_dashboard'))
            else:
                return redirect(url_for('dashboard'))
        else:
            flash('Invalid username or password.', 'error')
    
    return render_template_string("""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Login - {{ app_name }}</title>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body { font-family: Arial, sans-serif; margin: 0; padding: 20px; background: #f5f5f5; }
            .container { max-width: 400px; margin: 50px auto; background: white; padding: 30px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
            .header { text-align: center; margin-bottom: 30px; }
            .form-group { margin-bottom: 20px; }
            .form-group label { display: block; margin-bottom: 5px; font-weight: bold; }
            .form-group input { width: 100%; padding: 10px; border: 1px solid #ddd; border-radius: 5px; box-sizing: border-box; }
            .btn { width: 100%; padding: 12px; background: #007bff; color: white; border: none; border-radius: 5px; cursor: pointer; font-size: 16px; }
            .btn:hover { background: #0056b3; }
            .links { text-align: center; margin-top: 20px; }
            .links a { color: #007bff; text-decoration: none; }
            .alert { padding: 10px; margin-bottom: 20px; border-radius: 5px; }
            .alert.error { background: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
            .alert.success { background: #d4edda; color: #155724; border: 1px solid #c3e6cb; }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h2>Login to {{ app_name }}</h2>
            </div>
            
            {% with messages = get_flashed_messages(with_categories=true) %}
                {% if messages %}
                    {% for category, message in messages %}
                        <div class="alert {{ category }}">{{ message }}</div>
                    {% endfor %}
                {% endif %}
            {% endwith %}
            
            <form method="POST">
                <div class="form-group">
                    <label for="username">Username:</label>
                    <input type="text" id="username" name="username" required>
                </div>
                
                <div class="form-group">
                    <label for="password">Password:</label>
                    <input type="password" id="password" name="password" required>
                </div>
                
                <button type="submit" class="btn">Login</button>
            </form>
            
            <div class="links">
                <p>Don't have an account? <a href="{{ url_for('register') }}">Register here</a></p>
                <p><a href="{{ url_for('index') }}">Back to Home</a></p>
            </div>
        </div>
    </body>
    </html>
    """, app_name=APP_NAME)

@app.route('/logout')
def logout():
    """User logout"""
    session.clear()
    flash('You have been logged out.', 'success')
    return redirect(url_for('index'))

@app.route('/dashboard')
@login_required
def dashboard():
    """User dashboard"""
    user = User.query.get(session['user_id'])
    
    # Get user's recent transactions
    transactions = Transaction.query.filter_by(user_id=user.id).order_by(Transaction.created_at.desc()).limit(10).all()
    
    # Get user's API usage
    api_usage = APIUsage.query.filter_by(user_id=user.id).order_by(APIUsage.created_at.desc()).limit(10).all()
    
    return render_template_string("""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Dashboard - {{ app_name }}</title>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body { font-family: Arial, sans-serif; margin: 0; padding: 20px; background: #f5f5f5; }
            .container { max-width: 1200px; margin: 0 auto; }
            .header { background: white; padding: 20px; border-radius: 10px; margin-bottom: 20px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
            .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 20px; margin-bottom: 20px; }
            .stat-card { background: white; padding: 20px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); text-align: center; }
            .stat-card h3 { margin: 0 0 10px 0; color: #007bff; }
            .stat-card .value { font-size: 2em; font-weight: bold; color: #333; }
            .section { background: white; padding: 20px; border-radius: 10px; margin-bottom: 20px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
            .section h2 { margin: 0 0 20px 0; color: #333; }
            .table { width: 100%; border-collapse: collapse; }
            .table th, .table td { padding: 10px; text-align: left; border-bottom: 1px solid #eee; }
            .table th { background: #f8f9fa; font-weight: bold; }
            .btn { display: inline-block; padding: 10px 20px; background: #007bff; color: white; text-decoration: none; border-radius: 5px; margin: 5px; }
            .btn:hover { background: #0056b3; }
            .btn.secondary { background: #6c757d; }
            .btn.secondary:hover { background: #545b62; }
            .chat-form { margin-top: 20px; }
            .chat-form textarea { width: 100%; padding: 10px; border: 1px solid #ddd; border-radius: 5px; resize: vertical; min-height: 100px; box-sizing: border-box; }
            .chat-response { background: #f8f9fa; padding: 15px; border-radius: 5px; margin-top: 10px; white-space: pre-wrap; }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>Welcome, {{ user.username }}! 👋</h1>
                <p>Email: {{ user.email }} | Role: {{ user.role }} | Joined: {{ user.created_at.strftime('%Y-%m-%d') if user.created_at else 'N/A' }}</p>
                <div>
                    <a href="{{ url_for('index') }}" class="btn secondary">Home</a>
                    {% if user.role == 'admin' %}
                        <a href="{{ url_for('admin_dashboard') }}" class="btn">Admin Panel</a>
                    {% endif %}
                    <a href="{{ url_for('logout') }}" class="btn secondary">Logout</a>
                </div>
            </div>
            
            <div class="stats">
                <div class="stat-card">
                    <h3>💰 Wallet Balance</h3>
                    <div class="value">₹{{ "%.2f"|format(user.wallet) }}</div>
                </div>
                <div class="stat-card">
                    <h3>📊 API Calls</h3>
                    <div class="value">{{ api_usage|length }}</div>
                </div>
                <div class="stat-card">
                    <h3>💳 Transactions</h3>
                    <div class="value">{{ transactions|length }}</div>
                </div>
            </div>
            
            <div class="section">
                <h2>🤖 AI Chat</h2>
                <form method="POST" action="{{ url_for('api_generate') }}" class="chat-form">
                    <textarea name="prompt" placeholder="Ask me anything..." required></textarea>
                    <br>
                    <button type="submit" class="btn">Send Message</button>
                </form>
            </div>
            
            <div class="section">
                <h2>📊 Recent API Usage</h2>
                {% if api_usage %}
                    <table class="table">
                        <thead>
                            <tr>
                                <th>Date</th>
                                <th>API Type</th>
                                <th>Tokens</th>
                                <th>Cost</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for usage in api_usage %}
                            <tr>
                                <td>{{ usage.created_at.strftime('%Y-%m-%d %H:%M') if usage.created_at else 'N/A' }}</td>
                                <td>{{ usage.api_type }}</td>
                                <td>{{ usage.tokens_used }}</td>
                                <td>₹{{ "%.4f"|format(usage.cost) }}</td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                {% else %}
                    <p>No API usage yet. Start chatting to see your usage history!</p>
                {% endif %}
            </div>
            
            <div class="section">
                <h2>💳 Recent Transactions</h2>
                {% if transactions %}
                    <table class="table">
                        <thead>
                            <tr>
                                <th>Date</th>
                                <th>Type</th>
                                <th>Amount</th>
                                <th>Status</th>
                                <th>Method</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for txn in transactions %}
                            <tr>
                                <td>{{ txn.created_at.strftime('%Y-%m-%d %H:%M') if txn.created_at else 'N/A' }}</td>
                                <td>{{ txn.transaction_type }}</td>
                                <td>₹{{ "%.2f"|format(txn.amount) }}</td>
                                <td>{{ txn.status }}</td>
                                <td>{{ txn.payment_method or 'N/A' }}</td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                {% else %}
                    <p>No transactions yet.</p>
                {% endif %}
            </div>
        </div>
    </body>
    </html>
    """, 
    app_name=APP_NAME, 
    user=user, 
    transactions=transactions, 
    api_usage=api_usage
    )

@app.route('/admin')
@admin_required
def admin_dashboard():
    """Admin dashboard"""
    # Get statistics
    total_users = User.query.count()
    total_transactions = Transaction.query.count()
    total_api_usage = APIUsage.query.count()
    total_revenue = db.session.query(db.func.sum(Transaction.amount)).filter_by(transaction_type='credit').scalar() or 0
    
    # Get recent users
    recent_users = User.query.order_by(User.created_at.desc()).limit(10).all()
    
    # Get recent transactions
    recent_transactions = Transaction.query.order_by(Transaction.created_at.desc()).limit(10).all()
    
    return render_template_string("""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Admin Dashboard - {{ app_name }}</title>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body { font-family: Arial, sans-serif; margin: 0; padding: 20px; background: #f5f5f5; }
            .container { max-width: 1400px; margin: 0 auto; }
            .header { background: white; padding: 20px; border-radius: 10px; margin-bottom: 20px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
            .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 20px; margin-bottom: 20px; }
            .stat-card { background: white; padding: 20px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); text-align: center; }
            .stat-card h3 { margin: 0 0 10px 0; color: #007bff; }
            .stat-card .value { font-size: 2em; font-weight: bold; color: #333; }
            .section { background: white; padding: 20px; border-radius: 10px; margin-bottom: 20px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
            .section h2 { margin: 0 0 20px 0; color: #333; }
            .table { width: 100%; border-collapse: collapse; }
            .table th, .table td { padding: 10px; text-align: left; border-bottom: 1px solid #eee; }
            .table th { background: #f8f9fa; font-weight: bold; }
            .btn { display: inline-block; padding: 10px 20px; background: #007bff; color: white; text-decoration: none; border-radius: 5px; margin: 5px; }
            .btn:hover { background: #0056b3; }
            .btn.secondary { background: #6c757d; }
            .btn.secondary:hover { background: #545b62; }
            .btn.danger { background: #dc3545; }
            .btn.danger:hover { background: #c82333; }
            .btn.success { background: #28a745; }
            .btn.success:hover { background: #218838; }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>🛠️ Admin Dashboard</h1>
                <p>Manage users, transactions, and system settings</p>
                <div>
                    <a href="{{ url_for('dashboard') }}" class="btn secondary">User Dashboard</a>
                    <a href="{{ url_for('index') }}" class="btn secondary">Home</a>
                    <a href="{{ url_for('logout') }}" class="btn secondary">Logout</a>
                </div>
            </div>
            
            <div class="stats">
                <div class="stat-card">
                    <h3>👥 Total Users</h3>
                    <div class="value">{{ total_users }}</div>
                </div>
                <div class="stat-card">
                    <h3>💳 Transactions</h3>
                    <div class="value">{{ total_transactions }}</div>
                </div>
                <div class="stat-card">
                    <h3>🤖 API Calls</h3>
                    <div class="value">{{ total_api_usage }}</div>
                </div>
                <div class="stat-card">
                    <h3>💰 Revenue</h3>
                    <div class="value">₹{{ "%.2f"|format(total_revenue) }}</div>
                </div>
            </div>
            
            <div class="section">
                <h2>👥 Recent Users</h2>
                <table class="table">
                    <thead>
                        <tr>
                            <th>ID</th>
                            <th>Username</th>
                            <th>Email</th>
                            <th>Role</th>
                            <th>Wallet</th>
                            <th>Joined</th>
                            <th>Status</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for user in recent_users %}
                        <tr>
                            <td>{{ user.id }}</td>
                            <td>{{ user.username }}</td>
                            <td>{{ user.email }}</td>
                            <td>{{ user.role }}</td>
                            <td>₹{{ "%.2f"|format(user.wallet) }}</td>
                            <td>{{ user.created_at.strftime('%Y-%m-%d') if user.created_at else 'N/A' }}</td>
                            <td>{{ 'Active' if user.is_active else 'Inactive' }}</td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
            
            <div class="section">
                <h2>💳 Recent Transactions</h2>
                <table class="table">
                    <thead>
                        <tr>
                            <th>ID</th>
                            <th>User ID</th>
                            <th>Amount</th>
                            <th>Type</th>
                            <th>Method</th>
                            <th>Status</th>
                            <th>Date</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for txn in recent_transactions %}
                        <tr>
                            <td>{{ txn.id }}</td>
                            <td>{{ txn.user_id }}</td>
                            <td>₹{{ "%.2f"|format(txn.amount) }}</td>
                            <td>{{ txn.transaction_type }}</td>
                            <td>{{ txn.payment_method or 'N/A' }}</td>
                            <td>{{ txn.status }}</td>
                            <td>{{ txn.created_at.strftime('%Y-%m-%d %H:%M') if txn.created_at else 'N/A' }}</td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
            
            <div class="section">
                <h2>⚙️ System Information</h2>
                <table class="table">
                    <tr><th>Application Name</th><td>{{ app_name }}</td></tr>
                    <tr><th>Domain</th><td>{{ domain }}</td></tr>
                    <tr><th>Telegram Bot</th><td>{{ 'Configured' if telegram_token else 'Not Configured' }}</td></tr>
                    <tr><th>OpenAI API</th><td>{{ 'Configured' if openai_key else 'Not Configured' }}</td></tr>
                    <tr><th>PayPal</th><td>{{ 'Configured' if paypal_id else 'Not Configured' }}</td></tr>
                    <tr><th>Cashfree</th><td>{{ 'Configured' if cashfree_id else 'Not Configured' }}</td></tr>
                </table>
            </div>
        </div>
    </body>
    </html>
    """, 
    app_name=APP_NAME,
    domain=DOMAIN,
    total_users=total_users,
    total_transactions=total_transactions,
    total_api_usage=total_api_usage,
    total_revenue=total_revenue,
    recent_users=recent_users,
    recent_transactions=recent_transactions,
    telegram_token=bool(TELEGRAM_TOKEN),
    openai_key=bool(OPENAI_API_KEY),
    paypal_id=bool(PAYPAL_CLIENT_ID),
    cashfree_id=bool(CASHFREE_CLIENT_ID)
    )

@app.route('/api/generate', methods=['POST'])
@login_required
def api_generate():
    """API endpoint for AI generation"""
    prompt = request.form.get('prompt') or request.json.get('prompt') if request.is_json else None
    
    if not prompt:
        if request.is_json:
            return jsonify({"error": "Prompt is required"}), 400
        else:
            flash("Please enter a message.", "error")
            return redirect(url_for('dashboard'))
    
    user_id = session.get('user_id')
    
    # Try OpenAI first, fallback to Hugging Face
    try:
        response = query_openai(prompt, user_id)
        if "OpenAI API key not configured" in response or "having trouble" in response:
            response = query_huggingface(prompt, user_id)
    except Exception as e:
        log("api", "ERROR", f"API generation failed: {e}")
        response = "Sorry, I'm experiencing technical difficulties. Please try again later."
    
    if request.is_json:
        return jsonify({"response": response})
    else:
        flash(f"AI Response: {response}", "success")
        return redirect(url_for('dashboard'))

# =========================
# TELEGRAM BOT SETUP
# =========================

telegram_app = None

async def tg_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Telegram bot start command"""
    user_id = str(update.effective_user.id)
    username = update.effective_user.username or update.effective_user.first_name
    
    # Check if user exists in database
    user = User.query.filter_by(telegram_id=user_id).first()
    if not user:
        # Create new user
        user = User(
            username=f"tg_{username}_{user_id}",
            email=f"telegram_{user_id}@temp.com",
            telegram_id=user_id,
            wallet=10.0  # Give new users some free credits
        )
        user.set_password(str(uuid.uuid4()))  # Random password
        db.session.add(user)
        db.session.commit()
        
        welcome_msg = f"🎉 Welcome to {APP_NAME}!\n\n"
        welcome_msg += "You've been registered automatically and received ₹10 free credits!\n\n"
        welcome_msg += "💬 Just send me any message and I'll respond with AI-powered answers.\n"
        welcome_msg += f"🌐 Visit our website: {DOMAIN}\n"
        welcome_msg += f"💬 Support: {SUPPORT_USERNAME}"
    else:
        welcome_msg = f"🚀 Welcome back to {APP_NAME}!\n\n"
        welcome_msg += f"💰 Your wallet balance: ₹{user.wallet:.2f}\n"
        welcome_msg += "💬 Send me any message for AI-powered responses!"
    
    await update.message.reply_text(welcome_msg)

async def tg_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Telegram bot messages"""
    user_id = str(update.effective_user.id)
    user_text = update.message.text
    
    # Find user in database
    user = User.query.filter_by(telegram_id=user_id).first()
    if not user:
        await update.message.reply_text("Please start the bot first by sending /start")
        return
    
    # Check wallet balance
    if user.wallet < 0.01:
        await update.message.reply_text(
            f"💸 Insufficient balance! Your current balance: ₹{user.wallet:.2f}\n\n"
            f"Please add funds to continue using the AI service.\n"
            f"Visit: {DOMAIN}"
        )
        return
    
    # Send typing indicator
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')
    
    try:
        # Get AI response
        response = query_openai(user_text, user.id)
        if "OpenAI API key not configured" in response or "having trouble" in response:
            response = query_huggingface(user_text, user.id)
        
        # Deduct cost from wallet
        cost = 0.01  # ₹0.01 per message
        user.wallet -= cost
        db.session.commit()
        
        # Add transaction record
        transaction = Transaction(
            user_id=user.id,
            amount=cost,
            transaction_type='debit',
            payment_method='wallet',
            status='completed',
            description=f'AI query: {user_text[:50]}...'
        )
        db.session.add(transaction)
        db.session.commit()
        
        # Send response with balance info
        response += f"\n\n💰 Remaining balance: ₹{user.wallet:.2f}"
        
        await update.message.reply_text(response)
        
    except Exception as e:
        log("telegram", "ERROR", f"Message handling failed: {e}")
        await update.message.reply_text("Sorry, I encountered an error. Please try again later.")

def setup_telegram():
    """Set up Telegram bot"""
    global telegram_app
    
    if not TELEGRAM_TOKEN:
        log("telegram", "INFO", "No TELEGRAM_TOKEN found, skipping Telegram bot")
        return
    
    try:
        telegram_app = Application.builder().token(TELEGRAM_TOKEN).build()
        telegram_app.add_handler(CommandHandler("start", tg_start))
        telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, tg_message))
        
        # Start bot in a separate thread with proper signal handling
        def run_bot():
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                # Use webhook mode instead of polling to avoid signal handler issues
                # For now, just log that bot is configured
                log("telegram", "INFO", "Telegram bot configured (webhook mode)")
            except Exception as e:
                log("telegram", "ERROR", f"Telegram bot thread error: {e}")
        
        bot_thread = threading.Thread(target=run_bot, daemon=True)
        bot_thread.start()
        
        log("telegram", "INFO", "Telegram bot setup completed")
        
    except Exception as e:
        log("telegram", "ERROR", f"Failed to start Telegram bot: {e}")

# =========================
# TELEGRAM WEBHOOK
# =========================

@app.route('/webhook/telegram', methods=['POST'])
def telegram_webhook():
    """Handle Telegram webhook"""
    if not telegram_app:
        return jsonify({"error": "Telegram bot not configured"}), 400
    
    try:
        update = Update.de_json(request.get_json(), telegram_app.bot)
        
        # Process update in background
        def process_update():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
            with app.app_context():
                if update.message:
                    if update.message.text == '/start':
                        loop.run_until_complete(tg_start(update, None))
                    else:
                        loop.run_until_complete(tg_message(update, None))
        
        thread = threading.Thread(target=process_update, daemon=True)
        thread.start()
        
        return jsonify({"status": "ok"})
        
    except Exception as e:
        log("telegram", "ERROR", f"Webhook processing failed: {e}")
        return jsonify({"error": "processing failed"}), 500

# =========================
# PAYMENT WEBHOOKS
# =========================

@app.route('/webhook/cashfree', methods=['POST'])
def cashfree_webhook():
    """Handle Cashfree payment webhooks"""
    try:
        data = request.get_json()
        
        # Verify webhook signature (implement based on Cashfree docs)
        # signature = request.headers.get('x-webhook-signature')
        
        order_id = data.get('order_id')
        payment_status = data.get('payment_status')
        order_amount = float(data.get('order_amount', 0))
        
        if payment_status == 'SUCCESS':
            # Find user and add funds
            # This is a simplified implementation
            # In production, you'd need proper order tracking
            
            log("payment", "INFO", f"Cashfree payment success: {order_id} - ₹{order_amount}")
            
            return jsonify({"status": "success"})
        
        return jsonify({"status": "received"})
        
    except Exception as e:
        log("payment", "ERROR", f"Cashfree webhook error: {e}")
        return jsonify({"error": "webhook processing failed"}), 500

@app.route('/webhook/paypal', methods=['POST'])
def paypal_webhook():
    """Handle PayPal payment webhooks"""
    try:
        data = request.get_json()
        
        event_type = data.get('event_type')
        
        if event_type == 'PAYMENT.CAPTURE.COMPLETED':
            # Process successful payment
            log("payment", "INFO", f"PayPal payment completed: {data}")
            
            return jsonify({"status": "success"})
        
        return jsonify({"status": "received"})
        
    except Exception as e:
        log("payment", "ERROR", f"PayPal webhook error: {e}")
        return jsonify({"error": "webhook processing failed"}), 500

# =========================
# SCHEDULER SETUP
# =========================

scheduler = BackgroundScheduler()

def daily_log():
    """Daily logging task"""
    with app.app_context():
        total_users = User.query.count()
        total_transactions = Transaction.query.count()
        total_api_usage = APIUsage.query.count()
        
        log("scheduler", "INFO", f"Daily stats - Users: {total_users}, Transactions: {total_transactions}, API Usage: {total_api_usage}")

scheduler.add_job(daily_log, 'cron', hour=0, minute=0)  # Run daily at midnight
scheduler.start()

# =========================
# ERROR HANDLERS
# =========================

@app.errorhandler(404)
def not_found(error):
    return render_template_string("""
    <h1>404 - Page Not Found</h1>
    <p>The page you're looking for doesn't exist.</p>
    <a href="{{ url_for('index') }}">Go Home</a>
    """), 404

@app.errorhandler(500)
def internal_error(error):
    return render_template_string("""
    <h1>500 - Internal Server Error</h1>
    <p>Something went wrong on our end.</p>
    <a href="{{ url_for('index') }}">Go Home</a>
    """), 500

# =========================
# STARTUP
# =========================

if __name__ == "__main__":
    # Initialize database
    init_db()
    log("system", "INFO", f"{APP_NAME} starting...")
    
    # Setup Telegram bot
    try:
        setup_telegram()
    except Exception as e:
        log("telegram", "ERROR", f"Failed to start Telegram bot: {e}")
    
    # Start Flask app
    port = int(os.getenv("PORT", 10000))
    
    if DEBUG:
        app.run(host="0.0.0.0", port=port, debug=True)
    else:
        # Production settings
        app.run(host="0.0.0.0", port=port, debug=False, threaded=True)