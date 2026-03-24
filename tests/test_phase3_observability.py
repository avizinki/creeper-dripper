from __future__ import annotations

from types import SimpleNamespace

import requests

from creeper_dripper.clients.birdeye import BirdeyeClient
from creeper_dripper.config import load_settings
from creeper_dripper.engine.scoring import rejection_reasons
from creeper_dripper.engine.trader import CreeperDripper
from creeper_dripper.errors import REJECT_LOW_LIQUIDITY, SAFETY_DAILY_LOSS_CAP, SAFETY_MAX_CONSEC_EXEC_FAILURES
from creeper_dripper.execution.executor import TradeExecutor
from creeper_dripper.models import ExecutionResult, PortfolioState, ProbeQuote, TokenCandidate
from creeper_dripper.storage.state import new_portfolio
from creeper_dripper.utils import utc_now_iso


def _settings(monkeypatch, tmp_path):
    monkeypatch.setenv("BIRDEYE_API_KEY", "x")
    monkeypatch.setenv("JUPITER_API_KEY", "x")
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("JOURNAL_PATH", str(tmp_path / "journal.jsonl"))
    return load_settings()


def test_rejection_reason_emission(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    c = TokenCandidate(address="m", symbol="S", liquidity_usd=1.0)
    reasons = rejection_reasons(c, settings)
    assert REJECT_LOW_LIQUIDITY in reasons


def test_birdeye_retry_behavior(monkeypatch):
    client = BirdeyeClient("x")
    calls = {"n": 0}

    class DummyResponse:
        def __init__(self, ok: bool):
            self.status_code = 200
            self._ok = ok

        def raise_for_status(self):
            if not self._ok:
                raise requests.exceptions.Timeout("timeout")

        def json(self):
            return {"success": True, "data": {"tokens": []}}

    def fake_request(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise requests.exceptions.Timeout("timeout")
        return DummyResponse(True)

    monkeypatch.setattr(client._session, "request", fake_request)
    out = client.trending_tokens(limit=1)
    assert out == []
    assert calls["n"] == 3


class DummyExecutor:
    def __init__(self):
        self.jupiter = object()

    def wallet_token_balance_atomic(self, _mint):
        return 100

    def transaction_status(self, _sig):
        return None

    def buy(self, *_args, **_kwargs):
        return ExecutionResult(status="failed", requested_amount=1, diagnostic_code="order_failed", error="x"), ProbeQuote(
            input_amount_atomic=1, out_amount_atomic=None, price_impact_bps=None, route_ok=False, raw={}
        )

    def sell(self, *_args, **_kwargs):
        return ExecutionResult(status="failed", requested_amount=1, diagnostic_code="execute_failed", error="x"), ProbeQuote(
            input_amount_atomic=1, out_amount_atomic=None, price_impact_bps=None, route_ok=False, raw={}
        )


class DummyBirdeye:
    pass


def test_safety_stop_on_consecutive_execution_failures(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    portfolio: PortfolioState = new_portfolio(5.0)
    portfolio.consecutive_execution_failures = settings.max_consecutive_execution_failures
    engine = CreeperDripper(settings, DummyBirdeye(), DummyExecutor(), portfolio)
    reason = engine._evaluate_safety(utc_now_iso())
    assert reason == SAFETY_MAX_CONSEC_EXEC_FAILURES


def test_safety_stop_on_daily_realized_loss_cap(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    portfolio: PortfolioState = new_portfolio(5.0)
    portfolio.total_realized_sol = -abs(settings.daily_realized_loss_cap_sol)
    engine = CreeperDripper(settings, DummyBirdeye(), DummyExecutor(), portfolio)
    reason = engine._evaluate_safety(utc_now_iso())
    assert reason == SAFETY_DAILY_LOSS_CAP


def test_cycle_summary_counts(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    portfolio: PortfolioState = new_portfolio(5.0)
    engine = CreeperDripper(settings, DummyBirdeye(), DummyExecutor(), portfolio)
    summary = engine._cycle_summary(
        utc_now_iso(),
        {"seeds_total": 10, "candidates_built": 8, "candidates_accepted": 2, "candidates_rejected_total": 6, "rejection_counts": {"reject_low_liquidity": 3}},
        [],
    )
    assert summary["seeds_total"] == 10
    assert summary["candidates_rejected_total"] == 6
    assert "reject_low_liquidity" in summary["rejection_counts"]


def test_jupiter_diagnostic_reason_mapping():
    raw = SimpleNamespace(output_amount_result=None, input_amount_result=None, signature=None, error="boom")
    res = TradeExecutor._normalize_execution_result(raw, requested_amount=10)
    assert res.status == "failed"
    assert res.diagnostic_code == "execute_failed"
