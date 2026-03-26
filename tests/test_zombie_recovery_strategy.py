from __future__ import annotations

from creeper_dripper.config import load_settings
from creeper_dripper.engine.trader import CreeperDripper
from creeper_dripper.errors import EXEC_V2_EXECUTE_FAILED
from creeper_dripper.models import ExecutionResult, PortfolioState, PositionState, ProbeQuote, TakeProfitStep, TradeDecision
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
    pass


def _zombie_position(now: str) -> PositionState:
    return PositionState(
        token_mint="mintZ",
        symbol="Z",
        decimals=6,
        status="ZOMBIE",
        opened_at=now,
        updated_at=now,
        entry_price_usd=1.0,
        avg_entry_price_usd=1.0,
        entry_sol=1.0,
        remaining_qty_atomic=1_000_000,
        remaining_qty_ui=1.0,
        peak_price_usd=1.0,
        last_price_usd=1.0,
        take_profit_steps=[TakeProfitStep(trigger_pct=25.0, fraction=1.0)],
        valuation_status="no_route",
        exit_blocked_cycles=20,
        zombie_reason="no_route_persistent",
        zombie_since=now,
        pending_exit_reason="stop_loss",
        pending_exit_qty_atomic=1_000_000,
    )


def test_zombie_capital_estimation_emitted(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    portfolio: PortfolioState = new_portfolio(5.0)
    now = utc_now_iso()
    pos = _zombie_position(now)
    pos.last_estimated_exit_value_sol = 0.42
    pos.zombie_class = "SOFT_ZOMBIE"
    portfolio.open_positions[pos.token_mint] = pos

    class _Exec:
        def __init__(self):
            self.jupiter = object()

        def transaction_status(self, _sig):
            return None

        def quote_sell(self, *_a, **_kw):
            return ProbeQuote(input_amount_atomic=1, out_amount_atomic=None, price_impact_bps=None, route_ok=False, raw={})

    engine = CreeperDripper(settings, _DummyBirdeye(), _Exec(), portfolio)
    monkeypatch.setattr(engine, "_discover_with_cadence", lambda: ([], {"seeds_total": 0, "candidates_built": 0, "candidates_accepted": 0, "candidates_rejected_total": 0, "rejection_counts": {}}))
    out = engine.run_cycle()
    assert any(e.get("event_type") == "zombie_capital_estimated" for e in out.get("events", []))
    assert out.get("zombie_locked_sol_estimate") is not None


def test_fake_liquid_classification_and_partial_exit_attempt(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    portfolio: PortfolioState = new_portfolio(5.0)
    now = utc_now_iso()
    pos = _zombie_position(now)
    portfolio.open_positions[pos.token_mint] = pos

    sell_calls: list[int] = []

    class _Exec:
        def __init__(self):
            self.jupiter = object()

        def transaction_status(self, _sig):
            return None

        def quote_sell(self, *_a, **_kw):
            return ProbeQuote(input_amount_atomic=1, out_amount_atomic=None, price_impact_bps=None, route_ok=False, raw={})

        def sell(self, _mint, requested_qty):
            sell_calls.append(int(requested_qty))
            # Report a system execution failure with a "good" quote (route_ok + out_amount_atomic).
            res = ExecutionResult(
                status="failed",
                requested_amount=int(requested_qty),
                diagnostic_code="execute_failed",
                error="boom",
                diagnostic_metadata={"classification": EXEC_V2_EXECUTE_FAILED},
            )
            quote = ProbeQuote(
                input_amount_atomic=int(requested_qty),
                out_amount_atomic=123,
                price_impact_bps=10.0,
                route_ok=True,
                raw={},
            )
            return res, quote

    engine = CreeperDripper(settings, _DummyBirdeye(), _Exec(), portfolio)
    decisions: list[TradeDecision] = []
    engine._attempt_exit(pos, decisions, now)
    assert pos.zombie_class == "FAKE_LIQUID"
    # Next attempt should shrink qty to 25%.
    engine._attempt_exit(pos, decisions, now)
    assert len(sell_calls) >= 2
    assert sell_calls[1] < sell_calls[0]
