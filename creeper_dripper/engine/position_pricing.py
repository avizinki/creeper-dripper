"""
Jupiter-only position valuation (no RPC, no Birdeye in this path).

Exit value for the full remaining position is estimated from a single Jupiter sell quote
for `remaining_qty_atomic` (same size-bucket labels as discovery for logging only).
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from creeper_dripper.models import PositionState

if TYPE_CHECKING:
    from creeper_dripper.execution.executor import TradeExecutor

LOGGER = logging.getLogger(__name__)


def _size_bucket(amount_atomic: int) -> str:
    """Same bucket labels as discovery._size_bucket (for logging only)."""
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


SOURCE_JUPITER_SELL = "jupiter_sell"
VALUATION_STATUS_OK = "ok"
VALUATION_STATUS_NO_ROUTE = "no_route"


@dataclass(slots=True)
class PositionValuation:
    """Jupiter sell quote only. value_sol is estimated SOL out for full remaining position."""

    value_sol: float | None
    mark_sol_per_token: float | None
    source: str | None
    status: str
    size_bucket: str
    detail: str | None = None


def is_valid_sol_mark(p: float | None) -> bool:
    return _valid_positive(p)


def _valid_positive(p: float | None) -> bool:
    if p is None:
        return False
    try:
        x = float(p)
    except (TypeError, ValueError):
        return False
    if math.isnan(x) or math.isinf(x):
        return False
    return x > 0.0


def resolve_position_valuation(
    *,
    mint: str,
    symbol: str,
    position: PositionState,
    executor: TradeExecutor,
) -> PositionValuation:
    """Jupiter `quote_sell` only. No wallet RPC, no Birdeye, no held/USD fallbacks."""
    remaining_atomic = int(position.remaining_qty_atomic)
    decimals = int(position.decimals)
    bucket = _size_bucket(max(1, remaining_atomic))

    if remaining_atomic <= 0:
        return PositionValuation(
            None,
            None,
            None,
            VALUATION_STATUS_NO_ROUTE,
            bucket,
            detail="empty_position",
        )
    if decimals < 0:
        return PositionValuation(
            None,
            None,
            None,
            VALUATION_STATUS_NO_ROUTE,
            bucket,
            detail="invalid_decimals",
        )

    probe = max(1, remaining_atomic)
    try:
        q = executor.quote_sell(mint, probe)
    except Exception as exc:
        LOGGER.warning(
            "event=position_valuation_failed mint=%s symbol=%s reason=no_route detail=quote_exc:%s",
            mint,
            symbol,
            exc,
        )
        return PositionValuation(
            None,
            None,
            None,
            VALUATION_STATUS_NO_ROUTE,
            bucket,
            detail=f"quote_exc:{exc}",
        )

    if not q.route_ok or not q.out_amount_atomic:
        LOGGER.info(
            "event=position_valuation_failed mint=%s symbol=%s reason=no_route detail=jupiter_quote_unusable",
            mint,
            symbol,
        )
        return PositionValuation(
            None,
            None,
            None,
            VALUATION_STATUS_NO_ROUTE,
            bucket,
            detail="jupiter_quote_unusable",
        )

    out_lamports = int(q.out_amount_atomic)
    if out_lamports <= 0:
        LOGGER.info(
            "event=position_valuation_failed mint=%s symbol=%s reason=no_route detail=jupiter_zero_out",
            mint,
            symbol,
        )
        return PositionValuation(
            None,
            None,
            None,
            VALUATION_STATUS_NO_ROUTE,
            bucket,
            detail="jupiter_zero_out",
        )

    value_sol = out_lamports / 1_000_000_000.0
    qty_ui = max(float(position.remaining_qty_ui), 1e-30)
    mark_sol_per_token = value_sol / qty_ui
    if not _valid_positive(mark_sol_per_token) or not _valid_positive(value_sol):
        LOGGER.info(
            "event=position_valuation_failed mint=%s symbol=%s reason=no_route detail=nonpositive_mark",
            mint,
            symbol,
        )
        return PositionValuation(
            None,
            None,
            None,
            VALUATION_STATUS_NO_ROUTE,
            bucket,
            detail="nonpositive_mark",
        )

    return PositionValuation(
        value_sol,
        mark_sol_per_token,
        SOURCE_JUPITER_SELL,
        VALUATION_STATUS_OK,
        bucket,
        detail=None,
    )


def ensure_entry_sol_mark(position: PositionState) -> None:
    """Backfill entry_mark_sol_per_token for legacy state."""
    if position.entry_mark_sol_per_token > 0.0:
        return
    if position.entry_sol > 0.0 and position.remaining_qty_ui > 0.0:
        position.entry_mark_sol_per_token = position.entry_sol / max(position.remaining_qty_ui, 1e-30)
        if position.peak_mark_sol_per_token <= 0.0:
            position.peak_mark_sol_per_token = position.entry_mark_sol_per_token
        if position.last_mark_sol_per_token <= 0.0:
            position.last_mark_sol_per_token = position.entry_mark_sol_per_token
