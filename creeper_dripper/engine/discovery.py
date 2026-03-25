from __future__ import annotations

from dataclasses import asdict, is_dataclass
import json
import logging
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Iterable

from creeper_dripper.clients.birdeye import BirdeyeClient
from creeper_dripper.clients.jupiter import JupiterBadRequestError, JupiterClient
from creeper_dripper.cache import TTLCache
from creeper_dripper.config import SOL_MINT, Settings
from creeper_dripper.engine.scoring import passes_filters, rejection_reasons, score_candidate
from creeper_dripper.errors import (
    BIRDEYE_EXIT_LIQUIDITY_UNSUPPORTED_CHAIN,
    REJECT_BAD_BUY_SELL_RATIO,
    REJECT_CANDIDATE_BUILD_FAILED,
    REJECT_JUPITER_BAD_PROBE,
    REJECT_JUPITER_UNTRADABLE,
    REJECT_LOW_SCORE,
    REJECT_NO_BUY_ROUTE,
    REJECT_NO_SELL_ROUTE,
    REJECT_TOKEN_TOO_OLD,
)
from creeper_dripper.models import ProbeQuote, TokenCandidate
from creeper_dripper.observability import EventCollector

LOGGER = logging.getLogger(__name__)

_EARLY_RISK_SOFT_REJECTS = {REJECT_BAD_BUY_SELL_RATIO, REJECT_LOW_SCORE}


def serialize_candidate(candidate: object) -> dict:
    """Return a stable JSON-safe candidate payload for scan artifacts."""
    if is_dataclass(candidate):
        raw = asdict(candidate)
    elif isinstance(candidate, dict):
        raw = dict(candidate)
    else:
        raw = {}
    address = raw.get("address") or raw.get("mint")
    return {
        "symbol": raw.get("symbol"),
        "address": address,
        "mint": address,
        "discovery_score": raw.get("discovery_score"),
        "liquidity_usd": raw.get("liquidity_usd"),
        "volume_24h_usd": raw.get("volume_24h_usd"),
        "buy_sell_ratio": raw.get("buy_sell_ratio"),
        "exit_liquidity_available": raw.get("exit_liquidity_available"),
        "exit_liquidity_reason": raw.get("exit_liquidity_reason"),
        "birdeye_exit_liquidity_supported": raw.get("birdeye_exit_liquidity_supported"),
        "sell_route_available": raw.get("sell_route_available"),
        "sell_quote_out_amount": raw.get("sell_quote_out_amount"),
        "sell_quote_price_impact_bps": raw.get("sell_quote_price_impact_bps"),
        "sell_quote_success": raw.get("sell_quote_success"),
        "sell_route_quality": raw.get("sell_route_quality"),
        "age_hours": raw.get("age_hours"),
        "age_source": raw.get("age_source"),
        "created_at_raw": raw.get("created_at_raw"),
        "rejection_reasons": list(raw.get("rejection_reasons") or []),
    }


def serialize_candidates(candidates: Iterable[object]) -> list[dict]:
    return [serialize_candidate(candidate) for candidate in candidates]


