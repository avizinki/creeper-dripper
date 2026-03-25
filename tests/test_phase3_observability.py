from __future__ import annotations

from types import SimpleNamespace

import requests

from creeper_dripper.clients.birdeye import BirdeyeClient
from creeper_dripper.config import load_settings
from creeper_dripper.engine.scoring import rejection_reasons
from creeper_dripper.engine.trader import CreeperDripper
from creeper_dripper.errors import REJECT_EXECUTION_ROUTE_MISSING, REJECT_LOW_LIQUIDITY, SAFETY_DAILY_LOSS_CAP, SAFETY_MAX_CONSEC_EXEC_FAILURES
from creeper_dripper.errors import EXEC_NO_ROUTE, EXEC_V2_EXECUTE_FAILED
from creeper_dripper.execution.executor import TradeExecutor
from creeper_dripper.models import ExecutionResult, PortfolioState, PositionState, ProbeQuote, TokenCandidate
from creeper_dripper.storage.state import new_portfolio
from creeper_dripper.utils import utc_now_iso


def _settings(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BIRDEYE_API_KEY", "x")
    monkeypatch.setenv("JUPITER_API_KEY", "x")
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("JOURNAL_PATH", str(tmp_path / "journal.jsonl"))
    return load_settings()


def test_rejection_reason_emission(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    c = TokenCandidate(address="m", symbol="S", liquidity_usd=1.0)
    reasons = rejection_reasons(c, settings)
    assert REJECT_LOW_LIQUIDITY not in reasons


def test_birdeye_retry_behavior(monkeypatch):
    client = BirdeyeClient("x")
    calls = {"n": 0}

    class DummyResponse:
        def __init__(self, ok: bool):
            self.status_code = 200
            self._ok = ok

        def raise_for_status(self):
            if not self._ok:
                raise requests.exceptions.Timeout("timeout")

        def json(self):
            return {"success": True, "data": {"tokens": []}}

    def fake_request(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise requests.exceptions.Timeout("timeout")
        return DummyResponse(True)

    monkeypatch.setattr(client._session, "request", fake_request)
    out = client.trending_tokens(limit=1)
    assert out == []
    assert calls["n"] == 3


class DummyExecutor:
    def __init__(self):
        self.jupiter = object()

    def transaction_status(self, _sig):
        return None

    def buy(self, *_args, **_kwargs):
        return ExecutionResult(status="failed", requested_amount=1, diagnostic_code="order_failed", error="x"), ProbeQuote(
            input_amount_atomic=1, out_amount_atomic=None, price_impact_bps=None, route_ok=False, raw={}
        )

    def sell(self, *_args, **_kwargs):
        return ExecutionResult(status="failed", requested_amount=1, diagnostic_code="execute_failed", error="x"), ProbeQuote(
            input_amount_atomic=1, out_amount_atomic=None, price_impact_bps=None, route_ok=False, raw={}
        )


class DummyBirdeye:
    pass


def test_safety_stop_on_consecutive_execution_failures(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    portfolio: PortfolioState = new_portfolio(5.0)
    portfolio.consecutive_execution_failures = settings.max_consecutive_execution_failures
    engine = CreeperDripper(settings, DummyBirdeye(), DummyExecutor(), portfolio)
    reason = engine._evaluate_safety(utc_now_iso())
    assert reason == SAFETY_MAX_CONSEC_EXEC_FAILURES


def test_safety_stop_on_daily_realized_loss_cap(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    portfolio: PortfolioState = new_portfolio(5.0)
    portfolio.total_realized_sol = -abs(settings.daily_realized_loss_cap_sol)
    engine = CreeperDripper(settings, DummyBirdeye(), DummyExecutor(), portfolio)
    reason = engine._evaluate_safety(utc_now_iso())
    assert reason == SAFETY_DAILY_LOSS_CAP


def test_fresh_same_cycle_data_does_not_trigger_stale_market_data(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    portfolio: PortfolioState = new_portfolio(5.0)
    portfolio.last_cycle_at = "2000-01-01T00:00:00+00:00"
    engine = CreeperDripper(settings, DummyBirdeye(), DummyExecutor(), portfolio)
    now = utc_now_iso()
    reason = engine._evaluate_safety(now, market_data_checked_at=now)
    assert reason is None


def test_old_market_data_triggers_stale_market_data(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    portfolio: PortfolioState = new_portfolio(5.0)
    # Stale-market safety applies only during an active run loop.
    settings.run_id = "test_run"
    portfolio.last_cycle_at = "2026-01-01T00:10:00+00:00"
    engine = CreeperDripper(settings, DummyBirdeye(), DummyExecutor(), portfolio)
    reason = engine._evaluate_safety("2026-01-01T00:30:00+00:00", market_data_checked_at="2026-01-01T00:00:00+00:00")
    assert reason == "safety_stale_market_data"
    assert engine._safety_diagnostics is not None
    assert engine._safety_diagnostics["data_age_seconds"] >= 1800


def test_cycle_summary_counts(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    portfolio: PortfolioState = new_portfolio(5.0)
    engine = CreeperDripper(settings, DummyBirdeye(), DummyExecutor(), portfolio)
    summary = engine._cycle_summary(
        utc_now_iso(),
        {"seeds_total": 10, "candidates_built": 8, "candidates_accepted": 2, "candidates_rejected_total": 6, "rejection_counts": {"reject_low_liquidity": 3}},
        [],
    )
    assert summary["seeds_total"] == 10
    assert summary["candidates_rejected_total"] == 6
    assert "reject_low_liquidity" in summary["rejection_counts"]


def test_run_cycle_surfaces_discovery_failure_in_summary(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    portfolio: PortfolioState = new_portfolio(5.0)
    engine = CreeperDripper(settings, DummyBirdeye(), DummyExecutor(), portfolio)

    def _boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(engine, "_discover_with_cadence", _boom)
    out = engine.run_cycle()
    summary = out["summary"]
    assert summary["discovery_failed"] is True
    assert summary["discovery_error_type"] == "RuntimeError"
    assert summary["discovery_error"] == "boom"
    assert any(e.get("event_type") == "discovery_failed" for e in out.get("events", []))


def test_entry_capacity_mode_summary_emitted(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    monkeypatch.setenv("ENTRY_CAPACITY_MODE", "balanced")
    settings = load_settings()
    engine = CreeperDripper(settings, DummyBirdeye(), DummyExecutor(), new_portfolio(5.0))
    out = engine.run_cycle()
    events = out.get("events", [])
    ev = next((e for e in events if e.get("event_type") == "entry_capacity_mode_summary"), None)
    assert ev is not None
    md = ev.get("metadata") or {}
    assert md.get("mode") == "balanced"
    for key in (
        "open_positions",
        "max_open_positions",
        "slots_available",
        "opened_today_count",
        "max_daily_new_positions",
        "early_risk_bucket_enabled",
        "cash_sol",
        "cash_reserve_sol",
    ):
        assert key in md


def test_jupiter_diagnostic_reason_mapping():
    raw = SimpleNamespace(output_amount_result=None, input_amount_result=None, signature=None, error="boom")
    res = TradeExecutor._normalize_execution_result(raw, requested_amount=10)
    assert res.status == "failed"
    assert res.diagnostic_code == "execute_failed"


def test_entry_path_refuses_candidate_with_no_sell_route(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    portfolio: PortfolioState = new_portfolio(5.0)

    class RouteProofExecutor(DummyExecutor):
        def quote_buy(self, _candidate, _size_sol):
            return ProbeQuote(input_amount_atomic=10_000_000, out_amount_atomic=50_000, price_impact_bps=100.0, route_ok=True, raw={})

        def quote_sell(self, _token_mint, _amount_atomic):
            return ProbeQuote(input_amount_atomic=50_000, out_amount_atomic=None, price_impact_bps=None, route_ok=False, raw={"classification": "no_route"})

        def buy(self, *_args, **_kwargs):
            raise AssertionError("buy should not be called when sell route is missing")

    engine = CreeperDripper(settings, DummyBirdeye(), RouteProofExecutor(), portfolio)
    candidate = TokenCandidate(address="mintX", symbol="TOK", decimals=6, price_usd=1.0, liquidity_usd=100000, volume_24h_usd=100000, buy_sell_ratio_1h=1.2)
    decisions = []
    engine._maybe_open_positions([candidate], decisions, utc_now_iso())
    assert any(d.reason == REJECT_EXECUTION_ROUTE_MISSING for d in decisions)


def test_candidate_blocked_when_fresh_buy_probe_fails(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    portfolio: PortfolioState = new_portfolio(5.0)

    class BuyFailExecutor(DummyExecutor):
        def quote_buy(self, _candidate, _size_sol):
            return ProbeQuote(input_amount_atomic=10_000_000, out_amount_atomic=None, price_impact_bps=None, route_ok=False, raw={"classification": "no_route"})

        def quote_sell(self, _token_mint, _amount_atomic):
            return ProbeQuote(input_amount_atomic=1, out_amount_atomic=1, price_impact_bps=1.0, route_ok=True, raw={})

        def buy(self, *_args, **_kwargs):
            raise AssertionError("buy should not run when buy probe fails")

    engine = CreeperDripper(settings, DummyBirdeye(), BuyFailExecutor(), portfolio)
    decisions = []
    candidate = TokenCandidate(address="mintB", symbol="BUYFAIL", decimals=6, price_usd=1.0, liquidity_usd=100000, volume_24h_usd=100000, buy_sell_ratio_1h=1.2)
    engine._maybe_open_positions([candidate], decisions, utc_now_iso())
    assert any(d.reason == "reject_no_buy_route" for d in decisions)


def test_candidate_blocked_when_price_impact_invalid(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    portfolio: PortfolioState = new_portfolio(5.0)

    class BadImpactExecutor(DummyExecutor):
        def quote_buy(self, _candidate, _size_sol):
            return ProbeQuote(input_amount_atomic=10_000_000, out_amount_atomic=100_000, price_impact_bps=-10000.0, route_ok=True, raw={})

        def quote_sell(self, _token_mint, _amount_atomic):
            return ProbeQuote(input_amount_atomic=100_000, out_amount_atomic=1_000_000, price_impact_bps=10.0, route_ok=True, raw={})

        def buy(self, *_args, **_kwargs):
            raise AssertionError("buy should not run for invalid price impact")

    engine = CreeperDripper(settings, DummyBirdeye(), BadImpactExecutor(), portfolio)
    decisions = []
    candidate = TokenCandidate(address="mintBad", symbol="BAD", decimals=6, price_usd=1.0, liquidity_usd=100000, volume_24h_usd=100000, buy_sell_ratio_1h=1.2)
    engine._maybe_open_positions([candidate], decisions, utc_now_iso())
    assert any(d.metadata.get("classification") == "reject_quote_price_impact_invalid" for d in decisions)


def test_candidate_blocked_when_roundtrip_economics_broken(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    portfolio: PortfolioState = new_portfolio(5.0)

    class BadEconomicsExecutor(DummyExecutor):
        def quote_buy(self, _candidate, _size_sol):
            return ProbeQuote(input_amount_atomic=10_000_000, out_amount_atomic=100_000, price_impact_bps=10.0, route_ok=True, raw={})

        def quote_sell(self, _token_mint, _amount_atomic):
            return ProbeQuote(input_amount_atomic=100_000, out_amount_atomic=1, price_impact_bps=10.0, route_ok=True, raw={})

        def buy(self, *_args, **_kwargs):
            raise AssertionError("buy should not run for broken roundtrip economics")

    engine = CreeperDripper(settings, DummyBirdeye(), BadEconomicsExecutor(), portfolio)
    decisions = []
    candidate = TokenCandidate(address="mintEco", symbol="ECO", decimals=6, price_usd=1.0, liquidity_usd=100000, volume_24h_usd=100000, buy_sell_ratio_1h=1.2)
    engine._maybe_open_positions([candidate], decisions, utc_now_iso())
    assert any(d.metadata.get("classification") == "reject_economic_sanity_failed" for d in decisions)


def test_run_summary_distinguishes_entry_failure_phases(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    engine = CreeperDripper(settings, DummyBirdeye(), DummyExecutor(), new_portfolio(5.0))
    decisions = [
        SimpleNamespace(action="BUY_SKIP", metadata={"phase": "pre_entry_probe"}),
        SimpleNamespace(action="BUY_SKIP", metadata={"phase": "order_build"}),
        SimpleNamespace(action="BUY_SKIP", metadata={"phase": "execute"}),
    ]
    summary = engine._cycle_summary(
        utc_now_iso(),
        {"seeds_total": 0, "candidates_built": 0, "candidates_accepted": 0, "candidates_rejected_total": 0, "rejection_counts": {}},
        decisions,
    )
    assert summary["entries_blocked_pre_execution"] == 1
    assert summary["entries_order_failed"] == 1
    assert summary["entries_execute_failed"] == 1


def test_run_summary_treats_mode_skips_separately(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    engine = CreeperDripper(settings, DummyBirdeye(), DummyExecutor(), new_portfolio(5.0))
    decisions = [
        SimpleNamespace(action="BUY_SKIP", metadata={"classification": "execute_skipped_dry_run", "phase": "mode_gate"}),
        SimpleNamespace(action="BUY_SKIP", metadata={"classification": "execute_skipped_live_disabled", "phase": "mode_gate"}),
    ]
    summary = engine._cycle_summary(
        utc_now_iso(),
        {"seeds_total": 0, "candidates_built": 0, "candidates_accepted": 0, "candidates_rejected_total": 0, "rejection_counts": {}},
        decisions,
    )
    assert summary["entries_skipped_dry_run"] == 1
    assert summary["entries_skipped_live_disabled"] == 1
    assert summary["entries_execute_failed"] == 0
    assert summary["execution_failures"] == 0


def test_market_no_route_does_not_count_as_execution_failure(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    portfolio: PortfolioState = new_portfolio(5.0)

    class NoRouteExitExecutor(DummyExecutor):
        def sell(self, *_args, **_kwargs):
            return (
                ExecutionResult(status="failed", requested_amount=1, diagnostic_code=EXEC_NO_ROUTE, error="no route"),
                ProbeQuote(input_amount_atomic=1, out_amount_atomic=None, price_impact_bps=None, route_ok=False, raw={"classification": EXEC_NO_ROUTE}),
            )

    engine = CreeperDripper(settings, DummyBirdeye(), NoRouteExitExecutor(), portfolio)
    pos = PositionState(
        token_mint="mintX",
        symbol="TOK",
        decimals=6,
        status="OPEN",
        opened_at=utc_now_iso(),
        updated_at=utc_now_iso(),
        entry_price_usd=0.0,
        avg_entry_price_usd=0.0,
        entry_sol=0.1,
        remaining_qty_atomic=100,
        remaining_qty_ui=0.0001,
        peak_price_usd=0.0,
        last_price_usd=0.0,
    )
    pos.pending_exit_qty_atomic = 100
    pos.pending_exit_reason = "liquidity_break_hard"
    portfolio.open_positions[pos.token_mint] = pos
    before = portfolio.consecutive_execution_failures
    engine._attempt_exit(pos, [], utc_now_iso())
    assert portfolio.consecutive_execution_failures == before


def test_system_execute_failure_counts_toward_safe_mode(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    portfolio: PortfolioState = new_portfolio(5.0)

    class ExecFailExitExecutor(DummyExecutor):
        def sell(self, *_args, **_kwargs):
            return (
                ExecutionResult(status="failed", requested_amount=1, diagnostic_code=EXEC_V2_EXECUTE_FAILED, error="boom"),
                ProbeQuote(input_amount_atomic=1, out_amount_atomic=None, price_impact_bps=None, route_ok=False, raw={"classification": EXEC_V2_EXECUTE_FAILED}),
            )

    engine = CreeperDripper(settings, DummyBirdeye(), ExecFailExitExecutor(), portfolio)
    pos = PositionState(
        token_mint="mintY",
        symbol="TOK2",
        decimals=6,
        status="OPEN",
        opened_at=utc_now_iso(),
        updated_at=utc_now_iso(),
        entry_price_usd=0.0,
        avg_entry_price_usd=0.0,
        entry_sol=0.1,
        remaining_qty_atomic=100,
        remaining_qty_ui=0.0001,
        peak_price_usd=0.0,
        last_price_usd=0.0,
    )
    pos.pending_exit_qty_atomic = 100
    pos.pending_exit_reason = "stop_loss"
    portfolio.open_positions[pos.token_mint] = pos
    before = portfolio.consecutive_execution_failures
    engine._attempt_exit(pos, [], utc_now_iso())
    assert portfolio.consecutive_execution_failures == before + 1


def test_route_proof_artifact_written_for_attempted_entry(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    portfolio: PortfolioState = new_portfolio(5.0)

    class RoutePassExecutor(DummyExecutor):
        def quote_buy(self, _candidate, _size_sol):
            return ProbeQuote(input_amount_atomic=10_000_000, out_amount_atomic=100_000, price_impact_bps=50.0, route_ok=True, raw={})

        def quote_sell(self, _token_mint, _amount_atomic):
            return ProbeQuote(input_amount_atomic=100_000, out_amount_atomic=1_000_000, price_impact_bps=50.0, route_ok=True, raw={})

        def buy(self, *_args, **_kwargs):
            return ExecutionResult(status="failed", requested_amount=1, diagnostic_code="order_failed", error="boom", diagnostic_metadata={"phase": "order_build"}), ProbeQuote(
                input_amount_atomic=1, out_amount_atomic=1, price_impact_bps=1.0, route_ok=True, raw={}
            )

    engine = CreeperDripper(settings, DummyBirdeye(), RoutePassExecutor(), portfolio)
    writes = []

    def fake_atomic_write_json(path, payload):
        writes.append((path, payload))

    monkeypatch.setattr("creeper_dripper.engine.trader.atomic_write_json", fake_atomic_write_json)
    decisions = []
    candidate = TokenCandidate(address="mintA", symbol="ARTIF", decimals=6, price_usd=1.0, liquidity_usd=100000, volume_24h_usd=100000, buy_sell_ratio_1h=1.2)
    now = utc_now_iso()
    engine._maybe_open_positions([candidate], decisions, now)
    assert writes, "expected entry probe artifact write"
    path, payload = writes[-1]
    assert "entry_probe_ARTIF_" in str(path)
    assert payload["candidate"]["mint"] == "mintA"
    assert payload["final_decision"] == "entry_probe_passed"


def test_stale_diagnostics_included_in_safety_event(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    settings.run_id = "test_run"
    portfolio: PortfolioState = new_portfolio(5.0)
    portfolio.last_cycle_at = "2026-01-01T00:00:00+00:00"
    engine = CreeperDripper(settings, DummyBirdeye(), DummyExecutor(), portfolio)

    old_checked = "2026-01-01T00:00:00+00:00"

    def fake_discover(_b, _j, _s, **_kwargs):
        return [], {
            "seeds_total": 0,
            "candidates_built": 0,
            "candidates_accepted": 0,
            "candidates_rejected_total": 0,
            "rejection_counts": {},
            "market_data_checked_at": old_checked,
        }

    monkeypatch.setattr("creeper_dripper.engine.trader.discover_candidates", fake_discover)
    monkeypatch.setattr("creeper_dripper.engine.trader.utc_now_iso", lambda: "2026-01-01T00:20:00+00:00")
    out = engine.run_cycle()
    stale_event = next(e for e in out["events"] if e["event_type"] == "safety_stop")
    assert stale_event["reason_code"] == "safety_stale_market_data"
    assert stale_event["metadata"]["checked_timestamp"] == old_checked
    assert stale_event["metadata"]["data_source_name"] == "discovery_cycle_market_data"
