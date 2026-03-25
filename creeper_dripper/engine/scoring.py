from __future__ import annotations

from creeper_dripper.config import Settings
from creeper_dripper.errors import (
    BIRDEYE_EXIT_LIQUIDITY_UNSUPPORTED_CHAIN,
    REJECT_BAD_BUY_SELL_RATIO,
    REJECT_FREEZABLE,
    REJECT_HIGH_BUY_IMPACT,
    REJECT_HIGH_SELL_IMPACT,
    REJECT_LOW_EXIT_LIQUIDITY,
    REJECT_LOW_LIQUIDITY,
    REJECT_LOW_SCORE,
    REJECT_LOW_VOLUME,
    REJECT_MINTABLE_MEMECOIN,
    REJECT_NO_SELL_ROUTE,
    REJECT_TOKEN_TOO_OLD,
)
from creeper_dripper.models import TokenCandidate
from creeper_dripper.utils import clamp

MAX_DISCOVERY_SELL_IMPACT_BPS = 200.0


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

    liq_min = float(settings.min_liquidity_usd or 0.0)
    liquidity_score_delta = 0.0
    if liq_min > 0.0:
        if liq >= liq_min:
            liquidity_score_delta = clamp((liq / liq_min) * 12.0, 0.0, 18.0)
            score += liquidity_score_delta
            reasons.append("spot_liquidity")
        else:
            # Liquidity is important but not a blunt early gate; penalize instead.
            liquidity_score_delta = -clamp((1.0 - (liq / liq_min)) * 12.0, 0.0, 12.0)
            score += liquidity_score_delta
            reasons.append("spot_liquidity_low")
    if candidate.raw is not None:
        candidate.raw["liquidity_score_delta"] = round(float(liquidity_score_delta), 4)
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

    # Age influences score/ranking only (not normal eligibility).
    # Bands are intentionally coarse to avoid overfitting and to keep Jupiter probe truth as the real gate.
    if age_h is not None:
        if age_h <= 24.0:
            score += 10.0
            reasons.append("age_very_fresh")
        elif age_h <= 72.0:
            score += 6.0
            reasons.append("age_fresh")
        elif age_h <= 168.0:
            score += 2.0
            reasons.append("age_recent")
        elif age_h <= 720.0:
            score -= 2.0
            reasons.append("age_older")
        else:
            score -= 6.0
            reasons.append("age_old")

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
    return len(rejection_reasons(candidate, settings, include_route_checks=True)) == 0


def rejection_reasons(candidate: TokenCandidate, settings: Settings, *, include_route_checks: bool = True) -> list[str]:
    reasons: list[str] = []
    if not candidate.address:
        reasons.append("reject_missing_address")
    if settings.require_birdeye_exit_liquidity and not candidate.exit_liquidity_available:
        reasons.append(BIRDEYE_EXIT_LIQUIDITY_UNSUPPORTED_CHAIN)
    if candidate.exit_liquidity_available and (candidate.exit_liquidity_usd or 0.0) < settings.min_exit_liquidity_usd:
        reasons.append(REJECT_LOW_EXIT_LIQUIDITY)
    if (candidate.volume_24h_usd or 0.0) < settings.min_volume_24h_usd:
        reasons.append(REJECT_LOW_VOLUME)
    if settings.block_mutable_mint and candidate.security_mint_mutable and _is_memecoin_universe_candidate(candidate):
        reasons.append(REJECT_MINTABLE_MEMECOIN)
    if settings.block_freezable and candidate.security_freezable:
        reasons.append(REJECT_FREEZABLE)
    if include_route_checks:
        # Keep prefilter permissive so more candidates reach Jupiter probe stage.
        # Enforce these only after probe (post-probe filtering).
        if (candidate.buy_sell_ratio_1h or 0.0) < settings.min_buy_sell_ratio:
            reasons.append(REJECT_BAD_BUY_SELL_RATIO)
        if candidate.discovery_score < settings.min_discovery_score:
            reasons.append(REJECT_LOW_SCORE)
        if settings.require_jup_sell_route and not candidate.sell_route_available:
            reasons.append(REJECT_NO_SELL_ROUTE)
        sell_impact = candidate.sell_quote_price_impact_bps
        if sell_impact is None:
            sell_impact = candidate.jupiter_sell_price_impact_bps
        if sell_impact is not None and (
            sell_impact > settings.max_acceptable_price_impact_bps or sell_impact > MAX_DISCOVERY_SELL_IMPACT_BPS
        ):
            reasons.append(REJECT_HIGH_SELL_IMPACT)
        if candidate.jupiter_buy_price_impact_bps is not None and candidate.jupiter_buy_price_impact_bps > settings.max_acceptable_price_impact_bps:
            reasons.append(REJECT_HIGH_BUY_IMPACT)
    # Age is not a standard eligibility gate. Keep only an extreme anti-garbage hard cap.
    hard_cap = float(getattr(settings, "max_token_age_hours_hard", 0.0) or 0.0)
    if hard_cap > 0 and candidate.age_hours is not None and candidate.age_hours > hard_cap:
        reasons.append(REJECT_TOKEN_TOO_OLD)
    return reasons


def _is_memecoin_universe_candidate(candidate: TokenCandidate) -> bool:
    mint = (candidate.address or "").lower()
    symbol = (candidate.symbol or "").lower()
    name = (candidate.name or "").lower()
    return mint.endswith("pump") or "pump" in symbol or "pump" in name
