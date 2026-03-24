from __future__ import annotations

from creeper_dripper.config import Settings
from creeper_dripper.errors import (
    REJECT_BAD_BUY_SELL_RATIO,
    REJECT_FREEZABLE,
    REJECT_HIGH_BUY_IMPACT,
    REJECT_HIGH_SELL_IMPACT,
    REJECT_LOW_EXIT_LIQUIDITY,
    REJECT_LOW_LIQUIDITY,
    REJECT_LOW_SCORE,
    REJECT_LOW_VOLUME,
    REJECT_MINTABLE,
    REJECT_NO_SELL_ROUTE,
    REJECT_TOKEN_TOO_OLD,
)
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
    return len(rejection_reasons(candidate, settings)) == 0


def rejection_reasons(candidate: TokenCandidate, settings: Settings) -> list[str]:
    reasons: list[str] = []
    if not candidate.address:
        reasons.append("reject_missing_address")
    if (candidate.liquidity_usd or 0.0) < settings.min_liquidity_usd:
        reasons.append(REJECT_LOW_LIQUIDITY)
    if (candidate.exit_liquidity_usd or 0.0) < settings.min_exit_liquidity_usd:
        reasons.append(REJECT_LOW_EXIT_LIQUIDITY)
    if (candidate.volume_24h_usd or 0.0) < settings.min_volume_24h_usd:
        reasons.append(REJECT_LOW_VOLUME)
    if (candidate.buy_sell_ratio_1h or 0.0) < settings.min_buy_sell_ratio:
        reasons.append(REJECT_BAD_BUY_SELL_RATIO)
    if settings.block_mutable_mint and candidate.security_mint_mutable:
        reasons.append(REJECT_MINTABLE)
    if settings.block_freezable and candidate.security_freezable:
        reasons.append(REJECT_FREEZABLE)
    if candidate.discovery_score < settings.min_discovery_score:
        reasons.append(REJECT_LOW_SCORE)
    if settings.require_jup_sell_route and candidate.jupiter_sell_price_impact_bps is None:
        reasons.append(REJECT_NO_SELL_ROUTE)
    if candidate.jupiter_sell_price_impact_bps is not None and candidate.jupiter_sell_price_impact_bps > settings.max_acceptable_price_impact_bps:
        reasons.append(REJECT_HIGH_SELL_IMPACT)
    if candidate.jupiter_buy_price_impact_bps is not None and candidate.jupiter_buy_price_impact_bps > settings.max_acceptable_price_impact_bps:
        reasons.append(REJECT_HIGH_BUY_IMPACT)
    if candidate.age_hours is not None and candidate.age_hours > settings.max_token_age_hours:
        reasons.append(REJECT_TOKEN_TOO_OLD)
    return reasons
