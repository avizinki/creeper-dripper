from __future__ import annotations

import pytest

from creeper_dripper.config import load_settings
from creeper_dripper.engine.discovery import discover_candidates
from creeper_dripper.models import ProbeQuote, TokenCandidate


GOOD_MINT = "B87LXUgp7cdAMv8k2cZwwRWhCKFwDMhXkuRtudJfXCxp"
BAD_MINT = "9wZjm1msCtiDxrkBipcjPnEPcwzqycbQpbsCydq68f2P"


def _env(monkeypatch: pytest.MonkeyPatch, tmp_path):
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


def _settings(monkeypatch: pytest.MonkeyPatch, tmp_path):
    _env(monkeypatch, tmp_path)
    s = load_settings()
    s.min_discovery_score = 0
    s.min_buy_sell_ratio = 0.0
    s.block_mutable_mint = False
    s.block_freezable = False
    s.require_jup_sell_route = False
    s.min_volume_24h_usd = 1
    return s


def _norm_keys():
    return {
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
        "route_fragile",
        "rejection_reason",
        "source_stage",
        "accepted_or_rejected",
        "probe_size_bucket_buy",
        "probe_size_bucket_sell",
    }


def test_seed_rejected_candidate_emits_shallow_normalized_data(monkeypatch: pytest.MonkeyPatch, tmp_path):
    s = _settings(monkeypatch, tmp_path)
    s.prefilter_min_recent_volume_usd = 10_000

    class B:
        def trending_tokens(self, limit=25):
            return [
                {"address": BAD_MINT, "symbol": "LOW", "volume24hUSD": 1},
                {"address": GOOD_MINT, "symbol": "OK", "volume24hUSD": 20_000},
            ]

        def new_listings(self, limit=10):
            return []

        def build_candidate_light(self, seed: dict):
            return TokenCandidate(address=seed["address"], symbol=seed["symbol"], decimals=6, liquidity_usd=200_000, volume_24h_usd=500_000, buy_sell_ratio_1h=1.2, age_hours=2.0)

        def enrich_candidate_heavy(self, c: TokenCandidate):
            return c

        def enrich_candidate_security_only(self, c: TokenCandidate):
            c.security_mint_mutable = False
            c.security_freezable = False
            return c

        def enrich_candidate_holders_only(self, c: TokenCandidate):
            c.top10_holder_percent = 0.0
            return c

    class J:
        def probe_quote(self, **kwargs):
            return ProbeQuote(input_amount_atomic=kwargs["amount_atomic"], out_amount_atomic=1_000_000, price_impact_bps=10.0, route_ok=True, raw={})

    _, summary = discover_candidates(B(), J(), s)
    ev = [e for e in summary["events"] if e["event_type"] == "candidate_rejected" and e["reason_code"] == "reject_low_volume"]
    assert ev, "expected reject_low_volume event"
    md = ev[0]["metadata"]
    for k in _norm_keys():
        assert k in md
    assert md["accepted_or_rejected"] == "rejected"
    assert md["source_stage"] == "seed_prefilter"
    assert md["rejection_reason"] == "reject_low_volume"


def test_accepted_candidate_emits_shallow_normalized_data(monkeypatch: pytest.MonkeyPatch, tmp_path):
    s = _settings(monkeypatch, tmp_path)

    class B:
        def trending_tokens(self, limit=25):
            return [{"address": GOOD_MINT, "symbol": "OK", "volume24hUSD": 50_000}]

        def new_listings(self, limit=10):
            return []

        def build_candidate_light(self, seed: dict):
            return TokenCandidate(
                address=seed["address"],
                symbol=seed["symbol"],
                decimals=6,
                liquidity_usd=250_000,
                exit_liquidity_available=False,
                exit_liquidity_reason="birdeye_exit_liquidity_skipped_unsupported_chain",
                birdeye_exit_liquidity_supported=False,
                volume_24h_usd=500_000,
                buy_sell_ratio_1h=1.3,
                age_hours=1.2,
            )

        def enrich_candidate_heavy(self, c: TokenCandidate):
            return c

        def enrich_candidate_security_only(self, c: TokenCandidate):
            c.security_mint_mutable = False
            c.security_freezable = False
            return c

        def enrich_candidate_holders_only(self, c: TokenCandidate):
            c.top10_holder_percent = 0.0
            return c

    class J:
        def probe_quote(self, **kwargs):
            return ProbeQuote(input_amount_atomic=kwargs["amount_atomic"], out_amount_atomic=1_000_000, price_impact_bps=12.0, route_ok=True, raw={})

    _, summary = discover_candidates(B(), J(), s)
    ev = [e for e in summary["events"] if e["event_type"] == "candidate_accepted"]
    assert ev
    md = ev[0]["metadata"]
    for k in _norm_keys():
        assert k in md
    assert md["accepted_or_rejected"] == "accepted"
    assert md["source_stage"] == "post_probe"
    assert md["route_exists"] is True


