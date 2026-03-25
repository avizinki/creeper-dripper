from creeper_dripper.config import load_settings
from creeper_dripper.engine.scoring import passes_filters, rejection_reasons, score_candidate
from creeper_dripper.errors import REJECT_BAD_BUY_SELL_RATIO, REJECT_LOW_SCORE
from creeper_dripper.models import TokenCandidate


def test_score_candidate_rewards_good_structure(monkeypatch):
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


def test_score_candidate_blocks_bad_sell_route(monkeypatch):
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


def test_age_filter_uses_settings_max_token_age_hours_only(monkeypatch):
    monkeypatch.setenv("BIRDEYE_API_KEY", "x")
    monkeypatch.setenv("JUPITER_API_KEY", "x")
    monkeypatch.setenv("BS58_PRIVATE_KEY", "x")
    # Make settings gate permissive so the only failing dimension is age.
    monkeypatch.setenv("MAX_TOKEN_AGE_HOURS", "72")
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
    assert passes_filters(scored, settings), "age should only be gated by MAX_TOKEN_AGE_HOURS"


def test_prefilter_does_not_reject_on_ratio_or_score_before_probe(monkeypatch):
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
