from __future__ import annotations

import json

from creeper_dripper.config import load_settings
from creeper_dripper.engine.trader import CreeperDripper
from creeper_dripper.models import PortfolioState, PositionState, TakeProfitStep
from creeper_dripper.storage.state import new_portfolio
from creeper_dripper.utils import utc_now_iso


def _settings(monkeypatch, tmp_path, *, extra_env: dict | None = None):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BIRDEYE_API_KEY", "x")
    monkeypatch.setenv("JUPITER_API_KEY", "x")
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("JOURNAL_PATH", str(tmp_path / "journal.jsonl"))
    if extra_env:
        for k, v in extra_env.items():
            monkeypatch.setenv(k, str(v))
    return load_settings()


class _DummyBirdeye:
    def build_candidate(self, seed):
        # Minimal candidate to satisfy mark path.
        from creeper_dripper.models import TokenCandidate

        return TokenCandidate(address=seed.get("address"), symbol=seed.get("symbol", "X"), decimals=6)


class _DummyExecutor:
    def __init__(self):
        self.jupiter = object()

    def transaction_status(self, _sig):
        return None

    def quote_sell(self, _mint, qty_atomic):
        # Default: no route.
        from creeper_dripper.models import ProbeQuote

        return ProbeQuote(input_amount_atomic=qty_atomic, out_amount_atomic=None, price_impact_bps=None, route_ok=False, raw={})


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


def _read_status(tmp_path) -> dict:
    p = tmp_path / "status.json"
    assert p.exists(), "status.json was not written"
    return json.loads(p.read_text(encoding="utf-8"))


def _assert_policy_fields(policy: dict):
    required = (
        "runtime_risk_mode",
        "policy_posture",
        "policy_reason_summary",
        "policy_adjustments_applied",
        "wallet_pressure_level",
        "deployable_pressure_level",
        "zombie_pressure_level",
        "effective_position_size_sol",
        "effective_max_open_positions",
        "effective_max_daily_new_positions",
        "effective_min_score",
        "effective_min_liquidity_usd",
        "effective_min_buy_sell_ratio",
        "effective_final_zombie_recovery_probe_interval_cycles",
        "effective_exit_probe_aggressiveness",
        "effective_dripper_enabled",
        "entry_enabled",
        "entries_blocked_reason",
    )
    for k in required:
        assert k in policy, f"missing policy field: {k}"


def test_policy_visible_in_status_low_wallet_constrained(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path, extra_env={"BASE_POSITION_SIZE_SOL": "0.2", "RISK_MODE": "balanced"})
    portfolio: PortfolioState = new_portfolio(5.0)
    engine = CreeperDripper(settings, _DummyBirdeye(), _DummyExecutor(), portfolio)
    # Low wallet snapshot => deployable shrinks.
    engine.set_wallet_snapshot(available_sol=0.25, snapshot_at=utc_now_iso())
    # Avoid any entry work in this test.
    monkeypatch.setattr(engine, "_maybe_open_positions", lambda *_a, **_kw: None)
    out = engine.run_cycle()
    assert out
    status = _read_status(tmp_path)
    policy = status.get("derived_policy")
    assert isinstance(policy, dict)
    _assert_policy_fields(policy)
    assert policy["policy_posture"] in {"constrained", "balanced", "conservative", "recovery_only"}


def test_policy_visible_in_status_high_zombie_pressure(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path, extra_env={"MAX_OPEN_POSITIONS": "4", "MAX_DAILY_NEW_POSITIONS": "6"})
    portfolio: PortfolioState = new_portfolio(5.0)
    now = utc_now_iso()
    portfolio.open_positions["mintZ"] = _zombie_position(now, "mintZ")
    engine = CreeperDripper(settings, _DummyBirdeye(), _DummyExecutor(), portfolio)
    engine.set_wallet_snapshot(available_sol=5.0, snapshot_at=now)
    monkeypatch.setattr(engine, "_maybe_open_positions", lambda *_a, **_kw: None)
    engine.run_cycle()
    policy = _read_status(tmp_path).get("derived_policy")
    assert isinstance(policy, dict)
    _assert_policy_fields(policy)
    assert policy["zombie_pressure_level"] in {"low", "medium", "high"}


def test_policy_visible_in_status_entry_blocked(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path, extra_env={"RISK_MODE": "aggressive"})
    portfolio: PortfolioState = new_portfolio(5.0)
    engine = CreeperDripper(settings, _DummyBirdeye(), _DummyExecutor(), portfolio)
    # Missing wallet snapshot => T-013 blocks entries; policy should reflect recovery_only posture.
    engine.set_wallet_snapshot(available_sol=None, snapshot_at=utc_now_iso())
    monkeypatch.setattr(engine, "_maybe_open_positions", lambda *_a, **_kw: None)
    engine.run_cycle()
    policy = _read_status(tmp_path).get("derived_policy")
    assert isinstance(policy, dict)
    _assert_policy_fields(policy)
    assert policy["entry_enabled"] is False
    assert policy["entries_blocked_reason"] is not None
