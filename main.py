"""
x402 Payment Facilitator
========================
Wallet-to-wallet payments over HTTP. USDC on Base. No processor, no KYC.

Two independent checks stand between a request and a payment marked paid:

  1. The caller proves it is you, with an HMAC signature over the request body.
  2. The chain proves the money moved, by way of the ERC-20 Transfer log.

Neither alone is sufficient. A signature says who is asking, not that anyone
paid; an on-chain transfer says money moved, not that it was for this order.
Both are required, always — there is no unauthenticated path that marks a
payment settled.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

app = FastAPI(title="x402 Facilitator", version="2.0.0")

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

ADDRESS_PATTERN = re.compile(r"^0x[0-9a-fA-F]{40}$")

PAYMENT_ADDRESS = os.getenv("PAYMENT_ADDRESS", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
BASE_RPC_URL = os.getenv("BASE_RPC_URL", "https://mainnet.base.org").strip()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# Comma-separated list of sites allowed to call this facilitator from a browser.
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
]

# Base mainnet.
CHAIN_ID = 8453
USDC_CONTRACT = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
USDC_DECIMALS = 6

# keccak256("Transfer(address,address,uint256)")
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# Base produces blocks every ~2s. Two confirmations is a couple of seconds of
# reorg protection, which is proportionate for payments of this size.
MIN_CONFIRMATIONS = int(os.getenv("MIN_CONFIRMATIONS", "2"))

# Shakepay and most custodial deposit addresses reject dust. Anything smaller
# than this is refused up front rather than silently vanishing on arrival.
MIN_AMOUNT_USDC = float(os.getenv("MIN_AMOUNT_USDC", "0.01"))

if not ADDRESS_PATTERN.match(PAYMENT_ADDRESS):
    raise RuntimeError(
        "PAYMENT_ADDRESS is unset or malformed. Set it to the 0x address that "
        "should receive funds. There is deliberately no default: shipping a "
        "fallback address means payments quietly land in a wallet nobody holds "
        "the keys to."
    )

if not WEBHOOK_SECRET or WEBHOOK_SECRET == "changeme":
    raise RuntimeError(
        "WEBHOOK_SECRET is unset or left at the placeholder. It authenticates "
        "settlement callbacks; without a real one, anyone who can reach /webhook "
        "can mark any payment paid."
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS or ["https://kim.juche.org"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-Signature"],
)

# ─────────────────────────────────────────────────────────────────────────────
# State
# ─────────────────────────────────────────────────────────────────────────────

# In-memory. A restart drops pending payments; settled ones are recoverable
# from the chain. Point SUPABASE_URL/SUPABASE_KEY at a table to persist.
payments: dict[str, dict] = {}

# Transaction hashes already credited, so one transfer cannot settle two orders.
spent_tx_hashes: dict[str, str] = {}

PRODUCTS = {
    "sweater": 100.0,
    "hoodie": 80.0,
    "music-track": 0.50,
    "album-art": 0.10,
    "brand-kit": 0.25,
    "seal": 0.05,
    "rug-score": 0.10,
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def to_base_units(amount: float) -> int:
    return int(round(amount * 10**USDC_DECIMALS))


def payment_uri(amount: Optional[float] = None) -> str:
    """
    EIP-681 request for a USDC transfer:

        ethereum:<token>@<chainId>/transfer?address=<recipient>&uint256=<baseUnits>

    The URI target is the token contract and the recipient is the `address`
    argument to transfer(). The shape that reads more naturally — recipient as
    the target, amount in a `value` parameter — asks the wallet for native ETH
    instead, and delivers no USDC at all.
    """
    uri = f"ethereum:{USDC_CONTRACT}@{CHAIN_ID}/transfer?address={PAYMENT_ADDRESS}"
    if amount is None:
        return uri
    return f"{uri}&uint256={to_base_units(amount)}"


# ─────────────────────────────────────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────────────────────────────────────


class PaymentRequest(BaseModel):
    item: str
    amount: float = Field(gt=0)
    description: Optional[str] = None
    callback_url: Optional[str] = None


class SupportRequest(BaseModel):
    amount: float = Field(gt=0)
    note: Optional[str] = None


class SettlementRequest(BaseModel):
    payment_id: str
    tx_hash: str


# ─────────────────────────────────────────────────────────────────────────────
# Chain access
# ─────────────────────────────────────────────────────────────────────────────


async def rpc(client: httpx.AsyncClient, method: str, params: list):
    response = await client.post(
        BASE_RPC_URL,
        json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1},
        timeout=20.0,
    )
    response.raise_for_status()
    body = response.json()
    if "error" in body:
        raise HTTPException(502, f"RPC error: {body['error']}")
    return body.get("result")


async def verify_transfer(tx_hash: str, expected: float) -> dict:
    """
    Confirm the chain actually moved `expected` USDC to PAYMENT_ADDRESS in this
    transaction.

    A mined receipt on its own proves nothing useful: it says a transaction
    succeeded, not that it paid us, not that it paid enough, and not that it
    moved USDC rather than some worthless token. So this walks the receipt's
    ERC-20 Transfer logs and sums only those emitted by the USDC contract with
    our address as recipient.
    """
    if not re.match(r"^0x[0-9a-fA-F]{64}$", tx_hash):
        return {"verified": False, "reason": "malformed transaction hash"}

    async with httpx.AsyncClient() as client:
        receipt = await rpc(client, "eth_getTransactionReceipt", [tx_hash])
        if not receipt:
            return {"verified": False, "reason": "transaction not found or not yet mined"}
        if receipt.get("status") != "0x1":
            return {"verified": False, "reason": "transaction reverted"}

        head = int(await rpc(client, "eth_blockNumber", []), 16)

    mined_in = int(receipt["blockNumber"], 16)
    confirmations = head - mined_in + 1
    if confirmations < MIN_CONFIRMATIONS:
        return {
            "verified": False,
            "reason": f"only {confirmations} confirmation(s), need {MIN_CONFIRMATIONS}",
            "confirmations": confirmations,
        }

    recipient = PAYMENT_ADDRESS[2:].lower()
    received = 0
    for log in receipt.get("logs", []):
        topics = log.get("topics", [])
        if len(topics) < 3:
            continue
        if log.get("address", "").lower() != USDC_CONTRACT.lower():
            continue
        if topics[0].lower() != TRANSFER_TOPIC:
            continue
        # topics[2] is the indexed `to` address, left-padded to 32 bytes.
        if topics[2][-40:].lower() != recipient:
            continue
        received += int(log.get("data", "0x0"), 16)

    if received == 0:
        return {
            "verified": False,
            "reason": f"no USDC transfer to {PAYMENT_ADDRESS} in this transaction",
        }

    required = to_base_units(expected)
    if received < required:
        return {
            "verified": False,
            "reason": (
                f"underpaid: received {received / 10**USDC_DECIMALS:.6f} USDC, "
                f"expected {expected:.6f}"
            ),
            "received_usdc": received / 10**USDC_DECIMALS,
        }

    return {
        "verified": True,
        "received_usdc": received / 10**USDC_DECIMALS,
        "confirmations": confirmations,
        "block": mined_in,
    }


def require_signature(body: bytes, signature: Optional[str]) -> None:
    """
    Reject anything not signed with WEBHOOK_SECRET.

    The header is mandatory. Verifying only when a signature happens to be
    present — the shape this had previously — means an attacker simply omits
    the header and walks through unchecked.
    """
    if not signature:
        raise HTTPException(401, "missing X-Signature header")
    expected = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature.strip()):
        raise HTTPException(401, "invalid signature")


async def persist(payment: dict) -> None:
    if not SUPABASE_URL or not SUPABASE_KEY:
        return
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{SUPABASE_URL}/rest/v1/payments",
                headers={
                    "apikey": SUPABASE_KEY,
                    "Authorization": f"Bearer {SUPABASE_KEY}",
                    "Content-Type": "application/json",
                },
                json=payment,
                timeout=10.0,
            )
    except httpx.HTTPError:
        # Persistence is a convenience; the chain is the record of truth.
        pass


def new_challenge(item: str, amount: float, description: str | None, callback_url: str | None) -> dict:
    if amount < MIN_AMOUNT_USDC:
        raise HTTPException(
            400,
            f"amount below minimum of {MIN_AMOUNT_USDC} USDC — smaller transfers "
            f"are commonly rejected on arrival",
        )
    payment_id = uuid.uuid4().hex[:12]
    payment = {
        "id": payment_id,
        "item": item,
        "amount": amount,
        "description": description,
        "status": "pending",
        "created_at": now(),
        "callback_url": callback_url,
    }
    payments[payment_id] = payment
    return payment


def challenge_body(payment: dict) -> dict:
    return {
        "status": "payment_required",
        "payment_id": payment["id"],
        "amount": payment["amount"],
        "currency": "USDC",
        "network": "base",
        "chain_id": CHAIN_ID,
        "asset": USDC_CONTRACT,
        "pay_to": PAYMENT_ADDRESS,
        "item": payment["item"],
        "description": payment["description"],
        "qr_data": payment_uri(payment["amount"]),
        "min_confirmations": MIN_CONFIRMATIONS,
    }


def apply_challenge_headers(response: Response, payment: dict) -> None:
    response.status_code = 402
    response.headers["X-Payment-Required"] = "true"
    response.headers["X-Payment-Address"] = PAYMENT_ADDRESS
    response.headers["X-Payment-Amount"] = str(payment["amount"])
    response.headers["X-Payment-Currency"] = "USDC"
    response.headers["X-Payment-Network"] = "base"
    response.headers["X-Payment-Chain-Id"] = str(CHAIN_ID)
    response.headers["X-Payment-Asset"] = USDC_CONTRACT
    response.headers["X-Payment-ID"] = payment["id"]


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────


@app.get("/")
async def root():
    return {
        "service": "x402 Facilitator",
        "status": "operational",
        "network": {"name": "base", "chain_id": CHAIN_ID, "asset": USDC_CONTRACT},
        "endpoints": {
            "POST /pay": "create a payment challenge (402)",
            "POST /support": "create a donation challenge (402)",
            "GET /pay/{payment_id}": "check payment status",
            "POST /webhook": "submit settlement proof (signed, verified on-chain)",
            "POST /verify-onchain": "verify a transaction without settling",
            "GET /buy/{product}": "preconfigured product checkout",
        },
    }


@app.get("/health")
async def health():
    return {"status": "healthy", "timestamp": now()}


@app.post("/pay")
async def create_payment(req: PaymentRequest, response: Response):
    payment = new_challenge(req.item, req.amount, req.description, req.callback_url)
    await persist(payment)
    apply_challenge_headers(response, payment)
    return challenge_body(payment)


@app.post("/support")
async def create_support(req: SupportRequest, response: Response):
    """Donations. Same rails as a purchase, nothing owed in return."""
    payment = new_challenge("support", req.amount, req.note, None)
    await persist(payment)
    apply_challenge_headers(response, payment)
    return challenge_body(payment)


@app.get("/pay/{payment_id}")
async def check_payment(payment_id: str):
    payment = payments.get(payment_id)
    if not payment:
        raise HTTPException(404, "payment not found")
    return payment


@app.post("/webhook")
async def settle(request: Request, x_signature: Optional[str] = Header(None)):
    """
    Mark a payment settled.

    Requires a valid HMAC signature *and* a matching on-chain transfer. A
    signature alone cannot settle a payment, and neither can a transaction
    hash alone.
    """
    body = await request.body()
    require_signature(body, x_signature)

    try:
        data = SettlementRequest.model_validate_json(body)
    except ValueError as exc:
        raise HTTPException(400, f"malformed body: {exc}")

    payment = payments.get(data.payment_id)
    if not payment:
        raise HTTPException(404, "payment not found")

    if payment["status"] == "paid":
        # Replay of a settlement already applied: return the original result
        # rather than crediting the order twice.
        return {"status": "already_settled", "payment": payment}

    tx_hash = data.tx_hash.lower()
    claimed_by = spent_tx_hashes.get(tx_hash)
    if claimed_by and claimed_by != data.payment_id:
        raise HTTPException(409, f"transaction already settled payment {claimed_by}")

    result = await verify_transfer(tx_hash, payment["amount"])
    if not result["verified"]:
        raise HTTPException(402, f"payment not verified: {result['reason']}")

    spent_tx_hashes[tx_hash] = data.payment_id
    payment.update(
        status="paid",
        tx_hash=tx_hash,
        paid_at=now(),
        received_usdc=result["received_usdc"],
        confirmations=result["confirmations"],
    )
    await persist(payment)

    if payment.get("callback_url"):
        try:
            async with httpx.AsyncClient() as client:
                await client.post(payment["callback_url"], json=payment, timeout=10.0)
        except httpx.HTTPError:
            # The payment is settled regardless of whether the shop was reachable.
            pass

    return {"status": "verified", "payment": payment}


@app.post("/verify-onchain")
async def verify_onchain(data: SettlementRequest):
    """Read-only check of a transaction against a payment. Settles nothing."""
    payment = payments.get(data.payment_id)
    if not payment:
        raise HTTPException(404, "payment not found")
    return await verify_transfer(data.tx_hash.lower(), payment["amount"])


@app.api_route("/buy/{product}", methods=["GET", "POST"])
async def buy_product(product: str, response: Response, amount: Optional[float] = None):
    price = amount if amount is not None else PRODUCTS.get(product)
    if price is None:
        raise HTTPException(404, f"unknown product '{product}' and no amount given")
    payment = new_challenge(product, price, f"Purchase: {product}", None)
    await persist(payment)
    apply_challenge_headers(response, payment)
    return challenge_body(payment)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
