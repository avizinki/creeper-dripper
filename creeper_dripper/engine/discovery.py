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

def _normalized_seed_metadata(seed: dict[str, Any], *, rejection_reason: str | None, stage: str) -> dict[str, Any]:
    """
    Normalized shallow metadata from seed/prefilter stage.

    No external calls; only values already present in seed payload.
    """
    mint = str(seed.get("address") or seed.get("token_address") or seed.get("mint") or "").strip() or None
    symbol = seed.get("symbol")
    liquidity = _as_float(seed.get("liquidity") or seed.get("liquidityUSD") or seed.get("liquidity_usd"))
    recent_volume = _as_float(seed.get("volume24hUSD") or seed.get("volume24h") or seed.get("v24hUSD") or seed.get("volume_24h_usd"))
    buy_sell_ratio = _as_float(seed.get("buySellRatio") or seed.get("buy_sell_ratio") or seed.get("buySellRatio1h"))
    age_hours = _seed_age_hours(seed)
    return {
        "mint": mint,
        "symbol": symbol,
        "score": None,
        "liquidity_usd": liquidity,
        "buy_sell_ratio": buy_sell_ratio,
        "age_hours": age_hours,
        "recent_volume_usd": recent_volume,
        "source_stage": stage,
        "accepted_or_rejected": "rejected",
        "rejection_reason": rejection_reason,
        # route/probe fields are unknown at seed stage
        "route_exists_buy": None,
        "route_exists_sell": None,
        "route_exists": None,
        "no_route": None,
        "price_impact_bps_buy": None,
        "price_impact_bps_sell": None,
        "route_fragile": None,
        "probe_size_bucket_buy": None,
        "probe_size_bucket_sell": None,
    }


def _normalized_candidate_metadata(candidate: TokenCandidate, *, liq: float | None = None, liq_min: float | None = None) -> dict[str, Any]:
    """
    Normalized, additive discovery metadata for runtime artifacts.

    This is observability/data-capture only: do not change acceptance behavior.
    """
    sell_quality = candidate.sell_route_quality
    fragile_route = True if sell_quality in {"fragile", "bad"} else False if sell_quality in {"good"} else None
    buy_ok = bool(candidate.jupiter_buy_out_amount)
    sell_ok = bool(candidate.sell_quote_success and candidate.sell_quote_out_amount)
    no_route = (not buy_ok) or (not sell_ok)
    route_fragile = True if sell_quality in {"fragile", "weak", "bad"} else False if sell_quality in {"good"} else None
    return {
        # identity
        "mint": candidate.address,
        "symbol": candidate.symbol,
        # required fields (explicit None when unknown)
        "score": candidate.discovery_score,
        "liquidity_usd": float(liq) if liq is not None else (float(candidate.liquidity_usd) if candidate.liquidity_usd is not None else None),
        "min_liquidity_usd": float(liq_min) if liq_min is not None else None,
        "buy_sell_ratio": candidate.buy_sell_ratio_1h,
        "age_hours": candidate.age_hours,
        # price impacts
        "price_impact_bps_buy": candidate.jupiter_buy_price_impact_bps,
        "price_impact_bps_sell": candidate.sell_quote_price_impact_bps,
        # route state
        "route_exists_buy": buy_ok,
        "route_exists_sell": sell_ok,
        "route_exists": bool(buy_ok and sell_ok),
        "no_route": bool(no_route),
        "fragile_route": fragile_route,
        "sell_route_quality": sell_quality,
        "route_fragile": route_fragile,
        # rejection/acceptance context
        "rejection_reason": None,
        "source_stage": "candidate",
        "accepted_or_rejected": None,
        "recent_volume_usd": candidate.volume_24h_usd,
        "probe_size_bucket_buy": None,
        "probe_size_bucket_sell": None,
    }


