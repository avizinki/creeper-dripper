from __future__ import annotations

import json

from creeper_dripper.config import load_settings
from creeper_dripper.engine.trader import CreeperDripper
from creeper_dripper.errors import (
    EXIT_RECONCILED_CLOSED,
    EXIT_TX_CONFIRMED_NEEDS_SETTLEMENT,
    EXIT_UNKNOWN_PENDING_RECONCILE,
    POSITION_RECONCILE_PENDING,
)
from creeper_dripper.models import ExecutionResult, PortfolioState, PositionState, ProbeQuote
from creeper_dripper.storage.state import load_portfolio, new_portfolio, save_portfolio
from creeper_dripper.utils import utc_now_iso

# Valid Solana pubkey for persistence tests (save_portfolio drops non-pubkey mints).
_VALID_TEST_MINT = "J7MzyZ4Tvwn2LBREnLU48TmKxJE28qstv35dRGBJPCpE"


def _sell_success_reconciled(requested: int, sold: int, output_amount: int | None) -> ExecutionResult:
    return ExecutionResult(
        status="success",
        requested_amount=requested,
        executed_amount=sold,
        output_amount=output_amount,
        signature="sig-x",
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
        raise RuntimeError("not needed")


class DummyExecutor:
    def __init__(self, tx_status: str | None = None, sell_result: ExecutionResult | None = None) -> None:
        self._tx_status = tx_status
        self.sell_result = sell_result or ExecutionResult(status="unknown", requested_amount=1, executed_amount=None, output_amount=None, error="timeout")
        self.jupiter = object()

    def transaction_status(self, _signature: str) -> str | None:
        return self._tx_status

    def sell(self, _token_mint: str, _amount_atomic: int):
        return self.sell_result, ProbeQuote(input_amount_atomic=1, out_amount_atomic=500, price_impact_bps=100.0, route_ok=True, raw={})

    def buy(self, *_args, **_kwargs):
        return ExecutionResult(status="failed", requested_amount=1, error="unused"), ProbeQuote(
            input_amount_atomic=1, out_amount_atomic=None, price_impact_bps=None, route_ok=False, raw={}
        )


def _settings(monkeypatch, tmp_path):
    monkeypatch.setenv("BIRDEYE_API_KEY", "x")
    monkeypatch.setenv("JUPITER_API_KEY", "x")
    monkeypatch.setenv("STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("JOURNAL_PATH", str(tmp_path / "journal.jsonl"))
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path))
    return load_settings()


def _position(now: str) -> PositionState:
    return PositionState(
        token_mint=_VALID_TEST_MINT,
        symbol="TOK",
        decimals=0,
        status="OPEN",
        opened_at=now,
        updated_at=now,
        entry_price_usd=1.0,
        avg_entry_price_usd=1.0,
        entry_sol=1.0,
        remaining_qty_atomic=100,
        remaining_qty_ui=100.0,
        peak_price_usd=1.0,
        last_price_usd=1.0,
        position_id=f"{_VALID_TEST_MINT}:{now}",
    )


def test_startup_recovery_tx_confirmed_without_settlement_truth_does_not_close(monkeypatch, tmp_path):
    """tx_status=success is not settlement truth; EXIT_PENDING must not be closed on startup."""
    settings = _settings(monkeypatch, tmp_path)
    now = utc_now_iso()
    portfolio: PortfolioState = new_portfolio(5.0)
    pos = _position(now)
    pos.status = "EXIT_PENDING"
    pos.pending_exit_qty_atomic = 100
    pos.pending_exit_reason = "stop_loss"
    pos.pending_exit_signature = "confirmed-sig"
    portfolio.open_positions[pos.token_mint] = pos
    engine = CreeperDripper(settings, DummyBirdeye(), DummyExecutor(tx_status="success"), portfolio)
    decisions = engine.run_startup_recovery()
    assert pos.token_mint in portfolio.open_positions
    assert portfolio.open_positions[_VALID_TEST_MINT].status == POSITION_RECONCILE_PENDING
    assert portfolio.open_positions[_VALID_TEST_MINT].reconcile_context == "exit"
    assert portfolio.open_positions[_VALID_TEST_MINT].pending_exit_signature == "confirmed-sig"
    assert portfolio.open_positions[_VALID_TEST_MINT].remaining_qty_atomic == 100
    assert any(d.reason == EXIT_TX_CONFIRMED_NEEDS_SETTLEMENT for d in decisions)


