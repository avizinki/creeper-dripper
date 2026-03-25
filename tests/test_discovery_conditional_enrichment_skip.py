from __future__ import annotations

import pytest

from creeper_dripper.config import load_settings
from creeper_dripper.engine.discovery import discover_candidates
from creeper_dripper.models import ProbeQuote, TokenCandidate


def test_skip_security_and_holders_when_best_case_below_score(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BIRDEYE_API_KEY", "x")
    monkeypatch.setenv("JUPITER_API_KEY", "x")
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("STATE_PATH", str(tmp_path / "runtime" / "state.json"))
    monkeypatch.setenv("JOURNAL_PATH", str(tmp_path / "runtime" / "journal.jsonl"))
    monkeypatch.setenv("DISCOVERY_INTERVAL_SECONDS", "30")
    monkeypatch.setenv("MAX_ACTIVE_CANDIDATES", "1")
    monkeypatch.setenv("DISCOVERY_MAX_CANDIDATES", "1")
    monkeypatch.setenv("DISCOVERY_OVERVIEW_LIMIT", "3")
    monkeypatch.setenv("CANDIDATE_CACHE_TTL_SECONDS", "20")
    monkeypatch.setenv("ROUTE_CHECK_CACHE_TTL_SECONDS", "15")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    monkeypatch.setenv("DRY_RUN", "true")

    # Make cheap seed + prefilter permissive, but force the min_discovery_score gate high.
    monkeypatch.setenv("PREFILTER_MIN_RECENT_VOLUME_USD", "0")
    monkeypatch.setenv("MIN_VOLUME_24H_USD", "1")
    monkeypatch.setenv("MIN_BUY_SELL_RATIO", "0")
    monkeypatch.setenv("MIN_DISCOVERY_SCORE", "90")
    monkeypatch.setenv("EARLY_RISK_BUCKET_ENABLED", "false")

    settings = load_settings()

    calls = {"overview": 0, "creation": 0, "security_only": 0, "holders_only": 0}

    class StubBirdeye:
        def trending_tokens(self, limit=25):
            return [{"address": "mintX", "symbol": "X", "volume24hUSD": 10_000}]

        def new_listings(self, limit=10):
            return []

        def build_candidate_light(self, seed: dict):
            calls["overview"] += 1
            return TokenCandidate(
                address=seed["address"],
                symbol=seed["symbol"],
                decimals=6,
                liquidity_usd=0.0,
                volume_24h_usd=1.0,
                exit_liquidity_available=False,
                exit_liquidity_reason="birdeye_exit_liquidity_skipped_unsupported_chain",
                birdeye_exit_liquidity_supported=False,
                buy_sell_ratio_1h=0.0,
                change_1h_pct=100.0,  # heavily overextended => large negative score
                age_hours=None,
                top10_holder_percent=None,
                security_mint_mutable=None,
                security_freezable=None,
            )

        def enrich_candidate_heavy(self, candidate: TokenCandidate) -> TokenCandidate:
            # discovery prefilter stage still calls "heavy" (creation/age enrichment).
            calls["creation"] += 1
            return candidate

        def enrich_candidate_security_only(self, candidate: TokenCandidate) -> TokenCandidate:
            calls["security_only"] += 1
            candidate.security_mint_mutable = False
            candidate.security_freezable = False
            return candidate

        def enrich_candidate_holders_only(self, candidate: TokenCandidate) -> TokenCandidate:
            calls["holders_only"] += 1
            candidate.top10_holder_percent = 0.0
            return candidate

    class StubJupiter:
        def probe_quote(self, **kwargs):
            # Very high price impact => large negative score even in best-case.
            return ProbeQuote(
                input_amount_atomic=kwargs["amount_atomic"],
                out_amount_atomic=1_000_000,
                price_impact_bps=2000.0,
                route_ok=True,
                raw={},
            )

    _candidates, summary = discover_candidates(StubBirdeye(), StubJupiter(), settings)

    assert calls["overview"] == 1
    assert calls["creation"] == 1
    assert calls["security_only"] == 0
    assert calls["holders_only"] == 0
    assert summary["candidates_accepted"] == 0
