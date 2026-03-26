"""T-004: Focused tests for cash_sol accounting correctness.

Covers the accounting behaviors introduced by the T-004 fix in trader.py:

  1. Sell success with output_amount present
     → cash_sol credited from output_amount; proceeds_source="output_amount";
       cash_sol_credited event emitted.

  2. Sell success with output_amount=None (any quote value)
     → cash_sol unchanged; RECONCILE_PENDING(exit); sell_proceeds_unknown emitted;
       cash_sol_credited NOT emitted. Quote is NOT used as a credit source.

  3. cash_sol_credited event includes proceeds_source and cash_sol_after fields
     → verify event payload structure on the happy path.

  4. Unconfirmed buy (SETTLEMENT_UNCONFIRMED)
     → position.pending_proceeds_sol set to size_sol; cash_sol_debited_pending emitted.
"""
from __future__ import annotations

from typing import Any

from creeper_dripper.config import load_settings
from creeper_dripper.engine.trader import CreeperDripper
from creeper_dripper.errors import POSITION_RECONCILE_PENDING, SETTLEMENT_UNCONFIRMED
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

_VALID_TEST_MINT = "J7MzyZ4Tvwn2LBREnLU48TmKxJE28qstv35dRGBJPCpE"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _EventCapture:
    """Minimal event bus replacement that records emitted events."""

    def __init__(self) -> None:
        self.events: list[tuple[str, tuple, dict]] = []

    def emit(self, event_name: str, *args: Any, **kwargs: Any) -> None:
        self.events.append((event_name, args, kwargs))

    def event_names(self) -> list[str]:
        return [e[0] for e in self.events]

    def find(self, event_name: str) -> list[dict]:
        return [kw for (n, _a, kw) in self.events if n == event_name]


class _DummyBirdeye:
    def build_candidate(self, seed):
        return TokenCandidate(
            address=seed["address"],
            symbol=seed.get("symbol", "T"),
            decimals=6,
            price_usd=1.0,
        )


class _SellExecutor:
    """Executor that returns a configurable sell result. buy() raises to catch misuse."""

    def __init__(self, *, output_amount: int | None, quote_out_atomic: int | None = 1_000_000_000, sold_atomic: int = 100) -> None:
        self._output_amount = output_amount
        self._quote_out = quote_out_atomic
        self._sold = sold_atomic
        self.jupiter = object()

    def sell(self, _mint: str, _qty: int) -> tuple[ExecutionResult, ProbeQuote]:
        result = ExecutionResult(
            status="success",
            requested_amount=self._sold,
            executed_amount=self._sold,
            output_amount=self._output_amount,
            signature="sig_sell_test",
            is_partial=False,
            diagnostic_metadata={
                "post_sell_settlement": {
                    "settlement_confirmed": True,
                    "sold_atomic_settled": self._sold,
                    "sold_atomic_source": "jupiter_execute",
                }
            },
        )
        probe = ProbeQuote(
            input_amount_atomic=_qty,
            out_amount_atomic=self._quote_out,
            price_impact_bps=100.0,
            route_ok=True,
            raw={},
        )
        return result, probe

    def buy(self, *_a, **_kw):
        raise AssertionError("buy() must not be called in sell-path tests")

    def quote_buy(self, *_a, **_kw):
        raise AssertionError("quote_buy() must not be called in sell-path tests")

    def quote_sell(self, *_a, **_kw):
        raise AssertionError("quote_sell() must not be called in sell-path tests")


class _UnconfirmedBuyExecutor:
    """Executor that returns SETTLEMENT_UNCONFIRMED on buy() with a provisional quote."""

    jupiter = object()

    def quote_buy(self, _token: TokenCandidate, size_sol: float) -> ProbeQuote:
        out = max(1, int(size_sol * 1_000_000))
        return ProbeQuote(
            input_amount_atomic=int(size_sol * 1_000_000_000),
            out_amount_atomic=out,
            price_impact_bps=200.0,
            route_ok=True,
            raw={"outAmount": str(out)},
        )

    def quote_sell(self, _mint: str, amount_atomic: int) -> ProbeQuote:
        # Return enough so roundtrip ratio passes (>= MIN_ROUNDTRIP_RETURN_RATIO = 0.02)
        out = max(1, int(amount_atomic * 1_000_000))
        return ProbeQuote(
            input_amount_atomic=amount_atomic,
            out_amount_atomic=out,
            price_impact_bps=200.0,
            route_ok=True,
            raw={"outAmount": str(out)},
        )

    def buy(self, token: TokenCandidate, size_sol: float) -> tuple[ExecutionResult, ProbeQuote]:
        result = ExecutionResult(
            status="unknown",
            requested_amount=int(size_sol * 1_000_000_000),
            executed_amount=0,
            output_amount=None,
            signature="sig_buy_unconfirmed",
            diagnostic_metadata={"classification": SETTLEMENT_UNCONFIRMED},
            diagnostic_code=SETTLEMENT_UNCONFIRMED,
        )
        provisional = max(1, int(size_sol * 1_000_000))
        probe = ProbeQuote(
            input_amount_atomic=int(size_sol * 1_000_000_000),
            out_amount_atomic=provisional,
            price_impact_bps=200.0,
            route_ok=True,
            raw={},
        )
        return result, probe

    def sell(self, *_a, **_kw):
        raise AssertionError("sell() must not be called in buy-path tests")


