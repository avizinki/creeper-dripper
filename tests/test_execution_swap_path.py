from __future__ import annotations

from creeper_dripper.clients.jupiter import JupiterClient
from creeper_dripper.config import load_settings
from creeper_dripper.execution.executor import TradeExecutor
from creeper_dripper.models import ProbeQuote, TokenCandidate


def _settings(monkeypatch):
    monkeypatch.setenv("BIRDEYE_API_KEY", "x")
    monkeypatch.setenv("JUPITER_API_KEY", "x")
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    return load_settings()


def test_jupiter_swap_endpoint_returns_transaction(monkeypatch):
    client = JupiterClient("x")

    class Resp:
        status_code = 200
        text = ""

        def raise_for_status(self):
            return None

        def json(self):
            return {"swapTransaction": "ZmFrZV90eA=="}

    monkeypatch.setattr(client._session, "post", lambda *_args, **_kwargs: Resp())
    tx = client.swap_transaction(
        input_mint="So11111111111111111111111111111111111111112",
        output_mint="mint",
        amount_atomic=10_000_000,
        user_public_key="owner",
        slippage_bps=250,
    )
    assert tx == "ZmFrZV90eA=="


def test_executor_builds_swap_and_passes_to_signer(monkeypatch):
    settings = _settings(monkeypatch)

    class FakeOwner:
        def pubkey(self):
            return "owner_pubkey"

    class StubJupiter:
        def __init__(self):
            self.payloads = []

        def probe_quote(self, **kwargs):
            return ProbeQuote(
                input_amount_atomic=kwargs["amount_atomic"],
                out_amount_atomic=777,
                price_impact_bps=50.0,
                route_ok=True,
                raw={},
            )

        def swap_transaction(self, **kwargs):
            self.payloads.append(kwargs)
            return "ZmFrZV90eA=="

    jupiter = StubJupiter()
    executor = TradeExecutor(jupiter, owner=FakeOwner(), settings=settings)
    seen = {"tx": None}

    def fake_sign_and_send(tx_b64: str):
        seen["tx"] = tx_b64
        return "sig123"

    monkeypatch.setattr(executor, "sign_and_send", fake_sign_and_send)
    token = TokenCandidate(address="mint1", symbol="TOK", decimals=6, price_usd=1.0)
    result, _quote = executor.buy(token, 0.1)
    assert jupiter.payloads, "swap endpoint should be called"
    assert seen["tx"] == "ZmFrZV90eA=="
    assert result.status == "success"
    assert result.signature == "sig123"