def _extract_economics_field_records(event_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Persist compact, normalized discovery economics rows for downstream runtime artifacts.
    """
    keys = (
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
        "fragile_route",
        "estimated_exit_value_sol",
        "zombie_class",
        "rejection_reason",
        "source_stage",
        "accepted_or_rejected",
    )
    out: list[dict[str, Any]] = []
    for row in event_rows:
        if row.get("event_type") not in {"candidate_accepted", "candidate_rejected"}:
            continue
        md = row.get("metadata") or {}
        mint = md.get("mint")
        if not mint:
            continue
        normalized = {k: md.get(k) for k in keys}
        normalized["event_type"] = row.get("event_type")
        normalized["reason_code"] = row.get("reason_code")
        out.append(normalized)
    return out


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
        "buy_sell_ratio": raw.get("buy_sell_ratio") if raw.get("buy_sell_ratio") is not None else raw.get("buy_sell_ratio_1h"),
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
    budget_snapshot = (
        birdeye.budget_snapshot()
        if hasattr(birdeye, "budget_snapshot")
        else {
            "birdeye_budget_mode": "healthy",
            "birdeye_requests_count": 0,
            "birdeye_429_count": 0,
            "birdeye_success_rate": 1.0,
            "budget_reason_summary": "unsupported",
            "endpoints_disabled": [],
        }
    )
    trending: list[dict[str, Any]] = []
    seed_limit = int(getattr(settings, "discovery_seed_limit", settings.discovery_limit) or settings.discovery_limit)
    overview_limit = int(getattr(settings, "discovery_overview_limit", settings.discovery_max_candidates) or settings.discovery_max_candidates)
    overview_limit = max(0, overview_limit)
    max_active_candidates = int(getattr(settings, "max_active_candidates", settings.max_active_candidates) or settings.max_active_candidates)
    max_active_candidates = max(1, max_active_candidates)
    if hasattr(birdeye, "adjusted_discovery_limits"):
        seed_limit, overview_limit, max_active_candidates = birdeye.adjusted_discovery_limits(
            int(seed_limit),
            int(overview_limit),
            int(max_active_candidates),
        )
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
    overview_seeds_processed = 0
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
    def _seed_volume_sort_key(s: dict[str, Any]) -> float:
        v = _as_float(
            s.get("volume24hUSD")
            or s.get("volume24h")
            or s.get("v24hUSD")
            or s.get("volume_24h_usd")
        )
        return float(v or 0.0)

    for seed in sorted(seeds[:seed_limit], key=_seed_volume_sort_key, reverse=True):
        processed_total = built + int(sum(rejection_counts.values()))
        try:
            prefilter_decision = _seed_prefilter(seed, settings)
            if prefilter_decision:
                seed_prefiltered_out += 1
                rejection_counts[prefilter_decision] += 1
                events.emit(
                    "candidate_rejected",
                    prefilter_decision,
                    **_normalized_seed_metadata(
                        seed,
                        rejection_reason=prefilter_decision,
                        stage="seed_prefilter",
                    ),
                )
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

            # Phase 1 hard cap: only run `token_overview` on the top N seeds (cheap ranking via seed volume).
            if overview_seeds_processed >= overview_limit:
                seed_prefiltered_out += 1
                rejection_counts["reject_overview_limit"] += 1
                events.emit(
                    "candidate_rejected",
                    "reject_overview_limit",
                    **_normalized_seed_metadata(
                        seed,
                        rejection_reason="reject_overview_limit",
                        stage="overview_limit",
                    ),
                )
                continue

            address = str(seed.get("address") or seed.get("token_address") or seed.get("mint") or "").strip()
            cache_key = f"candidate:{address}"
            cache_keys_used.append(cache_key)
            candidate_keys_used.append(cache_key)
            candidate = candidate_cache.get(cache_key)
            if candidate is None:
                birdeye_candidate_build_calls += 1
                candidate = birdeye.build_candidate_light(seed)
                candidate_cache.set(cache_key, candidate)
                overview_seeds_processed += 1
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
                    events.emit(
                        "candidate_rejected",
                        reason,
                        **{
                            **_normalized_candidate_metadata(candidate),
                            "rejection_reason": reason,
                            "accepted_or_rejected": "rejected",
                            "source_stage": "prefilter",
                        },
                    )
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
            # Phase 2 (expensive): only enrich survivors of cheap prefilter.
            skip_creation = hasattr(birdeye, "should_skip_endpoint") and birdeye.should_skip_endpoint("/defi/token_creation_info")
            if not skip_creation:
                try:
                    candidate = birdeye.enrich_candidate_heavy(candidate)
                except Exception as exc:
                    # Treat enrichment failures as build failures (avoid continuing with partial heavy fields).
                    LOGGER.warning("candidate heavy enrichment failed mint=%s symbol=%s err=%s", candidate.address, candidate.symbol, exc)
                    rejection_counts[REJECT_CANDIDATE_BUILD_FAILED] += 1
                    events.emit(
                        "candidate_rejected",
                        REJECT_CANDIDATE_BUILD_FAILED,
                        **{
                            **_normalized_candidate_metadata(candidate),
                            "rejection_reason": REJECT_CANDIDATE_BUILD_FAILED,
                            "source_stage": "heavy_enrich",
                            "accepted_or_rejected": "rejected",
                            "error": str(exc),
                        },
                    )
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
    top_n = prefiltered_candidates[: max_active_candidates]

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
            md = _normalized_candidate_metadata(candidate)
            md["rejection_reason"] = REJECT_NO_BUY_ROUTE
            md["source_stage"] = "probe_buy"
            md["accepted_or_rejected"] = "rejected"
            md["probe_size_bucket_buy"] = buy_size_bucket
            events.emit(
                "candidate_rejected",
                REJECT_NO_BUY_ROUTE,
                **md,
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
                md = _normalized_candidate_metadata(candidate)
                md["rejection_reason"] = reason
                md["source_stage"] = "probe_sell"
                md["accepted_or_rejected"] = "rejected"
                md["probe_size_bucket_buy"] = buy_size_bucket
                md["probe_size_bucket_sell"] = sell_size_bucket
                events.emit(
                    "candidate_rejected",
                    reason,
                    **md,
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

        # Conditional security/holder enrichment:
        # - We can skip `/defi/token_security` and `/defi/v3/token/holder` if they cannot
        #   change acceptance decisions w.r.t. `min_discovery_score`.
        # - We preserve correctness by using an explicit score upper/lower bound:
        #   * best-case: security safe + holder spread (top10 <= 25 → +8 score)
        #   * worst-case: security actual + holder concentrated (top10 >= 55 → -12 score)
        #   If best-case is below threshold, we skip both endpoints.
        #   If worst-case is still above threshold, we skip holder.
        if candidate.top10_holder_percent is None and not getattr(candidate, "needs_holder_check", False):
            # If caller didn't request holder, treat as needing it (safety).
            candidate.needs_holder_check = True
        if getattr(candidate, "needs_security_check", False) is False:
            candidate.needs_security_check = True

        min_disc = float(settings.min_discovery_score or 0.0)
        # Compute best-case score without expensive endpoints:
        candidate.security_mint_mutable = False
        candidate.security_freezable = False
        candidate.top10_holder_percent = 0.0  # <= 25 → +8 contribution
        candidate = score_candidate(candidate, settings)
        best_score = float(candidate.discovery_score or 0.0)

        if best_score < min_disc:
            # Even in the best-case world, candidate can't pass the score gate.
            # Skip expensive endpoints; later scoring stays <= best_score.
            candidate.needs_security_check = False
            candidate.needs_holder_check = False
            candidate.security_mint_mutable = None
            candidate.security_freezable = None
            candidate.top10_holder_percent = None
        else:
            # Security is needed to correctly apply binary reject flags and score penalties.
            candidate.needs_security_check = True
            if hasattr(birdeye, "should_skip_endpoint") and birdeye.should_skip_endpoint("/defi/token_security"):
                candidate.needs_security_check = False
            elif hasattr(birdeye, "enrich_candidate_security_only"):
                candidate = birdeye.enrich_candidate_security_only(candidate)

            # Decide whether holder is needed by checking worst-case holder penalty.
            candidate.needs_holder_check = True
            candidate.top10_holder_percent = 100.0  # >= 55 → -12 contribution
            candidate = score_candidate(candidate, settings)
            worst_score = float(candidate.discovery_score or 0.0)
            if worst_score >= min_disc:
                candidate.needs_holder_check = False
                # Keep pessimistic top10 so later scoring reflects worst-case.
                candidate.top10_holder_percent = 100.0
            else:
                if hasattr(birdeye, "should_skip_endpoint") and birdeye.should_skip_endpoint("/defi/v3/token/holder"):
                    candidate.needs_holder_check = False
                elif hasattr(birdeye, "enrich_candidate_holders_only"):
                    candidate = birdeye.enrich_candidate_holders_only(candidate)

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
                    **{
                        **_normalized_candidate_metadata(candidate, liq=liq, liq_min=liq_min),
                        "rejection_reason": None,
                        "accepted_or_rejected": "accepted",
                        "source_stage": "post_probe",
                        "soft_rejects": "soft_low_liquidity",
                        "liquidity_soft": True,
                        "liquidity_score_delta": (candidate.raw or {}).get("liquidity_score_delta"),
                        "probe_size_bucket_buy": buy_size_bucket,
                        "probe_size_bucket_sell": sell_size_bucket,
                    },
                )
                continue
            candidates.append(candidate)
            events.emit(
                "candidate_accepted",
                "ok",
                **{
                    **_normalized_candidate_metadata(candidate, liq=liq, liq_min=liq_min),
                    "rejection_reason": None,
                    "accepted_or_rejected": "accepted",
                    "source_stage": "post_probe",
                    "liquidity_soft": liquidity_soft,
                    "liquidity_score_delta": (candidate.raw or {}).get("liquidity_score_delta"),
                    "probe_size_bucket_buy": buy_size_bucket,
                    "probe_size_bucket_sell": sell_size_bucket,
                },
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
                    **{
                        **_normalized_candidate_metadata(candidate, liq=liq, liq_min=liq_min),
                        "rejection_reason": None,
                        "accepted_or_rejected": "accepted",
                        "source_stage": "post_probe",
                        "soft_rejects": ",".join(soft),
                        "liquidity_soft": liquidity_soft,
                        "liquidity_score_delta": (candidate.raw or {}).get("liquidity_score_delta"),
                        "probe_size_bucket_buy": buy_size_bucket,
                        "probe_size_bucket_sell": sell_size_bucket,
                    },
                )
                continue
        for reason in reasons:
            rejection_counts[reason] += 1
            metadata = _normalized_candidate_metadata(candidate, liq=liq, liq_min=liq_min)
            metadata["rejection_reason"] = reason
            metadata["accepted_or_rejected"] = "rejected"
            metadata["source_stage"] = "post_probe"
            metadata["probe_size_bucket_buy"] = buy_size_bucket
            metadata["probe_size_bucket_sell"] = sell_size_bucket
            if reason == REJECT_TOKEN_TOO_OLD:
                metadata.update(
                    {
                        "age_source": candidate.age_source,
                        "created_at_raw": candidate.created_at_raw,
                    }
                )
            if reason in {REJECT_NO_BUY_ROUTE, REJECT_NO_SELL_ROUTE}:
                metadata.update(
                    {
                        "sell_route_available": candidate.sell_route_available,
                        "sell_quote_success": candidate.sell_quote_success,
                        "price_impact_bps_sell": candidate.sell_quote_price_impact_bps,
                    }
                )
            events.emit("candidate_rejected", reason, **metadata)

    candidates.sort(key=lambda item: item.discovery_score, reverse=True)
    accepted = candidates[: settings.discovery_max_candidates]
    # Long cycles can exceed per-key TTL between first and next-cycle lookup;
    # re-anchor active keys at cycle end so TTL applies across cycle boundaries.
    candidate_cache.touch_keys(candidate_keys_used)
    route_cache.touch_keys(route_keys_used)

    event_rows = events.to_dicts()
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
        "events": event_rows,
        "economics_field_records": _extract_economics_field_records(event_rows),
        "market_data_checked_at": market_data_checked_at,
        "birdeye_budget_mode": budget_snapshot.get("birdeye_budget_mode"),
        "birdeye_requests_count": budget_snapshot.get("birdeye_requests_count"),
        "birdeye_429_count": budget_snapshot.get("birdeye_429_count"),
        "birdeye_success_rate": budget_snapshot.get("birdeye_success_rate"),
        "budget_reason_summary": budget_snapshot.get("budget_reason_summary"),
        "endpoints_disabled": budget_snapshot.get("endpoints_disabled", []),
        "effective_discovery_seed_limit": seed_limit,
        "effective_discovery_overview_limit": overview_limit,
        "effective_max_active_candidates": max_active_candidates,
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
