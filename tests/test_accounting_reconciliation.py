from __future__ import annotations

from creeper_dripper.config import load_settings
from creeper_dripper.engine.trader import CreeperDripper
from creeper_dripper.models import PositionState, TakeProfitStep, TokenCandidate
from creeper_dripper.storage.state import new_portfolio
from creeper_dripper.utils import utc_now_iso


def _settings(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BIRDEYE_API_KEY", "x")
    monkeypatch.setenv("JUPITER_API_KEY", "x")
    monkeypatch.setenv("DISCOVERY_INTERVAL_SECONDS", "90")
    monkeypatch.setenv("MAX_ACTIVE_CANDIDATES", "1")
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("JOURNAL_PATH", str(tmp_path / "journal.jsonl"))
    return load_settings()


class DummyBirdeye:
    pass


class DummyExecutor:
    def __init__(self):
        self.jupiter = object()

    def transaction_status(self, _sig):
        return None


def _fake_discovery(*_args, **_kwargs):
    c = TokenCandidate(
        address="mintX",
        symbol="TOK",
        decimals=6,
        price_usd=1.0,
        liquidity_usd=100000,
        volume_24h_usd=100000,
        buy_sell_ratio_1h=1.2,
    )
    summary = {
        "seeds_total": 1,
        "candidates_built": 1,
        "candidates_accepted": 1,
        "candidates_rejected_total": 0,
        "rejection_counts": {},
    }
    return [c], summary


def _open_position(now: str, *, mint: str, symbol: str) -> PositionState:
    return PositionState(
        token_mint=mint,
        symbol=symbol,
        decimals=6,
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
        entry_mark_sol_per_token=0.01,
        last_mark_sol_per_token=0.01,
        peak_mark_sol_per_token=0.01,
        position_id=f"{mint}:{now}",
        take_profit_steps=[TakeProfitStep(trigger_pct=25.0, fraction=1.0)],
    )


def test_drift_reconciles_cash_to_wallet_upper_bound(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    portfolio = new_portfolio(5.0)
    engine = CreeperDripper(settings, DummyBirdeye(), DummyExecutor(), portfolio)

    engine.set_wallet_snapshot(available_sol=1.0, snapshot_at=utc_now_iso())
    assert abs(engine.portfolio.cash_sol - 1.0) < 1e-9


def test_no_drift_no_reconciliation_and_entries_not_blocked(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    portfolio = new_portfolio(1.0)
    engine = CreeperDripper(settings, DummyBirdeye(), DummyExecutor(), portfolio)
    engine.set_wallet_snapshot(available_sol=1.0, snapshot_at=utc_now_iso())

    monkeypatch.setattr(engine, "_discover_with_cadence", _fake_discovery)
    called = {"n": 0}

    def _no_op_open_positions(_candidates, _decisions, _now):
        called["n"] += 1

    monkeypatch.setattr(engine, "_maybe_open_positions", _no_op_open_positions)
    out = engine.run_cycle()
    assert called["n"] == 1
    md = next(e for e in out.get("events", []) if e.get("event_type") == "entry_capacity_mode_summary").get("metadata") or {}
    assert md.get("entries_blocked_reason") is None


def test_pending_proceeds_reduces_cash_from_wallet_total(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    portfolio = new_portfolio(1.0)
    # Simulate an unconfirmed entry debit marker: pending_proceeds_sol should reduce free cash.
    now = utc_now_iso()
    portfolio.open_positions["mintP"] = _open_position(now, mint="mintP", symbol="P")
    pos = portfolio.open_positions["mintP"]
    pos.pending_proceeds_sol = 0.4

    engine = CreeperDripper(settings, DummyBirdeye(), DummyExecutor(), portfolio)
    engine.set_wallet_snapshot(available_sol=1.0, snapshot_at=utc_now_iso())
    assert abs(engine.portfolio.cash_sol - 0.6) < 1e-9


def test_startup_reconciliation_emits_accounting_reconciled_event(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    portfolio = new_portfolio(5.0)
    engine = CreeperDripper(settings, DummyBirdeye(), DummyExecutor(), portfolio)
    monkeypatch.setattr(engine, "_discover_with_cadence", _fake_discovery)
    monkeypatch.setattr(engine, "_maybe_open_positions", lambda *_a, **_kw: None)
    engine.set_wallet_snapshot(available_sol=1.0, snapshot_at=utc_now_iso())
    out = engine.run_cycle()
    assert any(e.get("event_type") == "accounting_reconciled" for e in out.get("events", []))

