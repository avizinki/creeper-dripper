from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from creeper_dripper.config import Settings
    from creeper_dripper.execution.executor import TradeExecutor
    from creeper_dripper.models import PositionState

LOGGER = logging.getLogger(__name__)


def select_drip_chunk(
    position: "PositionState",
    executor: "TradeExecutor",
    settings: "Settings",
) -> int | None:
    """Return the best chunk qty (atomic) to sell next, or None if no viable quote.

    Probes each percentage from ``settings.drip_chunk_pcts`` against the
    position's *remaining* qty and picks the largest chunk within
    ``settings.drip_near_equal_band`` of the best per-token output
    efficiency.  The drip target (``drip_qty_remaining_atomic``) is
    respected as an upper bound so we never over-sell relative to the
    original drip plan.
    """
    remaining = position.remaining_qty_atomic
    if remaining <= 0:
        return None

    drip_target = position.drip_qty_remaining_atomic
    max_chunk = (
        min(remaining, drip_target)
        if drip_target is not None and drip_target > 0
        else remaining
    )
    if max_chunk <= 0:
        return None

    candidates: list[tuple[int, float]] = []
    for pct in settings.drip_chunk_pcts:
        chunk_qty = max(1, int(remaining * pct))
        chunk_qty = min(chunk_qty, max_chunk)
        try:
            probe = executor.quote_sell(position.token_mint, chunk_qty)
        except Exception as exc:
            LOGGER.debug(
                "drip_chunker: quote_sell failed mint=%s qty=%s err=%s",
                position.token_mint,
                chunk_qty,
                exc,
            )
            continue
        if not probe.route_ok or not probe.out_amount_atomic or probe.out_amount_atomic <= 0:
            continue
        efficiency = probe.out_amount_atomic / float(chunk_qty)
        candidates.append((chunk_qty, efficiency))

    if not candidates:
        return None

    best_efficiency = max(r for _, r in candidates)
    threshold = best_efficiency * (1.0 - settings.drip_near_equal_band)
    near_equal = [(qty, r) for qty, r in candidates if r >= threshold]
    # Prefer larger chunk among near-equal options (fewer cycles).
    return max(qty for qty, _ in near_equal)
