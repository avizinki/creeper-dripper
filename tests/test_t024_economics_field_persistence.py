from __future__ import annotations

import json

import pytest

from creeper_dripper.config import load_settings
from creeper_dripper.engine.trader import CreeperDripper
from creeper_dripper.models import PortfolioState, PositionState, ProbeQuote, TakeProfitStep, TokenCandidate, TradeDecision
from creeper_dripper.storage.state import new_portfolio
from creeper_dripper.utils import utc_now_iso


GOOD_MINT = "B87LXUgp7cdAMv8k2cZwwRWhCKFwDMhXkuRtudJfXCxp"
LOWVOL_MINT = "9wZjm1msCtiDxrkBipcjPnEPcwzqycbQpbsCydq68f2P"


def _base_env(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BIRDEYE_API_KEY", "x")
    monkeypatch.setenv("JUPITER_API_KEY", "x")
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("STATE_PATH", str(tmp_path / "runtime" / "state.json"))
    monkeypatch.setenv("JOURNAL_PATH", str(tmp_path / "runtime" / "journal.jsonl"))
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("DISCOVERY_SEED_LIMIT", "2")
    monkeypatch.setenv("DISCOVERY_OVERVIEW_LIMIT", "2")
    monkeypatch.setenv("DISCOVERY_MAX_CANDIDATES", "2")
    monkeypatch.setenv("MAX_ACTIVE_CANDIDATES", "2")


def _read_status(tmp_path) -> dict:
    p = tmp_path / "runtime" / "status.json"
    assert p.exists(), "status.json was not written"
    return json.loads(p.read_text(encoding="utf-8"))


def test_discovery_economics_fields_persisted_in_status_artifact(monkeypatch: pytest.MonkeyPatch, tmp_path):
    _base_env(monkeypatch, tmp_path)
    settings = load_settings()
    settings.prefilter_min_recent_volume_usd = 10_000
    settings.min_discovery_score = 0
    settings.min_buy_sell_ratio = 0.0
    settings.block_mutable_mint = False
    settings.block_freezable = False
    settings.require_jup_sell_route = False
    settings.min_volume_24h_usd = 1

    class StubBirdeye:
        def trending_tokens(self, limit=25):
            return [
                {"address": LOWVOL_MINT, "symbol": "LOW", "volume24hUSD": 1},
                {"address": GOOD_MINT, "symbol": "OK", "volume24hUSD": 50_000},
            ]

        def new_listings(self, limit=10):
            return []

        def build_candidate_light(self, seed: dict):
            return TokenCandidate(
                address=seed["address"],
                symbol=seed["symbol"],
                decimals=6,
                liquidity_usd=250_000.0,
                exit_liquidity_available=False,
                exit_liquidity_reason="birdeye_exit_liquidity_skipped_unsupported_chain",
                birdeye_exit_liquidity_supported=False,
                volume_24h_usd=500_000.0,
                buy_sell_ratio_1h=1.31,
                age_hours=2.2,
            )

        def enrich_candidate_heavy(self, candidate: TokenCandidate):
            return candidate

        def enrich_candidate_security_only(self, candidate: TokenCandidate):
            candidate.security_mint_mutable = False
            candidate.security_freezable = False
            return candidate

        def enrich_candidate_holders_only(self, candidate: TokenCandidate):
            candidate.top10_holder_percent = 0.0
            return candidate

    class StubExecutor:
        def __init__(self):
            self.jupiter = self

        def probe_quote(self, **kwargs):
            return ProbeQuote(
                input_amount_atomic=kwargs["amount_atomic"],
                out_amount_atomic=1_000_000,
                price_impact_bps=13.0,
                route_ok=True,
                raw={},
            )

        def transaction_status(self, _sig):
            return None

    portfolio: PortfolioState = new_portfolio(5.0)
    engine = CreeperDripper(settings, StubBirdeye(), StubExecutor(), portfolio)
    monkeypatch.setattr(engine, "_maybe_open_positions", lambda *_a, **_kw: None)
    engine.run_cycle()

    records = (_read_status(tmp_path).get("summary") or {}).get("economics_field_records") or []
    assert records, "expected persisted economics_field_records in status summary"
    low = next(r for r in records if r.get("mint") == LOWVOL_MINT)
    accepted = next(r for r in records if r.get("mint") == GOOD_MINT and r.get("event_type") == "candidate_accepted")
    for key in ("liquidity_usd", "buy_sell_ratio", "age_hours"):
        assert key in low
        assert key in accepted
    assert low.get("accepted_or_rejected") == "rejected"
    assert low.get("rejection_reason") == "reject_low_volume"


def test_probe_route_fields_persisted_in_status_artifact(monkeypatch: pytest.MonkeyPatch, tmp_path):
    _base_env(monkeypatch, tmp_path)
    settings = load_settings()
    settings.min_discovery_score = 0
    settings.min_buy_sell_ratio = 0.0
    settings.block_mutable_mint = False
    settings.block_freezable = False
    settings.require_jup_sell_route = False

    class StubBirdeye:
        def trending_tokens(self, limit=25):
            return [{"address": GOOD_MINT, "symbol": "OK", "volume24hUSD": 50_000}]

        def new_listings(self, limit=10):
            return []

        def build_candidate_light(self, seed: dict):
            return TokenCandidate(
                address=seed["address"],
                symbol=seed["symbol"],
                decimals=6,
                liquidity_usd=210_000.0,
                exit_liquidity_available=False,
                exit_liquidity_reason="birdeye_exit_liquidity_skipped_unsupported_chain",
                birdeye_exit_liquidity_supported=False,
                volume_24h_usd=500_000.0,
                buy_sell_ratio_1h=1.2,
                age_hours=1.1,
            )

        def enrich_candidate_heavy(self, candidate: TokenCandidate):
            return candidate

        def enrich_candidate_security_only(self, candidate: TokenCandidate):
            candidate.security_mint_mutable = False
            candidate.security_freezable = False
            return candidate

        def enrich_candidate_holders_only(self, candidate: TokenCandidate):
            candidate.top10_holder_percent = 0.0
            return candidate

    class StubExecutor:
        def __init__(self):
            self.jupiter = self
            self.calls = 0

        def probe_quote(self, **kwargs):
            self.calls += 1
            impact = 11.0 if self.calls == 1 else 22.0
            return ProbeQuote(
                input_amount_atomic=kwargs["amount_atomic"],
                out_amount_atomic=1_000_000,
                price_impact_bps=impact,
                route_ok=True,
                raw={},
            )

        def transaction_status(self, _sig):
            return None

    engine = CreeperDripper(settings, StubBirdeye(), StubExecutor(), new_portfolio(5.0))
    monkeypatch.setattr(engine, "_maybe_open_positions", lambda *_a, **_kw: None)
    engine.run_cycle()

    records = (_read_status(tmp_path).get("summary") or {}).get("economics_field_records") or []
    rec = next(r for r in records if r.get("mint") == GOOD_MINT and r.get("event_type") == "candidate_accepted")
    assert rec["price_impact_bps_buy"] == 11.0
    assert rec["price_impact_bps_sell"] == 22.0
    assert rec["route_exists"] is True
    assert rec["no_route"] is False
    for key in (
        "liquidity_usd",
        "buy_sell_ratio",
        "price_impact_bps_buy",
        "price_impact_bps_sell",
        "route_exists",
        "no_route",
        "fragile_route",
        "estimated_exit_value_sol",
        "zombie_class",
    ):
        assert key in rec


def test_zombie_fields_persisted_in_journal_metadata(monkeypatch: pytest.MonkeyPatch, tmp_path):
    _base_env(monkeypatch, tmp_path)
    settings = load_settings()
    now = utc_now_iso()
    position = PositionState(
        token_mint=GOOD_MINT,
        symbol="Z",
        decimals=6,
        status="ZOMBIE",
        opened_at=now,
        updated_at=now,
        entry_price_usd=1.0,
        avg_entry_price_usd=1.0,
        entry_sol=0.1,
        remaining_qty_atomic=100,
        remaining_qty_ui=100.0,
        peak_price_usd=1.0,
        last_price_usd=1.0,
        take_profit_steps=[TakeProfitStep(trigger_pct=25.0, fraction=1.0)],
        notes=["score=77.7"],
        valuation_status="no_route",
        zombie_class="HARD_ZOMBIE",
        last_estimated_exit_value_sol=0.0123,
        zombie_reason="no_route_persistent",
    )
    portfolio = new_portfolio(5.0)
    portfolio.open_positions[GOOD_MINT] = position

    class DummyBirdeye:
        pass

    class DummyExecutor:
        def __init__(self):
            self.jupiter = object()

    engine = CreeperDripper(settings, DummyBirdeye(), DummyExecutor(), portfolio)
    decisions = [TradeDecision(action="SELL_BLOCKED", token_mint=GOOD_MINT, symbol="Z", reason="no_route")]
    engine._persist_cycle(now, decisions, cycle_summary={"ok": True})

    journal_path = tmp_path / "runtime" / "journal.jsonl"
    rec = json.loads(journal_path.read_text(encoding="utf-8").strip().splitlines()[-1])
    md = rec.get("metadata") or {}
    assert md.get("zombie_class") == "HARD_ZOMBIE"
    assert md.get("estimated_exit_value_sol") == 0.0123
    assert md.get("no_route") is True
    assert md.get("route_exists") is False
    for key in (
        "liquidity_usd",
        "buy_sell_ratio",
        "price_impact_bps_buy",
        "price_impact_bps_sell",
        "route_exists",
        "no_route",
        "fragile_route",
        "estimated_exit_value_sol",
        "zombie_class",
    ):
        assert key in md
