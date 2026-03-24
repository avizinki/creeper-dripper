from __future__ import annotations

import json

from creeper_dripper.config import load_settings
from creeper_dripper.engine.trader import CreeperDripper
from creeper_dripper.errors import RECOVERY_WALLET_GT_STATE
from creeper_dripper.models import ExecutionResult, PortfolioState, PositionState, ProbeQuote
from creeper_dripper.storage.state import load_portfolio, new_portfolio
from creeper_dripper.utils import utc_now_iso


class DummyBirdeye:
    def build_candidate(self, seed):
        raise RuntimeError("not needed")


class DummyExecutor:
    def __init__(self, balances: dict[str, int], sell_result: ExecutionResult | None = None) -> None:
        self.balances = balances
        self.sell_result = sell_result or ExecutionResult(status="unknown", requested_amount=1, executed_amount=None, output_amount=None, error="timeout")
        self.jupiter = object()

    def wallet_token_balance_atomic(self, token_mint: str) -> int | None:
        return self.balances.get(token_mint)

    def transaction_status(self, _signature: str) -> str | None:
        return None

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
        token_mint="mint1",
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
        position_id=f"mint1:{now}",
    )


def test_startup_recovery_reduces_qty_to_wallet(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    now = utc_now_iso()
    portfolio: PortfolioState = new_portfolio(5.0)
    pos = _position(now)
    portfolio.open_positions[pos.token_mint] = pos
    engine = CreeperDripper(settings, DummyBirdeye(), DummyExecutor({"mint1": 60}), portfolio)
    decisions = engine.run_startup_recovery()
    assert portfolio.open_positions["mint1"].remaining_qty_atomic == 60
    assert any(d.reason == "recovery_qty_reduced_to_wallet" for d in decisions)


def test_wallet_balance_greater_than_state_logs_discrepancy(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    now = utc_now_iso()
    portfolio: PortfolioState = new_portfolio(5.0)
    pos = _position(now)
    portfolio.open_positions[pos.token_mint] = pos
    engine = CreeperDripper(settings, DummyBirdeye(), DummyExecutor({"mint1": 140}), portfolio)
    decisions = engine.run_startup_recovery()
    assert portfolio.open_positions["mint1"].remaining_qty_atomic == 100
    assert any(d.reason == RECOVERY_WALLET_GT_STATE for d in decisions)


def test_corrupted_state_file_recovery(monkeypatch, tmp_path):
    bad = tmp_path / "state.json"
    bad.write_text("{broken json", encoding="utf-8")
    portfolio = load_portfolio(bad, 5.0)
    assert portfolio.cash_sol == 5.0
    archived = list((tmp_path / "archive").glob("state.*.corrupted.json"))
    assert archived


def test_opened_today_count_resets_on_new_day(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    now = utc_now_iso()
    portfolio: PortfolioState = new_portfolio(5.0)
    portfolio.opened_today_count = 3
    portfolio.opened_today_date = "1999-01-01"
    engine = CreeperDripper(settings, DummyBirdeye(), DummyExecutor({"mint1": 100}), portfolio)
    engine.run_startup_recovery()
    assert portfolio.opened_today_count == 0
    assert portfolio.opened_today_date == now[:10]


def test_unknown_exit_reconciled_on_startup(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    now = utc_now_iso()
    portfolio: PortfolioState = new_portfolio(5.0)
    pos = _position(now)
    pos.status = "EXIT_PENDING"
    pos.pending_exit_qty_atomic = 50
    pos.pending_exit_reason = "take_profit_25"
    pos.pending_exit_signature = "sig-1"
    portfolio.open_positions[pos.token_mint] = pos
    engine = CreeperDripper(settings, DummyBirdeye(), DummyExecutor({"mint1": 20}), portfolio)
    decisions = engine.run_startup_recovery()
    assert portfolio.open_positions["mint1"].status == "PARTIAL"
    assert any(d.reason == "exit_reconciled_partial" for d in decisions)


def test_realized_proceeds_not_from_quote_when_execution_missing(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    now = utc_now_iso()
    portfolio: PortfolioState = new_portfolio(5.0)
    pos = _position(now)
    portfolio.open_positions[pos.token_mint] = pos
    engine = CreeperDripper(
        settings,
        DummyBirdeye(),
        DummyExecutor({"mint1": 100}, sell_result=ExecutionResult(status="success", requested_amount=40, executed_amount=40, output_amount=None, signature="sig-x")),
        portfolio,
    )
    decisions = []
    engine._start_exit(pos, 40, "take_profit_25", decisions, now)
    assert pos.realized_sol == 0.0
    assert portfolio.cash_sol == 5.0
