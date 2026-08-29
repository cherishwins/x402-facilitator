# x402-facilitator

> A working third-party x402 facilitator. USDC on Base. No Stripe. No KYC. Wallet-to-wallet.
> Production-deployed. Used by multiple live consumer apps. Open source.

**[Live](https://x402-facilitator.onrender.com)** · **[Hire us for x402 integration →](https://cal.com/cherishwins/x402)**

---

## What this is

A reference implementation of an [x402 protocol](https://www.x402.org) facilitator. x402 is the open standard, co-stewarded by [Coinbase and Cloudflare's x402 Foundation](https://www.coinbase.com/blog/coinbase-and-cloudflare-will-launch-x402-foundation), that lets HTTP servers charge for resources using stablecoin payments — settled in seconds, no accounts, no chargebacks, no card networks.

This repo is a **lightweight, self-hostable facilitator** that handles:
- `POST /pay` — creates a payment challenge (returns HTTP 402)
- `GET /pay/{id}` — checks payment status
- `POST /webhook` — payment verification
- `GET /buy/{product}` — preconfigured product flow
- `POST /verify-onchain` — on-chain tx verification on Base

## Why this exists

For production, most builders should use [Coinbase's CDP Facilitator](https://docs.cdp.coinbase.com/x402/welcome) — it's free up to 1,000 tx/month, multi-chain (Base, Polygon, Arbitrum, Solana, World), gas-sponsored, and OFAC-screened.

**This facilitator is for the case where you want:**
- Self-hosted infrastructure with zero third-party dependency
- A learning reference for how x402 actually works under the hood
- A starting point for chains or token configurations Coinbase doesn't yet support
- A facilitator you control end-to-end for compliance or geographic reasons

## Who's using it

This facilitator currently powers:
- **[outlier-clothiers](https://outlier-clothiers.vercel.app)** — direct-to-wallet clothing sales
- **[notaryton-bot](https://t.me/NotaryTON_bot)** — TON-side notarization with USDC settlement
- **[memescan](https://github.com/cherishwins/memescan-astro)** — paid rug-score lookups
- **[music](https://github.com/MobilityLink/music)** — paid AI music generation

## Deploy in 2 minutes

```bash
git clone https://github.com/cherishwins/x402-facilitator
cd x402-facilitator
# Push to your GitHub
git remote set-url origin https://github.com/YOU/x402-facilitator
git push -u origin main
```

Then on [Render](https://render.com/new): connect the repo and set the two required variables.

| Variable | Required | Notes |
|---|---|---|
| `PAYMENT_ADDRESS` | yes | Your Base address. No default — the service refuses to start without it. |
| `WEBHOOK_SECRET` | yes | `openssl rand -hex 32`. Rejected if left as `changeme`. |
| `ALLOWED_ORIGINS` | no | Comma-separated origins allowed to call it from a browser. |
| `BASE_RPC_URL` | no | Defaults to the public Base endpoint, which is rate-limited. |
| `MIN_CONFIRMATIONS` | no | Defaults to `2`. |

Both required variables fail closed at startup rather than falling back to a
default, because the failure mode of a default payment address is that money
lands somewhere nobody can spend it from.

## Endpoints

| Endpoint | Method | What it does |
|---|---|---|
| `/pay` | POST | Create payment challenge, returns HTTP 402 |
| `/support` | POST | Donation challenge — same rails, nothing owed in return |
| `/pay/{id}` | GET | Check payment status |
| `/webhook` | POST | Submit settlement proof. Signed **and** verified on-chain |
| `/verify-onchain` | POST | Check a transaction without settling it |
| `/buy/{product}` | GET/POST | Preconfigured product checkout |

## Quick usage

### Create a payment

```bash
curl -X POST https://x402-facilitator.onrender.com/pay \
  -H "Content-Type: application/json" \
  -d '{"item": "sweater", "amount": 100}'
```

Response (HTTP 402):
```json
{
  "status": "payment_required",
  "payment_id": "abc123",
  "amount": 100,
  "currency": "USDC",
  "network": "base",
  "pay_to": "0x14E6..."
}
```

### Quick buy (preconfigured products)

```bash
curl https://x402-facilitator.onrender.com/buy/seal
curl https://x402-facilitator.onrender.com/buy/music-track
curl https://x402-facilitator.onrender.com/buy/rug-score
```

### Settle after payment

`/webhook` requires an HMAC-SHA256 signature over the raw request body, in the
`X-Signature` header. The amount is **not** taken from the request — it comes
from the stored challenge and is checked against the chain.

```bash
BODY='{"payment_id":"abc123","tx_hash":"0x..."}'
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$WEBHOOK_SECRET" -hex | sed 's/.* //')

curl -X POST https://x402-facilitator.onrender.com/webhook \
  -H "Content-Type: application/json" \
  -H "X-Signature: $SIG" \
  -d "$BODY"
```

A request without a valid signature returns `401`. A signed request whose
transaction did not actually move the full amount to `PAYMENT_ADDRESS` returns
`402` with the reason.

## Default product catalog

| Product | Price (USDC) |
|---|---|
| sweater | $100 |
| hoodie | $80 |
| music-track | $0.50 |
| album-art | $0.10 |
| brand-kit | $0.25 |
| seal | $0.05 |
| rug-score | $0.10 |

Edit `main.py` to customize.

## Integration example

```js
// 1. Request the resource. A 402 carries the challenge.
const res = await fetch('https://your-x402.example.com/buy/sweater');
const challenge = await res.json();

// 2. Hand the user a payment request. `qr_data` is an EIP-681 URI naming the
//    token and the chain, so there is nothing for them to select by hand.
//    Render it as a QR code, or use it as a deep link:
window.location.href = challenge.qr_data;
// ethereum:0x8335…2913@8453/transfer?address=0x…&uint256=100000000

// 3. Settle from your SERVER, never the browser — the signature requires
//    WEBHOOK_SECRET, which must not ship to a client.
//    POST { payment_id, tx_hash } with an X-Signature header.
```

The `qr_data` URI targets the **token contract**, with the recipient as the
`transfer` argument. The arrangement that reads more naturally — recipient as
the target, amount in a `value` parameter — asks the wallet for native ETH
instead, and delivers no USDC at all.

## Architecture

```
x402-facilitator/
├── main.py                # FastAPI app: routes, payment state, verification
├── test_main.py           # Settlement-path tests, mostly adversarial
├── requirements.txt       # FastAPI, uvicorn, httpx, pydantic
├── requirements-dev.txt   # + pytest
├── render.yaml            # Render deployment config
└── .env.example           # PAYMENT_ADDRESS, WEBHOOK_SECRET, etc.
```

## Security posture

Two independent checks stand between a request and a payment marked settled,
and both are mandatory:

1. **An HMAC signature** over the raw request body proves the caller holds
   `WEBHOOK_SECRET`.
2. **The chain** proves the money moved, via the USDC `Transfer` log in the
   transaction receipt.

Neither is sufficient alone. A signature says who is asking, not that anyone
paid. A transaction hash says money moved, not that it was for this order.

What settlement verifies:

- ✅ Signature present and valid — a missing `X-Signature` is `401`, not a pass
- ✅ Transaction mined and not reverted
- ✅ At least `MIN_CONFIRMATIONS` deep
- ✅ Transfer emitted by the **USDC contract** — an arbitrary token does not count
- ✅ Recipient is `PAYMENT_ADDRESS`
- ✅ Amount is at least the challenge amount, taken from stored state rather than
  from the request body
- ✅ One transaction settles one payment — reuse returns `409`
- ✅ Re-settling a paid payment is idempotent, not a double credit

Still worth knowing:

- ⚠️ State is in-memory by default; a restart drops **pending** payments. Settled
  ones are recoverable from the chain. Set `SUPABASE_URL`/`SUPABASE_KEY` to persist.
- ⚠️ No rate limiting by default — put Cloudflare or a reverse proxy in front
- ⚠️ Free-tier Render sleeps after 15 min idle — Starter plan minimum for real use

```bash
pip install -r requirements-dev.txt
pytest
```

### Upgrading from 1.x

Three breaking changes, all of them closing holes:

1. `X-Signature` on `/webhook` is now **required**. Previously the signature was
   verified only when the header happened to be present, so omitting it skipped
   verification entirely and any caller could mark any payment paid.
2. `/webhook` no longer accepts `amount` or `sender` from the request body. The
   amount is read from the stored challenge and checked against the chain.
3. `PAYMENT_ADDRESS` and `WEBHOOK_SECRET` have no defaults and the service will
   not start without them.

## Need integration help?

If you want to add x402 to your app, API, or AI agent, we do this for a living. Our consumer apps prove the pattern works end-to-end — TON + Base, Telegram + browser, micropayments + larger purchases.

**Engagements:**
- 🛠️ **x402 integration sprint** — 1-2 weeks, your app gets paid endpoints. From $5,000.
- 🏗️ **Custom facilitator build** — self-hosted, multi-chain, your compliance needs. From $15,000.
- 🤝 **Strategic partnership** — ongoing dev partnership for x402-native product companies.

**[→ Book a 20-min scoping call](https://cal.com/cherishwins/x402)**

## License

MIT for the facilitator code. The reference implementation is free to use, fork, and adapt. Commercial integration consulting is a separate engagement.

---

*Built by [cherishwins](https://github.com/cherishwins). Used in production. Listed on the [x402 ecosystem page](https://www.x402.org/ecosystem) (pending submission).*
