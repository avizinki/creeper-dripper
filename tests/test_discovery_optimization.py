from __future__ import annotations

from creeper_dripper.cache import TTLCache
from creeper_dripper.config import load_settings
from creeper_dripper.engine.discovery import discover_candidates
from creeper_dripper.engine.trader import CreeperDripper
from creeper_dripper.models import PortfolioState, ProbeQuote, TokenCandidate
from creeper_dripper.storage.state import new_portfolio


def _settings(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BIRDEYE_API_KEY", "x")
    monkeypatch.setenv("JUPITER_API_KEY", "x")
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("JOURNAL_PATH", str(tmp_path / "journal.jsonl"))
    monkeypatch.setenv("DISCOVERY_INTERVAL_SECONDS", "30")
    monkeypatch.setenv("MAX_ACTIVE_CANDIDATES", "2")
    monkeypatch.setenv("PREFILTER_MIN_LIQUIDITY_USD", "50000")
    monkeypatch.setenv("PREFILTER_MIN_RECENT_VOLUME_USD", "30000")
    monkeypatch.setenv("PREFILTER_MAX_AGE_HOURS", "48")
    monkeypatch.setenv("MIN_DISCOVERY_SCORE", "0")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    monkeypatch.delenv("SOLANA_KEYPAIR_PATH", raising=False)
    monkeypatch.delenv("BS58_PRIVATE_KEY", raising=False)
    return load_settings()


def test_seed_prefilter_skips_expensive_candidate_build(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    build_calls = {"n": 0}

    class StubBirdeye:
        def trending_tokens(self, limit=25):
            return [
                {"address": "weak", "symbol": "WEAK", "liquidityUsd": 1000, "volume24hUSD": 2000},
                {"address": "strong", "symbol": "STRONG", "liquidityUsd": 200000, "volume24hUSD": 500000},
            ]

        def new_listings(self, limit=10):
            return []

        def build_candidate(self, seed):
            build_calls["n"] += 1
            return TokenCandidate(
                address=seed["address"],
                symbol=seed["symbol"],
                decimals=6,
                liquidity_usd=150000,
                    exit_liquidity_available=False,
                    exit_liquidity_reason="birdeye_exit_liquidity_unsupported_chain",
                volume_24h_usd=500000,
                buy_sell_ratio_1h=1.5,
                age_hours=10,
            )

    class StubJupiter:
        def probe_quote(self, **kwargs):
            return ProbeQuote(
                input_amount_atomic=kwargs["amount_atomic"],
                out_amount_atomic=100000,
                price_impact_bps=50.0,
                route_ok=True,
                raw={},
            )

    candidates, summary = discover_candidates(StubBirdeye(), StubJupiter(), settings)
    assert build_calls["n"] == 1
    assert summary["seed_prefiltered_out"] >= 1
    assert any(c.symbol == "STRONG" for c in candidates)


def test_topn_cap_limits_route_checks(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    settings.max_active_candidates = 2

    class StubBirdeye:
        def trending_tokens(self, limit=25):
            return [{"address": f"m{i}", "symbol": f"T{i}"} for i in range(5)]

        def new_listings(self, limit=10):
            return []

        def build_candidate(self, seed):
            idx = int(seed["address"][1:])
            return TokenCandidate(
                address=seed["address"],
                symbol=seed["symbol"],
                decimals=6,
                liquidity_usd=200000 - (idx * 1000),
                    exit_liquidity_available=False,
                    exit_liquidity_reason="birdeye_exit_liquidity_unsupported_chain",
                volume_24h_usd=600000,
                buy_sell_ratio_1h=1.5,
                age_hours=5,
            )

    calls = {"n": 0}

    class StubJupiter:
        def probe_quote(self, **kwargs):
            calls["n"] += 1
            return ProbeQuote(
                input_amount_atomic=kwargs["amount_atomic"],
                out_amount_atomic=100000 + calls["n"],
                price_impact_bps=25.0,
                route_ok=True,
                raw={},
            )

    _candidates, summary = discover_candidates(StubBirdeye(), StubJupiter(), settings)
    assert summary["topn_candidates"] == 2
    assert summary["route_checked_candidates"] == 4
    assert calls["n"] == 4
    assert summary["jupiter_buy_probe_calls"] == 2
    assert summary["jupiter_sell_probe_calls"] == 2


def test_ttl_cache_hits_within_ttl():
    cache = TTLCache[int](ttl_seconds=10)
    assert cache.get("k") is None
    cache.set("k", 7)
    assert cache.get("k") == 7
    assert cache.stats.hits == 1
    assert cache.stats.misses == 1


def test_discovery_cadence_reuses_recent_results(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    portfolio: PortfolioState = new_portfolio(5.0)

    class DummyBirdeye:
        pass

    class DummyExecutor:
        jupiter = object()

        def wallet_token_balance_atomic(self, _mint):
            return None

        def transaction_status(self, _sig):
            return None

    engine = CreeperDripper(settings, DummyBirdeye(), DummyExecutor(), portfolio)
    discover_calls = {"n": 0}

    def fake_discover(_b, _j, _s, **_kwargs):
        discover_calls["n"] += 1
        return [], {
            "seeds_total": 1,
            "candidates_built": 1,
            "candidates_accepted": 0,
            "candidates_rejected_total": 1,
            "rejection_counts": {"reject_low_liquidity": 1},
        }

    monkeypatch.setattr("creeper_dripper.engine.trader.discover_candidates", fake_discover)
    first = engine._discover_with_cadence()
    second = engine._discover_with_cadence()
    assert discover_calls["n"] == 1
    assert first[1]["discovery_cached"] is False
    assert second[1]["discovery_cached"] is True


def test_discovery_shared_cache_persists_across_calls(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    build_calls = {"n": 0}

    class StubBirdeye:
        def trending_tokens(self, limit=25):
            return [{"address": "stable", "symbol": "STABLE", "liquidityUsd": 200000, "volume24hUSD": 500000}]

        def new_listings(self, limit=10):
            return []

        def build_candidate(self, seed):
            build_calls["n"] += 1
            return TokenCandidate(
                address=seed["address"],
                symbol=seed["symbol"],
                decimals=6,
                liquidity_usd=200000,
                volume_24h_usd=500000,
                buy_sell_ratio_1h=1.5,
                age_hours=5,
            )

    class StubJupiter:
        def probe_quote(self, **kwargs):
            return ProbeQuote(
                input_amount_atomic=kwargs["amount_atomic"],
                out_amount_atomic=120000,
                price_impact_bps=20.0,
                route_ok=True,
                raw={},
            )

    candidate_cache = TTLCache[TokenCandidate](ttl_seconds=60)
    route_cache = TTLCache[ProbeQuote](ttl_seconds=60)
    _, first_summary = discover_candidates(
        StubBirdeye(),
        StubJupiter(),
        settings,
        candidate_cache=candidate_cache,
        route_cache=route_cache,
    )
    _, second_summary = discover_candidates(
        StubBirdeye(),
        StubJupiter(),
        settings,
        candidate_cache=candidate_cache,
        route_cache=route_cache,
    )
    assert build_calls["n"] == 1
    assert first_summary["candidate_cache_hits"] == 0
    assert second_summary["candidate_cache_hits"] > 0


def test_discovery_reuse_does_not_block_held_position_marking(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    settings.time_stop_minutes = 999999
    portfolio: PortfolioState = new_portfolio(5.0)

    from creeper_dripper.models import PositionState, TakeProfitStep

    portfolio.open_positions["mintH"] = PositionState(
        token_mint="mintH",
        symbol="HELD",
        decimals=6,
        status="OPEN",
        opened_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        entry_price_usd=1.0,
        avg_entry_price_usd=1.0,
        entry_sol=0.1,
        remaining_qty_atomic=1000,
        remaining_qty_ui=0.001,
        peak_price_usd=1.0,
        last_price_usd=1.0,
        take_profit_steps=[TakeProfitStep(trigger_pct=25.0, fraction=0.2)],
    )

    class DummyExecutor:
        jupiter = object()

        def wallet_token_balance_atomic(self, _mint):
            return None

        def transaction_status(self, _sig):
            return None

    class StubBirdeye:
        def build_candidate(self, seed):
            return TokenCandidate(
                address=seed["address"],
                symbol=seed["symbol"],
                decimals=6,
                price_usd=1.1,
                liquidity_usd=200000,
                volume_24h_usd=500000,
                buy_sell_ratio_1h=1.2,
                age_hours=5,
            )

    engine = CreeperDripper(settings, StubBirdeye(), DummyExecutor(), portfolio)
    engine._last_discovery_at = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    engine._last_discovery_candidates = []
    engine._last_discovery_summary = {
        "seeds_total": 0,
        "candidates_built": 0,
        "candidates_accepted": 0,
        "candidates_rejected_total": 0,
        "rejection_counts": {},
    }
    decisions = []
    engine._mark_positions([], decisions, "2026-01-01T00:00:01+00:00")
    assert engine.portfolio.open_positions["mintH"].last_price_usd == 1.1

