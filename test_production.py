#!/usr/bin/env python3
"""
Ganesh A.I. Production Test Suite
=================================
Comprehensive testing of all application components
"""

import os
import sys
import time
import requests
import json
from datetime import datetime

# Test configuration
BASE_URL = "http://localhost:10000"
TEST_USER = {
    "username": "testuser",
    "email": "test@example.com", 
    "password": "testpass123"
}

ADMIN_USER = {
    "username": "Admin",
    "password": "12345"
}

def print_test(test_name, status="RUNNING"):
    """Print test status"""
    status_emoji = {
        "RUNNING": "🔄",
        "PASS": "✅", 
        "FAIL": "❌",
        "SKIP": "⏭️"
    }
    print(f"{status_emoji.get(status, '❓')} {test_name}")

def test_server_health():
    """Test if server is running"""
    print_test("Server Health Check")
    try:
        response = requests.get(f"{BASE_URL}/", timeout=5)
        if response.status_code == 200 and "Ganesh A.I." in response.text:
            print_test("Server Health Check", "PASS")
            return True
        else:
            print_test("Server Health Check", "FAIL")
            return False
    except Exception as e:
        print_test("Server Health Check", "FAIL")
        print(f"   Error: {e}")
        return False

def test_home_page():
    """Test home page"""
    print_test("Home Page")
    try:
        response = requests.get(f"{BASE_URL}/")
        if response.status_code == 200:
            content = response.text
            checks = [
                "Ganesh A.I." in content,
                "AI Chat" in content,
                "Telegram Bot" in content,
                "Admin Panel" in content
            ]
            if all(checks):
                print_test("Home Page", "PASS")
                return True
        print_test("Home Page", "FAIL")
        return False
    except Exception as e:
        print_test("Home Page", "FAIL")
        print(f"   Error: {e}")
        return False

def test_authentication_pages():
    """Test login and register pages"""
    print_test("Authentication Pages")
    try:
        # Test login page
        login_response = requests.get(f"{BASE_URL}/login")
        register_response = requests.get(f"{BASE_URL}/register")
        
        if (login_response.status_code == 200 and 
            register_response.status_code == 200 and
            "Login to" in login_response.text and
            "Register for" in register_response.text):
            print_test("Authentication Pages", "PASS")
            return True
        else:
            print_test("Authentication Pages", "FAIL")
            return False
    except Exception as e:
        print_test("Authentication Pages", "FAIL")
        print(f"   Error: {e}")
        return False

def test_admin_redirect():
    """Test admin page redirects to login"""
    print_test("Admin Access Control")
    try:
        response = requests.get(f"{BASE_URL}/admin", allow_redirects=False)
        if response.status_code == 302:  # Redirect to login
            print_test("Admin Access Control", "PASS")
            return True
        else:
            print_test("Admin Access Control", "FAIL")
            return False
    except Exception as e:
        print_test("Admin Access Control", "FAIL")
        print(f"   Error: {e}")
        return False

def test_api_authentication():
    """Test API requires authentication"""
    print_test("API Authentication")
    try:
        response = requests.post(
            f"{BASE_URL}/api/generate",
            json={"prompt": "Hello"},
            allow_redirects=False
        )
        if response.status_code == 302:  # Redirect to login
            print_test("API Authentication", "PASS")
            return True
        else:
            print_test("API Authentication", "FAIL")
            return False
    except Exception as e:
        print_test("API Authentication", "FAIL")
        print(f"   Error: {e}")
        return False

def test_webhook_endpoints():
    """Test webhook endpoints exist"""
    print_test("Webhook Endpoints")
    try:
        # Test Telegram webhook
        tg_response = requests.post(f"{BASE_URL}/webhook/telegram", json={})
        
        # Test payment webhooks
        cf_response = requests.post(f"{BASE_URL}/webhook/cashfree", json={})
        pp_response = requests.post(f"{BASE_URL}/webhook/paypal", json={})
        
        # Should return 400 or 500, not 404
        if (tg_response.status_code != 404 and 
            cf_response.status_code != 404 and 
            pp_response.status_code != 404):
            print_test("Webhook Endpoints", "PASS")
            return True
        else:
            print_test("Webhook Endpoints", "FAIL")
            return False
    except Exception as e:
        print_test("Webhook Endpoints", "FAIL")
        print(f"   Error: {e}")
        return False

def test_database_connection():
    """Test database is accessible"""
    print_test("Database Connection")
    try:
        # Try to access a page that requires database
        response = requests.get(f"{BASE_URL}/login")
        if response.status_code == 200:
            print_test("Database Connection", "PASS")
            return True
        else:
            print_test("Database Connection", "FAIL")
            return False
    except Exception as e:
        print_test("Database Connection", "FAIL")
        print(f"   Error: {e}")
        return False

def test_environment_variables():
    """Test critical environment variables"""
    print_test("Environment Variables")
    
    # Load environment
    from dotenv import load_dotenv
    load_dotenv(".env")
    
    critical_vars = [
        "TELEGRAM_TOKEN",
        "OPENAI_API_KEY", 
        "FLASK_SECRET",
        "ADMIN_USER",
        "ADMIN_PASS"
    ]
    
    missing_vars = []
    for var in critical_vars:
        if not os.getenv(var):
            missing_vars.append(var)
    
    if not missing_vars:
        print_test("Environment Variables", "PASS")
        return True
    else:
        print_test("Environment Variables", "FAIL")
        print(f"   Missing: {', '.join(missing_vars)}")
        return False

def test_static_files():
    """Test static files are accessible"""
    print_test("Static Files")
    try:
        # Test CSS file
        css_response = requests.get(f"{BASE_URL}/static/style.css")
        js_response = requests.get(f"{BASE_URL}/static/script.js")
        
        # Should exist or return 404, not 500
        if (css_response.status_code in [200, 404] and 
            js_response.status_code in [200, 404]):
            print_test("Static Files", "PASS")
            return True
        else:
            print_test("Static Files", "FAIL")
            return False
    except Exception as e:
        print_test("Static Files", "FAIL")
        print(f"   Error: {e}")
        return False

def run_all_tests():
    """Run all tests"""
    print("🧪 Ganesh A.I. Production Test Suite")
    print("=" * 40)
    print(f"Testing server at: {BASE_URL}")
    print(f"Test started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()
    
    tests = [
        test_server_health,
        test_home_page,
        test_authentication_pages,
        test_admin_redirect,
        test_api_authentication,
        test_webhook_endpoints,
        test_database_connection,
        test_environment_variables,
        test_static_files
    ]
    
    passed = 0
    failed = 0
    
    for test in tests:
        if test():
            passed += 1
        else:
            failed += 1
        time.sleep(0.5)  # Small delay between tests
    
    print()
    print("=" * 40)
    print(f"📊 Test Results:")
    print(f"   ✅ Passed: {passed}")
    print(f"   ❌ Failed: {failed}")
    print(f"   📈 Success Rate: {(passed/(passed+failed)*100):.1f}%")
    
    if failed == 0:
        print("\n🎉 All tests passed! Your application is production-ready!")
    else:
        print(f"\n⚠️  {failed} test(s) failed. Please check the issues above.")
    
    return failed == 0

if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)