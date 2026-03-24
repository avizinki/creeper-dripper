from __future__ import annotations

import logging
from collections import Counter
from typing import Iterable

from creeper_dripper.clients.birdeye import BirdeyeClient
from creeper_dripper.clients.jupiter import JupiterClient
from creeper_dripper.config import SOL_MINT, Settings
from creeper_dripper.engine.scoring import passes_filters, rejection_reasons, score_candidate
from creeper_dripper.errors import REJECT_CANDIDATE_BUILD_FAILED
from creeper_dripper.models import TokenCandidate
from creeper_dripper.observability import EventCollector

LOGGER = logging.getLogger(__name__)


def discover_candidates(
    birdeye: BirdeyeClient,
    jupiter: JupiterClient,
    settings: Settings,
) -> tuple[list[TokenCandidate], dict]:
    events = EventCollector()
    seeds = _dedupe_by_address([*birdeye.trending_tokens(limit=settings.discovery_limit), *birdeye.new_listings(limit=max(4, settings.discovery_limit // 3))])
    events.emit("discovery_seed_loaded", "ok", seeds_total=len(seeds))
    candidates: list[TokenCandidate] = []
    rejection_counts: Counter[str] = Counter()
    built = 0
    for seed in seeds[: settings.discovery_limit + 5]:
        try:
            candidate = birdeye.build_candidate(seed)
            built += 1
            events.emit("candidate_built", "ok", mint=candidate.address, symbol=candidate.symbol)
            if not candidate.decimals:
                rejection_counts["reject_missing_decimals"] += 1
                events.emit("candidate_rejected", "reject_missing_decimals", mint=candidate.address, symbol=candidate.symbol)
                continue
            probe_buy_amount = max(1, int(settings.min_order_size_sol * 1_000_000_000))
            buy_probe = jupiter.probe_quote(
                input_mint=SOL_MINT,
                output_mint=candidate.address,
                amount_atomic=probe_buy_amount,
                slippage_bps=settings.default_slippage_bps,
            )
            candidate.jupiter_buy_out_amount = buy_probe.out_amount_atomic
            candidate.jupiter_buy_price_impact_bps = buy_probe.price_impact_bps
            if buy_probe.out_amount_atomic:
                sell_probe = jupiter.probe_quote(
                    input_mint=candidate.address,
                    output_mint=SOL_MINT,
                    amount_atomic=max(1, buy_probe.out_amount_atomic),
                    slippage_bps=settings.default_slippage_bps,
                )
                candidate.jupiter_sell_price_impact_bps = sell_probe.price_impact_bps
            candidate = score_candidate(candidate, settings)
            reasons = rejection_reasons(candidate, settings)
            if not reasons and passes_filters(candidate, settings):
                candidates.append(candidate)
                events.emit("candidate_accepted", "ok", mint=candidate.address, symbol=candidate.symbol, score=candidate.discovery_score)
            else:
                for reason in reasons:
                    rejection_counts[reason] += 1
                    events.emit("candidate_rejected", reason, mint=candidate.address, symbol=candidate.symbol, score=candidate.discovery_score)
        except Exception as exc:
            LOGGER.warning("candidate build failed for %r: %s", seed.get("symbol") if isinstance(seed, dict) else seed, exc)
            rejection_counts[REJECT_CANDIDATE_BUILD_FAILED] += 1
            events.emit("candidate_rejected", REJECT_CANDIDATE_BUILD_FAILED, symbol=seed.get("symbol") if isinstance(seed, dict) else "unknown", error=str(exc))
    candidates.sort(key=lambda item: item.discovery_score, reverse=True)
    accepted = candidates[: settings.discovery_max_candidates]
    summary = {
        "seeds_total": len(seeds),
        "candidates_built": built,
        "candidates_accepted": len(accepted),
        "candidates_rejected_total": int(sum(rejection_counts.values())),
        "rejection_counts": dict(rejection_counts),
        "events": events.to_dicts(),
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
