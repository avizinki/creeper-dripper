from creeper_dripper.config import load_settings
from creeper_dripper.engine.scoring import passes_filters, rejection_reasons, score_candidate
from creeper_dripper.errors import REJECT_BAD_BUY_SELL_RATIO, REJECT_LOW_LIQUIDITY, REJECT_LOW_SCORE, REJECT_TOKEN_TOO_OLD
from creeper_dripper.models import TokenCandidate


def test_score_candidate_rewards_good_structure(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BIRDEYE_API_KEY", "x")
    monkeypatch.setenv("JUPITER_API_KEY", "x")
    monkeypatch.setenv("BS58_PRIVATE_KEY", "x")
    settings = load_settings()
    c = TokenCandidate(
        address="mint",
        symbol="TEST",
        decimals=6,
        liquidity_usd=200_000,
        exit_liquidity_usd=150_000,
        volume_24h_usd=500_000,
        buy_sell_ratio_1h=1.8,
        change_1h_pct=5,
        age_hours=12,
        top10_holder_percent=20,
        jupiter_buy_price_impact_bps=120,
        jupiter_sell_price_impact_bps=180,
        sell_route_available=True,   # explicitly mark route confirmed for filter pass
    )
    scored = score_candidate(c, settings)
    assert scored.discovery_score >= settings.min_discovery_score
    assert passes_filters(scored, settings)


def test_score_candidate_blocks_bad_sell_route(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BIRDEYE_API_KEY", "x")
    monkeypatch.setenv("JUPITER_API_KEY", "x")
    monkeypatch.setenv("BS58_PRIVATE_KEY", "x")
    settings = load_settings()
    c = TokenCandidate(
        address="mint",
        symbol="TEST",
        decimals=6,
        liquidity_usd=200_000,
        exit_liquidity_usd=150_000,
        volume_24h_usd=500_000,
        buy_sell_ratio_1h=1.8,
        change_1h_pct=5,
        age_hours=12,
        jupiter_buy_price_impact_bps=120,
        jupiter_sell_price_impact_bps=2_000,
    )
    scored = score_candidate(c, settings)
    assert not passes_filters(scored, settings)


def test_age_filter_uses_settings_max_token_age_hours_only(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BIRDEYE_API_KEY", "x")
    monkeypatch.setenv("JUPITER_API_KEY", "x")
    monkeypatch.setenv("BS58_PRIVATE_KEY", "x")
    # Age is a scoring factor (not a normal eligibility gate).
    monkeypatch.setenv("MAX_TOKEN_AGE_HOURS", "72")
    monkeypatch.setenv("MAX_TOKEN_AGE_HOURS_HARD", "20000")
    settings = load_settings()
    c = TokenCandidate(
        address="mint",
        symbol="TEST",
        decimals=6,
        liquidity_usd=200_000,
        exit_liquidity_usd=150_000,
        volume_24h_usd=500_000,
        buy_sell_ratio_1h=1.8,
        change_1h_pct=5,
        age_hours=60.0,
        top10_holder_percent=20,
        jupiter_buy_price_impact_bps=120,
        jupiter_sell_price_impact_bps=180,
        sell_route_available=True,
    )
    scored = score_candidate(c, settings)
    assert passes_filters(scored, settings), "age should not be a normal eligibility gate"


def test_age_is_soft_until_hard_cap(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BIRDEYE_API_KEY", "x")
    monkeypatch.setenv("JUPITER_API_KEY", "x")
    monkeypatch.setenv("BS58_PRIVATE_KEY", "x")
    monkeypatch.setenv("MAX_TOKEN_AGE_HOURS", "72")
    monkeypatch.setenv("MAX_TOKEN_AGE_HOURS_HARD", "720")
    settings = load_settings()
    # Older than MAX_TOKEN_AGE_HOURS (penalized in scoring) but younger than hard cap: should NOT be rejected.
    c = TokenCandidate(
        address="mint",
        symbol="TEST",
        decimals=6,
        liquidity_usd=500_000,
        exit_liquidity_usd=500_000,
        volume_24h_usd=1_000_000,
        buy_sell_ratio_1h=2.0,
        change_1h_pct=5,
        age_hours=200.0,
        top10_holder_percent=20,
        jupiter_buy_price_impact_bps=120,
        jupiter_sell_price_impact_bps=180,
        sell_route_available=True,
    )
    scored = score_candidate(c, settings)
    reasons = rejection_reasons(scored, settings, include_route_checks=True)
    assert REJECT_TOKEN_TOO_OLD not in reasons


def test_age_hard_cap_still_rejects(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BIRDEYE_API_KEY", "x")
    monkeypatch.setenv("JUPITER_API_KEY", "x")
    monkeypatch.setenv("BS58_PRIVATE_KEY", "x")
    monkeypatch.setenv("MAX_TOKEN_AGE_HOURS", "72")
    monkeypatch.setenv("MAX_TOKEN_AGE_HOURS_HARD", "720")
    settings = load_settings()
    c = TokenCandidate(
        address="mint",
        symbol="TEST",
        decimals=6,
        liquidity_usd=500_000,
        exit_liquidity_usd=500_000,
        volume_24h_usd=1_000_000,
        buy_sell_ratio_1h=2.0,
        change_1h_pct=5,
        age_hours=1000.0,
        top10_holder_percent=20,
        jupiter_buy_price_impact_bps=120,
        jupiter_sell_price_impact_bps=180,
        sell_route_available=True,
    )
    scored = score_candidate(c, settings)
    reasons = rejection_reasons(scored, settings, include_route_checks=True)
    assert REJECT_TOKEN_TOO_OLD in reasons


def test_seed_prefilter_does_not_reject_on_age(monkeypatch, tmp_path):
    # Seed prefilter should not reject by age; age is a scoring/ranking signal and probe truth is Jupiter.
    from creeper_dripper.engine.discovery import _seed_prefilter

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BIRDEYE_API_KEY", "x")
    monkeypatch.setenv("JUPITER_API_KEY", "x")
    monkeypatch.setenv("BS58_PRIVATE_KEY", "x")
    settings = load_settings()
    seed = {"address": "mint", "symbol": "OLD", "blockUnixTime": 0, "liquidityUsd": 1e9, "volume24hUSD": 1e9}
    assert _seed_prefilter(seed, settings) is None


def test_seed_prefilter_does_not_reject_on_low_liquidity(monkeypatch, tmp_path):
    # Seed prefilter should not hard-reject by liquidity; liquidity shaping is score + sizing + route truth.
    from creeper_dripper.engine.discovery import _seed_prefilter

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BIRDEYE_API_KEY", "x")
    monkeypatch.setenv("JUPITER_API_KEY", "x")
    monkeypatch.setenv("BS58_PRIVATE_KEY", "x")
    settings = load_settings()
    seed = {"address": "mint", "symbol": "LOWLIQ", "liquidityUsd": 1.0, "volume24hUSD": 1e9}
    assert _seed_prefilter(seed, settings) is None


def test_low_liquidity_is_soft_not_rejection(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BIRDEYE_API_KEY", "x")
    monkeypatch.setenv("JUPITER_API_KEY", "x")
    monkeypatch.setenv("BS58_PRIVATE_KEY", "x")
    monkeypatch.setenv("MIN_LIQUIDITY_USD", "100000")
    monkeypatch.setenv("MIN_VOLUME_24H_USD", "50000")
    settings = load_settings()
    c = TokenCandidate(
        address="mint",
        symbol="LOWLIQ",
        decimals=6,
        liquidity_usd=10_000,   # below MIN_LIQUIDITY_USD
        exit_liquidity_usd=200_000,
        volume_24h_usd=500_000,
        buy_sell_ratio_1h=2.0,
        change_1h_pct=5,
        age_hours=12.0,
        top10_holder_percent=20,
        jupiter_buy_price_impact_bps=120,
        jupiter_sell_price_impact_bps=180,
        sell_route_available=True,
    )
    scored = score_candidate(c, settings)
    reasons = rejection_reasons(scored, settings, include_route_checks=True)
    assert REJECT_LOW_LIQUIDITY not in reasons
    assert "spot_liquidity_low" in (scored.reasons or [])
    assert float((scored.raw or {}).get("liquidity_score_delta") or 0.0) < 0.0


def test_prefilter_does_not_reject_on_ratio_or_score_before_probe(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BIRDEYE_API_KEY", "x")
    monkeypatch.setenv("JUPITER_API_KEY", "x")
    monkeypatch.setenv("BS58_PRIVATE_KEY", "x")
    settings = load_settings()

    # Candidate is strong on liquidity/volume but weak on buy/sell ratio + score.
    # We want it to reach the Jupiter probe stage (prefilter), then be rejected post-probe.
    c = TokenCandidate(
        address="mint",
        symbol="TEST",
        decimals=6,
        liquidity_usd=max(settings.min_liquidity_usd * 2, 200_000),
        exit_liquidity_usd=max(settings.min_exit_liquidity_usd * 2, 150_000),
        volume_24h_usd=max(settings.min_volume_24h_usd * 2, 500_000),
        buy_sell_ratio_1h=max(0.0, settings.min_buy_sell_ratio - 0.5),
        age_hours=min(settings.max_token_age_hours - 1, 12),
        discovery_score=max(0.0, settings.min_discovery_score - 10),
        sell_route_available=True,
        jupiter_buy_price_impact_bps=min(settings.max_acceptable_price_impact_bps - 1, 120),
        jupiter_sell_price_impact_bps=min(settings.max_acceptable_price_impact_bps - 1, 180),
    )

    pre = rejection_reasons(c, settings, include_route_checks=False)
    assert REJECT_BAD_BUY_SELL_RATIO not in pre
    assert REJECT_LOW_SCORE not in pre

    post = rejection_reasons(c, settings, include_route_checks=True)
    assert REJECT_BAD_BUY_SELL_RATIO in post
    assert REJECT_LOW_SCORE in post