def test_startup_recovery_tx_confirmed_with_internal_full_exit_truth_allows_close(monkeypatch, tmp_path):
    """If the position already records a full exit (remaining=0), tx success may finalize closure."""
    settings = _settings(monkeypatch, tmp_path)
    now = utc_now_iso()
    portfolio: PortfolioState = new_portfolio(5.0)
    pos = _position(now)
    pos.status = "EXIT_PENDING"
    pos.remaining_qty_atomic = 0
    pos.remaining_qty_ui = 0.0
    pos.pending_exit_qty_atomic = 0
    pos.pending_exit_reason = "stop_loss"
    pos.pending_exit_signature = "confirmed-sig"
    pos.last_sell_signature = "confirmed-sig"
    portfolio.open_positions[pos.token_mint] = pos
    engine = CreeperDripper(settings, DummyBirdeye(), DummyExecutor(tx_status="success"), portfolio)
    decisions = engine.run_startup_recovery()
    assert pos.token_mint not in portfolio.open_positions
    assert any(d.reason == EXIT_RECONCILED_CLOSED for d in decisions)


def test_startup_recovery_requeues_exit_pending_when_tx_reverted(monkeypatch, tmp_path):
    """EXIT_PENDING position with failed tx is re-queued for retry — no wallet read."""
    settings = _settings(monkeypatch, tmp_path)
    now = utc_now_iso()
    portfolio: PortfolioState = new_portfolio(5.0)
    pos = _position(now)
    pos.status = "EXIT_PENDING"
    pos.pending_exit_qty_atomic = 100
    pos.pending_exit_reason = "stop_loss"
    pos.pending_exit_signature = "failed-sig"
    portfolio.open_positions[pos.token_mint] = pos
    engine = CreeperDripper(settings, DummyBirdeye(), DummyExecutor(tx_status="failed"), portfolio)
    decisions = engine.run_startup_recovery()
    # Position is re-queued as EXIT_PENDING (signature cleared for retry).
    assert _VALID_TEST_MINT in portfolio.open_positions
    assert portfolio.open_positions[_VALID_TEST_MINT].status == "EXIT_PENDING"
    assert portfolio.open_positions[_VALID_TEST_MINT].pending_exit_signature is None
    assert any(d.reason == EXIT_UNKNOWN_PENDING_RECONCILE for d in decisions)


def test_startup_recovery_leaves_open_positions_unchanged(monkeypatch, tmp_path):
    """OPEN positions are not touched by startup recovery."""
    settings = _settings(monkeypatch, tmp_path)
    now = utc_now_iso()
    portfolio: PortfolioState = new_portfolio(5.0)
    pos = _position(now)
    portfolio.open_positions[pos.token_mint] = pos
    engine = CreeperDripper(settings, DummyBirdeye(), DummyExecutor(tx_status=None), portfolio)
    decisions = engine.run_startup_recovery()
    assert portfolio.open_positions[_VALID_TEST_MINT].remaining_qty_atomic == 100
    assert decisions == []


def test_corrupted_state_file_recovery(monkeypatch, tmp_path):
    bad = tmp_path / "state.json"
    bad.write_text("{broken json", encoding="utf-8")
    portfolio = load_portfolio(bad, 5.0)
    assert portfolio.cash_sol == 5.0
    archived = list((tmp_path / "archive").glob("state.*.corrupted.json"))
    assert archived