def _settings(monkeypatch, tmp_path):
    monkeypatch.setenv("BIRDEYE_API_KEY", "x")
    monkeypatch.setenv("JUPITER_API_KEY", "x")
    monkeypatch.setenv("BS58_PRIVATE_KEY", "x")
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("JOURNAL_PATH", str(tmp_path / "journal.jsonl"))
    return load_settings()


def _open_position(now: str, qty_atomic: int = 100) -> PositionState:
    return PositionState(
        token_mint=_VALID_TEST_MINT,
        symbol="TKN",
        decimals=0,
        status="OPEN",
        opened_at=now,
        updated_at=now,
        entry_price_usd=1.0,
        avg_entry_price_usd=1.0,
        entry_sol=1.0,
        remaining_qty_atomic=qty_atomic,
        remaining_qty_ui=float(qty_atomic),
        peak_price_usd=1.0,
        last_price_usd=1.0,
        entry_mark_sol_per_token=0.01,
        last_mark_sol_per_token=0.01,
        peak_mark_sol_per_token=0.01,
        position_id=f"{_VALID_TEST_MINT}:{now}",
        take_profit_steps=[TakeProfitStep(trigger_pct=25.0, fraction=1.0)],
    )


def _engine(settings, portfolio, executor) -> tuple[CreeperDripper, _EventCapture]:
    engine = CreeperDripper(settings, _DummyBirdeye(), executor, portfolio)
    cap = _EventCapture()
    engine.events = cap
    return engine, cap


# ---------------------------------------------------------------------------
# Scenario 1: output_amount present → credit from output_amount
# ---------------------------------------------------------------------------


def test_sell_credits_cash_sol_from_output_amount(monkeypatch, tmp_path):
    """When output_amount is returned by Jupiter, cash_sol is credited from it."""
    settings = _settings(monkeypatch, tmp_path)
    portfolio: PortfolioState = new_portfolio(5.0)
    now = utc_now_iso()
    position = _open_position(now)
    portfolio.open_positions[position.token_mint] = position
    initial_cash = portfolio.cash_sol

    executor = _SellExecutor(
        output_amount=50_000_000,     # 0.05 SOL
        quote_out_atomic=40_000_000,  # must NOT be used
    )
    engine, cap = _engine(settings, portfolio, executor)
    decisions = []
    engine._start_exit(position, 100, "stop_loss", decisions, now)

    expected_credit = 50_000_000 / 1_000_000_000.0  # 0.05 SOL
    assert abs(portfolio.cash_sol - (initial_cash + expected_credit)) < 1e-9, (
        f"cash_sol should be initial+0.05; got {portfolio.cash_sol}"
    )

    sell_decisions = [d for d in decisions if d.action == "SELL"]
    assert len(sell_decisions) == 1
    assert sell_decisions[0].metadata.get("proceeds_source") == "output_amount"
    assert sell_decisions[0].metadata.get("proceeds_pending_reconcile") is False

    emitted = cap.event_names()
    assert "cash_sol_credited" in emitted, f"cash_sol_credited not emitted; got {emitted}"
    assert "sell_proceeds_unknown" not in emitted

    credited = cap.find("cash_sol_credited")
    assert len(credited) == 1
    assert abs(credited[0]["credited_sol"] - expected_credit) < 1e-9
    assert credited[0]["proceeds_source"] == "output_amount"
    assert "cash_sol_after" in credited[0]


# ---------------------------------------------------------------------------
# Scenario 2: output_amount=None → RECONCILE_PENDING regardless of quote
# ---------------------------------------------------------------------------


