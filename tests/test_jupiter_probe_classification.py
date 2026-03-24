from __future__ import annotations

import requests

from creeper_dripper.clients.jupiter import JupiterBadRequestError, JupiterClient
from creeper_dripper.config import load_settings
from creeper_dripper.engine.discovery import discover_candidates
from creeper_dripper.errors import REJECT_JUPITER_BAD_PROBE
from creeper_dripper.execution.executor import TradeExecutor
from creeper_dripper.models import TokenCandidate


def _settings(monkeypatch):
    monkeypatch.setenv("BIRDEYE_API_KEY", "x")
    monkeypatch.setenv("JUPITER_API_KEY", "x")
    return load_settings()


def test_jupiter_400_buy_probe_classified_and_body_preserved(monkeypatch):
    settings = _settings(monkeypatch)

    class StubBirdeye:
        def trending_tokens(self, limit=25):
            return [{"address": "mint1", "symbol": "TOK1"}, {"address": "mint2", "symbol": "TOK2"}]

        def new_listings(self, limit=10):
            return []

        def build_candidate(self, seed):
            from creeper_dripper.models import TokenCandidate

            return TokenCandidate(
                address=seed["address"],
                symbol=seed["symbol"],
                decimals=6,
                price_usd=1.0,
                liquidity_usd=250000,
                volume_24h_usd=400000,
                buy_sell_ratio_1h=1.4,
                exit_liquidity_available=False,
                exit_liquidity_reason="birdeye_exit_liquidity_unsupported_chain",
            )

    class StubJupiter:
        def __init__(self):
            self.calls = 0

        def probe_quote(self, **kwargs):
            self.calls += 1
            if kwargs["output_mint"] == "mint1":
                raise JupiterBadRequestError(
                    endpoint="/order",
                    params={"inputMint": kwargs["input_mint"], "outputMint": kwargs["output_mint"]},
                    body='{"error":"probe validation failed"}',
                )
            from creeper_dripper.models import ProbeQuote

            return ProbeQuote(
                input_amount_atomic=kwargs["amount_atomic"],
                out_amount_atomic=1000000,
                price_impact_bps=100.0,
                route_ok=True,
                raw={},
            )

    jupiter = StubJupiter()
    candidates, summary = discover_candidates(StubBirdeye(), jupiter, settings)
    assert len(candidates) >= 1  # second candidate proceeds; scan continues
    assert summary["rejection_counts"].get(REJECT_JUPITER_BAD_PROBE, 0) >= 1
    event = next(e for e in summary["events"] if e["reason_code"] == REJECT_JUPITER_BAD_PROBE)
    assert "jupiter_error_body" in event["metadata"]
    assert "probe validation failed" in event["metadata"]["jupiter_error_body"]


def test_jupiter_client_preserves_400_response_body(monkeypatch):
    client = JupiterClient("x")

    class Resp:
        status_code = 400
        text = '{"error":"route disabled"}'

        def raise_for_status(self):
            raise requests.HTTPError("bad request")

    def fake_get(*_args, **_kwargs):
        return Resp()

    monkeypatch.setattr(client._session, "get", fake_get)
    try:
        client.order(
            input_mint="So11111111111111111111111111111111111111112",
            output_mint="x95HN3DWvbfCBtTjGm587z8suK3ec6cwQwgZNLbWKyp",
            amount_atomic=10_000_000,
            slippage_bps=250,
        )
    except JupiterBadRequestError as exc:
        assert exc.status_code == 400
        assert "route disabled" in exc.body
        assert exc.endpoint == "/order"
        assert exc.params["inputMint"] == "So11111111111111111111111111111111111111112"
    else:
        raise AssertionError("expected JupiterBadRequestError")


def test_curl_proofed_success_params_match_app_params():
    params = JupiterClient.build_order_params(
        input_mint="So11111111111111111111111111111111111111112",
        output_mint="x95HN3DWvbfCBtTjGm587z8suK3ec6cwQwgZNLbWKyp",
        amount_atomic=10_000_000,
        slippage_bps=250,
    )
    assert params == {
        "inputMint": "So11111111111111111111111111111111111111112",
        "outputMint": "x95HN3DWvbfCBtTjGm587z8suK3ec6cwQwgZNLbWKyp",
        "amount": "10000000",
        "slippageBps": "250",
    }


def test_buy_classifies_dry_run_as_skipped(monkeypatch):
    settings = _settings(monkeypatch)
    settings.dry_run = True
    settings.live_trading_enabled = False

    class StubJupiter:
        def probe_quote(self, **kwargs):
            from creeper_dripper.models import ProbeQuote

            return ProbeQuote(
                input_amount_atomic=kwargs["amount_atomic"],
                out_amount_atomic=123,
                price_impact_bps=10.0,
                route_ok=True,
                raw={},
            )

    executor = TradeExecutor(StubJupiter(), owner=None, settings=settings)
    token = TokenCandidate(address="mint1", symbol="TOK", decimals=6, price_usd=1.0)
    result, _quote = executor.buy(token, 0.1)
    assert result.status == "skipped"
    assert result.diagnostic_code == "execute_skipped_dry_run"