def test_route_probe_rejection_emits_route_fields(monkeypatch: pytest.MonkeyPatch, tmp_path):
    s = _settings(monkeypatch, tmp_path)

    class B:
        def trending_tokens(self, limit=25):
            return [{"address": BAD_MINT, "symbol": "BAD", "volume24hUSD": 50_000}]

        def new_listings(self, limit=10):
            return []

        def build_candidate_light(self, seed: dict):
            return TokenCandidate(
                address=seed["address"],
                symbol=seed["symbol"],
                decimals=6,
                liquidity_usd=200_000,
                exit_liquidity_available=False,
                exit_liquidity_reason="birdeye_exit_liquidity_skipped_unsupported_chain",
                birdeye_exit_liquidity_supported=False,
                volume_24h_usd=500_000,
                buy_sell_ratio_1h=1.2,
                age_hours=2.0,
            )

        def enrich_candidate_heavy(self, c: TokenCandidate):
            return c

        def enrich_candidate_security_only(self, c: TokenCandidate):
            c.security_mint_mutable = False
            c.security_freezable = False
            return c

        def enrich_candidate_holders_only(self, c: TokenCandidate):
            c.top10_holder_percent = 0.0
            return c

    class J:
        def probe_quote(self, **kwargs):
            # Fail buy probe => no route rejection.
            return ProbeQuote(input_amount_atomic=kwargs["amount_atomic"], out_amount_atomic=None, price_impact_bps=None, route_ok=False, raw={})

    _, summary = discover_candidates(B(), J(), s)
    ev = [e for e in summary["events"] if e["event_type"] == "candidate_rejected" and e["reason_code"] == "reject_no_buy_route"]
    assert ev
    md = ev[0]["metadata"]
    assert md["no_route"] is True
    assert md["route_exists"] is False
    assert md["probe_size_bucket_buy"] is not None
    assert md["source_stage"] == "probe_buy"


def test_field_names_consistent_between_accepted_and_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path):
    s = _settings(monkeypatch, tmp_path)

    class B:
        def trending_tokens(self, limit=25):
            return [
                {"address": GOOD_MINT, "symbol": "OK", "volume24hUSD": 50_000},
                {"address": BAD_MINT, "symbol": "BAD", "volume24hUSD": 50_000},
            ]

        def new_listings(self, limit=10):
            return []

        def build_candidate_light(self, seed: dict):
            return TokenCandidate(
                address=seed["address"],
                symbol=seed["symbol"],
                decimals=6,
                liquidity_usd=200_000,
                exit_liquidity_available=False,
                exit_liquidity_reason="birdeye_exit_liquidity_skipped_unsupported_chain",
                birdeye_exit_liquidity_supported=False,
                volume_24h_usd=500_000,
                buy_sell_ratio_1h=1.2,
                age_hours=2.0,
            )

        def enrich_candidate_heavy(self, c: TokenCandidate):
            return c

        def enrich_candidate_security_only(self, c: TokenCandidate):
            c.security_mint_mutable = False
            c.security_freezable = False
            return c

        def enrich_candidate_holders_only(self, c: TokenCandidate):
            c.top10_holder_percent = 0.0
            return c

    class J:
        def probe_quote(self, **kwargs):
            # GOOD gets route; BAD fails on buy.
            target = kwargs.get("output_mint")
            if target == BAD_MINT:
                return ProbeQuote(input_amount_atomic=kwargs["amount_atomic"], out_amount_atomic=None, price_impact_bps=None, route_ok=False, raw={})
            return ProbeQuote(input_amount_atomic=kwargs["amount_atomic"], out_amount_atomic=1_000_000, price_impact_bps=11.0, route_ok=True, raw={})

    _, summary = discover_candidates(B(), J(), s)
    accepted = next(e for e in summary["events"] if e["event_type"] == "candidate_accepted")
    rejected = next(e for e in summary["events"] if e["event_type"] == "candidate_rejected" and e["reason_code"] == "reject_no_buy_route")
    a_keys = set(accepted["metadata"].keys())
    r_keys = set(rejected["metadata"].keys())
    for k in _norm_keys():
        assert k in a_keys
        assert k in r_keys