def test_sell_no_output_amount_leaves_cash_sol_unchanged(monkeypatch, tmp_path):
    """When output_amount is absent, cash_sol must NOT be modified regardless of quote.
    Quote is NOT used as a credit source. Position enters RECONCILE_PENDING(exit)."""
    settings = _settings(monkeypatch, tmp_path)
    portfolio: PortfolioState = new_portfolio(5.0)
    now = utc_now_iso()
    position = _open_position(now)
    portfolio.open_positions[position.token_mint] = position
    initial_cash = portfolio.cash_sol

    # Quote IS available but must still not be credited
    executor = _SellExecutor(
        output_amount=None,
        quote_out_atomic=30_000_000,  # 0.03 SOL — must NOT be credited
    )
    engine, cap = _engine(settings, portfolio, executor)
    decisions = []
    engine._start_exit(position, 100, "stop_loss", decisions, now)

    assert abs(portfolio.cash_sol - initial_cash) < 1e-12, (
        f"cash_sol must not change when output_amount is absent; got {portfolio.cash_sol}"
    )
    assert position.realized_sol == 0.0, (
        f"realized_sol must not be credited from quote; got {position.realized_sol}"
    )

    emitted = cap.event_names()
    assert "sell_proceeds_unknown" in emitted, f"sell_proceeds_unknown not emitted; got {emitted}"
    assert "cash_sol_credited" not in emitted, "cash_sol_credited must not fire when output_amount is absent"

    assert position.status == POSITION_RECONCILE_PENDING
    assert position.reconcile_context == "exit"
    assert position.pending_exit_qty_atomic == 0

    pending = [d for d in decisions if d.action == "SELL_SETTLEMENT_PENDING"]
    assert len(pending) == 1
    sell = [d for d in decisions if d.action == "SELL"]
    assert len(sell) == 0


# ---------------------------------------------------------------------------
# Scenario 3: output_amount=None AND quote=None → same RECONCILE_PENDING behavior
# ---------------------------------------------------------------------------


def test_sell_no_output_no_quote_leaves_cash_sol_unchanged(monkeypatch, tmp_path):
    """When both output_amount and quote are absent, cash_sol must NOT be modified."""
    settings = _settings(monkeypatch, tmp_path)
    portfolio: PortfolioState = new_portfolio(5.0)
    now = utc_now_iso()
    position = _open_position(now)
    portfolio.open_positions[position.token_mint] = position
    initial_cash = portfolio.cash_sol

    executor = _SellExecutor(
        output_amount=None,
        quote_out_atomic=None,
    )
    engine, cap = _engine(settings, portfolio, executor)
    decisions = []
    engine._start_exit(position, 100, "stop_loss", decisions, now)

    assert abs(portfolio.cash_sol - initial_cash) < 1e-12, (
        f"cash_sol must not change when proceeds are unknown; got {portfolio.cash_sol}"
    )
    assert position.status == POSITION_RECONCILE_PENDING
    assert position.reconcile_context == "exit"

    emitted = cap.event_names()
    assert "sell_proceeds_unknown" in emitted
    assert "cash_sol_credited" not in emitted


# ---------------------------------------------------------------------------
# Scenario 4: Unconfirmed buy → pending_proceeds_sol set, event emitted
# ---------------------------------------------------------------------------


def test_unconfirmed_buy_sets_pending_proceeds_and_emits_event(monkeypatch, tmp_path):
    """On SETTLEMENT_UNCONFIRMED buy, pending_proceeds_sol must equal the cash_sol
    debit and cash_sol_debited_pending must be emitted."""
    settings = _settings(monkeypatch, tmp_path)
    portfolio: PortfolioState = new_portfolio(5.0)
    initial_cash = portfolio.cash_sol

    executor = _UnconfirmedBuyExecutor()

    candidate = TokenCandidate(
        address=_VALID_TEST_MINT,
        symbol="TKN",
        decimals=6,
        price_usd=1.0,
        volume_24h_usd=500_000.0,
        liquidity_usd=200_000.0,
        discovery_score=80.0,
        reasons=["test"],
    )

    engine = CreeperDripper(settings, _DummyBirdeye(), executor, portfolio)
    cap = _EventCapture()
    engine.events = cap

    decisions: list = []
    engine._maybe_open_positions([candidate], decisions, utc_now_iso())

    assert portfolio.cash_sol < initial_cash, "cash_sol must be debited after unconfirmed buy"
    expected_debit = initial_cash - portfolio.cash_sol
    assert expected_debit > 0

    assert _VALID_TEST_MINT in portfolio.open_positions, "position must be created for unconfirmed buy"
    pos = portfolio.open_positions[_VALID_TEST_MINT]

    assert pos.pending_proceeds_sol > 0.0, "pending_proceeds_sol must be > 0 for SETTLEMENT_UNCONFIRMED buy"
    assert abs(pos.pending_proceeds_sol - expected_debit) < 1e-9, (
        f"pending_proceeds_sol={pos.pending_proceeds_sol} != debited={expected_debit}"
    )

    emitted = cap.event_names()
    assert "cash_sol_debited_pending" in emitted, f"cash_sol_debited_pending not emitted; got {emitted}"

    debited_events = cap.find("cash_sol_debited_pending")
    assert len(debited_events) >= 1
    assert abs(debited_events[0]["debited_sol"] - expected_debit) < 1e-9
    assert abs(debited_events[0]["cash_sol_after"] - portfolio.cash_sol) < 1e-9