def test_load_portfolio_drops_non_pubkey_positions(tmp_path):
    good_mint = "J7MzyZ4Tvwn2LBREnLU48TmKxJE28qstv35dRGBJPCpE"
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "version": 2,
                "cash_sol": 4.0,
                "reserved_sol": 0.0,
                "total_realized_sol": 0.0,
                "open_positions": {
                    "mint1": {
                        "token_mint": "mint1",
                        "symbol": "TOK",
                        "decimals": 0,
                        "status": "OPEN",
                        "opened_at": "2026-01-01T00:00:00+00:00",
                        "updated_at": "2026-01-01T00:00:00+00:00",
                        "entry_price_usd": 1.0,
                        "avg_entry_price_usd": 1.0,
                        "entry_sol": 1.0,
                        "remaining_qty_atomic": 1,
                        "remaining_qty_ui": 1.0,
                        "peak_price_usd": 1.0,
                        "last_price_usd": 1.0,
                        "position_id": "mint1:2026-01-01T00:00:00+00:00",
                        "take_profit_steps": [],
                        "notes": [],
                    },
                    good_mint: {
                        "token_mint": good_mint,
                        "symbol": "PRl",
                        "decimals": 6,
                        "status": "OPEN",
                        "opened_at": "2026-01-01T00:00:00+00:00",
                        "updated_at": "2026-01-01T00:00:00+00:00",
                        "entry_price_usd": 0.1,
                        "avg_entry_price_usd": 0.1,
                        "entry_sol": 0.06,
                        "remaining_qty_atomic": 100,
                        "remaining_qty_ui": 0.0001,
                        "peak_price_usd": 0.1,
                        "last_price_usd": 0.1,
                        "position_id": f"{good_mint}:2026-01-01T00:00:00+00:00",
                        "take_profit_steps": [],
                        "notes": [],
                    },
                },
                "closed_positions": [
                    {
                        "token_mint": "mint2",
                        "symbol": "X",
                        "decimals": 6,
                        "status": "CLOSED",
                        "opened_at": "2026-01-01T00:00:00+00:00",
                        "updated_at": "2026-01-01T00:00:00+00:00",
                        "entry_price_usd": 1.0,
                        "avg_entry_price_usd": 1.0,
                        "entry_sol": 1.0,
                        "remaining_qty_atomic": 0,
                        "remaining_qty_ui": 0.0,
                        "peak_price_usd": 1.0,
                        "last_price_usd": 1.0,
                        "take_profit_steps": [],
                        "notes": [],
                    }
                ],
                "cooldowns": {},
                "opened_today_count": 0,
                "opened_today_date": "2026-01-01",
                "last_cycle_at": None,
                "safe_mode_active": False,
                "safety_stop_reason": None,
                "consecutive_execution_failures": 0,
                "entries_skipped_dry_run": 0,
                "entries_skipped_live_disabled": 0,
            }
        ),
        encoding="utf-8",
    )
    portfolio = load_portfolio(state_path, 5.0)
    assert list(portfolio.open_positions.keys()) == [good_mint]
    assert portfolio.closed_positions == []


def test_save_portfolio_drops_non_pubkey_positions(tmp_path):
    state_path = tmp_path / "state.json"
    now = utc_now_iso()
    portfolio = new_portfolio(5.0)
    junk = _position(now)
    junk.token_mint = "mint1"
    junk.position_id = f"mint1:{now}"
    portfolio.open_positions["mint1"] = junk
    save_portfolio(state_path, portfolio)
    assert "mint1" not in portfolio.open_positions
    on_disk = json.loads(state_path.read_text(encoding="utf-8"))
    assert on_disk["open_positions"] == {}


