"""Tests for Phase 1 drip (chunked) exit integration."""
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
)
from creeper_dripper.storage.state import new_portfolio
from creeper_dripper.utils import utc_now_iso

_VALID_MINT = "J7MzyZ4Tvwn2LBREnLU48TmKxJE28qstv35dRGBJPCpE"
_NOW = "2026-06-15T12:00:00+00:00"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _settings(monkeypatch, tmp_path, *, drip_enabled: bool = True):
    monkeypatch.setenv("BIRDEYE_API_KEY", "x")
    monkeypatch.setenv("JUPITER_API_KEY", "x")
    monkeypatch.setenv("BS58_PRIVATE_KEY", "x")
    monkeypatch.setenv("STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("JOURNAL_PATH", str(tmp_path / "journal.jsonl"))
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("DRIP_EXIT_ENABLED", "true" if drip_enabled else "false")
    monkeypatch.setenv("DRIP_CHUNK_PCTS", "0.10,0.25,0.50")
    monkeypatch.setenv("DRIP_MIN_CHUNK_WAIT_SECONDS", "30")
    settings = load_settings()
    # Force-assign so load_dotenv(override=True) from .env doesn't stomp test values.
    settings.drip_exit_enabled = drip_enabled
    settings.hachi_dripper_enabled = False  # drip_exit tests must not run hachi path
    return settings


def _position(*, remaining: int = 1000, entry_sol: float = 1.0) -> PositionState:
    return PositionState(
        token_mint=_VALID_MINT,
        symbol="TKN",
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
        entry_mark_sol_per_token=entry_sol / max(remaining, 1),
        last_mark_sol_per_token=entry_sol / max(remaining, 1),
        peak_mark_sol_per_token=entry_sol / max(remaining, 1),
        position_id=f"{_VALID_MINT}:{_NOW}",
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
    """Controllable executor stub: cycles through sell_results, returns probe for quote_sell."""

    def __init__(
        self,
        sell_results: list[ExecutionResult],
        *,
        wallet_balance: int = 1000,
        quote_out_per_atomic: float = 1.0,
    ) -> None:
        self.sell_results = sell_results
        self.wallet_balance = wallet_balance
        self.quote_out_per_atomic = quote_out_per_atomic
        self.jupiter = object()
        self._idx = 0
        self.sell_calls: list[tuple[str, int]] = []
        self.quote_calls: list[tuple[str, int]] = []

    def wallet_token_balance_atomic(self, _mint: str) -> int:
        return self.wallet_balance

    def sell(self, token_mint: str, amount_atomic: int):
        self.sell_calls.append((token_mint, amount_atomic))
        result = self.sell_results[min(self._idx, len(self.sell_results) - 1)]
        self._idx += 1
        probe = ProbeQuote(
            input_amount_atomic=amount_atomic,
            out_amount_atomic=int(amount_atomic * self.quote_out_per_atomic),
            price_impact_bps=50.0,
            route_ok=True,
            raw={},
        )
        return result, probe

    def quote_sell(self, token_mint: str, amount_atomic: int) -> ProbeQuote:
        self.quote_calls.append((token_mint, amount_atomic))
        return ProbeQuote(
            input_amount_atomic=amount_atomic,
            out_amount_atomic=int(amount_atomic * self.quote_out_per_atomic),
            price_impact_bps=50.0,
            route_ok=True,
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
# Tests
# ---------------------------------------------------------------------------


def test_take_profit_starts_drip(monkeypatch, tmp_path):
    """take_profit_ reason with drip enabled activates drip state and paces chunks.

    Uses DRIP_CHUNK_PCTS=0.10 so the first chunk (100 atoms) is smaller than the
    drip target (500 atoms), ensuring the position stays EXIT_PENDING after the
    first chunk rather than completing the entire drip in one shot.
    """
    monkeypatch.setenv("DRIP_CHUNK_PCTS", "0.10")  # 10% of 1000 remaining = 100 per chunk
    settings = _settings(monkeypatch, tmp_path, drip_enabled=True)
    portfolio = new_portfolio(5.0)
    pos = _position(remaining=1000)
    portfolio.open_positions[_VALID_MINT] = pos

    # First chunk: 10% of 1000 = 100 atoms
    executor = DummyExecutor([_success_result(requested=100, sold=100)], wallet_balance=1000)
    engine = CreeperDripper(settings, DummyBirdeye(), executor, portfolio)

    decisions = []
    engine._start_exit(pos, 500, "take_profit_25", decisions, _NOW)

    # Drip should remain active: 400 atoms of the 500-atom target still to sell
    assert pos.drip_exit_active is True, "drip should remain active after partial first chunk"
    assert pos.drip_exit_reason == "take_profit_25"
    assert pos.drip_chunks_done == 1
    assert pos.drip_qty_remaining_atomic == 400
    # Position stays EXIT_PENDING waiting for the next chunk
    assert pos.status == "EXIT_PENDING"
    assert pos.drip_next_chunk_at is not None

    actions = [d.action for d in decisions]
    assert "EXIT_PENDING" in actions
    assert "DRIP_CHUNK_EXECUTED" in actions


def test_hard_exit_bypasses_drip(monkeypatch, tmp_path):
    """stop_loss / trailing_stop reasons must NOT activate drip (hard exits skip drip)."""
    settings = _settings(monkeypatch, tmp_path, drip_enabled=True)
    portfolio = new_portfolio(5.0)
    pos = _position(remaining=1000)
    portfolio.open_positions[_VALID_MINT] = pos

    executor = DummyExecutor([_success_result(requested=1000, sold=1000)], wallet_balance=1000)
    engine = CreeperDripper(settings, DummyBirdeye(), executor, portfolio)

    decisions = []
    engine._start_exit(pos, 1000, "stop_loss", decisions, _NOW)

    assert pos.drip_exit_active is False, "hard exit must not activate drip"
    # Full sell → position closes
    assert pos.status == "CLOSED"


def test_drip_chunk_waiting_gate(monkeypatch, tmp_path):
    """When drip_next_chunk_at is in the future _attempt_exit emits DRIP_CHUNK_WAITING."""
    settings = _settings(monkeypatch, tmp_path, drip_enabled=True)
    portfolio = new_portfolio(5.0)
    pos = _position(remaining=1000)
    pos.status = "EXIT_PENDING"
    pos.pending_exit_qty_atomic = 500
    pos.pending_exit_reason = "take_profit_25"
    pos.drip_exit_active = True
    pos.drip_exit_reason = "take_profit_25"
    pos.drip_qty_remaining_atomic = 500
    pos.drip_chunks_done = 1
    # Set drip_next_chunk_at 5 minutes in the future
    future = (datetime.fromisoformat(_NOW) + timedelta(minutes=5)).isoformat()
    pos.drip_next_chunk_at = future
    portfolio.open_positions[_VALID_MINT] = pos

    executor = DummyExecutor([], wallet_balance=1000)
    engine = CreeperDripper(settings, DummyBirdeye(), executor, portfolio)

    decisions = []
    engine._attempt_exit(pos, decisions, _NOW)

    actions = [d.action for d in decisions]
    assert "DRIP_CHUNK_WAITING" in actions, f"expected DRIP_CHUNK_WAITING, got {actions}"
    assert len(executor.sell_calls) == 0, "no sell should fire during waiting gate"


def test_drip_chunk_decrement_and_resume(monkeypatch, tmp_path):
    """Multiple chunks progressively decrement drip_qty_remaining_atomic.

    Uses DRIP_CHUNK_PCTS=0.10 so each chunk is ~10% of remaining, well below
    the drip target (500 atoms), guaranteeing two consecutive DRIP_CHUNK_EXECUTED
    events without exhausting the target.
    """
    monkeypatch.setenv("DRIP_CHUNK_PCTS", "0.10")  # 10% → chunk1=100, chunk2=90
    settings = _settings(monkeypatch, tmp_path, drip_enabled=True)
    portfolio = new_portfolio(5.0)
    pos = _position(remaining=1000)
    portfolio.open_positions[_VALID_MINT] = pos

    # chunk sizes: 10% of 1000=100, then 10% of 900=90
    chunk1 = _success_result(requested=100, sold=100, signature="s1")
    chunk2 = _success_result(requested=90, sold=90, signature="s2")
    executor = DummyExecutor([chunk1, chunk2], wallet_balance=1000)
    engine = CreeperDripper(settings, DummyBirdeye(), executor, portfolio)

    # First chunk
    decisions: list = []
    engine._start_exit(pos, 500, "take_profit_25", decisions, _NOW)
    assert pos.drip_chunks_done == 1
    first_remaining = pos.drip_qty_remaining_atomic
    assert first_remaining == 400  # 500 - 100

    # Second chunk: bypass timing gate
    later = (datetime.fromisoformat(_NOW) + timedelta(seconds=60)).isoformat()
    pos.drip_next_chunk_at = None
    decisions2: list = []
    engine._attempt_exit(pos, decisions2, later)
    assert pos.drip_chunks_done == 2
    assert pos.drip_qty_remaining_atomic == 310  # 400 - 90
    actions2 = [d.action for d in decisions2]
    assert "DRIP_CHUNK_EXECUTED" in actions2


def test_hard_exit_interrupts_drip(monkeypatch, tmp_path):
    """A hard exit reason while drip is active overrides drip and sells the full remainder."""
    settings = _settings(monkeypatch, tmp_path, drip_enabled=True)
    portfolio = new_portfolio(5.0)
    pos = _position(remaining=1000)
    portfolio.open_positions[_VALID_MINT] = pos

    chunk1 = _success_result(requested=250, sold=250, signature="s1")
    full_exit = _success_result(requested=750, sold=750, signature="s_full")
    executor = DummyExecutor([chunk1, full_exit], wallet_balance=1000)
    engine = CreeperDripper(settings, DummyBirdeye(), executor, portfolio)

    # Start a drip exit (TP)
    decisions: list = []
    engine._start_exit(pos, 500, "take_profit_25", decisions, _NOW)
    assert pos.drip_exit_active is True
    assert pos.status == "EXIT_PENDING"

    # Hard exit arrives mid-drip
    decisions2: list = []
    result = engine._start_exit(pos, pos.remaining_qty_atomic, "stop_loss", decisions2, _NOW)
    assert result is True, "_start_exit should return True for hard override"
    assert pos.drip_exit_active is False, "drip must be cleared on hard override"
    assert pos.status == "CLOSED", "full quantity sold → CLOSED"


def test_reconcile_clears_drip_state(monkeypatch, tmp_path):
    """When a sell attempt enters RECONCILE_PENDING drip state is fully cleared."""
    from creeper_dripper.errors import SETTLEMENT_UNCONFIRMED

    settings = _settings(monkeypatch, tmp_path, drip_enabled=True)
    portfolio = new_portfolio(5.0)
    pos = _position(remaining=1000)
    portfolio.open_positions[_VALID_MINT] = pos

    unknown_result = ExecutionResult(
        status="unknown",
        requested_amount=250,
        executed_amount=None,
        output_amount=None,
        signature="sig_unk",
        diagnostic_code=SETTLEMENT_UNCONFIRMED,
        diagnostic_metadata={"post_sell_settlement": None},
    )
    executor = DummyExecutor([unknown_result], wallet_balance=1000)
    engine = CreeperDripper(settings, DummyBirdeye(), executor, portfolio)

    decisions: list = []
    engine._start_exit(pos, 500, "take_profit_25", decisions, _NOW)

    # Drip state must be wiped when reconcile pending
    assert pos.drip_exit_active is False
    assert pos.drip_qty_remaining_atomic is None
    assert pos.drip_next_chunk_at is None
    from creeper_dripper.errors import POSITION_RECONCILE_PENDING
    assert pos.status == POSITION_RECONCILE_PENDING
