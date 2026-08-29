"""
Tests for the facilitator's settlement path.

The cases that matter here are the negative ones. A payment gateway that
accepts good input is unremarkable; one that rejects forged settlement is the
entire product. Each test below corresponds to a way an attacker gets goods
without paying for them.
"""

import hashlib
import hmac
import importlib
import json
import sys

import pytest

ADDRESS = "0x0e392132755757926F7965965A86B88880E90bca"
SECRET = "test-secret"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


@pytest.fixture
def main(monkeypatch):
    monkeypatch.setenv("PAYMENT_ADDRESS", ADDRESS)
    monkeypatch.setenv("WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("ALLOWED_ORIGINS", "https://kim.juche.org")
    import main as module

    return importlib.reload(module)


@pytest.fixture
def client(main):
    from fastapi.testclient import TestClient

    return TestClient(main.app)


def sign(payload: dict) -> tuple[bytes, str]:
    body = json.dumps(payload).encode()
    return body, hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def topic_address(address: str) -> str:
    return "0x" + address[2:].lower().rjust(64, "0")


def receipt(to_address=ADDRESS, amount_units=5_000_000, token=USDC, status="0x1"):
    return {
        "status": status,
        "blockNumber": hex(1000),
        "logs": [
            {
                "address": token,
                "topics": [
                    TRANSFER_TOPIC,
                    topic_address("0x1111111111111111111111111111111111111111"),
                    topic_address(to_address),
                ],
                "data": hex(amount_units),
            }
        ],
    }


def stub_chain(main, monkeypatch, tx_receipt, head=1010):
    async def fake_rpc(client, method, params):
        if method == "eth_getTransactionReceipt":
            return tx_receipt
        if method == "eth_blockNumber":
            return hex(head)
        raise AssertionError(f"unexpected RPC call: {method}")

    monkeypatch.setattr(main, "rpc", fake_rpc)


# ── configuration must fail closed ───────────────────────────────────────────


def import_fresh():
    """Import main from scratch so module-level validation runs again."""
    sys.modules.pop("main", None)
    return importlib.import_module("main")


def test_refuses_to_start_without_payment_address(monkeypatch):
    monkeypatch.setenv("PAYMENT_ADDRESS", "")
    monkeypatch.setenv("WEBHOOK_SECRET", SECRET)

    with pytest.raises(RuntimeError, match="PAYMENT_ADDRESS"):
        import_fresh()


def test_refuses_to_start_with_a_malformed_address(monkeypatch):
    monkeypatch.setenv("PAYMENT_ADDRESS", "0xnope")
    monkeypatch.setenv("WEBHOOK_SECRET", SECRET)

    with pytest.raises(RuntimeError, match="PAYMENT_ADDRESS"):
        import_fresh()


def test_refuses_to_start_with_placeholder_secret(monkeypatch):
    monkeypatch.setenv("PAYMENT_ADDRESS", ADDRESS)
    monkeypatch.setenv("WEBHOOK_SECRET", "changeme")

    with pytest.raises(RuntimeError, match="WEBHOOK_SECRET"):
        import_fresh()


# ── the payment challenge ────────────────────────────────────────────────────


def test_pay_returns_402_with_eip681_transfer_uri(client):
    response = client.post("/pay", json={"item": "sweater", "amount": 100})
    assert response.status_code == 402
    body = response.json()

    # The URI must target the token contract and carry the recipient as the
    # transfer argument. Targeting the recipient with a `value` parameter asks
    # for native ETH and delivers no USDC.
    assert body["qr_data"] == (
        f"ethereum:{USDC}@8453/transfer?address={ADDRESS}&uint256=100000000"
    )
    assert body["pay_to"] == ADDRESS
    assert response.headers["X-Payment-Address"] == ADDRESS


def test_support_endpoint_issues_a_challenge(client):
    response = client.post("/support", json={"amount": 5, "note": "briefings"})
    assert response.status_code == 402
    assert response.json()["qr_data"].endswith("uint256=5000000")


def test_dust_amounts_are_refused_up_front(client):
    response = client.post("/support", json={"amount": 0.001})
    assert response.status_code == 400
    assert "minimum" in response.json()["detail"]


# ── settlement cannot be forged ──────────────────────────────────────────────


def test_webhook_without_signature_is_rejected(client):
    """The original bug: no header meant no verification, so anyone could
    mark any payment paid simply by omitting X-Signature."""
    payment_id = client.post("/pay", json={"item": "seal", "amount": 5}).json()["payment_id"]

    response = client.post(
        "/webhook", json={"payment_id": payment_id, "tx_hash": "0x" + "ab" * 32}
    )

    assert response.status_code == 401
    assert client.get(f"/pay/{payment_id}").json()["status"] == "pending"


def test_webhook_with_wrong_signature_is_rejected(client):
    payment_id = client.post("/pay", json={"item": "seal", "amount": 5}).json()["payment_id"]
    body, _ = sign({"payment_id": payment_id, "tx_hash": "0x" + "ab" * 32})

    response = client.post(
        "/webhook", content=body, headers={"X-Signature": "0" * 64}
    )

    assert response.status_code == 401
    assert client.get(f"/pay/{payment_id}").json()["status"] == "pending"


def test_signed_request_still_needs_a_real_transfer(client, main, monkeypatch):
    """A valid signature proves who is asking, not that anyone paid."""
    payment_id = client.post("/pay", json={"item": "seal", "amount": 5}).json()["payment_id"]
    stub_chain(main, monkeypatch, None)  # transaction does not exist
    body, signature = sign({"payment_id": payment_id, "tx_hash": "0x" + "ab" * 32})

    response = client.post("/webhook", content=body, headers={"X-Signature": signature})

    assert response.status_code == 402
    assert "not found" in response.json()["detail"]


def test_transfer_to_another_address_does_not_settle(client, main, monkeypatch):
    payment_id = client.post("/pay", json={"item": "seal", "amount": 5}).json()["payment_id"]
    stub_chain(
        main, monkeypatch,
        receipt(to_address="0x9999999999999999999999999999999999999999"),
    )
    body, signature = sign({"payment_id": payment_id, "tx_hash": "0x" + "ab" * 32})

    response = client.post("/webhook", content=body, headers={"X-Signature": signature})

    assert response.status_code == 402
    assert "no USDC transfer" in response.json()["detail"]


def test_transfer_of_a_worthless_token_does_not_settle(client, main, monkeypatch):
    """Anyone can mint a token and send a billion of it. Only USDC counts."""
    payment_id = client.post("/pay", json={"item": "seal", "amount": 5}).json()["payment_id"]
    stub_chain(
        main, monkeypatch,
        receipt(token="0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef", amount_units=10**18),
    )
    body, signature = sign({"payment_id": payment_id, "tx_hash": "0x" + "ab" * 32})

    response = client.post("/webhook", content=body, headers={"X-Signature": signature})

    assert response.status_code == 402


def test_underpayment_does_not_settle(client, main, monkeypatch):
    payment_id = client.post("/pay", json={"item": "seal", "amount": 5}).json()["payment_id"]
    stub_chain(main, monkeypatch, receipt(amount_units=1_000_000))  # $1 against $5
    body, signature = sign({"payment_id": payment_id, "tx_hash": "0x" + "ab" * 32})

    response = client.post("/webhook", content=body, headers={"X-Signature": signature})

    assert response.status_code == 402
    assert "underpaid" in response.json()["detail"]


def test_reverted_transaction_does_not_settle(client, main, monkeypatch):
    payment_id = client.post("/pay", json={"item": "seal", "amount": 5}).json()["payment_id"]
    stub_chain(main, monkeypatch, receipt(status="0x0"))
    body, signature = sign({"payment_id": payment_id, "tx_hash": "0x" + "ab" * 32})

    response = client.post("/webhook", content=body, headers={"X-Signature": signature})

    assert response.status_code == 402
    assert "reverted" in response.json()["detail"]


def test_insufficient_confirmations_does_not_settle(client, main, monkeypatch):
    payment_id = client.post("/pay", json={"item": "seal", "amount": 5}).json()["payment_id"]
    stub_chain(main, monkeypatch, receipt(), head=1000)  # same block, 1 confirmation
    body, signature = sign({"payment_id": payment_id, "tx_hash": "0x" + "ab" * 32})

    response = client.post("/webhook", content=body, headers={"X-Signature": signature})

    assert response.status_code == 402
    assert "confirmation" in response.json()["detail"]


def test_valid_settlement_succeeds(client, main, monkeypatch):
    payment_id = client.post("/pay", json={"item": "seal", "amount": 5}).json()["payment_id"]
    stub_chain(main, monkeypatch, receipt(amount_units=5_000_000))
    body, signature = sign({"payment_id": payment_id, "tx_hash": "0x" + "ab" * 32})

    response = client.post("/webhook", content=body, headers={"X-Signature": signature})

    assert response.status_code == 200
    assert response.json()["payment"]["status"] == "paid"
    assert client.get(f"/pay/{payment_id}").json()["received_usdc"] == 5.0


def test_one_transaction_cannot_settle_two_payments(client, main, monkeypatch):
    """Without this, a single $5 transfer buys everything in the catalogue."""
    first = client.post("/pay", json={"item": "seal", "amount": 5}).json()["payment_id"]
    second = client.post("/pay", json={"item": "seal", "amount": 5}).json()["payment_id"]
    stub_chain(main, monkeypatch, receipt(amount_units=5_000_000))
    tx = "0x" + "ab" * 32

    body, signature = sign({"payment_id": first, "tx_hash": tx})
    assert client.post("/webhook", content=body, headers={"X-Signature": signature}).status_code == 200

    body, signature = sign({"payment_id": second, "tx_hash": tx})
    response = client.post("/webhook", content=body, headers={"X-Signature": signature})

    assert response.status_code == 409
    assert client.get(f"/pay/{second}").json()["status"] == "pending"


def test_replaying_a_settled_payment_is_idempotent(client, main, monkeypatch):
    payment_id = client.post("/pay", json={"item": "seal", "amount": 5}).json()["payment_id"]
    stub_chain(main, monkeypatch, receipt(amount_units=5_000_000))
    body, signature = sign({"payment_id": payment_id, "tx_hash": "0x" + "ab" * 32})

    first = client.post("/webhook", content=body, headers={"X-Signature": signature})
    second = client.post("/webhook", content=body, headers={"X-Signature": signature})

    assert first.json()["status"] == "verified"
    assert second.json()["status"] == "already_settled"


def test_overpayment_settles(client, main, monkeypatch):
    payment_id = client.post("/pay", json={"item": "seal", "amount": 5}).json()["payment_id"]
    stub_chain(main, monkeypatch, receipt(amount_units=10_000_000))
    body, signature = sign({"payment_id": payment_id, "tx_hash": "0x" + "ab" * 32})

    response = client.post("/webhook", content=body, headers={"X-Signature": signature})

    assert response.status_code == 200
    assert response.json()["payment"]["received_usdc"] == 10.0
