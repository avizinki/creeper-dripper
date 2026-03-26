from __future__ import annotations

from creeper_dripper.config import load_settings
from creeper_dripper.engine.trader import CreeperDripper
from creeper_dripper.models import PortfolioState, PositionState, TakeProfitStep
from creeper_dripper.storage.state import new_portfolio
from creeper_dripper.utils import utc_now_iso


def _settings(monkeypatch, tmp_path, *, extra_env: dict | None = None):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BIRDEYE_API_KEY", "x")
    monkeypatch.setenv("JUPITER_API_KEY", "x")
    monkeypatch.setenv("DISCOVERY_INTERVAL_SECONDS", "90")
    monkeypatch.setenv("MAX_ACTIVE_CANDIDATES", "1")
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("JOURNAL_PATH", str(tmp_path / "journal.jsonl"))
    if extra_env:
        for k, v in extra_env.items():
            monkeypatch.setenv(k, str(v))
    return load_settings()


class _DummyBirdeye:
    pass


class _DummyExecutor:
    def __init__(self):
        self.jupiter = object()

    def transaction_status(self, _sig):
        return None


def _zombie_position(now: str, mint: str) -> PositionState:
    return PositionState(
        token_mint=mint,
        symbol="Z",
        decimals=6,
        status="ZOMBIE",
        opened_at=now,
        updated_at=now,
        entry_price_usd=1.0,
        avg_entry_price_usd=1.0,
        entry_sol=1.0,
        remaining_qty_atomic=100,
        remaining_qty_ui=100.0,
        peak_price_usd=1.0,
        last_price_usd=1.0,
        take_profit_steps=[TakeProfitStep(trigger_pct=25.0, fraction=1.0)],
        valuation_status="no_route",
        exit_blocked_cycles=12,
        zombie_reason="no_route_persistent",
        zombie_since=now,
    )


def test_low_wallet_shrinks_effective_position_size(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path, extra_env={"BASE_POSITION_SIZE_SOL": "0.2"})
    portfolio: PortfolioState = new_portfolio(5.0)
    engine = CreeperDripper(settings, _DummyBirdeye(), _DummyExecutor(), portfolio)
    engine.set_wallet_snapshot(available_sol=0.3, snapshot_at=utc_now_iso())
    out = engine.run_cycle()
    ev = next(e for e in out.get("events", []) if e.get("event_type") == "entry_capacity_mode_summary")
    md = ev.get("metadata") or {}
    assert md.get("effective_position_size_sol") is not None
    assert float(md["effective_position_size_sol"]) <= 0.2


def test_zombie_pressure_tightens_policy_caps(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path, extra_env={"MAX_OPEN_POSITIONS": "4", "MAX_DAILY_NEW_POSITIONS": "6"})
    portfolio: PortfolioState = new_portfolio(5.0)
    now = utc_now_iso()
    portfolio.open_positions["mintZ"] = _zombie_position(now, "mintZ")
    engine = CreeperDripper(settings, _DummyBirdeye(), _DummyExecutor(), portfolio)
    engine.set_wallet_snapshot(available_sol=5.0, snapshot_at=now)
    out = engine.run_cycle()
    md = next(e for e in out.get("events", []) if e.get("event_type") == "entry_capacity_mode_summary").get("metadata") or {}
    assert int(md.get("effective_policy_max_open_positions")) == 3
    assert int(md.get("effective_policy_max_daily_new_positions")) == 5


def test_base_position_size_sol_is_primary_operator_input(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path, extra_env={"BASE_POSITION_SIZE_SOL": "0.06", "RISK_MODE": "balanced"})
    portfolio: PortfolioState = new_portfolio(5.0)
    engine = CreeperDripper(settings, _DummyBirdeye(), _DummyExecutor(), portfolio)
    engine.set_wallet_snapshot(available_sol=5.0, snapshot_at=utc_now_iso())
    out = engine.run_cycle()
    md = next(e for e in out.get("events", []) if e.get("event_type") == "entry_capacity_mode_summary").get("metadata") or {}
    assert abs(float(md.get("effective_position_size_sol")) - 0.06) < 1e-9


def test_observability_exposes_runtime_risk_mode(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path, extra_env={"RISK_MODE": "aggressive"})
    portfolio: PortfolioState = new_portfolio(5.0)
    engine = CreeperDripper(settings, _DummyBirdeye(), _DummyExecutor(), portfolio)
    engine.set_wallet_snapshot(available_sol=5.0, snapshot_at=utc_now_iso())
    out = engine.run_cycle()
    md = next(e for e in out.get("events", []) if e.get("event_type") == "entry_capacity_mode_summary").get("metadata") or {}
    assert md.get("runtime_risk_mode") == "aggressive"

