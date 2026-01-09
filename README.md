# x402 Payment Facilitator

Drop-in payment gateway. No Stripe. No KYC. Wallet-to-wallet.

## Deploy to Render (2 minutes)

1. Push to GitHub:
```bash
cd x402-facilitator
git init
git add .
git commit -m "x402 facilitator"
gh repo create x402-facilitator --public --push
```

2. Go to [render.com/new](https://render.com/new)
3. Connect repo
4. Set env vars:
   - `PAYMENT_ADDRESS` = your Base USDC wallet
5. Deploy

## Endpoints

| Endpoint | Method | What |
|----------|--------|------|
| `/pay` | POST | Create payment challenge (returns 402) |
| `/pay/{id}` | GET | Check payment status |
| `/webhook` | POST | Payment verification webhook |
| `/buy/{product}` | GET/POST | Quick product purchase |
| `/verify-onchain` | POST | Verify tx on Base chain |

## Quick Usage

### Create Payment
```bash
curl -X POST https://your-app.onrender.com/pay \
  -H "Content-Type: application/json" \
  -d '{"item": "sweater", "amount": 100}'
```

Response (402):
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

### Quick Buy
```bash
# Pre-configured products
curl https://your-app.onrender.com/buy/sweater
curl https://your-app.onrender.com/buy/music-track
curl https://your-app.onrender.com/buy/seal
```

### Verify Payment
```bash
curl -X POST https://your-app.onrender.com/webhook \
  -H "Content-Type: application/json" \
  -d '{"payment_id": "abc123", "tx_hash": "0x...", "amount": 100, "sender": "0x..."}'
```

## Products (default prices)

| Product | Price |
|---------|-------|
| sweater | $100 |
| hoodie | $80 |
| music-track | $0.50 |
| album-art | $0.10 |
| brand-kit | $0.25 |
| seal | $0.05 |
| rug-score | $0.10 |

## Integration

In your frontend:
```javascript
// Request purchase
const res = await fetch('https://x402.yourdomain.com/buy/sweater');
const data = await res.json();

// Show QR or deep link
window.location.href = `https://pay.coinbase.com/buy?address=${data.pay_to}&amount=${data.amount}`;

// After user pays, verify
await fetch('https://x402.yourdomain.com/webhook', {
  method: 'POST',
  body: JSON.stringify({
    payment_id: data.payment_id,
    tx_hash: userTxHash,
    amount: data.amount,
    sender: userWallet
  })
});
```

## Connect to All Your Apps

This one service handles payments for:
- outlierclothiers.com (clothes)
- @MSUCOBot (music gen)
- @NotaryTON_bot (seals)
- memescan (rug scores)

Just call the appropriate `/buy/{product}` endpoint from each app.
