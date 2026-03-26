from __future__ import annotations

from creeper_dripper.config import load_settings
from creeper_dripper.engine.trader import CreeperDripper
from creeper_dripper.errors import POSITION_FINAL_ZOMBIE
from creeper_dripper.models import PortfolioState, ProbeQuote, TakeProfitStep, TradeDecision
from creeper_dripper.models import PositionState
from creeper_dripper.storage.state import new_portfolio
from creeper_dripper.utils import utc_now_iso


def _settings(monkeypatch, tmp_path, *, extra_env: dict | None = None):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BIRDEYE_API_KEY", "x")
    monkeypatch.setenv("JUPITER_API_KEY", "x")
    monkeypatch.setenv("DISCOVERY_INTERVAL_SECONDS", "90")
    monkeypatch.setenv("MAX_ACTIVE_CANDIDATES", "1")
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("JOURNAL_PATH", str(tmp_path / "journal.jsonl"))
    if extra_env:
        for k, v in extra_env.items():
            monkeypatch.setenv(k, str(v))
    return load_settings()


class _DummyBirdeye:
    pass


def _final_zombie_position(now: str) -> PositionState:
    return PositionState(
        token_mint="mintZ",
        symbol="ZED",
        decimals=6,
        status=POSITION_FINAL_ZOMBIE,
        opened_at=now,
        updated_at=now,
        entry_price_usd=1.0,
        avg_entry_price_usd=1.0,
        entry_sol=1.0,
        remaining_qty_atomic=1000,
        remaining_qty_ui=1000.0,
        peak_price_usd=1.0,
        last_price_usd=1.0,
        entry_mark_sol_per_token=0.01,
        last_mark_sol_per_token=0.01,
        peak_mark_sol_per_token=0.01,
        position_id=f"mintZ:{now}",
        take_profit_steps=[TakeProfitStep(trigger_pct=25.0, fraction=1.0)],
        valuation_status="no_route",
        zombie_since=now,
        final_zombie_at=now,
        exit_blocked_cycles=0,
    )


def test_final_zombie_probe_skipped_until_interval(monkeypatch, tmp_path):
    settings = _settings(
        monkeypatch,
        tmp_path,
        extra_env={"FINAL_ZOMBIE_RECOVERY_PROBE_INTERVAL_CYCLES": "4"},
    )
    portfolio: PortfolioState = new_portfolio(5.0)
    pos = _final_zombie_position(utc_now_iso())
    portfolio.open_positions[pos.token_mint] = pos

    quote_calls = []

    class _Exec:
        def __init__(self):
            self.jupiter = object()

        def quote_sell(self, mint, qty):
            quote_calls.append((mint, qty))
            return ProbeQuote(input_amount_atomic=qty, out_amount_atomic=None, price_impact_bps=None, route_ok=False, raw={})

    engine = CreeperDripper(settings, _DummyBirdeye(), _Exec(), portfolio)
    # Avoid side effects.
    monkeypatch.setattr(engine, "_retry_blocked_exit_if_due", lambda *_a, **_kw: None)
    monkeypatch.setattr(engine, "_attempt_exit", lambda *_a, **_kw: None)

    decisions: list[TradeDecision] = []
    # cycles 1-3: skip probes
    for _ in range(3):
        engine._handle_exit_blocked_survival_layer(pos, decisions, utc_now_iso(), valuation_no_route=False)
    assert len(quote_calls) == 0

    # cycle 4: due -> one probe attempt
    engine._handle_exit_blocked_survival_layer(pos, decisions, utc_now_iso(), valuation_no_route=False)
    assert len(quote_calls) == 1


def test_final_zombie_route_return_marks_recoverable_and_allows_exit_path(monkeypatch, tmp_path):
    settings = _settings(
        monkeypatch,
        tmp_path,
        extra_env={"FINAL_ZOMBIE_RECOVERY_PROBE_INTERVAL_CYCLES": "1"},
    )
    portfolio: PortfolioState = new_portfolio(5.0)
    pos = _final_zombie_position(utc_now_iso())
    portfolio.open_positions[pos.token_mint] = pos

    class _Exec:
        def __init__(self):
            self.jupiter = object()

        def quote_sell(self, _mint, qty):
            return ProbeQuote(input_amount_atomic=qty, out_amount_atomic=123, price_impact_bps=10.0, route_ok=True, raw={})

    engine = CreeperDripper(settings, _DummyBirdeye(), _Exec(), portfolio)
    monkeypatch.setattr(engine, "_retry_blocked_exit_if_due", lambda *_a, **_kw: None)
    monkeypatch.setattr(engine, "_attempt_exit", lambda *_a, **_kw: None)

    engine._handle_exit_blocked_survival_layer(pos, [], utc_now_iso(), valuation_no_route=False)
    assert pos.status == "ZOMBIE"
    assert pos.zombie_reason == "final_zombie_recovered_route"

