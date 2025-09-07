#!/bin/bash

# Ganesh A.I. Production Startup Script
# ====================================

echo "🚀 Starting Ganesh A.I. Production Server..."

# Check if .env file exists
if [ ! -f ".env" ]; then
    echo "❌ Error: .env file not found!"
    echo "Please create .env file with your configuration."
    exit 1
fi

# Check if virtual environment exists
if [ ! -d "venv" ]; then
    echo "📦 Creating virtual environment..."
    python3 -m venv venv
fi

# Activate virtual environment
echo "🔧 Activating virtual environment..."
source venv/bin/activate

# Install/update dependencies
echo "📥 Installing dependencies..."
pip install -r requirements.txt

# Check database
echo "🗄️ Initializing database..."
python -c "
from main import app, init_db
with app.app_context():
    init_db()
    print('Database initialized successfully!')
"

# Start the application
echo "🌟 Starting Ganesh A.I. server..."
echo "📍 Server will be available at: http://localhost:10000"
echo "🛠️ Admin Panel: http://localhost:10000/admin"
echo "👤 Admin Login: Admin / 12345"
echo ""
echo "Press Ctrl+C to stop the server"
echo "================================"

# Choose deployment method
if command -v gunicorn &> /dev/null; then
    echo "🚀 Starting with Gunicorn (Production)..."
    gunicorn -w 4 -b 0.0.0.0:10000 --timeout 120 --keep-alive 2 main:app
else
    echo "🔧 Starting with Flask development server..."
    echo "⚠️  For production, install gunicorn: pip install gunicorn"
    python main.py
fi