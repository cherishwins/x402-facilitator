"""
x402 Payment Facilitator
========================
Drop-in payment gateway. Returns 402 "pay me" for any endpoint.
Wallet-to-wallet. No Stripe. No KYC. You are the bank.

Deploy to Render in 2 minutes.
"""

import os
import hmac
import hashlib
import json
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx

app = FastAPI(title="x402 Facilitator", version="1.0.0")

# CORS for all your apps
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Lock down in prod
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Config from env
PAYMENT_ADDRESS = os.getenv("PAYMENT_ADDRESS", "0x14E6076eAC2420e56b4E2E18c815b2DD52264D54")  # Your Base USDC address
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "changeme")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# In-memory payment tracking (use Supabase for production)
payments = {}


class PaymentRequest(BaseModel):
    item: str
    amount: float  # USD
    description: Optional[str] = None
    callback_url: Optional[str] = None


class PaymentVerify(BaseModel):
    payment_id: str
    tx_hash: str
    amount: float
    sender: str


@app.get("/")
async def root():
    return {
        "service": "x402 Facilitator",
        "status": "operational",
        "endpoints": {
            "/pay": "POST - Create payment challenge",
            "/pay/{payment_id}": "GET - Check payment status",
            "/webhook": "POST - Payment webhook",
            "/health": "GET - Health check"
        }
    }


@app.get("/health")
async def health():
    return {"status": "healthy", "timestamp": datetime.utcnow().isoformat()}


@app.post("/pay")
async def create_payment(req: PaymentRequest, response: Response):
    """
    Create a payment challenge. Returns 402 with x402 headers.
    Client wallet reads headers, pays, then retries request.
    """
    import uuid
    payment_id = str(uuid.uuid4())[:8]

    # Store payment intent
    payments[payment_id] = {
        "id": payment_id,
        "item": req.item,
        "amount": req.amount,
        "description": req.description,
        "status": "pending",
        "created_at": datetime.utcnow().isoformat(),
        "callback_url": req.callback_url
    }

    # If Supabase configured, persist
    if SUPABASE_URL and SUPABASE_KEY:
        await save_to_supabase(payments[payment_id])

    # Return 402 with x402 challenge
    response.status_code = 402
    response.headers["X-Payment-Required"] = "true"
    response.headers["X-Payment-Address"] = PAYMENT_ADDRESS
    response.headers["X-Payment-Amount"] = str(req.amount)
    response.headers["X-Payment-Currency"] = "USDC"
    response.headers["X-Payment-Network"] = "base"
    response.headers["X-Payment-ID"] = payment_id

    return {
        "status": "payment_required",
        "payment_id": payment_id,
        "amount": req.amount,
        "currency": "USDC",
        "network": "base",
        "pay_to": PAYMENT_ADDRESS,
        "item": req.item,
        "description": req.description,
        "qr_data": f"ethereum:{PAYMENT_ADDRESS}@8453?value={int(req.amount * 1e6)}"  # Base chain ID 8453
    }


@app.get("/pay/{payment_id}")
async def check_payment(payment_id: str):
    """Check if a payment has been verified."""
    if payment_id not in payments:
        raise HTTPException(status_code=404, detail="Payment not found")

    return payments[payment_id]


@app.post("/webhook")
async def payment_webhook(request: Request):
    """
    Webhook for payment verification.
    Call this from your frontend after user pays, or from on-chain listener.
    """
    body = await request.body()

    # Verify signature if provided
    signature = request.headers.get("X-Signature")
    if signature:
        expected = hmac.new(
            WEBHOOK_SECRET.encode(),
            body,
            hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise HTTPException(status_code=401, detail="Invalid signature")

    data = await request.json()
    payment_id = data.get("payment_id")
    tx_hash = data.get("tx_hash")
    amount = data.get("amount")
    sender = data.get("sender")

    if payment_id not in payments:
        raise HTTPException(status_code=404, detail="Payment not found")

    # Mark as paid
    payments[payment_id]["status"] = "paid"
    payments[payment_id]["tx_hash"] = tx_hash
    payments[payment_id]["sender"] = sender
    payments[payment_id]["paid_at"] = datetime.utcnow().isoformat()

    # Trigger callback if configured
    callback_url = payments[payment_id].get("callback_url")
    if callback_url:
        async with httpx.AsyncClient() as client:
            try:
                await client.post(callback_url, json=payments[payment_id])
            except:
                pass  # Don't fail on callback errors

    return {"status": "verified", "payment": payments[payment_id]}


@app.post("/verify-onchain")
async def verify_onchain(payment_id: str, tx_hash: str):
    """
    Verify payment on-chain via Base RPC.
    Call this to confirm a transaction actually happened.
    """
    # Base mainnet RPC
    rpc_url = "https://mainnet.base.org"

    async with httpx.AsyncClient() as client:
        response = await client.post(rpc_url, json={
            "jsonrpc": "2.0",
            "method": "eth_getTransactionReceipt",
            "params": [tx_hash],
            "id": 1
        })

        result = response.json()
        receipt = result.get("result")

        if not receipt:
            return {"verified": False, "reason": "Transaction not found"}

        if receipt.get("status") != "0x1":
            return {"verified": False, "reason": "Transaction failed"}

        # Could add more checks: verify 'to' address, amount, etc.

        if payment_id in payments:
            payments[payment_id]["status"] = "verified"
            payments[payment_id]["tx_hash"] = tx_hash
            payments[payment_id]["verified_at"] = datetime.utcnow().isoformat()

        return {"verified": True, "tx_hash": tx_hash, "payment_id": payment_id}


async def save_to_supabase(payment: dict):
    """Persist payment to Supabase if configured."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return

    async with httpx.AsyncClient() as client:
        await client.post(
            f"{SUPABASE_URL}/rest/v1/payments",
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json"
            },
            json=payment
        )


# Quick product endpoints - customize these for your apps
@app.api_route("/buy/{product}", methods=["GET", "POST"])
async def buy_product(product: str, response: Response, amount: Optional[float] = None):
    """
    Quick endpoint for common products.

    Examples:
      /buy/sweater?amount=100
      /buy/music-track?amount=0.50
      /buy/album-art?amount=0.10
      /buy/seal?amount=0.05
    """
    # Default prices
    PRODUCTS = {
        "sweater": 100.0,
        "hoodie": 80.0,
        "music-track": 0.50,
        "album-art": 0.10,
        "brand-kit": 0.25,
        "seal": 0.05,
        "rug-score": 0.10,
    }

    price = amount or PRODUCTS.get(product, 1.0)

    req = PaymentRequest(
        item=product,
        amount=price,
        description=f"Purchase: {product}"
    )

    return await create_payment(req, response)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
