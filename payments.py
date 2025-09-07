import os
import httpx
import stripe
import razorpay
import paypalhttp
from paypalcheckoutsdk.orders import OrdersCreateRequest, OrdersGetRequest

# ===================== STRIPE =====================
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")

def create_stripe_payment(amount, currency="INR"):
    intent = stripe.PaymentIntent.create(
        amount=int(amount * 100),  # in paisa
        currency=currency
    )
    return intent.client_secret

# ===================== RAZORPAY =====================
razorpay_client = razorpay.Client(
    auth=(os.getenv("RAZORPAY_KEY_ID"), os.getenv("RAZORPAY_KEY_SECRET"))
)

def create_razorpay_order(amount, currency="INR"):
    return razorpay_client.order.create({
        "amount": int(amount * 100),
        "currency": currency,
        "payment_capture": 1
    })

# ===================== PAYPAL =====================
from paypalcheckoutsdk.core import PayPalHttpClient, SandboxEnvironment

paypal_env = SandboxEnvironment(
    client_id=os.getenv("PAYPAL_CLIENT_ID"),
    client_secret=os.getenv("PAYPAL_CLIENT_SECRET")
)
paypal_client = PayPalHttpClient(paypal_env)

def create_paypal_order(amount, currency="USD"):
    request = OrdersCreateRequest()
    request.prefer("return=representation")
    request.request_body({
        "intent": "CAPTURE",
        "purchase_units": [{"amount": {"currency_code": currency, "value": str(amount)}}]
    })
    response = paypal_client.execute(request)
    return response.result

# ===================== CASHFREE =====================
CASHFREE_CLIENT_ID = os.getenv("CASHFREE_CLIENT_ID")
CASHFREE_CLIENT_SECRET = os.getenv("CASHFREE_CLIENT_SECRET")
CASHFREE_BASE_URL = "https://sandbox.cashfree.com/pg"  # change to prod later

async def create_cashfree_order(order_id: str, amount: float, email: str, phone: str):
    url = f"{CASHFREE_BASE_URL}/orders"
    headers = {
        "Content-Type": "application/json",
        "x-client-id": CASHFREE_CLIENT_ID,
        "x-client-secret": CASHFREE_CLIENT_SECRET,
        "x-api-version": "2022-09-01"
    }
    payload = {
        "order_id": order_id,
        "order_amount": amount,
        "order_currency": "INR",
        "customer_details": {
            "customer_id": f"CUST_{order_id}",
            "customer_email": email,
            "customer_phone": phone,
        },
        "order_meta": {
            "return_url": "https://your-domain.com/payment/cashfree/callback"
        }
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, headers=headers, json=payload)

    if resp.status_code == 200:
        return resp.json()
    else:
        raise Exception(f"Cashfree error: {resp.text}")

async def verify_cashfree_payment(order_id: str):
    url = f"{CASHFREE_BASE_URL}/orders/{order_id}"
    headers = {
        "x-client-id": CASHFREE_CLIENT_ID,
        "x-client-secret": CASHFREE_CLIENT_SECRET,
        "x-api-version": "2022-09-01"
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url, headers=headers)

    if resp.status_code == 200:
        return resp.json()
    else:
        raise Exception(f"Cashfree verify error: {resp.text}")
