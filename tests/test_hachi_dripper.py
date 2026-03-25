"""Tests for Hachi-style dripper: Jupiter-quote-driven chunked selling with no TP gate."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from creeper_dripper.config import load_settings
from creeper_dripper.engine.trader import CreeperDripper
from creeper_dripper.models import (
    ExecutionResult,
    PortfolioState,
    PositionState,
    ProbeQuote,
    TakeProfitStep,
    TokenCandidate,
    TradeDecision,
)
from creeper_dripper.storage.state import new_portfolio

_VALID_MINT = "HaChiXXXmintXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
_NOW = "2026-06-15T12:00:00+00:00"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _settings(monkeypatch, tmp_path, *, hachi_enabled: bool = True, max_impact: int = 900):
    monkeypatch.setenv("BIRDEYE_API_KEY", "x")
    monkeypatch.setenv("JUPITER_API_KEY", "x")
    monkeypatch.setenv("BS58_PRIVATE_KEY", "x")
    monkeypatch.setenv("STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("JOURNAL_PATH", str(tmp_path / "journal.jsonl"))
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("HACHI_DRIPPER_ENABLED", "true" if hachi_enabled else "false")
    monkeypatch.setenv("HACHI_MAX_PRICE_IMPACT_BPS", str(max_impact))
    monkeypatch.setenv("DRIP_CHUNK_PCTS", "0.10,0.25,0.50")
    monkeypatch.setenv("DRIP_MIN_CHUNK_WAIT_SECONDS", "30")
    monkeypatch.setenv("DRIP_NEAR_EQUAL_BAND", "0.002")
    settings = load_settings()
    # Force-assign in case load_dotenv(override=True) stomps on monkeypatched values.
    settings.hachi_dripper_enabled = hachi_enabled
    return settings


def _position(
    *,
    remaining: int = 1000,
    entry_sol: float = 1.0,
    pnl_pct: float = 0.0,
    status: str = "OPEN",
) -> PositionState:
    """Build a PositionState with SOL marks set to reflect a given PnL%."""
    entry_mark = entry_sol / max(remaining, 1)
    last_mark = entry_mark * (1.0 + pnl_pct / 100.0)
    return PositionState(
        token_mint=_VALID_MINT,
        symbol="HCHI",
        decimals=0,
        status=status,
        opened_at=_NOW,
        updated_at=_NOW,
        entry_price_usd=1.0,
        avg_entry_price_usd=1.0,
        entry_sol=entry_sol,
        remaining_qty_atomic=remaining,
        remaining_qty_ui=float(remaining),
        peak_price_usd=1.0,
        last_price_usd=1.0,
        entry_mark_sol_per_token=entry_mark,
        last_mark_sol_per_token=last_mark,
        peak_mark_sol_per_token=last_mark,
        position_id=f"{_VALID_MINT}:{_NOW}",
        # TP ladder at 25% – must NOT be required to start selling in Hachi mode
        take_profit_steps=[TakeProfitStep(trigger_pct=25.0, fraction=0.5)],
    )


def _success_result(*, requested: int, sold: int, signature: str = "sig1") -> ExecutionResult:
    return ExecutionResult(
        status="success",
        requested_amount=requested,
        executed_amount=sold,
        output_amount=1_000_000_000,
        signature=signature,
        is_partial=False,
        diagnostic_metadata={
            "post_sell_settlement": {
                "settlement_confirmed": True,
                "sold_atomic_settled": sold,
                "sold_atomic_source": "jupiter_execute",
            },
        },
    )


class DummyBirdeye:
    def build_candidate(self, seed):
        return TokenCandidate(
            address=seed["address"],
            symbol=seed.get("symbol", "T"),
            decimals=0,
            price_usd=1.0,
        )


class DummyExecutor:
    """Controllable executor stub."""

    def __init__(
        self,
        sell_results: list[ExecutionResult],
        *,
        quote_out_per_atomic: float = 1.0,
        quote_impact_bps: float = 50.0,
        quote_route_ok: bool = True,
    ) -> None:
        self.sell_results = sell_results
        self.quote_out_per_atomic = quote_out_per_atomic
        self.quote_impact_bps = quote_impact_bps
        self.quote_route_ok = quote_route_ok
        self.jupiter = object()
        self._idx = 0
        self.sell_calls: list[tuple[str, int]] = []
        self.quote_calls: list[tuple[str, int]] = []

    def sell(self, token_mint: str, amount_atomic: int):
        self.sell_calls.append((token_mint, amount_atomic))
        result = self.sell_results[min(self._idx, len(self.sell_results) - 1)]
        self._idx += 1
        probe = ProbeQuote(
            input_amount_atomic=amount_atomic,
            out_amount_atomic=int(amount_atomic * self.quote_out_per_atomic),
            price_impact_bps=self.quote_impact_bps,
            route_ok=True,
            raw={},
        )
        return result, probe

    def quote_sell(self, token_mint: str, amount_atomic: int) -> ProbeQuote:
        self.quote_calls.append((token_mint, amount_atomic))
        out = int(amount_atomic * self.quote_out_per_atomic) if self.quote_route_ok else None
        return ProbeQuote(
            input_amount_atomic=amount_atomic,
            out_amount_atomic=out,
            price_impact_bps=self.quote_impact_bps if self.quote_route_ok else None,
            route_ok=self.quote_route_ok,
            raw={},
        )

    def buy(self, *_a, **_k):
        return ExecutionResult(status="failed", requested_amount=1, error="buy not used"), ProbeQuote(
            input_amount_atomic=1,
            out_amount_atomic=None,
            price_impact_bps=None,
            route_ok=False,
            raw={},
        )


# ---------------------------------------------------------------------------
# A. Pre-TP-threshold selling
# ---------------------------------------------------------------------------


def test_hachi_sells_before_25pct_tp_threshold(monkeypatch, tmp_path):
    """Hachi dripper must sell a chunk even when PnL is only 5% (well below the 25% TP trigger).

    This is the core regression test: the old code would log decision=none /
    reason=below_threshold here and never sell.  Hachi must sell immediately.
    """
    settings = _settings(monkeypatch, tmp_path, hachi_enabled=True)
    portfolio = new_portfolio(5.0)
    pos = _position(remaining=1000, pnl_pct=5.0)  # 5% gain, far below 25% TP
    portfolio.open_positions[_VALID_MINT] = pos

    # chunk = 50% of 1000 = 500 atoms (DRIP_CHUNK_PCTS has 0.50 → 500 is the largest viable)
    executor = DummyExecutor(
        [_success_result(requested=500, sold=500)],
    )
    engine = CreeperDripper(settings, DummyBirdeye(), executor, portfolio)

    decisions: list[TradeDecision] = []
    # Call _evaluate_exit_rules directly so we can control PnL via mark fields
    candidate = TokenCandidate(address=_VALID_MINT, symbol="HCHI", decimals=0)
    engine._evaluate_exit_rules(pos, candidate, decisions, _NOW)

    actions = [d.action for d in decisions]
    assert "DRIPPER_CHUNK_SELECTED" in actions, f"expected DRIPPER_CHUNK_SELECTED, got {actions}"
    assert "DRIPPER_CHUNK_EXECUTED" in actions, f"expected DRIPPER_CHUNK_EXECUTED, got {actions}"
    assert "SELL" in actions, f"expected SELL action in decisions, got {actions}"
    # TP thresholds must NOT have been the trigger
    assert not any("take_profit" in (d.reason or "") for d in decisions), (
        "TP reason must not appear when hachi is the driver"
    )
    assert pos.remaining_qty_atomic == 500
    assert pos.drip_chunks_done == 1
    assert pos.drip_next_chunk_at is not None, "next chunk must be scheduled"


def test_hachi_sells_at_zero_pct_pnl(monkeypatch, tmp_path):
    """Hachi must sell even when PnL is exactly 0% (position hasn't moved)."""
    settings = _settings(monkeypatch, tmp_path, hachi_enabled=True)
    portfolio = new_portfolio(5.0)
    pos = _position(remaining=1000, pnl_pct=0.0)
    portfolio.open_positions[_VALID_MINT] = pos

    executor = DummyExecutor([_success_result(requested=500, sold=500)])
    engine = CreeperDripper(settings, DummyBirdeye(), executor, portfolio)

    decisions: list[TradeDecision] = []
    candidate = TokenCandidate(address=_VALID_MINT, symbol="HCHI", decimals=0)
    engine._evaluate_exit_rules(pos, candidate, decisions, _NOW)

    assert any(d.action == "DRIPPER_CHUNK_EXECUTED" for d in decisions)
    assert pos.remaining_qty_atomic == 500


# ---------------------------------------------------------------------------
# B. Jupiter quote drives chunk selection
# ---------------------------------------------------------------------------


def test_hachi_chunk_driven_by_jupiter_quote(monkeypatch, tmp_path):
    """When Jupiter returns a good quote, dripper selects the chunk and executes it.
    When the quote has no route, dripper emits DRIPPER_WAIT instead of selling.
    """
    settings = _settings(monkeypatch, tmp_path, hachi_enabled=True)
    portfolio = new_portfolio(5.0)
    pos = _position(remaining=1000, pnl_pct=10.0)
    portfolio.open_positions[_VALID_MINT] = pos

    # Case 1: good route → should sell
    executor = DummyExecutor(
        [_success_result(requested=500, sold=500)],
        quote_route_ok=True,
    )
    engine = CreeperDripper(settings, DummyBirdeye(), executor, portfolio)
    decisions: list[TradeDecision] = []
    engine._run_hachi_dripper(pos, decisions, _NOW)

    assert any(d.action == "DRIPPER_CHUNK_SELECTED" for d in decisions)
    assert any(d.action == "DRIPPER_CHUNK_EXECUTED" for d in decisions)
    assert len(executor.quote_calls) > 0, "dripper must call quote_sell"
    assert pos.remaining_qty_atomic == 500


def test_hachi_no_route_emits_dripper_wait(monkeypatch, tmp_path):
    """When Jupiter has no sell route, dripper emits DRIPPER_WAIT (never attempts a sell)."""
    settings = _settings(monkeypatch, tmp_path, hachi_enabled=True)
    portfolio = new_portfolio(5.0)
    pos = _position(remaining=1000, pnl_pct=10.0)
    portfolio.open_positions[_VALID_MINT] = pos

    executor = DummyExecutor([], quote_route_ok=False)
    engine = CreeperDripper(settings, DummyBirdeye(), executor, portfolio)

    decisions: list[TradeDecision] = []
    engine._run_hachi_dripper(pos, decisions, _NOW)

    actions = [d.action for d in decisions]
    assert "DRIPPER_WAIT" in actions, f"expected DRIPPER_WAIT, got {actions}"
    assert "DRIPPER_CHUNK_SELECTED" not in actions
    assert len(executor.sell_calls) == 0


def test_hachi_high_impact_quote_emits_dripper_wait(monkeypatch, tmp_path):
    """If all chunks have price impact above hachi_max_price_impact_bps, no sell fires."""
    settings = _settings(monkeypatch, tmp_path, hachi_enabled=True, max_impact=300)
    portfolio = new_portfolio(5.0)
    pos = _position(remaining=1000, pnl_pct=5.0)
    portfolio.open_positions[_VALID_MINT] = pos

    # Quote returns impact_bps=1000, which exceeds max_impact=300
    executor = DummyExecutor([], quote_route_ok=True, quote_impact_bps=1000.0)
    engine = CreeperDripper(settings, DummyBirdeye(), executor, portfolio)

    decisions: list[TradeDecision] = []
    engine._run_hachi_dripper(pos, decisions, _NOW)

    actions = [d.action for d in decisions]
    assert "DRIPPER_WAIT" in actions
    assert "DRIPPER_CHUNK_SELECTED" not in actions


# ---------------------------------------------------------------------------
# C. Hard exits override the dripper
# ---------------------------------------------------------------------------


def test_hachi_stop_loss_overrides_dripper_timing_gate(monkeypatch, tmp_path):
    """When stop_loss fires while dripper has a scheduled next chunk, it must:
    - emit DRIPPER_OVERRIDE_HARD_EXIT
    - clear drip_next_chunk_at
    - immediately sell the full remaining quantity
    """
    settings = _settings(monkeypatch, tmp_path, hachi_enabled=True)
    portfolio = new_portfolio(5.0)
    pos = _position(remaining=1000, pnl_pct=-25.0)  # -25% → stop_loss triggers
    portfolio.open_positions[_VALID_MINT] = pos

    # Simulate dripper having previously executed one chunk and scheduled the next
    future = (datetime.fromisoformat(_NOW) + timedelta(minutes=5)).isoformat()
    pos.drip_next_chunk_at = future
    pos.drip_chunks_done = 1

    executor = DummyExecutor(
        [_success_result(requested=1000, sold=1000)]
    )
    engine = CreeperDripper(settings, DummyBirdeye(), executor, portfolio)

    decisions: list[TradeDecision] = []
    candidate = TokenCandidate(address=_VALID_MINT, symbol="HCHI", decimals=0)
    engine._evaluate_exit_rules(pos, candidate, decisions, _NOW)

    actions = [d.action for d in decisions]
    assert "DRIPPER_OVERRIDE_HARD_EXIT" in actions, (
        f"expected DRIPPER_OVERRIDE_HARD_EXIT when stop_loss fires mid-drip, got {actions}"
    )
    assert pos.drip_next_chunk_at is None, "hard exit must clear the timing gate"
    assert "SELL" in actions, "stop_loss must complete the sell"
    assert pos.status == "CLOSED"
    # Override reason must reference stop_loss
    override_d = next(d for d in decisions if d.action == "DRIPPER_OVERRIDE_HARD_EXIT")
    assert override_d.reason == "stop_loss"


def test_hachi_trailing_stop_overrides_dripper(monkeypatch, tmp_path):
    """trailing_stop also clears drip_next_chunk_at and fires a full exit."""
    settings = _settings(monkeypatch, tmp_path, hachi_enabled=True)
    portfolio = new_portfolio(5.0)
    # Position peaked at 50% above entry; last mark is 13% below peak → trailing stop fires.
    # trailing_arm_pct=25 → arm requires pnl>=25%; pnl here = 30.5% ✓
    # trailing_stop_pct=12 → fires when last <= peak*(1-0.12)=peak*0.88; 0.87 < 0.88 ✓
    entry_mark = 0.001
    peak_mark = entry_mark * 1.50   # peaked at +50%
    last_mark = peak_mark * 0.87    # retreated 13% from peak (past the 12% trailing floor)
    pos = PositionState(
        token_mint=_VALID_MINT,
        symbol="HCHI",
        decimals=0,
        status="OPEN",
        opened_at=_NOW,
        updated_at=_NOW,
        entry_price_usd=1.0,
        avg_entry_price_usd=1.0,
        entry_sol=1.0,
        remaining_qty_atomic=1000,
        remaining_qty_ui=1000.0,
        peak_price_usd=1.5,
        last_price_usd=float(last_mark / entry_mark),
        entry_mark_sol_per_token=entry_mark,
        last_mark_sol_per_token=float(last_mark),
        peak_mark_sol_per_token=float(peak_mark),
        position_id=f"{_VALID_MINT}:{_NOW}",
        trailing_arm_pct=25.0,
        trailing_stop_pct=12.0,
        take_profit_steps=[TakeProfitStep(trigger_pct=25.0, fraction=0.5)],
    )
    portfolio.open_positions[_VALID_MINT] = pos

    future = (datetime.fromisoformat(_NOW) + timedelta(minutes=2)).isoformat()
    pos.drip_next_chunk_at = future

    executor = DummyExecutor([_success_result(requested=1000, sold=1000)])
    engine = CreeperDripper(settings, DummyBirdeye(), executor, portfolio)

    decisions: list[TradeDecision] = []
    candidate = TokenCandidate(address=_VALID_MINT, symbol="HCHI", decimals=0)
    engine._evaluate_exit_rules(pos, candidate, decisions, _NOW)

    actions = [d.action for d in decisions]
    assert "DRIPPER_OVERRIDE_HARD_EXIT" in actions
    assert pos.drip_next_chunk_at is None
    assert pos.status == "CLOSED"


# ---------------------------------------------------------------------------
# D. Accounting correctness
# ---------------------------------------------------------------------------


def test_hachi_sol_accounting_after_chunk(monkeypatch, tmp_path):
    """After a successful Hachi chunk, SOL proceeds are credited to portfolio.cash_sol."""
    settings = _settings(monkeypatch, tmp_path, hachi_enabled=True)
    portfolio = new_portfolio(5.0)
    cash_before = portfolio.cash_sol
    pos = _position(remaining=1000, pnl_pct=5.0)
    portfolio.open_positions[_VALID_MINT] = pos

    # output_amount = 1_000_000_000 lamports = 1.0 SOL
    executor = DummyExecutor([_success_result(requested=500, sold=500)])
    engine = CreeperDripper(settings, DummyBirdeye(), executor, portfolio)

    decisions: list[TradeDecision] = []
    engine._run_hachi_dripper(pos, decisions, _NOW)

    assert any(d.action == "DRIPPER_CHUNK_EXECUTED" for d in decisions)
    assert portfolio.cash_sol == pytest.approx(cash_before + 1.0), (
        "SOL proceeds from chunk must be credited to portfolio cash"
    )
    assert pos.realized_sol == pytest.approx(1.0), (
        "position realized_sol must reflect the chunk proceeds"
    )
    assert pos.remaining_qty_atomic == 500


def test_hachi_remaining_qty_never_goes_negative(monkeypatch, tmp_path):
    """remaining_qty_atomic must stay >= 0 even when chunk_qty >= remaining."""
    settings = _settings(monkeypatch, tmp_path, hachi_enabled=True)
    portfolio = new_portfolio(5.0)
    pos = _position(remaining=100, pnl_pct=5.0)  # small position
    portfolio.open_positions[_VALID_MINT] = pos

    # Executor sells all 100 (the 50% chunk = 50, but sell returns sold=100 due to rounding)
    executor = DummyExecutor([_success_result(requested=50, sold=100)])
    engine = CreeperDripper(settings, DummyBirdeye(), executor, portfolio)

    decisions: list[TradeDecision] = []
    engine._run_hachi_dripper(pos, decisions, _NOW)

    assert pos.remaining_qty_atomic >= 0, "remaining qty must never go negative"


# ---------------------------------------------------------------------------
# E. Repeated chunks reduce remaining qty
# ---------------------------------------------------------------------------


def test_hachi_repeated_chunks_drain_position(monkeypatch, tmp_path):
    """Three consecutive Hachi cycles each sell a chunk, progressively draining the position."""
    monkeypatch.setenv("DRIP_CHUNK_PCTS", "0.10")  # 10% each cycle
    settings = _settings(monkeypatch, tmp_path, hachi_enabled=True)
    portfolio = new_portfolio(5.0)
    pos = _position(remaining=1000, pnl_pct=3.0)
    portfolio.open_positions[_VALID_MINT] = pos

    results = [
        _success_result(requested=100, sold=100, signature="s1"),
        _success_result(requested=90, sold=90, signature="s2"),
        _success_result(requested=81, sold=81, signature="s3"),
    ]
    executor = DummyExecutor(results)
    engine = CreeperDripper(settings, DummyBirdeye(), executor, portfolio)

    # Chunk 1
    d1: list[TradeDecision] = []
    engine._run_hachi_dripper(pos, d1, _NOW)
    assert pos.remaining_qty_atomic == 900
    assert pos.drip_chunks_done == 1
    assert pos.drip_next_chunk_at is not None

    # Chunk 2: bypass timing gate
    pos.drip_next_chunk_at = None
    later1 = (datetime.fromisoformat(_NOW) + timedelta(seconds=60)).isoformat()
    d2: list[TradeDecision] = []
    engine._run_hachi_dripper(pos, d2, later1)
    assert pos.remaining_qty_atomic == 810
    assert pos.drip_chunks_done == 2

    # Chunk 3: bypass timing gate again
    pos.drip_next_chunk_at = None
    later2 = (datetime.fromisoformat(_NOW) + timedelta(seconds=120)).isoformat()
    d3: list[TradeDecision] = []
    engine._run_hachi_dripper(pos, d3, later2)
    assert pos.remaining_qty_atomic == 729
    assert pos.drip_chunks_done == 3

    # Confirm chunk sold amounts match
    for d in (d1, d2, d3):
        assert any(x.action == "DRIPPER_CHUNK_EXECUTED" for x in d)
        assert any(x.action == "SELL" for x in d)


def test_hachi_timing_gate_prevents_rapid_resell(monkeypatch, tmp_path):
    """After a chunk executes, DRIPPER_WAIT fires on the immediate next call (gate not yet due)."""
    settings = _settings(monkeypatch, tmp_path, hachi_enabled=True)
    portfolio = new_portfolio(5.0)
    pos = _position(remaining=1000, pnl_pct=5.0)
    portfolio.open_positions[_VALID_MINT] = pos

    executor = DummyExecutor([_success_result(requested=500, sold=500)])
    engine = CreeperDripper(settings, DummyBirdeye(), executor, portfolio)

    # First call: chunk executes
    d1: list[TradeDecision] = []
    engine._run_hachi_dripper(pos, d1, _NOW)
    assert any(x.action == "DRIPPER_CHUNK_EXECUTED" for x in d1)

    # Immediate second call with same timestamp: gate must block
    d2: list[TradeDecision] = []
    engine._run_hachi_dripper(pos, d2, _NOW)
    actions2 = [x.action for x in d2]
    assert "DRIPPER_WAIT" in actions2, "timing gate must block rapid re-entry"
    assert "DRIPPER_CHUNK_SELECTED" not in actions2


# ---------------------------------------------------------------------------
# F. Disabled Hachi: legacy TP ladder still works
# ---------------------------------------------------------------------------


def test_legacy_tp_ladder_used_when_hachi_disabled(monkeypatch, tmp_path):
    """When hachi_dripper_enabled=False, the TP ladder is the trigger (legacy path unchanged)."""
    settings = _settings(monkeypatch, tmp_path, hachi_enabled=False)
    portfolio = new_portfolio(5.0)
    pos = _position(remaining=1000, pnl_pct=5.0)  # below 25% TP → no sell
    portfolio.open_positions[_VALID_MINT] = pos

    executor = DummyExecutor([])
    engine = CreeperDripper(settings, DummyBirdeye(), executor, portfolio)

    decisions: list[TradeDecision] = []
    candidate = TokenCandidate(address=_VALID_MINT, symbol="HCHI", decimals=0)
    engine._evaluate_exit_rules(pos, candidate, decisions, _NOW)

    actions = [d.action for d in decisions]
    # No Hachi decisions
    assert "DRIPPER_CHUNK_SELECTED" not in actions
    assert "DRIPPER_CHUNK_EXECUTED" not in actions
    # No sell attempt (below TP threshold)
    assert "SELL" not in actions
    assert len(executor.sell_calls) == 0
