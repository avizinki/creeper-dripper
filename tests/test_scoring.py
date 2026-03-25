from creeper_dripper.config import load_settings
from creeper_dripper.engine.scoring import passes_filters, score_candidate
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
