from __future__ import annotations

from creeper_dripper.config import Settings
from creeper_dripper.models import TokenCandidate
from creeper_dripper.utils import clamp


def score_candidate(candidate: TokenCandidate, settings: Settings) -> TokenCandidate:
    score = 0.0
    reasons: list[str] = []

    liq = candidate.liquidity_usd or 0.0
    exit_liq = candidate.exit_liquidity_usd or 0.0
    vol = candidate.volume_24h_usd or 0.0
    ratio = candidate.buy_sell_ratio_1h or 0.0
    change_1h = candidate.change_1h_pct or 0.0
    top10 = candidate.top10_holder_percent
    age_h = candidate.age_hours

    if liq >= settings.min_liquidity_usd:
        score += clamp((liq / settings.min_liquidity_usd) * 12.0, 0.0, 18.0)
        reasons.append("spot_liquidity")
    if exit_liq >= settings.min_exit_liquidity_usd:
        score += clamp((exit_liq / settings.min_exit_liquidity_usd) * 14.0, 0.0, 20.0)
        reasons.append("exit_liquidity")
    if vol >= settings.min_volume_24h_usd:
        score += clamp((vol / settings.min_volume_24h_usd) * 10.0, 0.0, 14.0)
        reasons.append("volume")
    if ratio >= settings.min_buy_sell_ratio:
        score += clamp((ratio - settings.min_buy_sell_ratio + 1.0) * 8.0, 0.0, 14.0)
        reasons.append("flow")
    if -3.0 <= change_1h <= 18.0:
        score += 10.0
        reasons.append("not_overextended")
    elif change_1h > 18.0:
        score -= clamp((change_1h - 18.0) * 0.8, 0.0, 20.0)
        reasons.append("overextended")
    else:
        score -= clamp(abs(change_1h) * 0.5, 0.0, 10.0)

    if age_h is not None:
        if age_h <= settings.max_token_age_hours:
            score += 8.0
            reasons.append("fresh")
        else:
            score -= 6.0
            reasons.append("stale")

    if top10 is not None:
        if top10 <= 25.0:
            score += 8.0
            reasons.append("holder_spread")
        elif top10 >= 55.0:
            score -= 12.0
            reasons.append("holder_concentrated")

    if candidate.security_mint_mutable:
        score -= 20.0
        reasons.append("mutable_mint")
    if candidate.security_freezable:
        score -= 14.0
        reasons.append("freezable")

    if candidate.jupiter_buy_price_impact_bps is not None:
        if candidate.jupiter_buy_price_impact_bps <= settings.max_acceptable_price_impact_bps * 0.5:
            score += 10.0
            reasons.append("buy_route_quality")
        elif candidate.jupiter_buy_price_impact_bps > settings.max_acceptable_price_impact_bps:
            score -= 18.0
            reasons.append("buy_route_bad")

    if candidate.jupiter_sell_price_impact_bps is not None:
        if candidate.jupiter_sell_price_impact_bps <= settings.max_acceptable_price_impact_bps * 0.65:
            score += 8.0
            reasons.append("sell_route_quality")
        elif candidate.jupiter_sell_price_impact_bps > settings.max_acceptable_price_impact_bps:
            score -= 24.0
            reasons.append("sell_route_bad")

    candidate.discovery_score = round(clamp(score, 0.0, 100.0), 2)
    candidate.reasons = reasons
    return candidate


def passes_filters(candidate: TokenCandidate, settings: Settings) -> bool:
    if not candidate.address:
        return False
    if (candidate.liquidity_usd or 0.0) < settings.min_liquidity_usd:
        return False
    if (candidate.exit_liquidity_usd or 0.0) < settings.min_exit_liquidity_usd:
        return False
    if (candidate.volume_24h_usd or 0.0) < settings.min_volume_24h_usd:
        return False
    if (candidate.buy_sell_ratio_1h or 0.0) < settings.min_buy_sell_ratio:
        return False
    if settings.block_mutable_mint and candidate.security_mint_mutable:
        return False
    if settings.block_freezable and candidate.security_freezable:
        return False
    if candidate.discovery_score < settings.min_discovery_score:
        return False
    if settings.require_jup_sell_route and candidate.jupiter_sell_price_impact_bps is None:
        return False
    if candidate.jupiter_sell_price_impact_bps is not None and candidate.jupiter_sell_price_impact_bps > settings.max_acceptable_price_impact_bps:
        return False
    if candidate.jupiter_buy_price_impact_bps is not None and candidate.jupiter_buy_price_impact_bps > settings.max_acceptable_price_impact_bps:
        return False
    if candidate.age_hours is not None and candidate.age_hours > settings.max_token_age_hours:
        return False
    return True
