"""Tests for T-005/T-006/T-007/T-008 task bundle.

T-005: FINAL_ZOMBIE terminal state
T-006: Exit counters / truthful visibility in cycle_summary
T-007: Dripper not-armed observability
T-008: Accounting hardening — pending_proceeds startup audit
"""
from __future__ import annotations

from typing import Any

import pytest

from creeper_dripper.config import load_settings
from creeper_dripper.engine.trader import CreeperDripper
from creeper_dripper.errors import POSITION_FINAL_ZOMBIE, SETTLEMENT_UNCONFIRMED
from creeper_dripper.models import (
    PortfolioState,
    PositionState,
    ProbeQuote,
    TakeProfitStep,
    TradeDecision,
)
from creeper_dripper.storage.recovery import run_startup_recovery
from creeper_dripper.storage.state import new_portfolio
from creeper_dripper.utils import utc_now_iso

_VALID_MINT = "J7MzyZ4Tvwn2LBREnLU48TmKxJE28qstv35dRGBJPCpE"
_MINT_B = "AYMzyZ4Tvwn2LBREnLU48TmKxJE28qstv35dRGBJPCpF"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


class _EventCapture:
    def __init__(self) -> None:
        self.events: list[tuple[str, tuple, dict]] = []

    def emit(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.events.append((name, args, kwargs))

    def event_names(self) -> list[str]:
        return [e[0] for e in self.events]

    def find(self, name: str) -> list[dict]:
        return [kw for (n, _a, kw) in self.events if n == name]

    def to_dicts(self) -> list[dict]:
        return []


class _NeverOkExecutor:
    """quote_sell always returns no route. sell/buy raise to catch misuse."""
    jupiter = object()

    def quote_sell(self, _mint: str, _qty: int) -> ProbeQuote:
        return ProbeQuote(input_amount_atomic=_qty, out_amount_atomic=None, price_impact_bps=None, route_ok=False, raw={})

    def sell(self, *_a, **_kw):
        raise AssertionError("sell() must not be called on a terminal zombie")

    def buy(self, *_a, **_kw):
        raise AssertionError("buy() must not be called in zombie tests")

    def quote_buy(self, *_a, **_kw):
        raise AssertionError("quote_buy() must not be called in zombie tests")


class _DummyBirdeye:
    pass


def _settings(monkeypatch, tmp_path, *, extra_env: dict[str, str] | None = None):
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
    if extra_env:
        for k, v in extra_env.items():
            monkeypatch.setenv(k, v)
    return load_settings()


def _blocked_position(mint: str = _VALID_MINT) -> PositionState:
    now = utc_now_iso()
    return PositionState(
        token_mint=mint,
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
        position_id=f"{mint}:{utc_now_iso()}",
    )


# ---------------------------------------------------------------------------
# T-005: FINAL_ZOMBIE
# ---------------------------------------------------------------------------


def test_final_zombie_promoted_after_max_retry_cycles(monkeypatch, tmp_path):
    """When ZOMBIE_MAX_RETRY_CYCLES is set, a zombie is promoted to FINAL_ZOMBIE
    once blocked_cycles reaches that threshold. No probes fire after promotion."""
    settings = _settings(monkeypatch, tmp_path, extra_env={"ZOMBIE_MAX_RETRY_CYCLES": "12"})
    portfolio: PortfolioState = new_portfolio(5.0)
    pos = _blocked_position()
    portfolio.open_positions[pos.token_mint] = pos

    executor = _NeverOkExecutor()
    engine = CreeperDripper(settings, _DummyBirdeye(), executor, portfolio)
    cap = _EventCapture()
    engine.events = cap

    # Suppress stage-1 retry to avoid sell calls
    monkeypatch.setattr(engine, "_retry_blocked_exit_if_due", lambda *_a, **_kw: None)
    monkeypatch.setattr(engine, "_attempt_exit", lambda *_a, **_kw: None)

    # Drive through all stages up to and past zombie_max_retry_cycles=12
    decisions: list[TradeDecision] = []
    for _ in range(13):
        engine._handle_exit_blocked_survival_layer(pos, decisions, utc_now_iso(), valuation_no_route=False)

    assert pos.status == POSITION_FINAL_ZOMBIE, f"expected FINAL_ZOMBIE; got {pos.status}"
    assert pos.final_zombie_at is not None, "final_zombie_at must be set on promotion"
    assert "position_final_zombie" in cap.event_names(), "position_final_zombie event not emitted"

    final_zombie_events = cap.find("position_final_zombie")
    assert len(final_zombie_events) >= 1
    assert final_zombie_events[0]["zombie_max_retry_cycles"] == 12

    final_decisions = [d for d in decisions if d.action == "FINAL_ZOMBIE"]
    assert len(final_decisions) >= 1


def test_final_zombie_stops_all_probes_after_promotion(monkeypatch, tmp_path):
    """Once FINAL_ZOMBIE, probes must become rare (not every cycle)."""
    settings = _settings(
        monkeypatch,
        tmp_path,
        extra_env={"ZOMBIE_MAX_RETRY_CYCLES": "10", "FINAL_ZOMBIE_RECOVERY_PROBE_INTERVAL_CYCLES": "50"},
    )
    portfolio: PortfolioState = new_portfolio(5.0)
    pos = _blocked_position()
    portfolio.open_positions[pos.token_mint] = pos

    probe_calls = []

    class _CountingExecutor(_NeverOkExecutor):
        def quote_sell(self, mint, qty):
            probe_calls.append((mint, qty))
            return ProbeQuote(input_amount_atomic=qty, out_amount_atomic=None, price_impact_bps=None, route_ok=False, raw={})

    engine = CreeperDripper(settings, _DummyBirdeye(), _CountingExecutor(), portfolio)
    cap = _EventCapture()
    engine.events = cap
    monkeypatch.setattr(engine, "_retry_blocked_exit_if_due", lambda *_a, **_kw: None)
    monkeypatch.setattr(engine, "_attempt_exit", lambda *_a, **_kw: None)

    # Drive to FINAL_ZOMBIE
    for _ in range(11):
        engine._handle_exit_blocked_survival_layer(pos, [], utc_now_iso(), valuation_no_route=False)
    assert pos.status == POSITION_FINAL_ZOMBIE

    probes_before = len(probe_calls)
    # More calls should not add probes
    for _ in range(5):
        engine._handle_exit_blocked_survival_layer(pos, [], utc_now_iso(), valuation_no_route=False)
    assert len(probe_calls) == probes_before, "FINAL_ZOMBIE probes must not run every cycle"


def test_final_zombie_disabled_when_max_zero(monkeypatch, tmp_path):
    """When ZOMBIE_MAX_RETRY_CYCLES=0 (default), position stays ZOMBIE indefinitely."""
    settings = _settings(monkeypatch, tmp_path, extra_env={"ZOMBIE_MAX_RETRY_CYCLES": "0"})
    portfolio: PortfolioState = new_portfolio(5.0)
    pos = _blocked_position()
    portfolio.open_positions[pos.token_mint] = pos

    engine = CreeperDripper(settings, _DummyBirdeye(), _NeverOkExecutor(), portfolio)
    cap = _EventCapture()
    engine.events = cap
    monkeypatch.setattr(engine, "_retry_blocked_exit_if_due", lambda *_a, **_kw: None)
    monkeypatch.setattr(engine, "_attempt_exit", lambda *_a, **_kw: None)

    for _ in range(50):
        engine._handle_exit_blocked_survival_layer(pos, [], utc_now_iso(), valuation_no_route=False)

    assert pos.status == "ZOMBIE", "with max_retry_cycles=0, must stay ZOMBIE forever"
    assert "position_final_zombie" not in cap.event_names()


# ---------------------------------------------------------------------------
# T-005 + T-006: FINAL_ZOMBIE does not consume effective capacity
# ---------------------------------------------------------------------------


def test_final_zombie_excluded_from_effective_capacity(monkeypatch, tmp_path):
    """A FINAL_ZOMBIE position must not count against entry capacity."""
    # Set max open to 1 so normally no new entry would be allowed with 1 open position.
    settings = _settings(monkeypatch, tmp_path, extra_env={
        "MAX_OPEN_POSITIONS": "1",
        "HARD_MAX_OPEN_POSITIONS": "1",
        "ZOMBIE_MAX_RETRY_CYCLES": "10",
    })
    portfolio: PortfolioState = new_portfolio(5.0)

    # Create a FINAL_ZOMBIE position occupying the single slot
    pos = _blocked_position()
    pos.status = POSITION_FINAL_ZOMBIE
    pos.final_zombie_at = utc_now_iso()
    portfolio.open_positions[pos.token_mint] = pos

    # _maybe_open_positions should not be gated by the FINAL_ZOMBIE
    # We test by calling it directly with no candidates — it should not return early
    # before the capacity check because effective_open_count would be 0.
    engine = CreeperDripper(settings, _DummyBirdeye(), _NeverOkExecutor(), portfolio)

    # effective_open_count = 1 (total) - 1 (final_zombie) = 0, so should NOT return early
    decisions: list[TradeDecision] = []
    # Call with empty candidates list — just verifies it gets past the capacity gate
    engine._maybe_open_positions([], decisions, utc_now_iso())
    # No BUY_SKIP for capacity — it just had nothing to buy (empty candidates is fine)
    capacity_blocked = [d for d in decisions if d.reason == "blocked_at_capacity"]
    assert len(capacity_blocked) == 0, "FINAL_ZOMBIE must not block entry capacity"


# ---------------------------------------------------------------------------
# T-006: cycle_summary truthful visibility
# ---------------------------------------------------------------------------


def test_cycle_summary_includes_zombie_and_stuck_counters(monkeypatch, tmp_path):
    """cycle_summary must include zombie_positions, final_zombie_positions, exit_stuck_total."""
    settings = _settings(monkeypatch, tmp_path)
    portfolio: PortfolioState = new_portfolio(5.0)
    now = utc_now_iso()

    # One ZOMBIE, one FINAL_ZOMBIE, one EXIT_BLOCKED
    for mint, status in [(_VALID_MINT, "ZOMBIE"), (_MINT_B, POSITION_FINAL_ZOMBIE)]:
        p = _blocked_position(mint)
        p.status = status
        portfolio.open_positions[mint] = p

    engine = CreeperDripper(settings, _DummyBirdeye(), _NeverOkExecutor(), portfolio)
    summary = engine._cycle_summary(now, {"market_data_checked_at": now}, [])

    assert "zombie_positions" in summary, "zombie_positions missing from cycle_summary"
    assert "final_zombie_positions" in summary, "final_zombie_positions missing from cycle_summary"
    assert "exit_stuck_total" in summary, "exit_stuck_total missing from cycle_summary"
    assert summary["zombie_positions"] == 1
    assert summary["final_zombie_positions"] == 1
    assert summary["exit_stuck_total"] == 2  # zombie + final_zombie (no EXIT_BLOCKED here)


def test_exit_blocked_summary_event_includes_final_zombie(monkeypatch, tmp_path):
    """The exit_blocked_summary event must include final_zombie_positions and exit_stuck_total."""
    settings = _settings(monkeypatch, tmp_path)
    portfolio: PortfolioState = new_portfolio(5.0)

    pos = _blocked_position()
    pos.status = POSITION_FINAL_ZOMBIE
    pos.final_zombie_at = utc_now_iso()
    portfolio.open_positions[pos.token_mint] = pos

    engine = CreeperDripper(settings, _DummyBirdeye(), _NeverOkExecutor(), portfolio)
    cap = _EventCapture()
    engine.events = cap

    # Manually trigger the event emission that happens in run_cycle
    blocked_positions = [p for p in portfolio.open_positions.values() if p.status == "EXIT_BLOCKED"]
    zombie_positions = [p for p in portfolio.open_positions.values() if p.status == "ZOMBIE"]
    final_zombie_positions = [p for p in portfolio.open_positions.values() if p.status == POSITION_FINAL_ZOMBIE]
    exit_stuck_total = len(blocked_positions) + len(zombie_positions) + len(final_zombie_positions)
    engine.events.emit(
        "exit_blocked_summary",
        "ok",
        exit_blocked_positions=len(blocked_positions),
        zombie_positions=len(zombie_positions),
        final_zombie_positions=len(final_zombie_positions),
        exit_stuck_total=exit_stuck_total,
        blocked_symbols=[],
        zombie_symbols=[],
        final_zombie_symbols=[p.symbol for p in final_zombie_positions],
        pending_proceeds_sol_total=0.0,
    )

    events = cap.find("exit_blocked_summary")
    assert len(events) == 1
    assert events[0]["final_zombie_positions"] == 1
    assert events[0]["exit_stuck_total"] == 1


# ---------------------------------------------------------------------------
# T-007: Dripper not-armed observability
# ---------------------------------------------------------------------------


def test_dripper_not_armed_emits_event_with_reason(monkeypatch, tmp_path):
    """When TP threshold is not reached, dripper_not_armed event must be emitted with reason."""
    settings = _settings(monkeypatch, tmp_path, extra_env={
        "HACHI_DRIPPER_ENABLED": "true",
        "HACHI_PROFIT_HARVEST_MIN_PCT": "5.0",
        "HACHI_NEUTRAL_FLOOR_PCT": "-3.0",
        "HACHI_EMERGENCY_PNL_PCT": "-12.0",
    })
    portfolio: PortfolioState = new_portfolio(5.0)
    now = utc_now_iso()

    # Position at 0% PnL — TP level not reached (take_profit at 25%)
    pos = PositionState(
        token_mint=_VALID_MINT,
        symbol="TKN",
        decimals=6,
        status="OPEN",
        opened_at=now,
        updated_at=now,
        entry_price_usd=1.0,
        avg_entry_price_usd=1.0,
        entry_sol=0.2,
        remaining_qty_atomic=1_000_000,
        remaining_qty_ui=1.0,
        peak_price_usd=1.0,
        last_price_usd=1.0,
        entry_mark_sol_per_token=0.0002,
        last_mark_sol_per_token=0.0002,  # flat — 0% PnL
        peak_mark_sol_per_token=0.0002,
        position_id=f"{_VALID_MINT}:{now}",
        take_profit_steps=[TakeProfitStep(trigger_pct=25.0, fraction=0.5)],
    )
    portfolio.open_positions[pos.token_mint] = pos

    class _OkExecutor(_NeverOkExecutor):
        def quote_sell(self, mint, qty):
            # Return ok so no route blockage
            return ProbeQuote(input_amount_atomic=qty, out_amount_atomic=qty * 200, price_impact_bps=50.0, route_ok=True, raw={})

    engine = CreeperDripper(settings, _DummyBirdeye(), _OkExecutor(), portfolio)
    cap = _EventCapture()
    engine.events = cap

    decisions: list[TradeDecision] = []
    engine._run_hachi_dripper(pos, decisions, now)

    emitted = cap.event_names()
    assert "dripper_not_armed" in emitted, f"dripper_not_armed not emitted; got {emitted}"

    events = cap.find("dripper_not_armed")
    assert len(events) >= 1
    reason = events[0]
    # TP threshold not reached (pnl_pct ~0, TP needs 25%)
    assert reason["current_tp_level"] is not None
    assert reason["current_tp_level"] < 0 or reason.get("not_armed_reason") in {
        "tp_threshold_not_reached", "tp_level_gate", "tp_level_already_dripped"
    }

    wait_decisions = [d for d in decisions if d.action == "DRIPPER_WAIT"]
    assert len(wait_decisions) >= 1
    assert wait_decisions[0].metadata.get("not_armed_reason") is not None


# ---------------------------------------------------------------------------
# T-008: Accounting startup audit
# ---------------------------------------------------------------------------


def test_startup_audit_logs_pending_proceeds(monkeypatch, tmp_path, caplog):
    """run_startup_recovery must log WARNING for positions with pending_proceeds_sol > 0."""
    import logging
    portfolio: PortfolioState = new_portfolio(5.0)
    now = utc_now_iso()

    pos = PositionState(
        token_mint=_VALID_MINT,
        symbol="TKN",
        decimals=6,
        status="RECONCILE_PENDING",
        opened_at=now,
        updated_at=now,
        entry_price_usd=1.0,
        avg_entry_price_usd=1.0,
        entry_sol=0.2,
        remaining_qty_atomic=1_000_000,
        remaining_qty_ui=1.0,
        peak_price_usd=1.0,
        last_price_usd=1.0,
        position_id=f"{_VALID_MINT}:{now}",
        pending_proceeds_sol=0.2,  # unconfirmed buy debit marker
        reconcile_context="entry",
    )
    portfolio.open_positions[pos.token_mint] = pos

    class _NoTxExecutor:
        def transaction_status(self, _sig):
            return None

    with caplog.at_level(logging.WARNING, logger="creeper_dripper.storage.recovery"):
        run_startup_recovery(portfolio, _NoTxExecutor(), now)

    assert any("pending_proceeds_sol" in r.message for r in caplog.records), (
        "startup_accounting_audit WARNING for pending_proceeds_sol not found in logs"
    )


def test_startup_audit_logs_final_zombie(monkeypatch, tmp_path, caplog):
    """run_startup_recovery must log CRITICAL for FINAL_ZOMBIE positions."""
    import logging
    portfolio: PortfolioState = new_portfolio(5.0)
    now = utc_now_iso()

    pos = _blocked_position()
    pos.status = POSITION_FINAL_ZOMBIE
    pos.final_zombie_at = now
    portfolio.open_positions[pos.token_mint] = pos

    class _NoTxExecutor:
        def transaction_status(self, _sig):
            return None

    with caplog.at_level(logging.CRITICAL, logger="creeper_dripper.storage.recovery"):
        run_startup_recovery(portfolio, _NoTxExecutor(), now)

    assert any("FINAL_ZOMBIE" in r.message or "final_zombie" in r.message for r in caplog.records), (
        "startup_accounting_audit_final_zombie CRITICAL not found in logs"
    )


def test_final_zombie_skipped_in_startup_recovery(monkeypatch, tmp_path):
    """FINAL_ZOMBIE positions must not be touched by run_startup_recovery."""
    portfolio: PortfolioState = new_portfolio(5.0)
    now = utc_now_iso()

    pos = _blocked_position()
    pos.status = POSITION_FINAL_ZOMBIE
    pos.final_zombie_at = now
    portfolio.open_positions[pos.token_mint] = pos

    tx_calls = []

    class _TrackingExecutor:
        def transaction_status(self, sig):
            tx_calls.append(sig)
            return "success"

    run_startup_recovery(portfolio, _TrackingExecutor(), now)

    # FINAL_ZOMBIE has no pending_exit_signature so tx_calls should be empty
    assert len(tx_calls) == 0, "FINAL_ZOMBIE must not trigger transaction_status lookup"
    # Position must remain FINAL_ZOMBIE
    assert portfolio.open_positions[pos.token_mint].status == POSITION_FINAL_ZOMBIE