def discover_candidates(
    birdeye: BirdeyeClient,
    jupiter: JupiterClient,
    settings: Settings,
    progress_callback=None,
    candidate_cache: TTLCache[TokenCandidate] | None = None,
    route_cache: TTLCache[ProbeQuote] | None = None,
) -> tuple[list[TokenCandidate], dict]:
    events = EventCollector()
    trending: list[dict[str, Any]] = []
    seed_limit = int(getattr(settings, "discovery_seed_limit", settings.discovery_limit) or settings.discovery_limit)
    try:
        trending = birdeye.trending_tokens(limit=seed_limit)
    except Exception as exc:
        LOGGER.warning("event=discovery_seed_failed stage=trending error=%s", exc)
    new_listing_seeds: list[dict[str, Any]] = []
    try:
        new_listing_seeds = birdeye.new_listings(limit=max(4, seed_limit // 3))
    except Exception as exc:
        LOGGER.warning("event=discovery_seed_failed stage=new_listings error=%s", exc)
    seeds = _dedupe_by_address([*trending, *new_listing_seeds])
    market_data_checked_at = None
    events.emit("discovery_seed_loaded", "ok", seeds_total=len(seeds))
    candidates: list[TokenCandidate] = []
    prefiltered_candidates: list[TokenCandidate] = []
    rejection_counts: Counter[str] = Counter()
    seed_prefiltered_out = 0
    built = 0
    route_checked = 0
    birdeye_candidate_build_calls = 0
    jupiter_buy_probe_calls = 0
    jupiter_sell_probe_calls = 0
    candidate_cache = candidate_cache if candidate_cache is not None else TTLCache[TokenCandidate](settings.candidate_cache_ttl_seconds)
    route_cache = route_cache if route_cache is not None else TTLCache[ProbeQuote](settings.route_check_cache_ttl_seconds)
    cycle_label = datetime.now(timezone.utc).isoformat()
    candidate_cache.start_trace(cycle_label=cycle_label, max_keys=3)
    route_cache.start_trace(cycle_label=cycle_label, max_keys=3)
    cache_keys_used: list[str] = []
    candidate_keys_used: list[str] = []
    route_keys_used: list[str] = []
    for seed in seeds[:seed_limit]:
        processed_total = built + int(sum(rejection_counts.values()))
        try:
            prefilter_decision = _seed_prefilter(seed, settings)
            if prefilter_decision:
                seed_prefiltered_out += 1
                rejection_counts[prefilter_decision] += 1
                events.emit("candidate_rejected", prefilter_decision, symbol=seed.get("symbol"), mint=seed.get("address"), stage="seed_prefilter")
                if progress_callback:
                    ranked = sorted(candidates, key=lambda x: x.discovery_score, reverse=True)
                    progress_callback(
                        {
                            "seeds_total": len(seeds),
                            "processed_total": processed_total + 1,
                            "built_total": built,
                            "accepted_total": len(candidates),
                            "rejection_counts": dict(rejection_counts),
                            "last_processed_symbol": seed.get("symbol"),
                            "last_processed_mint": seed.get("address"),
                            "top_candidates_seen": serialize_candidates(ranked[:5]),
                        },
                        serialize_candidates(ranked[: settings.discovery_max_candidates]),
                    )
                continue

            address = str(seed.get("address") or seed.get("token_address") or seed.get("mint") or "").strip()
            cache_key = f"candidate:{address}"
            cache_keys_used.append(cache_key)
            candidate_keys_used.append(cache_key)
            candidate = candidate_cache.get(cache_key)
            if candidate is None:
                birdeye_candidate_build_calls += 1
                candidate = birdeye.build_candidate(seed)
                candidate_cache.set(cache_key, candidate)
            built += 1
            events.emit(
                "candidate_built",
                "ok",
                mint=candidate.address,
                symbol=candidate.symbol,
                exit_liquidity_available=candidate.exit_liquidity_available,
                exit_liquidity_reason=candidate.exit_liquidity_reason,
                birdeye_exit_liquidity_supported=candidate.birdeye_exit_liquidity_supported,
                age_hours=candidate.age_hours,
                age_source=candidate.age_source,
                created_at_raw=candidate.created_at_raw,
            )
            market_data_checked_at = market_data_checked_at or candidate.raw.get("overview", {}).get("updatedAt") or None
            if not candidate.exit_liquidity_available:
                events.emit(
                    "candidate_built",
                    candidate.exit_liquidity_reason or BIRDEYE_EXIT_LIQUIDITY_UNSUPPORTED_CHAIN,
                    mint=candidate.address,
                    symbol=candidate.symbol,
                    informational_only=True,
                )
            if not candidate.decimals:
                rejection_counts["reject_missing_decimals"] += 1
                events.emit("candidate_rejected", "reject_missing_decimals", mint=candidate.address, symbol=candidate.symbol)
                if progress_callback:
                    ranked = sorted(candidates, key=lambda x: x.discovery_score, reverse=True)
                    progress_callback(
                        {
                            "seeds_total": len(seeds),
                            "processed_total": processed_total + 1,
                            "built_total": built,
                            "accepted_total": len(candidates),
                            "rejection_counts": dict(rejection_counts),
                            "last_processed_symbol": candidate.symbol,
                            "last_processed_mint": candidate.address,
                            "top_candidates_seen": serialize_candidates(ranked[:5]),
                        },
                        serialize_candidates(ranked[: settings.discovery_max_candidates]),
                    )
                continue
            candidate = score_candidate(candidate, settings)
            prelim_reasons = rejection_reasons(candidate, settings, include_route_checks=False)
            if prelim_reasons:
                for reason in prelim_reasons:
                    rejection_counts[reason] += 1
                    events.emit("candidate_rejected", reason, mint=candidate.address, symbol=candidate.symbol, score=candidate.discovery_score, stage="prefilter")
                if progress_callback:
                    ranked = sorted(candidates, key=lambda x: x.discovery_score, reverse=True)
                    progress_callback(
                        {
                            "seeds_total": len(seeds),
                            "processed_total": built + int(sum(rejection_counts.values())),
                            "built_total": built,
                            "accepted_total": len(candidates),
                            "rejection_counts": dict(rejection_counts),
                            "last_processed_symbol": candidate.symbol,
                            "last_processed_mint": candidate.address,
                            "top_candidates_seen": serialize_candidates(ranked[:5]),
                        },
                        serialize_candidates(ranked[: settings.discovery_max_candidates]),
                    )
                continue
            prefiltered_candidates.append(candidate)
            if progress_callback:
                ranked = sorted(prefiltered_candidates, key=lambda x: x.discovery_score, reverse=True)
                progress_callback(
                    {
                        "seeds_total": len(seeds),
                        "processed_total": built + int(sum(rejection_counts.values())),
                        "built_total": built,
                        "accepted_total": len(candidates),
                        "rejection_counts": dict(rejection_counts),
                        "last_processed_symbol": candidate.symbol,
                        "last_processed_mint": candidate.address,
                        "top_candidates_seen": serialize_candidates(ranked[:5]),
                    },
                    serialize_candidates(ranked[: settings.max_active_candidates]),
                )
        except Exception as exc:
            LOGGER.warning("candidate build failed for %r: %s", seed.get("symbol") if isinstance(seed, dict) else seed, exc)
            rejection_counts[REJECT_CANDIDATE_BUILD_FAILED] += 1
            events.emit("candidate_rejected", REJECT_CANDIDATE_BUILD_FAILED, symbol=seed.get("symbol") if isinstance(seed, dict) else "unknown", error=str(exc))
            if progress_callback:
                ranked = sorted(candidates, key=lambda x: x.discovery_score, reverse=True)
                progress_callback(
                    {
                        "seeds_total": len(seeds),
                        "processed_total": built + int(sum(rejection_counts.values())),
                        "built_total": built,
                        "accepted_total": len(candidates),
                        "rejection_counts": dict(rejection_counts),
                        "last_processed_symbol": seed.get("symbol") if isinstance(seed, dict) else None,
                        "last_processed_mint": seed.get("address") if isinstance(seed, dict) else None,
                        "top_candidates_seen": serialize_candidates(ranked[:5]),
                    },
                    serialize_candidates(ranked[: settings.discovery_max_candidates]),
                )
    prefiltered_candidates.sort(key=lambda item: item.discovery_score, reverse=True)
    top_n = prefiltered_candidates[: settings.max_active_candidates]

    for candidate in top_n:
        probe_buy_amount = max(1, int(settings.min_order_size_sol * 1_000_000_000))
        buy_size_bucket = _size_bucket(probe_buy_amount)
        buy_cache_key = f"route:{candidate.address}:buy:{buy_size_bucket}"
        cache_keys_used.append(buy_cache_key)
        route_keys_used.append(buy_cache_key)
        buy_probe = route_cache.get(buy_cache_key)
        if buy_probe is None:
            try:
                jupiter_buy_probe_calls += 1
                buy_probe = jupiter.probe_quote(
                    input_mint=SOL_MINT,
                    output_mint=candidate.address,
                    amount_atomic=probe_buy_amount,
                    slippage_bps=settings.default_slippage_bps,
                )
                if (buy_probe.raw or {}).get("error") != "jupiter_timeout":
                    route_cache.set(buy_cache_key, buy_probe)
            except JupiterBadRequestError as exc:
                reason = _classify_buy_probe_failure(exc)
                rejection_counts[reason] += 1
                events.emit(
                    "candidate_rejected",
                    reason,
                    symbol=candidate.symbol,
                    mint=candidate.address,
                    probe_amount_lamports=probe_buy_amount,
                    slippage_bps=settings.default_slippage_bps,
                    jupiter_error_body=exc.body,
                    jupiter_endpoint=exc.endpoint,
                    jupiter_params=exc.params,
                    jupiter_error_code=_extract_jupiter_error_code(exc.body),
                )
                continue
        route_checked += 1
        if not buy_probe.route_ok and (buy_probe.raw or {}).get("error") == "jupiter_timeout":
            rejection_counts["reject_probe_timeout"] += 1
            LOGGER.info(
                "event=candidate_probe_failed reason=jupiter_timeout mint=%s symbol=%s probe=buy",
                candidate.address,
                candidate.symbol,
            )
            events.emit(
                "candidate_probe_failed",
                "jupiter_timeout",
                mint=candidate.address,
                symbol=candidate.symbol,
                probe="buy",
            )
            continue
        candidate.jupiter_buy_out_amount = buy_probe.out_amount_atomic
        candidate.jupiter_buy_price_impact_bps = buy_probe.price_impact_bps
        if not buy_probe.out_amount_atomic:
            rejection_counts[REJECT_NO_BUY_ROUTE] += 1
            events.emit(
                "candidate_rejected",
                REJECT_NO_BUY_ROUTE,
                symbol=candidate.symbol,
                mint=candidate.address,
                probe_amount_lamports=probe_buy_amount,
                slippage_bps=settings.default_slippage_bps,
                jupiter_error_body=buy_probe.raw,
                jupiter_endpoint="/quote",
            )
            continue

        sell_probe_amount = max(1, buy_probe.out_amount_atomic)
        sell_size_bucket = _size_bucket(sell_probe_amount)
        sell_cache_key = f"route:{candidate.address}:sell:{sell_size_bucket}"
        cache_keys_used.append(sell_cache_key)
        route_keys_used.append(sell_cache_key)
        sell_probe = route_cache.get(sell_cache_key)
        if sell_probe is None:
            try:
                jupiter_sell_probe_calls += 1
                sell_probe = jupiter.probe_quote(
                    input_mint=candidate.address,
                    output_mint=SOL_MINT,
                    amount_atomic=sell_probe_amount,
                    slippage_bps=settings.default_slippage_bps,
                )
                if (sell_probe.raw or {}).get("error") != "jupiter_timeout":
                    route_cache.set(sell_cache_key, sell_probe)
            except JupiterBadRequestError as exc:
                reason = _classify_sell_probe_failure(exc)
                rejection_counts[reason] += 1
                events.emit(
                    "candidate_rejected",
                    reason,
                    symbol=candidate.symbol,
                    mint=candidate.address,
                    probe_amount_lamports=sell_probe_amount,
                    slippage_bps=settings.default_slippage_bps,
                    jupiter_error_body=exc.body,
                    jupiter_endpoint=exc.endpoint,
                    jupiter_params=exc.params,
                    jupiter_error_code=_extract_jupiter_error_code(exc.body),
                )
                continue
        route_checked += 1
        if not sell_probe.route_ok and (sell_probe.raw or {}).get("error") == "jupiter_timeout":
            rejection_counts["reject_probe_timeout"] += 1
            LOGGER.info(
                "event=candidate_probe_failed reason=jupiter_timeout mint=%s symbol=%s probe=sell",
                candidate.address,
                candidate.symbol,
            )
            events.emit(
                "candidate_probe_failed",
                "jupiter_timeout",
                mint=candidate.address,
                symbol=candidate.symbol,
                probe="sell",
            )
            continue
        candidate.jupiter_sell_price_impact_bps = sell_probe.price_impact_bps
        candidate.sell_route_available = bool(sell_probe.route_ok and sell_probe.out_amount_atomic)
        candidate.sell_quote_out_amount = sell_probe.out_amount_atomic
        candidate.sell_quote_price_impact_bps = sell_probe.price_impact_bps
        candidate.sell_quote_success = bool(sell_probe.route_ok)
        candidate.sell_route_quality = _sell_route_quality(
            sell_probe.price_impact_bps,
            settings.max_acceptable_price_impact_bps,
        )
        candidate = score_candidate(candidate, settings)
        reasons = rejection_reasons(candidate, settings, include_route_checks=True)
        liq = float(candidate.liquidity_usd or 0.0)
        liq_min = float(settings.min_liquidity_usd or 0.0)
        liquidity_soft = bool(liq_min > 0.0 and liq < liq_min)
        if liquidity_soft:
            candidate.raw["liquidity_soft_below_min"] = True
            candidate.raw["liquidity_soft_min_usd"] = liq_min
        if not reasons and passes_filters(candidate, settings):
            if liquidity_soft and settings.early_risk_bucket_enabled:
                candidate.raw["early_risk_bucket"] = True
                candidate.raw["early_risk_soft_rejects"] = ["soft_low_liquidity"]
                candidates.append(candidate)
                events.emit(
                    "candidate_accepted",
                    "early_risk_bucket",
                    mint=candidate.address,
                    symbol=candidate.symbol,
                    score=candidate.discovery_score,
                    soft_rejects="soft_low_liquidity",
                    liquidity_usd=liq,
                    min_liquidity_usd=liq_min,
                    liquidity_score_delta=(candidate.raw or {}).get("liquidity_score_delta"),
                )
                continue
            candidates.append(candidate)
            events.emit(
                "candidate_accepted",
                "ok",
                mint=candidate.address,
                symbol=candidate.symbol,
                score=candidate.discovery_score,
                liquidity_usd=liq,
                min_liquidity_usd=liq_min,
                liquidity_soft=liquidity_soft,
                liquidity_score_delta=(candidate.raw or {}).get("liquidity_score_delta"),
            )
            continue
        if settings.early_risk_bucket_enabled:
            soft = [r for r in reasons if r in _EARLY_RISK_SOFT_REJECTS]
            if liquidity_soft:
                soft.append("soft_low_liquidity")
            hard = [r for r in reasons if r not in _EARLY_RISK_SOFT_REJECTS]
            if soft and not hard and candidate.discovery_score >= settings.early_risk_min_score_floor:
                candidate.raw["early_risk_bucket"] = True
                candidate.raw["early_risk_soft_rejects"] = list(soft)
                candidates.append(candidate)
                events.emit(
                    "candidate_accepted",
                    "early_risk_bucket",
                    mint=candidate.address,
                    symbol=candidate.symbol,
                    score=candidate.discovery_score,
                    soft_rejects=",".join(soft),
                    liquidity_usd=liq,
                    min_liquidity_usd=liq_min,
                    liquidity_score_delta=(candidate.raw or {}).get("liquidity_score_delta"),
                )
                continue
        for reason in reasons:
            rejection_counts[reason] += 1
            metadata = {
                "mint": candidate.address,
                "symbol": candidate.symbol,
                "score": candidate.discovery_score,
            }
            if reason == REJECT_TOKEN_TOO_OLD:
                metadata.update(
                    {
                        "age_hours": candidate.age_hours,
                        "age_source": candidate.age_source,
                        "created_at_raw": candidate.created_at_raw,
                    }
                )
            if reason in {REJECT_NO_BUY_ROUTE, REJECT_NO_SELL_ROUTE}:
                metadata.update(
                    {
                        "sell_route_available": candidate.sell_route_available,
                        "sell_quote_success": candidate.sell_quote_success,
                        "sell_price_impact_bps": candidate.sell_quote_price_impact_bps,
                    }
                )
            events.emit("candidate_rejected", reason, **metadata)

    candidates.sort(key=lambda item: item.discovery_score, reverse=True)
    accepted = candidates[: settings.discovery_max_candidates]
    # Long cycles can exceed per-key TTL between first and next-cycle lookup;
    # re-anchor active keys at cycle end so TTL applies across cycle boundaries.
    candidate_cache.touch_keys(candidate_keys_used)
    route_cache.touch_keys(route_keys_used)

    summary = {
        "seeds_total": len(seeds),
        "discovered_candidates": len(seeds),
        "prefiltered_candidates": len(prefiltered_candidates),
        "seed_prefiltered_out": seed_prefiltered_out,
        "topn_candidates": len(top_n),
        "route_checked_candidates": route_checked,
        "cache_hits": candidate_cache.stats.hits + route_cache.stats.hits,
        "cache_misses": candidate_cache.stats.misses + route_cache.stats.misses,
        "candidate_cache_hits": candidate_cache.stats.hits,
        "candidate_cache_misses": candidate_cache.stats.misses,
        "route_cache_hits": route_cache.stats.hits,
        "route_cache_misses": route_cache.stats.misses,
        "birdeye_candidate_build_calls": birdeye_candidate_build_calls,
        "jupiter_buy_probe_calls": jupiter_buy_probe_calls,
        "jupiter_sell_probe_calls": jupiter_sell_probe_calls,
        "candidates_built": built,
        "candidates_accepted": len(accepted),
        "candidates_rejected_total": int(sum(rejection_counts.values())),
        "rejection_counts": dict(rejection_counts),
        "events": events.to_dicts(),
        "market_data_checked_at": market_data_checked_at,
        "cache_debug_first_keys": cache_keys_used[:3],
        "cache_debug_identity": {
            "candidate_cache_id": id(candidate_cache),
            "route_cache_id": id(route_cache),
        },
        "cache_debug_trace": {
            "candidate": candidate_cache.consume_trace(),
            "route": route_cache.consume_trace(),
        },
    }
    return accepted, summary


def _dedupe_by_address(items: Iterable[dict]) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for item in items:
        address = str(item.get("address") or item.get("token_address") or item.get("mint") or "").strip()
        if not address or address in seen:
            continue
        seen.add(address)
        out.append(item)
    return out


def _seed_prefilter(seed: dict, settings: Settings) -> str | None:
    vol = _as_float(
        seed.get("volume24hUSD")
        or seed.get("volume24h")
        or seed.get("v24hUSD")
        or seed.get("volume_24h_usd")
    )
    if vol is not None and vol < settings.prefilter_min_recent_volume_usd:
        return "reject_low_volume"
    return None


def _seed_age_hours(seed: dict) -> float | None:
    for key in ("blockUnixTime", "created_time", "createdAt", "created_at"):
        ts = seed.get(key)
        if ts is None:
            continue
        try:
            unix_ts = float(ts)
        except (TypeError, ValueError):
            return None
        now = datetime.now(timezone.utc).timestamp()
        return max(0.0, (now - unix_ts) / 3600.0)
    return None


def _as_float(value) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_jupiter_error_code(body: str | None) -> str | None:
    if not body:
        return None
    try:
        parsed = json.loads(body)
    except Exception:
        return None
    if isinstance(parsed, dict):
        val = parsed.get("errorCode")
        return str(val) if val is not None else None
    return None


def _classify_buy_probe_failure(exc: JupiterBadRequestError) -> str:
    body_text = (exc.body or str(exc) or "").lower()
    if "no route" in body_text or "route not found" in body_text or "no routes found" in body_text:
        return REJECT_NO_BUY_ROUTE
    if "not tradable" in body_text or "cannot be traded" in body_text or "tradable" in body_text:
        return REJECT_JUPITER_UNTRADABLE
    return REJECT_JUPITER_BAD_PROBE


def _classify_sell_probe_failure(exc: JupiterBadRequestError) -> str:
    body_text = (exc.body or str(exc) or "").lower()
    if "no route" in body_text or "route not found" in body_text or "no routes found" in body_text:
        return REJECT_NO_SELL_ROUTE
    if "not tradable" in body_text or "cannot be traded" in body_text or "tradable" in body_text:
        return REJECT_JUPITER_UNTRADABLE
    return REJECT_JUPITER_BAD_PROBE


def _sell_route_quality(price_impact_bps: float | None, max_impact_bps: float) -> str:
    if price_impact_bps is None:
        return "unknown"
    if price_impact_bps > max_impact_bps:
        return "bad"
    if price_impact_bps > max_impact_bps * 0.65:
        return "weak"
    return "good"


def _size_bucket(amount_atomic: int) -> str:
    amount = max(1, int(amount_atomic))
    if amount < 10_000:
        return "lt_1e4"
    if amount < 100_000:
        return "1e4_1e5"
    if amount < 1_000_000:
        return "1e5_1e6"
    if amount < 10_000_000:
        return "1e6_1e7"
    if amount < 100_000_000:
        return "1e7_1e8"
    if amount < 1_000_000_000:
        return "1e8_1e9"
    return "gte_1e9"
