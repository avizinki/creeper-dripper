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
    # swap_transaction takes a pre-built quote_response dict, not raw mint/amount params.
    tx = client.swap_transaction(
        quote_response={"inputMint": "So11111111111111111111111111111111111111112", "outputMint": "mint", "amount": "10000000"},
        user_public_key="owner",
    )
    assert tx == "ZmFrZV90eA=="


def test_executor_builds_swap_and_passes_to_signer(monkeypatch):
    settings = _settings(monkeypatch)

    class FakeOwner:
        def pubkey(self):
            return "owner_pubkey"

    class StubJupiter:
        def probe_quote(self, **kwargs):
            return ProbeQuote(
                input_amount_atomic=kwargs["amount_atomic"],
                out_amount_atomic=777,
                price_impact_bps=50.0,
                route_ok=True,
                raw={},
            )

    jupiter = StubJupiter()
    executor = TradeExecutor(jupiter, owner=FakeOwner(), settings=settings)
    monkeypatch.setattr(executor, "build_v2_execution_order", lambda **kw: {"transaction": "ZmFrZV90eA==", "requestId": "rid1"})
    monkeypatch.setattr(
        executor,
        "sign_and_execute_v2",
        lambda _order: ("sig123", {"totalOutputAmount": "777"}),
    )
    token = TokenCandidate(address="mint1", symbol="TOK", decimals=6, price_usd=1.0)
    result, _quote = executor.buy(token, 0.1)
    assert result.status == "success"
    assert result.signature == "sig123"
    assert result.executed_amount == 777
