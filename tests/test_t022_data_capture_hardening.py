from __future__ import annotations

import json

import pytest

from creeper_dripper.config import load_settings
from creeper_dripper.engine.discovery import discover_candidates
from creeper_dripper.engine.trader import CreeperDripper
from creeper_dripper.models import PortfolioState, PositionState, ProbeQuote, TakeProfitStep, TokenCandidate, TradeDecision
from creeper_dripper.storage.state import new_portfolio
from creeper_dripper.utils import utc_now_iso


VALID_MINT = "B87LXUgp7cdAMv8k2cZwwRWhCKFwDMhXkuRtudJfXCxp"


def _base_env(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BIRDEYE_API_KEY", "x")
    monkeypatch.setenv("JUPITER_API_KEY", "x")
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("STATE_PATH", str(tmp_path / "runtime" / "state.json"))
    monkeypatch.setenv("JOURNAL_PATH", str(tmp_path / "runtime" / "journal.jsonl"))
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("DISCOVERY_INTERVAL_SECONDS", "30")
    monkeypatch.setenv("MAX_ACTIVE_CANDIDATES", "3")
    monkeypatch.setenv("DISCOVERY_SEED_LIMIT", "2")
    monkeypatch.setenv("DISCOVERY_OVERVIEW_LIMIT", "2")
    monkeypatch.setenv("DISCOVERY_MAX_CANDIDATES", "2")
    monkeypatch.setenv("PREFILTER_MIN_RECENT_VOLUME_USD", "1")


def test_discovery_events_emit_normalized_candidate_metadata(monkeypatch: pytest.MonkeyPatch, tmp_path):
    _base_env(monkeypatch, tmp_path)
    settings = load_settings()
    settings.min_volume_24h_usd = 1
    # Make acceptance permissive; this test is about metadata capture, not scoring policy.
    settings.min_discovery_score = 0
    settings.min_buy_sell_ratio = 0.0
    settings.block_mutable_mint = False
    settings.block_freezable = False
    settings.require_jup_sell_route = False

    class StubBirdeye:
        def trending_tokens(self, limit=25):
            return [{"address": VALID_MINT, "symbol": "OK", "volume24hUSD": 50_000}, {"address": "9wZjm1msCtiDxrkBipcjPnEPcwzqycbQpbsCydq68f2P", "symbol": "BAD", "volume24hUSD": 50_000}]

        def new_listings(self, limit=10):
            return []

        def build_candidate_light(self, seed: dict):
            addr = str(seed.get("address"))
            c = TokenCandidate(
                address=addr,
                symbol=str(seed.get("symbol") or "?"),
                decimals=6,
                liquidity_usd=250_000.0,
                exit_liquidity_available=False,
                exit_liquidity_reason="birdeye_exit_liquidity_skipped_unsupported_chain",
                birdeye_exit_liquidity_supported=False,
                volume_24h_usd=500_000.0,
                buy_sell_ratio_1h=1.25,
                age_hours=2.5,
                raw={"seed": seed, "overview": {"updatedAt": utc_now_iso()}},
            )
            return c

        def enrich_candidate_heavy(self, candidate: TokenCandidate):
            return candidate

        def enrich_candidate_security_only(self, candidate: TokenCandidate):
            candidate.security_mint_mutable = False
            candidate.security_freezable = False
            return candidate

        def enrich_candidate_holders_only(self, candidate: TokenCandidate):
            candidate.top10_holder_percent = 0.0
            return candidate

    class StubJupiter:
        def __init__(self):
            self._calls = 0

        def probe_quote(self, **kwargs):
            self._calls += 1
            # For the second seed (BAD), simulate no route by returning no out_amount_atomic.
            # Buy probes are SOL->TOKEN (output_mint=token). Sell probes are TOKEN->SOL (input_mint=token).
            token_mint = kwargs.get("output_mint") if kwargs.get("output_mint") != "So11111111111111111111111111111111111111112" else kwargs.get("input_mint")
            if token_mint != VALID_MINT:
                return ProbeQuote(
                    input_amount_atomic=kwargs["amount_atomic"],
                    out_amount_atomic=None,
                    price_impact_bps=None,
                    route_ok=False,
                    raw={},
                )
            return ProbeQuote(
                input_amount_atomic=kwargs["amount_atomic"],
                out_amount_atomic=1_000_000,
                price_impact_bps=42.0,
                route_ok=True,
                raw={},
            )

    candidates, summary = discover_candidates(StubBirdeye(), StubJupiter(), settings)
    assert isinstance(summary, dict)
    events = summary.get("events")
    assert isinstance(events, list) and events, "expected discovery events in summary"

    accepted = [e for e in events if e.get("event_type") == "candidate_accepted"]
    rejected = [e for e in events if e.get("event_type") == "candidate_rejected"]
    assert accepted, "expected at least one accepted candidate"
    assert rejected, "expected at least one rejected candidate"

    acc_md = accepted[0]["metadata"]
    for k in (
        "mint",
        "symbol",
        "score",
        "liquidity_usd",
        "buy_sell_ratio",
        "age_hours",
        "price_impact_bps_buy",
        "price_impact_bps_sell",
        "route_exists",
        "no_route",
        "fragile_route",
        "rejection_reason",
    ):
        assert k in acc_md, f"missing normalized key in candidate_accepted: {k}"

    rej_md = rejected[0]["metadata"]
    assert "rejection_reason" in rej_md
    assert "mint" in rej_md and "symbol" in rej_md
    # Explicit nulls are allowed; keys must exist when candidate context exists.
    for k in ("score", "liquidity_usd", "buy_sell_ratio", "age_hours"):
        assert k in rej_md, f"missing normalized key in candidate_rejected: {k}"


def test_journal_decisions_include_normalized_token_fields(monkeypatch: pytest.MonkeyPatch, tmp_path):
    _base_env(monkeypatch, tmp_path)
    settings = load_settings()
    portfolio: PortfolioState = new_portfolio(5.0)
    now = utc_now_iso()

    position = PositionState(
        token_mint=VALID_MINT,
        symbol="Z",
        decimals=6,
        status="ZOMBIE",
        opened_at=now,
        updated_at=now,
        entry_price_usd=1.0,
        avg_entry_price_usd=1.0,
        entry_sol=0.06,
        remaining_qty_atomic=100,
        remaining_qty_ui=100.0,
        peak_price_usd=1.0,
        last_price_usd=1.0,
        take_profit_steps=[TakeProfitStep(trigger_pct=25.0, fraction=1.0)],
        notes=["score=70.4"],
        valuation_status="no_route",
        zombie_reason="no_route_persistent",
        zombie_class="HARD_ZOMBIE",
        last_estimated_exit_value_sol=0.0123,
    )
    portfolio.open_positions[VALID_MINT] = position

    class DummyBirdeye:
        pass

    class DummyExecutor:
        def __init__(self):
            self.jupiter = object()

    engine = CreeperDripper(settings, DummyBirdeye(), DummyExecutor(), portfolio)
    # Seed discovery candidates so normalization can pull liquidity/buy-sell/age when available.
    engine._last_discovery_candidates = [
        TokenCandidate(
            address=VALID_MINT,
            symbol="Z",
            decimals=6,
            liquidity_usd=123_456.0,
            buy_sell_ratio_1h=1.11,
            age_hours=3.3,
            jupiter_buy_price_impact_bps=12.0,
            jupiter_sell_price_impact_bps=34.0,
        )
    ]

    decisions = [TradeDecision(action="SELL_BLOCKED", token_mint=VALID_MINT, symbol="Z", reason="no_route")]
    engine._persist_cycle(now, decisions, cycle_summary={"ok": True})

    journal_path = tmp_path / "runtime" / "journal.jsonl"
    assert journal_path.exists()
    line = journal_path.read_text(encoding="utf-8").strip().splitlines()[-1]
    rec = json.loads(line)
    md = rec.get("metadata") or {}

    for k in (
        "mint",
        "symbol",
        "score",
        "liquidity_usd",
        "buy_sell_ratio",
        "age_hours",
        "price_impact_bps_buy",
        "price_impact_bps_sell",
        "no_route",
        "estimated_exit_value_sol",
        "zombie_class",
        "blocked_reason",
    ):
        assert k in md, f"missing normalized metadata key in journal decision: {k}"
    assert md["mint"] == VALID_MINT
    assert md["blocked_reason"] in {"no_route", "no_route_persistent"}

