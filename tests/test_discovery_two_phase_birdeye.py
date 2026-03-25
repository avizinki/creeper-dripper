from __future__ import annotations

import pytest

from creeper_dripper.config import load_settings
from creeper_dripper.engine.discovery import discover_candidates
from creeper_dripper.models import ProbeQuote, TokenCandidate


def test_heavy_endpoints_not_called_for_prefilter_rejects(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BIRDEYE_API_KEY", "x")
    monkeypatch.setenv("JUPITER_API_KEY", "x")
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("STATE_PATH", str(tmp_path / "runtime" / "state.json"))
    monkeypatch.setenv("JOURNAL_PATH", str(tmp_path / "runtime" / "journal.jsonl"))
    monkeypatch.setenv("DISCOVERY_INTERVAL_SECONDS", "30")
    monkeypatch.setenv("MAX_ACTIVE_CANDIDATES", "7")
    monkeypatch.setenv("CANDIDATE_CACHE_TTL_SECONDS", "20")
    monkeypatch.setenv("ROUTE_CHECK_CACHE_TTL_SECONDS", "15")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    monkeypatch.setenv("DRY_RUN", "true")

    settings = load_settings()
    # Make cheap prefilter strict enough to reject one seed by 24h volume (this is enforced in prefilter).
    settings.min_volume_24h_usd = 200_000
    # Force conditional holder enrichment to trigger (worst-case top10 penalty should drop below threshold).
    settings.min_discovery_score = 60

    calls = {"security_only": 0, "holders_only": 0, "creation": 0, "overview": 0}

    class StubBirdeye:
        def trending_tokens(self, limit=25):
            return [{"address": "mint_low", "symbol": "LOW"}, {"address": "mint_ok", "symbol": "OK"}]

        def new_listings(self, limit=10):
            return []

        def build_candidate_light(self, seed: dict):
            calls["overview"] += 1
            addr = str(seed.get("address"))
            # low liquidity should be rejected by cheap prefilter.
            liq = 10_000 if addr == "mint_low" else 250_000
            vol = 1_000.0 if addr == "mint_low" else 500_000.0
            return TokenCandidate(
                address=addr,
                symbol=str(seed.get("symbol") or "?"),
                decimals=6,
                liquidity_usd=float(liq),
                volume_24h_usd=float(vol),
                exit_liquidity_available=False,
                exit_liquidity_reason="birdeye_exit_liquidity_skipped_unsupported_chain",
                birdeye_exit_liquidity_supported=False,
                raw={"seed": seed, "overview": {"liquidity": liq}},
            )

        def enrich_candidate_heavy(self, candidate: TokenCandidate):
            calls["creation"] += 1
            # no-op enrichment
            return candidate

        def enrich_candidate_security_only(self, candidate: TokenCandidate):
            calls["security_only"] += 1
            # Safe defaults: treat as non-mutable / non-freezable.
            candidate.security_mint_mutable = False
            candidate.security_freezable = False
            return candidate

        def enrich_candidate_holders_only(self, candidate: TokenCandidate):
            calls["holders_only"] += 1
            # Spread holders (top10 <= 25) so final score can pass.
            candidate.top10_holder_percent = 0.0
            return candidate

    class StubJupiter:
        def probe_quote(self, **kwargs):
            return ProbeQuote(
                input_amount_atomic=kwargs["amount_atomic"],
                out_amount_atomic=1_000_000,
                price_impact_bps=50.0,
                route_ok=True,
                raw={},
            )

    candidates, summary = discover_candidates(StubBirdeye(), StubJupiter(), settings)
    # We should only enrich survivors (mint_ok), never mint_low.
    assert calls["overview"] == 2
    assert calls["security_only"] == 1
    assert calls["holders_only"] == 1
    assert calls["creation"] == 1
    assert any(c.address == "mint_ok" for c in candidates + []) or summary["candidates_built"] >= 1


def test_overview_limit_caps_token_overview_stage(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BIRDEYE_API_KEY", "x")
    monkeypatch.setenv("JUPITER_API_KEY", "x")
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("STATE_PATH", str(tmp_path / "runtime" / "state.json"))
    monkeypatch.setenv("JOURNAL_PATH", str(tmp_path / "runtime" / "journal.jsonl"))
    monkeypatch.setenv("DISCOVERY_INTERVAL_SECONDS", "30")
    monkeypatch.setenv("MAX_ACTIVE_CANDIDATES", "5")
    monkeypatch.setenv("DISCOVERY_OVERVIEW_LIMIT", "2")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("PREFILTER_MIN_RECENT_VOLUME_USD", "1")
    monkeypatch.delenv("CANDIDATE_CACHE_TTL_SECONDS", raising=False)
    monkeypatch.delenv("ROUTE_CHECK_CACHE_TTL_SECONDS", raising=False)

    settings = load_settings()
    settings.min_volume_24h_usd = 1

    calls = {"overview": 0, "heavy": 0}

    class StubBirdeye:
        def trending_tokens(self, limit=25):
            # All seeds have volume, so they pass `_seed_prefilter`.
            return [{"address": f"m{i}", "symbol": f"T{i}", "volume24hUSD": 50_000} for i in range(5)]

        def new_listings(self, limit=10):
            return []

        def build_candidate_light(self, seed: dict):
            calls["overview"] += 1
            i = int(seed["address"][1:])
            return TokenCandidate(
                address=seed["address"],
                symbol=seed["symbol"],
                decimals=6,
                liquidity_usd=200_000,
                volume_24h_usd=500_000,
                exit_liquidity_available=False,
                exit_liquidity_reason="birdeye_exit_liquidity_skipped_unsupported_chain",
                birdeye_exit_liquidity_supported=False,
                raw={"seed": seed, "overview": {"liquidity": 200_000}},
            )

        def enrich_candidate_heavy(self, candidate: TokenCandidate):
            calls["heavy"] += 1
            return candidate

        def enrich_candidate_security_only(self, candidate: TokenCandidate):
            candidate.security_mint_mutable = False
            candidate.security_freezable = False
            return candidate

        def enrich_candidate_holders_only(self, candidate: TokenCandidate):
            candidate.top10_holder_percent = 0.0
            return candidate

    class StubJupiter:
        def probe_quote(self, **kwargs):
            return ProbeQuote(
                input_amount_atomic=kwargs["amount_atomic"],
                out_amount_atomic=1_000_000,
                price_impact_bps=50.0,
                route_ok=True,
                raw={},
            )

    discover_candidates(StubBirdeye(), StubJupiter(), settings)
    assert calls["overview"] == 2
    # All overview-built candidates survive in this stub (no further filtering),
    # so heavy enrichment should run for those 2.
    assert calls["heavy"] == 2

