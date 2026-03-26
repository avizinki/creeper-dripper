from __future__ import annotations

from creeper_dripper.config import load_settings
from creeper_dripper.engine.trader import CreeperDripper
from creeper_dripper.models import ExecutionResult, ProbeQuote, TokenCandidate
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

    def quote_buy(self, *_args, **_kwargs):
        raise AssertionError("quote_buy should not be called by these tests")

    def quote_sell(self, *_args, **_kwargs):
        raise AssertionError("quote_sell should not be called by these tests")

    def buy(self, *_args, **_kwargs):
        return ExecutionResult(status="failed", requested_amount=1, diagnostic_code="order_failed", error="x"), ProbeQuote(
            input_amount_atomic=1, out_amount_atomic=None, price_impact_bps=None, route_ok=False, raw={}
        )

    def sell(self, *_args, **_kwargs):
        return ExecutionResult(status="failed", requested_amount=1, diagnostic_code="execute_failed", error="x"), ProbeQuote(
            input_amount_atomic=1, out_amount_atomic=None, price_impact_bps=None, route_ok=False, raw={}
        )


def _fake_discovery(*_args, **_kwargs):
    c = TokenCandidate(address="mintX", symbol="TOK", decimals=6, price_usd=1.0, liquidity_usd=100000, volume_24h_usd=100000, buy_sell_ratio_1h=1.2)
    summary = {"seeds_total": 1, "candidates_built": 1, "candidates_accepted": 1, "candidates_rejected_total": 0, "rejection_counts": {}}
    return [c], summary


def test_wallet_snapshot_missing_blocks_entries_and_deployable_zero(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    portfolio = new_portfolio(5.0)
    engine = CreeperDripper(settings, DummyBirdeye(), DummyExecutor(), portfolio)

    monkeypatch.setattr(engine, "_discover_with_cadence", _fake_discovery)
    engine.set_wallet_snapshot(available_sol=None, snapshot_at=utc_now_iso())

    out = engine.run_cycle()
    events = out.get("events", [])
    blocked = next((e for e in events if e.get("event_type") == "entries_blocked"), None)
    assert blocked is not None
    assert (blocked.get("metadata") or {}).get("reason") == "blocked_wallet_snapshot_missing"

    ev = next((e for e in events if e.get("event_type") == "entry_capacity_mode_summary"), None)
    assert ev is not None
    md = ev.get("metadata") or {}
    assert md.get("deployable_sol") == 0.0
    assert md.get("entries_blocked_reason") == "blocked_wallet_snapshot_missing"


def test_wallet_lower_than_cash_clamps_deployable_and_blocks_on_drift(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    portfolio = new_portfolio(2.0)
    engine = CreeperDripper(settings, DummyBirdeye(), DummyExecutor(), portfolio)

    monkeypatch.setattr(engine, "_discover_with_cadence", _fake_discovery)
    engine.set_wallet_snapshot(available_sol=1.0, snapshot_at=utc_now_iso())

    out = engine.run_cycle()
    events = out.get("events", [])
    blocked = next((e for e in events if e.get("event_type") == "entries_blocked"), None)
    assert blocked is not None
    assert (blocked.get("metadata") or {}).get("reason") == "blocked_accounting_drift_cash_gt_wallet"

    ev = next((e for e in events if e.get("event_type") == "entry_capacity_mode_summary"), None)
    assert ev is not None
    md = ev.get("metadata") or {}
    expected = max(0.0, 1.0 - float(settings.cash_reserve_sol))
    assert abs(float(md.get("deployable_sol")) - expected) < 1e-9


def test_with_large_epsilon_no_block_and_deployable_uses_wallet_floor(monkeypatch, tmp_path):
    monkeypatch.setenv("ACCOUNTING_DRIFT_EPSILON_SOL", "10.0")
    settings = _settings(monkeypatch, tmp_path)
    portfolio = new_portfolio(2.0)
    engine = CreeperDripper(settings, DummyBirdeye(), DummyExecutor(), portfolio)

    monkeypatch.setattr(engine, "_discover_with_cadence", _fake_discovery)
    engine.set_wallet_snapshot(available_sol=1.0, snapshot_at=utc_now_iso())

    called = {"n": 0}

    def _no_op_open_positions(_candidates, _decisions, _now):
        called["n"] += 1

    monkeypatch.setattr(engine, "_maybe_open_positions", _no_op_open_positions)

    out = engine.run_cycle()
    events = out.get("events", [])
    assert not any(e.get("event_type") == "entries_blocked" for e in events)
    assert called["n"] == 1

    ev = next((e for e in events if e.get("event_type") == "entry_capacity_mode_summary"), None)
    assert ev is not None
    md = ev.get("metadata") or {}
    expected = max(0.0, 1.0 - float(settings.cash_reserve_sol))
    assert abs(float(md.get("deployable_sol")) - expected) < 1e-9
    assert md.get("entries_blocked_reason") is None

