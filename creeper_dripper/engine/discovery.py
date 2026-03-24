from __future__ import annotations

import logging
from typing import Iterable

from creeper_dripper.clients.birdeye import BirdeyeClient
from creeper_dripper.clients.jupiter import JupiterClient
from creeper_dripper.config import SOL_MINT, Settings
from creeper_dripper.engine.scoring import passes_filters, score_candidate
from creeper_dripper.models import TokenCandidate

LOGGER = logging.getLogger(__name__)


def discover_candidates(
    birdeye: BirdeyeClient,
    jupiter: JupiterClient,
    settings: Settings,
) -> list[TokenCandidate]:
    seeds = _dedupe_by_address([*birdeye.trending_tokens(limit=settings.discovery_limit), *birdeye.new_listings(limit=max(4, settings.discovery_limit // 3))])
    candidates: list[TokenCandidate] = []
    for seed in seeds[: settings.discovery_limit + 5]:
        try:
            candidate = birdeye.build_candidate(seed)
            if not candidate.decimals:
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
            if passes_filters(candidate, settings):
                candidates.append(candidate)
        except Exception as exc:
            LOGGER.warning("candidate build failed for %r: %s", seed.get("symbol") if isinstance(seed, dict) else seed, exc)
    candidates.sort(key=lambda item: item.discovery_score, reverse=True)
    return candidates[: settings.discovery_max_candidates]


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