def test_opened_today_count_resets_on_new_day(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    now = utc_now_iso()
    portfolio: PortfolioState = new_portfolio(5.0)
    portfolio.opened_today_count = 3
    portfolio.opened_today_date = "1999-01-01"
    engine = CreeperDripper(settings, DummyBirdeye(), DummyExecutor(tx_status=None), portfolio)
    engine.run_startup_recovery()
    assert portfolio.opened_today_count == 0
    assert portfolio.opened_today_date == now[:10]


def test_exit_pending_with_no_signature_left_unchanged(monkeypatch, tmp_path):
    """EXIT_PENDING with no signature stays unchanged — will retry on next cycle."""
    settings = _settings(monkeypatch, tmp_path)
    now = utc_now_iso()
    portfolio: PortfolioState = new_portfolio(5.0)
    pos = _position(now)
    pos.status = "EXIT_PENDING"
    pos.pending_exit_qty_atomic = 100
    pos.pending_exit_reason = "stop_loss"
    pos.pending_exit_signature = None
    portfolio.open_positions[pos.token_mint] = pos
    engine = CreeperDripper(settings, DummyBirdeye(), DummyExecutor(tx_status="success"), portfolio)
    decisions = engine.run_startup_recovery()
    # No signature → cannot check tx_status → leave as EXIT_PENDING.
    assert _VALID_TEST_MINT in portfolio.open_positions
    assert portfolio.open_positions[_VALID_TEST_MINT].status == "EXIT_PENDING"
    assert decisions == []


def test_realized_proceeds_not_from_quote_when_execution_missing(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    now = utc_now_iso()
    portfolio: PortfolioState = new_portfolio(5.0)
    pos = _position(now)
    portfolio.open_positions[pos.token_mint] = pos
    engine = CreeperDripper(
        settings,
        DummyBirdeye(),
        DummyExecutor(sell_result=_sell_success_reconciled(40, 40, None)),
        portfolio,
    )
    decisions = []
    engine._start_exit(pos, 40, "take_profit_25", decisions, now)
    assert pos.realized_sol == 0.0
    assert portfolio.cash_sol == 5.0


def test_startup_recovery_reconcile_pending_exit_tx_confirmed(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    now = utc_now_iso()
    portfolio: PortfolioState = new_portfolio(5.0)
    pos = _position(now)
    pos.status = POSITION_RECONCILE_PENDING
    pos.reconcile_context = "exit"
    pos.pending_exit_qty_atomic = 100
    pos.pending_exit_reason = "stop_loss"
    pos.pending_exit_signature = "sig-reconcile"
    portfolio.open_positions[pos.token_mint] = pos
    engine = CreeperDripper(settings, DummyBirdeye(), DummyExecutor(tx_status="success"), portfolio)
    decisions = engine.run_startup_recovery()
    assert _VALID_TEST_MINT in portfolio.open_positions
    assert portfolio.open_positions[_VALID_TEST_MINT].status == POSITION_RECONCILE_PENDING
    assert portfolio.open_positions[_VALID_TEST_MINT].reconcile_context == "exit"
    assert any(d.reason == EXIT_TX_CONFIRMED_NEEDS_SETTLEMENT for d in decisions)


def test_startup_recovery_reconcile_pending_exit_failed_prevents_double_sell(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    now = utc_now_iso()
    portfolio: PortfolioState = new_portfolio(5.0)
    pos = _position(now)
    pos.status = POSITION_RECONCILE_PENDING
    pos.reconcile_context = "exit"
    pos.remaining_qty_atomic = 60
    pos.pending_exit_qty_atomic = 0
    pos.pending_exit_reason = "take_profit_25"
    pos.pending_exit_signature = "sig-proceeds-unk"
    portfolio.open_positions[pos.token_mint] = pos
    engine = CreeperDripper(settings, DummyBirdeye(), DummyExecutor(tx_status="failed"), portfolio)
    decisions = engine.run_startup_recovery()
    assert _VALID_TEST_MINT in portfolio.open_positions
    assert portfolio.open_positions[_VALID_TEST_MINT].status == "EXIT_PENDING"
    assert portfolio.open_positions[_VALID_TEST_MINT].pending_exit_qty_atomic == 0
    assert portfolio.open_positions[_VALID_TEST_MINT].remaining_qty_atomic == 60
