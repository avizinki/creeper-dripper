from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from creeper_dripper.config import load_settings
from creeper_dripper.engine.trader import (
    MAX_EXIT_RETRY_COUNT,
    MAX_EXIT_RETRY_DELAY_SECONDS,
    CreeperDripper,
    _next_retry_at,
)
from creeper_dripper.models import ExecutionResult, PortfolioState, PositionState, ProbeQuote, TakeProfitStep
from creeper_dripper.storage.state import new_portfolio
from creeper_dripper.utils import utc_now_iso


class DummyBirdeye:
    pass


class AlwaysFailNoRouteExecutor:
    def __init__(self):
        self.jupiter = object()
        self.calls = 0

    def sell(self, _mint: str, qty_atomic: int):
        self.calls += 1
        return (
            ExecutionResult(
                status="failed",
                requested_amount=qty_atomic,
                executed_amount=0,
                diagnostic_code="no_route",
                error="no_route",
                diagnostic_metadata={"classification": "no_route", "response_body": None},
            ),
            ProbeQuote(input_amount_atomic=qty_atomic, out_amount_atomic=None, price_impact_bps=None, route_ok=False, raw={}),
        )


def _settings(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BIRDEYE_API_KEY", "x")
    monkeypatch.setenv("JUPITER_API_KEY", "x")
    monkeypatch.setenv("BS58_PRIVATE_KEY", "x")
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("JOURNAL_PATH", str(tmp_path / "journal.jsonl"))
    monkeypatch.setenv("DISCOVERY_INTERVAL_SECONDS", "30")
    monkeypatch.setenv("MAX_ACTIVE_CANDIDATES", "5")
    return load_settings()


def _exit_pending_position() -> PositionState:
    now = utc_now_iso()
    return PositionState(
        token_mint="mintX",
        symbol="TOK",
        decimals=6,
        status="EXIT_PENDING",
        opened_at=now,
        updated_at=now,
        entry_price_usd=0.0,
        avg_entry_price_usd=0.0,
        entry_sol=0.1,
        remaining_qty_atomic=1_000_000,
        remaining_qty_ui=1.0,
        peak_price_usd=0.0,
        last_price_usd=0.0,
        pending_exit_qty_atomic=1_000_000,
        pending_exit_reason="stop_loss",
        take_profit_steps=[TakeProfitStep(trigger_pct=25.0, fraction=0.1)],
        valuation_status="no_route",
    )


def test_next_retry_at_huge_retry_count_is_bounded():
    now = utc_now_iso()
    ts = _next_retry_at(now, 10**12)
    dt_now = datetime.fromisoformat(now.replace("Z", "+00:00")).astimezone(timezone.utc)
    dt_ts = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)
    assert dt_ts >= dt_now
    assert dt_ts <= dt_now + timedelta(seconds=MAX_EXIT_RETRY_DELAY_SECONDS)


def test_attempt_exit_clamps_retry_count(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    portfolio: PortfolioState = new_portfolio(5.0)
    pos = _exit_pending_position()
    pos.status = "ZOMBIE"  # terminally stuck / survival layer state
    portfolio.open_positions[pos.token_mint] = pos

    engine = CreeperDripper(settings, DummyBirdeye(), AlwaysFailNoRouteExecutor(), portfolio)

    # Repeated attempts must not allow retry_count to grow unbounded.
    for _ in range(MAX_EXIT_RETRY_COUNT + 50):
        engine._attempt_exit(pos, [], utc_now_iso())

    assert 0 <= int(pos.exit_retry_count) <= MAX_EXIT_RETRY_COUNT

