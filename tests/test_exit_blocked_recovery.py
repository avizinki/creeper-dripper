from __future__ import annotations

from creeper_dripper.config import load_settings
from creeper_dripper.engine.trader import CreeperDripper
from creeper_dripper.models import PortfolioState, PositionState, ProbeQuote, TakeProfitStep
from creeper_dripper.storage.state import new_portfolio
from creeper_dripper.utils import utc_now_iso


class DummyBirdeye:
    pass


class ToggleQuoteExecutor:
    def __init__(self, *, ok_after: int):
        self.jupiter = object()
        self.ok_after = ok_after
        self.calls = 0

    def quote_sell(self, _mint: str, _qty_atomic: int):
        self.calls += 1
        ok = self.calls >= self.ok_after
        return ProbeQuote(
            input_amount_atomic=_qty_atomic,
            out_amount_atomic=(1 if ok else None),
            price_impact_bps=None,
            route_ok=ok,
            raw={},
        )

    def sell(self, *_args, **_kwargs):
        raise AssertionError("should not execute sells in this unit test")


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
    monkeypatch.setenv("EXIT_BLOCKED_RETRY_CYCLES", "3")
    monkeypatch.setenv("EXIT_BLOCKED_MICRO_PROBE_CYCLES", "8")
    monkeypatch.setenv("ZOMBIE_RETRY_INTERVAL_CYCLES", "10")
    return load_settings()


def _blocked_position() -> PositionState:
    now = utc_now_iso()
    return PositionState(
        token_mint="mintX",
        symbol="TOK",
        decimals=6,
        status="EXIT_BLOCKED",
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


def test_blocked_to_zombie_and_no_spam(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    portfolio: PortfolioState = new_portfolio(5.0)
    pos = _blocked_position()
    portfolio.open_positions[pos.token_mint] = pos
    executor = ToggleQuoteExecutor(ok_after=9999)  # never ok
    engine = CreeperDripper(settings, DummyBirdeye(), executor, portfolio)

    # Prevent any normal exit attempts in stage1 from calling executor.sell
    monkeypatch.setattr(engine, "_retry_blocked_exit_if_due", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(engine, "_attempt_exit", lambda *_args, **_kwargs: None)

    # Run cycles through micro-probe window and into zombie.
    for _ in range(9):
        engine._handle_exit_blocked_survival_layer(pos, [], utc_now_iso(), valuation_no_route=False)

    assert pos.status == "ZOMBIE"
    assert pos.zombie_reason == "no_route_persistent"
    assert pos.zombie_since is not None

    # After zombie, probes should only happen every ZOMBIE_RETRY_INTERVAL_CYCLES.
    calls_before = executor.calls
    for _ in range(9):
        engine._handle_exit_blocked_survival_layer(pos, [], utc_now_iso(), valuation_no_route=False)
    calls_after = executor.calls
    assert (calls_after - calls_before) <= 1


def test_zombie_recovery_emits_event(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    portfolio: PortfolioState = new_portfolio(5.0)
    pos = _blocked_position()
    pos.status = "ZOMBIE"
    pos.exit_blocked_cycles = 9  # next cycle -> 10 triggers retry interval
    portfolio.open_positions[pos.token_mint] = pos
    executor = ToggleQuoteExecutor(ok_after=1)  # ok immediately
    engine = CreeperDripper(settings, DummyBirdeye(), executor, portfolio)

    monkeypatch.setattr(engine, "_attempt_exit", lambda *_args, **_kwargs: None)

    engine._handle_exit_blocked_survival_layer(pos, [], utc_now_iso(), valuation_no_route=False)
    events = engine.events.to_dicts()
    assert any(e["event_type"] == "zombie_recovered" for e in events)

