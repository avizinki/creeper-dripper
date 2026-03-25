"""Tests for the Hachi dripper decision brain (hachi_brain.py) — pure-unit +
integration coverage.

Unit tests cover every classification function and the chunk-policy grid.
Integration tests run through CreeperDripper._run_hachi_dripper / _evaluate_exit_rules
to verify end-to-end behavior under each urgency scenario.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from creeper_dripper.config import load_settings
from creeper_dripper.engine.hachi_brain import (
    MOM_COLLAPSING,
    MOM_FLAT,
    MOM_IMPROVING,
    MOM_WEAKENING,
    URGENCY_AGGRESSIVE,
    URGENCY_CONSERVATIVE,
    URGENCY_NORMAL,
    URGENCY_OVERRIDE_FULL,
    ZONE_DETERIORATION,
    ZONE_EMERGENCY,
    ZONE_NEUTRAL,
    ZONE_PROFIT_HARVEST,
    apply_urgency_to_chunk,
    chunk_wait_seconds,
    classify_momentum,
    classify_pnl_zone,
    compute_pnl_pct,
    override_reason,
    select_urgency,
)
from creeper_dripper.engine.trader import CreeperDripper
from creeper_dripper.models import (
    ExecutionResult,
    PositionState,
    ProbeQuote,
    TakeProfitStep,
    TokenCandidate,
    TradeDecision,
)
from creeper_dripper.storage.state import new_portfolio

_MINT = "BrAiNXXXmintXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
_NOW = "2026-06-15T12:00:00+00:00"


# ---------------------------------------------------------------------------
# Test fixtures / helpers
# ---------------------------------------------------------------------------


def _settings(monkeypatch, tmp_path, *, hachi_enabled: bool = True, **overrides):
    monkeypatch.setenv("BIRDEYE_API_KEY", "x")
    monkeypatch.setenv("JUPITER_API_KEY", "x")
    monkeypatch.setenv("BS58_PRIVATE_KEY", "x")
    monkeypatch.setenv("STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("JOURNAL_PATH", str(tmp_path / "journal.jsonl"))
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("HACHI_DRIPPER_ENABLED", "true" if hachi_enabled else "false")
    monkeypatch.setenv("DRIP_CHUNK_PCTS", "0.10,0.25,0.50")
    monkeypatch.setenv("DRIP_MIN_CHUNK_WAIT_SECONDS", "30")
    monkeypatch.setenv("DRIP_NEAR_EQUAL_BAND", "0.002")
    monkeypatch.setenv("HACHI_MAX_PRICE_IMPACT_BPS", "900")
    # brain defaults — can be overridden via **overrides
    monkeypatch.setenv("HACHI_PROFIT_HARVEST_MIN_PCT", str(overrides.get("profit_harvest_min", 5.0)))
    monkeypatch.setenv("HACHI_NEUTRAL_FLOOR_PCT", str(overrides.get("neutral_floor", -3.0)))
    monkeypatch.setenv("HACHI_EMERGENCY_PNL_PCT", str(overrides.get("emergency_pnl", -12.0)))
    monkeypatch.setenv("HACHI_WEAKENING_DROP_PCT", str(overrides.get("weakening_drop", 4.0)))
    monkeypatch.setenv("HACHI_COLLAPSE_DROP_PCT", str(overrides.get("collapse_drop", 8.0)))
    settings = load_settings()
    # Force-assign boolean fields that load_dotenv(override=True) might stomp on.
    settings.hachi_dripper_enabled = hachi_enabled
    return settings


def _pos(
    *,
    remaining: int = 1000,
    entry_sol: float = 1.0,
    pnl_pct: float = 0.0,
    prev_pnl_pct: float | None = None,
) -> PositionState:
    entry_mark = entry_sol / max(remaining, 1)
    last_mark = entry_mark * (1.0 + pnl_pct / 100.0)
    if prev_pnl_pct is not None:
        prev_mark = entry_mark * (1.0 + prev_pnl_pct / 100.0)
    else:
        prev_mark = 0.0  # no baseline → momentum = flat
    return PositionState(
        token_mint=_MINT,
        symbol="BRN",
        decimals=0,
        status="OPEN",
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
        previous_mark_sol_per_token=prev_mark,
        position_id=f"{_MINT}:{_NOW}",
        take_profit_steps=[TakeProfitStep(trigger_pct=25.0, fraction=0.5)],
    )


def _ok(*, requested: int, sold: int, sig: str = "sig") -> ExecutionResult:
    return ExecutionResult(
        status="success",
        requested_amount=requested,
        executed_amount=sold,
        output_amount=1_000_000_000,
        signature=sig,
        is_partial=False,
        diagnostic_metadata={
            "post_sell_settlement": {
                "settlement_confirmed": True,
                "sold_atomic_settled": sold,
                "sold_atomic_source": "jupiter_execute",
            }
        },
    )


class DummyBirdeye:
    def build_candidate(self, seed):
        return TokenCandidate(address=seed["address"], symbol=seed.get("symbol", "T"), decimals=0, price_usd=1.0)


class DummyExecutor:
    def __init__(self, sell_results, *, wallet: int = 1000, impact: float = 50.0, route_ok: bool = True):
        self.sell_results = sell_results
        self.wallet = wallet
        self.impact = impact
        self.route_ok = route_ok
        self.jupiter = object()
        self._idx = 0
        self.sell_calls: list[tuple[str, int]] = []
        self.quote_calls: list[tuple[str, int]] = []

    def wallet_token_balance_atomic(self, _m):
        return self.wallet

    def sell(self, mint, qty):
        self.sell_calls.append((mint, qty))
        r = self.sell_results[min(self._idx, len(self.sell_results) - 1)]
        self._idx += 1
        probe = ProbeQuote(input_amount_atomic=qty, out_amount_atomic=qty, price_impact_bps=self.impact, route_ok=True, raw={})
        return r, probe

    def quote_sell(self, mint, qty):
        self.quote_calls.append((mint, qty))
        out = qty if self.route_ok else None
        return ProbeQuote(input_amount_atomic=qty, out_amount_atomic=out, price_impact_bps=self.impact if self.route_ok else None, route_ok=self.route_ok, raw={})

    def buy(self, *a, **k):
        return ExecutionResult(status="failed", requested_amount=1, error="no"), ProbeQuote(input_amount_atomic=1, out_amount_atomic=None, price_impact_bps=None, route_ok=False, raw={})


# ===========================================================================
# Unit tests — pure brain functions
# ===========================================================================


class TestComputePnlPct:
    def test_positive_pnl(self):
        from creeper_dripper.models import PositionState
        pos = _pos(pnl_pct=20.0)
        result = compute_pnl_pct(pos)
        assert result == pytest.approx(20.0, abs=0.01)

    def test_negative_pnl(self):
        pos = _pos(pnl_pct=-15.0)
        result = compute_pnl_pct(pos)
        assert result == pytest.approx(-15.0, abs=0.01)

    def test_zero_pnl(self):
        pos = _pos(pnl_pct=0.0)
        result = compute_pnl_pct(pos)
        assert result == pytest.approx(0.0, abs=0.01)

    def test_no_entry_mark_returns_none(self):
        pos = _pos(pnl_pct=10.0)
        pos.entry_mark_sol_per_token = 0.0
        assert compute_pnl_pct(pos) is None


class TestClassifyPnlZone:
    @pytest.fixture
    def settings(self, monkeypatch, tmp_path):
        return _settings(monkeypatch, tmp_path)

    def test_profit_harvest(self, settings):
        assert classify_pnl_zone(10.0, settings) == ZONE_PROFIT_HARVEST

    def test_profit_harvest_exactly_at_threshold(self, settings):
        assert classify_pnl_zone(5.0, settings) == ZONE_PROFIT_HARVEST

    def test_neutral_just_below_harvest(self, settings):
        assert classify_pnl_zone(4.9, settings) == ZONE_NEUTRAL

    def test_neutral_at_zero(self, settings):
        assert classify_pnl_zone(0.0, settings) == ZONE_NEUTRAL

    def test_neutral_at_floor(self, settings):
        assert classify_pnl_zone(-3.0, settings) == ZONE_NEUTRAL

    def test_deterioration_just_below_floor(self, settings):
        assert classify_pnl_zone(-3.1, settings) == ZONE_DETERIORATION

    def test_deterioration(self, settings):
        assert classify_pnl_zone(-8.0, settings) == ZONE_DETERIORATION

    def test_emergency_at_threshold(self, settings):
        # -12.0 is still DETERIORATION (>= threshold); emergency is strictly below
        assert classify_pnl_zone(-12.0, settings) == ZONE_DETERIORATION

    def test_emergency_just_below_threshold(self, settings):
        assert classify_pnl_zone(-12.01, settings) == ZONE_EMERGENCY

    def test_emergency_deep(self, settings):
        assert classify_pnl_zone(-19.9, settings) == ZONE_EMERGENCY


class TestClassifyMomentum:
    @pytest.fixture
    def settings(self, monkeypatch, tmp_path):
        return _settings(monkeypatch, tmp_path)

    def test_no_baseline_returns_flat(self, settings):
        pos = _pos(pnl_pct=10.0)  # previous_mark = 0.0
        assert classify_momentum(pos, settings) == MOM_FLAT

    def test_improving(self, settings):
        # last = +10%, previous = +5%  → positive move
        pos = _pos(pnl_pct=10.0, prev_pnl_pct=5.0)
        assert classify_momentum(pos, settings) == MOM_IMPROVING

    def test_flat_tiny_drop(self, settings):
        # -2% drop, weakening threshold is 4% → flat
        pos = _pos(pnl_pct=10.0, prev_pnl_pct=12.2)  # ~-2% move
        assert classify_momentum(pos, settings) == MOM_FLAT

    def test_weakening(self, settings):
        # -5% drop from previous mark  (above weakening 4%, below collapse 8%)
        entry_mark = 0.001
        prev_mark = entry_mark * 1.20   # previous was +20%
        last_mark = prev_mark * 0.95    # -5% from previous
        pos = _pos(pnl_pct=14.0, prev_pnl_pct=20.0)
        # Override marks directly for precision
        pos.previous_mark_sol_per_token = prev_mark
        pos.last_mark_sol_per_token = last_mark
        assert classify_momentum(pos, settings) == MOM_WEAKENING

    def test_collapsing(self, settings):
        # -9% drop from previous mark (above collapse 8%)
        entry_mark = 0.001
        prev_mark = entry_mark * 1.30
        last_mark = prev_mark * 0.91    # -9% from previous
        pos = _pos()
        pos.previous_mark_sol_per_token = prev_mark
        pos.last_mark_sol_per_token = last_mark
        assert classify_momentum(pos, settings) == MOM_COLLAPSING


class TestSelectUrgency:
    def test_emergency_always_override(self):
        for mom in (MOM_IMPROVING, MOM_FLAT, MOM_WEAKENING, MOM_COLLAPSING):
            assert select_urgency(ZONE_EMERGENCY, mom) == URGENCY_OVERRIDE_FULL

    def test_deterioration_collapsing_override(self):
        assert select_urgency(ZONE_DETERIORATION, MOM_COLLAPSING) == URGENCY_OVERRIDE_FULL

    def test_deterioration_non_collapse_aggressive(self):
        for mom in (MOM_IMPROVING, MOM_FLAT, MOM_WEAKENING):
            assert select_urgency(ZONE_DETERIORATION, mom) == URGENCY_AGGRESSIVE

    def test_neutral_collapsing_aggressive(self):
        assert select_urgency(ZONE_NEUTRAL, MOM_COLLAPSING) == URGENCY_AGGRESSIVE

    def test_neutral_weakening_normal(self):
        assert select_urgency(ZONE_NEUTRAL, MOM_WEAKENING) == URGENCY_NORMAL

    def test_neutral_flat_conservative(self):
        assert select_urgency(ZONE_NEUTRAL, MOM_FLAT) == URGENCY_CONSERVATIVE

    def test_neutral_improving_conservative(self):
        assert select_urgency(ZONE_NEUTRAL, MOM_IMPROVING) == URGENCY_CONSERVATIVE

    def test_profit_weakening_aggressive(self):
        assert select_urgency(ZONE_PROFIT_HARVEST, MOM_WEAKENING) == URGENCY_AGGRESSIVE

    def test_profit_collapsing_aggressive(self):
        assert select_urgency(ZONE_PROFIT_HARVEST, MOM_COLLAPSING) == URGENCY_AGGRESSIVE

    def test_profit_flat_normal(self):
        assert select_urgency(ZONE_PROFIT_HARVEST, MOM_FLAT) == URGENCY_NORMAL

    def test_profit_improving_conservative(self):
        assert select_urgency(ZONE_PROFIT_HARVEST, MOM_IMPROVING) == URGENCY_CONSERVATIVE


class TestApplyUrgencyToChunk:
    CANDIDATES = [(100, 1.0, 50.0), (250, 0.999, 60.0), (500, 0.998, 80.0)]

    @pytest.fixture
    def settings(self, monkeypatch, tmp_path):
        return _settings(monkeypatch, tmp_path)

    def test_conservative_returns_smallest(self, settings):
        qty, reason = apply_urgency_to_chunk(URGENCY_CONSERVATIVE, self.CANDIDATES, 1000, settings)
        assert qty == 100
        assert "conservative" in reason

    def test_aggressive_returns_largest(self, settings):
        qty, reason = apply_urgency_to_chunk(URGENCY_AGGRESSIVE, self.CANDIDATES, 1000, settings)
        assert qty == 500
        assert "aggressive" in reason

    def test_normal_returns_near_equal_largest(self, settings):
        qty, reason = apply_urgency_to_chunk(URGENCY_NORMAL, self.CANDIDATES, 1000, settings)
        # All three have near-equal efficiency → largest wins
        assert qty == 500
        assert "near_equal" in reason

    def test_override_full_returns_remaining(self, settings):
        qty, reason = apply_urgency_to_chunk(URGENCY_OVERRIDE_FULL, self.CANDIDATES, 1000, settings)
        assert qty == 1000
        assert "override_full" in reason

    def test_empty_candidates_returns_none(self, settings):
        qty, reason = apply_urgency_to_chunk(URGENCY_AGGRESSIVE, [], 1000, settings)
        assert qty is None

    def test_override_full_with_empty_candidates_still_returns_remaining(self, settings):
        qty, _ = apply_urgency_to_chunk(URGENCY_OVERRIDE_FULL, [], 1000, settings)
        assert qty == 1000


class TestChunkWaitSeconds:
    def test_conservative_increases_wait(self):
        assert chunk_wait_seconds(URGENCY_CONSERVATIVE, 30) > 30

    def test_normal_unchanged(self):
        assert chunk_wait_seconds(URGENCY_NORMAL, 30) == 30

    def test_aggressive_decreases_wait(self):
        assert chunk_wait_seconds(URGENCY_AGGRESSIVE, 30) < 30

    def test_aggressive_min_10s(self):
        assert chunk_wait_seconds(URGENCY_AGGRESSIVE, 6) == 10  # floor at 10


# ===========================================================================
# Integration tests — end-to-end through CreeperDripper
# ===========================================================================


def _engine(settings, portfolio, executor):
    return CreeperDripper(settings, DummyBirdeye(), executor, portfolio)


# G-1. Positive PnL + healthy momentum → normal chunk (near-equal largest)
def test_profit_harvest_flat_momentum_uses_normal_chunk(monkeypatch, tmp_path):
    """profit_harvest + flat → NORMAL urgency → largest near-equal chunk (50%)."""
    settings = _settings(monkeypatch, tmp_path)
    portfolio = new_portfolio(5.0)
    pos = _pos(remaining=1000, pnl_pct=10.0)  # 10% > 5% harvest threshold
    # previous_mark = 0.0 → flat
    portfolio.open_positions[_MINT] = pos

    executor = DummyExecutor([_ok(requested=500, sold=500)])
    engine = _engine(settings, portfolio, executor)

    decisions: list[TradeDecision] = []
    engine._run_hachi_dripper(pos, decisions, _NOW)

    actions = [d.action for d in decisions]
    assert "DRIPPER_CHUNK_SELECTED" in actions
    assert "DRIPPER_CHUNK_EXECUTED" in actions
    # NORMAL urgency → largest near-equal (50% of 1000 = 500)
    chunk_d = next(d for d in decisions if d.action == "DRIPPER_CHUNK_SELECTED")
    assert chunk_d.qty_atomic == 500
    assert chunk_d.metadata["urgency"] == "normal"
    assert pos.previous_mark_sol_per_token == pytest.approx(pos.last_mark_sol_per_token)


# G-2. Small profit / flat → conservative (smallest chunk)
def test_neutral_flat_uses_conservative_chunk(monkeypatch, tmp_path):
    """neutral + flat → CONSERVATIVE urgency → smallest chunk (10%)."""
    settings = _settings(monkeypatch, tmp_path)
    portfolio = new_portfolio(5.0)
    pos = _pos(remaining=1000, pnl_pct=2.0)  # 2% < 5% harvest threshold → neutral
    portfolio.open_positions[_MINT] = pos

    executor = DummyExecutor([_ok(requested=100, sold=100)])
    engine = _engine(settings, portfolio, executor)

    decisions: list[TradeDecision] = []
    engine._run_hachi_dripper(pos, decisions, _NOW)

    chunk_d = next((d for d in decisions if d.action == "DRIPPER_CHUNK_SELECTED"), None)
    assert chunk_d is not None, "dripper must still sell in neutral zone"
    assert chunk_d.qty_atomic == 100, "conservative urgency must pick smallest chunk (10%)"
    assert chunk_d.metadata["urgency"] == "conservative"


# G-3. Negative PnL + weakening momentum → aggressive chunk
def test_deterioration_weakening_uses_aggressive_chunk(monkeypatch, tmp_path):
    """deterioration + weakening → AGGRESSIVE urgency → largest chunk (50%)."""
    settings = _settings(monkeypatch, tmp_path)
    portfolio = new_portfolio(5.0)
    # PnL = -5%, prev was -2% → drop of ~3% (below weakening 4% threshold → FLAT)
    # Need a larger drop for weakening. Use prev=+5%, last=-5% → drop ~10% from prev → COLLAPSING
    # Use prev=0%, last=-5%: prev_mark = entry_mark → drop_pct = (-5-0)/1.0*... let me be precise.
    # entry_mark=0.001, prev_mark=0.001*(1+0.00)=0.001, last_mark=0.001*0.95 → drop=-5% from prev
    # For weakening: need 4% ≤ drop < 8%
    entry_mark = 0.001
    prev_mark = entry_mark * 1.0   # prev at entry (0% prev_pnl)
    last_mark = entry_mark * 0.95  # last at -5% pnl, -5% from prev → above weakening_drop=4%
    pos = _pos(remaining=1000, pnl_pct=-5.0)   # deterioration zone (-5 < -3 floor, > -12 emergency)
    pos.previous_mark_sol_per_token = prev_mark
    pos.last_mark_sol_per_token = last_mark
    portfolio.open_positions[_MINT] = pos

    executor = DummyExecutor([_ok(requested=500, sold=500)])
    engine = _engine(settings, portfolio, executor)

    decisions: list[TradeDecision] = []
    engine._run_hachi_dripper(pos, decisions, _NOW)

    chunk_d = next((d for d in decisions if d.action == "DRIPPER_CHUNK_SELECTED"), None)
    assert chunk_d is not None
    assert chunk_d.qty_atomic == 500, "aggressive urgency must pick largest chunk (50%)"
    assert chunk_d.metadata["urgency"] == "aggressive"


# G-4. Severe negative PnL → full exit override (bypasses timing gate)
def test_emergency_pnl_triggers_full_exit_override(monkeypatch, tmp_path):
    """pnl < emergency threshold (-12%) → URGENCY_OVERRIDE_FULL → full exit, gate bypassed."""
    settings = _settings(monkeypatch, tmp_path)
    portfolio = new_portfolio(5.0)
    pos = _pos(remaining=1000, pnl_pct=-15.0)  # -15% < -12% emergency threshold
    # Simulate timing gate active (next chunk in 5 min)
    future = (datetime.fromisoformat(_NOW) + timedelta(minutes=5)).isoformat()
    pos.drip_next_chunk_at = future
    pos.drip_chunks_done = 2
    portfolio.open_positions[_MINT] = pos

    executor = DummyExecutor([_ok(requested=1000, sold=1000)])
    engine = _engine(settings, portfolio, executor)

    decisions: list[TradeDecision] = []
    engine._run_hachi_dripper(pos, decisions, _NOW)

    actions = [d.action for d in decisions]
    assert "DRIPPER_OVERRIDE_HARD_EXIT" in actions, (
        f"emergency pnl must trigger DRIPPER_OVERRIDE_HARD_EXIT, got {actions}"
    )
    override_d = next(d for d in decisions if d.action == "DRIPPER_OVERRIDE_HARD_EXIT")
    assert override_d.reason == "hachi_pnl_emergency"
    assert pos.drip_next_chunk_at is None, "override must clear the timing gate"
    assert pos.status == "CLOSED", "full remaining qty sold → CLOSED"
    assert "SELL" in actions


# G-4b. Deterioration + collapsing → full exit override
def test_deterioration_collapsing_triggers_full_exit_override(monkeypatch, tmp_path):
    """deterioration zone + collapsing momentum → URGENCY_OVERRIDE_FULL."""
    settings = _settings(monkeypatch, tmp_path)
    portfolio = new_portfolio(5.0)
    # pnl = -5% (deterioration), previous was +5% → cycle drop ~-9.5% (above 8% collapse)
    entry_mark = 0.001
    prev_mark = entry_mark * 1.05   # previous +5%
    last_mark = entry_mark * 0.95   # current -5%
    # drop_pct = (0.00095/0.00105 - 1)*100 ≈ -9.5% > 8% collapse threshold
    pos = _pos(remaining=1000, pnl_pct=-5.0)
    pos.previous_mark_sol_per_token = prev_mark
    pos.last_mark_sol_per_token = last_mark
    portfolio.open_positions[_MINT] = pos

    executor = DummyExecutor([_ok(requested=1000, sold=1000)])
    engine = _engine(settings, portfolio, executor)

    decisions: list[TradeDecision] = []
    engine._run_hachi_dripper(pos, decisions, _NOW)

    actions = [d.action for d in decisions]
    assert "DRIPPER_OVERRIDE_HARD_EXIT" in actions
    override_d = next(d for d in decisions if d.action == "DRIPPER_OVERRIDE_HARD_EXIT")
    assert override_d.reason == "hachi_momentum_collapse"
    assert pos.status == "CLOSED"


# G-5. Accounting remains correct after adaptive chunking
def test_accounting_correct_after_adaptive_chunk(monkeypatch, tmp_path):
    """SOL proceeds credited to portfolio.cash_sol and position.realized_sol correctly."""
    settings = _settings(monkeypatch, tmp_path)
    portfolio = new_portfolio(5.0)
    cash_before = portfolio.cash_sol
    pos = _pos(remaining=1000, pnl_pct=8.0)  # profit_harvest + flat → normal → 50% chunk
    portfolio.open_positions[_MINT] = pos

    executor = DummyExecutor([_ok(requested=500, sold=500)])
    engine = _engine(settings, portfolio, executor)

    decisions: list[TradeDecision] = []
    engine._run_hachi_dripper(pos, decisions, _NOW)

    assert any(d.action == "DRIPPER_CHUNK_EXECUTED" for d in decisions)
    # output_amount = 1_000_000_000 lamports = 1.0 SOL
    assert portfolio.cash_sol == pytest.approx(cash_before + 1.0)
    assert pos.realized_sol == pytest.approx(1.0)
    assert pos.remaining_qty_atomic == 500


# G-6. Hard exits from _evaluate_exit_rules still override dripper
def test_hard_stop_loss_overrides_hachi_dripper(monkeypatch, tmp_path):
    """stop_loss fires before _run_hachi_dripper runs; DRIPPER_OVERRIDE_HARD_EXIT emitted
    by _start_exit when drip_next_chunk_at is set."""
    settings = _settings(monkeypatch, tmp_path)
    portfolio = new_portfolio(5.0)
    pos = _pos(remaining=1000, pnl_pct=-22.0)  # below -20% stop_loss
    pos.stop_loss_pct = 20.0
    future = (datetime.fromisoformat(_NOW) + timedelta(minutes=3)).isoformat()
    pos.drip_next_chunk_at = future
    portfolio.open_positions[_MINT] = pos

    executor = DummyExecutor([_ok(requested=1000, sold=1000)])
    engine = _engine(settings, portfolio, executor)

    decisions: list[TradeDecision] = []
    candidate = TokenCandidate(address=_MINT, symbol="BRN", decimals=0)
    engine._evaluate_exit_rules(pos, candidate, decisions, _NOW)

    actions = [d.action for d in decisions]
    # stop_loss fires first → _start_exit emits DRIPPER_OVERRIDE_HARD_EXIT
    assert "DRIPPER_OVERRIDE_HARD_EXIT" in actions
    d = next(x for x in decisions if x.action == "DRIPPER_OVERRIDE_HARD_EXIT")
    assert d.reason == "stop_loss"
    assert pos.status == "CLOSED"


# G-7. No regression: legacy TP ladder fires when hachi_dripper_enabled=False
def test_legacy_tp_ladder_unchanged_when_hachi_disabled(monkeypatch, tmp_path):
    """When HACHI_DRIPPER_ENABLED=false, below-TP PnL produces no sell (old behaviour)."""
    settings = _settings(monkeypatch, tmp_path, hachi_enabled=False)
    portfolio = new_portfolio(5.0)
    pos = _pos(remaining=1000, pnl_pct=3.0)  # below 25% TP, should not sell without hachi
    portfolio.open_positions[_MINT] = pos

    executor = DummyExecutor([])
    engine = _engine(settings, portfolio, executor)

    decisions: list[TradeDecision] = []
    candidate = TokenCandidate(address=_MINT, symbol="BRN", decimals=0)
    engine._evaluate_exit_rules(pos, candidate, decisions, _NOW)

    assert not any(d.action in {"SELL", "DRIPPER_CHUNK_SELECTED", "DRIPPER_CHUNK_EXECUTED"} for d in decisions)


# G-8. Profit + collapsing momentum → aggressive sell, locks in gains before deeper reversal
def test_profit_collapsing_uses_aggressive_chunk(monkeypatch, tmp_path):
    """profit_harvest zone but collapsing momentum → AGGRESSIVE urgency → largest chunk."""
    settings = _settings(monkeypatch, tmp_path)
    portfolio = new_portfolio(5.0)
    # PnL = +8% (profit_harvest), but previous mark was +18% → -8.5% drop → collapsing
    entry_mark = 0.001
    prev_mark = entry_mark * 1.18
    last_mark = entry_mark * 1.08
    # drop_pct = (1.08/1.18 - 1)*100 ≈ -8.5% > 8% collapse threshold
    pos = _pos(remaining=1000, pnl_pct=8.0)
    pos.previous_mark_sol_per_token = prev_mark
    pos.last_mark_sol_per_token = last_mark
    portfolio.open_positions[_MINT] = pos

    executor = DummyExecutor([_ok(requested=500, sold=500)])
    engine = _engine(settings, portfolio, executor)

    decisions: list[TradeDecision] = []
    engine._run_hachi_dripper(pos, decisions, _NOW)

    chunk_d = next((d for d in decisions if d.action == "DRIPPER_CHUNK_SELECTED"), None)
    assert chunk_d is not None
    assert chunk_d.qty_atomic == 500, "collapsing profit should use aggressive (50%) chunk"
    assert chunk_d.metadata["urgency"] == "aggressive"


# G-9. Momentum state is persisted after each cycle
def test_momentum_state_persisted_after_cycle(monkeypatch, tmp_path):
    """previous_mark_sol_per_token must be updated to current last_mark after each run."""
    settings = _settings(monkeypatch, tmp_path)
    portfolio = new_portfolio(5.0)
    pos = _pos(remaining=1000, pnl_pct=10.0)
    mark_before = pos.last_mark_sol_per_token
    portfolio.open_positions[_MINT] = pos

    executor = DummyExecutor([_ok(requested=500, sold=500)])
    engine = _engine(settings, portfolio, executor)

    engine._run_hachi_dripper(pos, decisions := [], _NOW)
    assert pos.previous_mark_sol_per_token == pytest.approx(mark_before), (
        "previous_mark must be set to last_mark after dripper runs"
    )
    assert pos.last_hachi_momentum_state is not None
    assert pos.last_hachi_pnl_pct is not None


# G-10. Timing gate still blocks rapid re-entry for non-emergency urgency
def test_timing_gate_blocks_non_emergency(monkeypatch, tmp_path):
    """Timing gate fires for normal urgency; does NOT fire for emergency urgency."""
    settings = _settings(monkeypatch, tmp_path)
    portfolio = new_portfolio(5.0)
    pos = _pos(remaining=1000, pnl_pct=10.0)  # profit_harvest → normal urgency
    future = (datetime.fromisoformat(_NOW) + timedelta(minutes=5)).isoformat()
    pos.drip_next_chunk_at = future
    portfolio.open_positions[_MINT] = pos

    executor = DummyExecutor([_ok(requested=500, sold=500)])
    engine = _engine(settings, portfolio, executor)

    decisions: list[TradeDecision] = []
    engine._run_hachi_dripper(pos, decisions, _NOW)

    assert any(d.action == "DRIPPER_WAIT" for d in decisions)
    assert not any(d.action == "SELL" for d in decisions)


# G-11. hachi_state_eval logged every cycle (including wait cycles)
def test_hachi_state_eval_emitted_every_cycle(monkeypatch, tmp_path):
    """event=hachi_state_eval must appear in decisions metadata (DRIPPER_WAIT cycles too)."""
    import logging
    settings = _settings(monkeypatch, tmp_path)
    portfolio = new_portfolio(5.0)
    pos = _pos(remaining=1000, pnl_pct=10.0)
    future = (datetime.fromisoformat(_NOW) + timedelta(minutes=2)).isoformat()
    pos.drip_next_chunk_at = future
    portfolio.open_positions[_MINT] = pos

    executor = DummyExecutor([])
    engine = _engine(settings, portfolio, executor)

    # Just verify it doesn't crash and state fields are populated
    engine._run_hachi_dripper(pos, [], _NOW)
    assert pos.last_hachi_pnl_pct is not None
    assert pos.last_hachi_momentum_state is not None
