"""Hachi dripper decision brain.

Classifies each open position into a PnL zone and a momentum state, then maps
(zone, momentum) → urgency level that drives chunk sizing and wait-time
scaling.  Pure functions — no I/O, no state mutation.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from creeper_dripper.config import Settings
    from creeper_dripper.models import PositionState

# ---------------------------------------------------------------------------
# PnL-zone labels
# ---------------------------------------------------------------------------
ZONE_PROFIT_HARVEST = "profit_harvest"   # pnl >= hachi_profit_harvest_min_pct
ZONE_NEUTRAL = "neutral"                 # hachi_neutral_floor_pct <= pnl < profit_harvest
ZONE_DETERIORATION = "deterioration"     # hachi_emergency_pnl_pct <= pnl < neutral_floor
ZONE_EMERGENCY = "emergency"             # pnl < hachi_emergency_pnl_pct → full exit

# ---------------------------------------------------------------------------
# Momentum-state labels (cycle-over-cycle mark movement)
# ---------------------------------------------------------------------------
MOM_IMPROVING = "improving"    # last_mark > previous_mark
MOM_FLAT = "flat"              # negligible change (or no baseline)
MOM_WEAKENING = "weakening"    # moderate drop (>= weakening_drop_pct)
MOM_COLLAPSING = "collapsing"  # severe drop (>= collapse_drop_pct)

# ---------------------------------------------------------------------------
# Urgency levels → drive chunk size and inter-chunk wait
# ---------------------------------------------------------------------------
URGENCY_CONSERVATIVE = "conservative"  # smallest chunk, longer wait
URGENCY_NORMAL = "normal"              # near-equal largest chunk, standard wait
URGENCY_AGGRESSIVE = "aggressive"      # largest chunk, shorter wait
URGENCY_OVERRIDE_FULL = "override_full"  # sell entire remaining qty immediately


# ---------------------------------------------------------------------------
# Core classification helpers
# ---------------------------------------------------------------------------


def compute_pnl_pct(position: "PositionState") -> float | None:
    """Return PnL% from SOL entry vs current mark.  None when marks are invalid."""
    entry = float(position.entry_mark_sol_per_token)
    last = float(position.last_mark_sol_per_token)
    if entry <= 0 or last <= 0:
        return None
    return (last / entry - 1.0) * 100.0


def classify_pnl_zone(pnl_pct: float, settings: "Settings") -> str:
    """Map numeric PnL% to a zone label using settings thresholds."""
    if pnl_pct >= settings.hachi_profit_harvest_min_pct:
        return ZONE_PROFIT_HARVEST
    if pnl_pct >= settings.hachi_neutral_floor_pct:
        return ZONE_NEUTRAL
    if pnl_pct >= settings.hachi_emergency_pnl_pct:
        return ZONE_DETERIORATION
    return ZONE_EMERGENCY


def classify_momentum(position: "PositionState", settings: "Settings") -> str:
    """Compare current mark vs previous-cycle mark to infer trend direction.

    Returns MOM_FLAT when no previous baseline is available (first cycle).
    """
    prev = float(position.previous_mark_sol_per_token)
    last = float(position.last_mark_sol_per_token)
    if prev <= 0 or last <= 0:
        return MOM_FLAT
    drop_pct = (last / prev - 1.0) * 100.0
    if drop_pct <= -settings.hachi_collapse_drop_pct:
        return MOM_COLLAPSING
    if drop_pct <= -settings.hachi_weakening_drop_pct:
        return MOM_WEAKENING
    if drop_pct > 0:
        return MOM_IMPROVING
    return MOM_FLAT


# ---------------------------------------------------------------------------
# Policy mapping
# ---------------------------------------------------------------------------

#: (zone, momentum) → urgency.  Table encodes the full 4×4 decision grid.
#  Logic:
#   - emergency → always override (full exit)
#   - deterioration + collapsing → override (too late for small chunks)
#   - deterioration otherwise → aggressive (drain fast)
#   - neutral + collapsing → aggressive (may be tipping into deterioration)
#   - neutral + weakening → normal (monitor but keep moving)
#   - neutral otherwise → conservative (preserve gains, don't dump)
#   - profit_harvest + weakening/collapsing → aggressive (lock profits before reversal)
#   - profit_harvest + flat → normal
#   - profit_harvest + improving → conservative (let winners run a little)
_URGENCY_TABLE: dict[tuple[str, str], str] = {
    (ZONE_EMERGENCY, MOM_IMPROVING): URGENCY_OVERRIDE_FULL,
    (ZONE_EMERGENCY, MOM_FLAT): URGENCY_OVERRIDE_FULL,
    (ZONE_EMERGENCY, MOM_WEAKENING): URGENCY_OVERRIDE_FULL,
    (ZONE_EMERGENCY, MOM_COLLAPSING): URGENCY_OVERRIDE_FULL,
    (ZONE_DETERIORATION, MOM_IMPROVING): URGENCY_AGGRESSIVE,
    (ZONE_DETERIORATION, MOM_FLAT): URGENCY_AGGRESSIVE,
    (ZONE_DETERIORATION, MOM_WEAKENING): URGENCY_AGGRESSIVE,
    (ZONE_DETERIORATION, MOM_COLLAPSING): URGENCY_OVERRIDE_FULL,
    (ZONE_NEUTRAL, MOM_IMPROVING): URGENCY_CONSERVATIVE,
    (ZONE_NEUTRAL, MOM_FLAT): URGENCY_CONSERVATIVE,
    (ZONE_NEUTRAL, MOM_WEAKENING): URGENCY_NORMAL,
    (ZONE_NEUTRAL, MOM_COLLAPSING): URGENCY_AGGRESSIVE,
    (ZONE_PROFIT_HARVEST, MOM_IMPROVING): URGENCY_CONSERVATIVE,
    (ZONE_PROFIT_HARVEST, MOM_FLAT): URGENCY_NORMAL,
    (ZONE_PROFIT_HARVEST, MOM_WEAKENING): URGENCY_AGGRESSIVE,
    (ZONE_PROFIT_HARVEST, MOM_COLLAPSING): URGENCY_AGGRESSIVE,
}


def select_urgency(pnl_zone: str, momentum: str) -> str:
    """Look up urgency from policy table; fall back to AGGRESSIVE on unknown keys."""
    return _URGENCY_TABLE.get((pnl_zone, momentum), URGENCY_AGGRESSIVE)


def override_reason(pnl_zone: str, momentum: str) -> str:
    """Return a descriptive exit-reason string for URGENCY_OVERRIDE_FULL triggers."""
    if pnl_zone == ZONE_EMERGENCY:
        return "hachi_pnl_emergency"
    if momentum == MOM_COLLAPSING:
        return "hachi_momentum_collapse"
    return "hachi_pnl_emergency"  # fallback


# ---------------------------------------------------------------------------
# Chunk-size application
# ---------------------------------------------------------------------------


def apply_urgency_to_chunk(
    urgency: str,
    candidates: list[tuple[int, float, float | None]],  # (qty, sol_out_per_token, impact_bps)
    remaining: int,
    settings: "Settings",
) -> tuple[int | None, str]:
    """Return ``(chosen_qty, selection_reason)`` for the given urgency level.

    *conservative* → smallest viable chunk (harvest slowly)
    *normal*       → largest chunk within near-equal efficiency band (existing logic)
    *aggressive*   → largest viable chunk (drain fast)
    *override_full* → entire remaining qty (caller handles hard-exit path)
    """
    if urgency == URGENCY_OVERRIDE_FULL:
        return remaining, "urgency_override_full"

    if not candidates:
        return None, "no_viable_candidates"

    if urgency == URGENCY_CONSERVATIVE:
        return min(qty for qty, _, _ in candidates), "urgency_conservative_smallest"

    if urgency == URGENCY_AGGRESSIVE:
        return max(qty for qty, _, _ in candidates), "urgency_aggressive_largest"

    # NORMAL: largest chunk within near-equal-efficiency band (original behaviour)
    best_eff = max(e for _, e, _ in candidates)
    threshold = best_eff * (1.0 - settings.drip_near_equal_band)
    near_equal = [qty for qty, eff, _ in candidates if eff >= threshold]
    return max(near_equal), "urgency_normal_near_equal"


def chunk_wait_seconds(urgency: str, base_wait: int) -> int:
    """Scale next-chunk wait time based on urgency level.

    conservative → +50 % longer (harvest slowly)
    normal       → unchanged
    aggressive   → halved, min 10 s (get out fast)
    """
    if urgency == URGENCY_CONSERVATIVE:
        return max(base_wait, int(base_wait * 1.5))
    if urgency == URGENCY_AGGRESSIVE:
        return max(10, base_wait // 2)
    return base_wait
