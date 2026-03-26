from __future__ import annotations

from dataclasses import asdict, dataclass

from creeper_dripper.errors import POSITION_FINAL_ZOMBIE


@dataclass(frozen=True, slots=True)
class DerivedRuntimePolicy:
    runtime_risk_mode: str
    policy_posture: str
    policy_adjustments_applied: tuple[str, ...]
    policy_reason_summary: str

    effective_position_size_sol: float
    effective_max_open_positions: int
    effective_max_daily_new_positions: int
    effective_min_score: float
    effective_min_liquidity_usd: float
    effective_min_buy_sell_ratio: float
    entry_enabled: bool
    entries_blocked_reason: str | None

    wallet_pressure_level: str | None = None
    zombie_pressure_level: str | None = None
    deployable_pressure_level: str | None = None
    wallet_pressure_factor: float | None = None
    zombie_pressure_factor: float | None = None

    effective_final_zombie_recovery_probe_interval_cycles: int | None = None
    effective_exit_probe_aggressiveness: float | None = None
    effective_dripper_enabled: bool | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _normalize_risk_mode(raw: object) -> str:
    mode = str(raw or "").strip().lower()
    if mode in {"conservative", "balanced", "aggressive"}:
        return mode
    return "conservative"


def derive_runtime_policy(
    *,
    settings,
    portfolio,
    wallet_available_sol: float | None,
    deployable_sol: float | None,
    accounting_entries_blocked_reason: str | None,
    safe_mode_active: bool,
) -> DerivedRuntimePolicy:
    """
    Derived runtime policy layer.

    Compatibility: uses existing Settings values as defaults, then tightens or scales based on
    runtime conditions (wallet/deployable, zombies) without redesigning the engine.
    """
    risk_mode = _normalize_risk_mode(getattr(settings, "risk_mode", None) or getattr(settings, "RISK_MODE", None))
    base_size = float(getattr(settings, "base_position_size_sol", 0.0) or 0.0)
    max_size = float(getattr(settings, "max_position_size_sol", base_size) or base_size)
    min_order = float(getattr(settings, "min_order_size_sol", 0.0) or 0.0)

    # Default thresholds from Settings (compat mode).
    min_score = float(getattr(settings, "min_discovery_score", 0.0) or 0.0)
    min_liq = float(getattr(settings, "min_liquidity_usd", 0.0) or 0.0)
    min_bsr = float(getattr(settings, "min_buy_sell_ratio", 0.0) or 0.0)

    # Risk-mode tuning (small deltas; safe defaults).
    adjustments: list[str] = []
    if risk_mode == "conservative":
        min_score += 5.0
        min_liq *= 1.25 if min_liq > 0 else 1.0
        min_bsr *= 1.02 if min_bsr > 0 else 1.0
        size_scale = 0.8
        adjustments.append("risk_mode_conservative")
    elif risk_mode == "aggressive":
        min_score = max(0.0, min_score - 5.0)
        min_liq *= 0.9 if min_liq > 0 else 1.0
        min_bsr = max(0.0, min_bsr * 0.98) if min_bsr > 0 else 0.0
        size_scale = 1.1
        adjustments.append("risk_mode_aggressive")
    else:  # balanced
        size_scale = 1.0
        adjustments.append("risk_mode_balanced")

    # Start from operator intent (BASE_POSITION_SIZE_SOL) then clamp.
    size = max(0.0, min(max_size, base_size * size_scale))
    size = max(min_order, size) if min_order > 0 else size

    # Wallet pressure: if deployable is low relative to target size, shrink size conservatively.
    reasons: list[str] = []
    wallet_pressure: float | None = None
    deployable_pressure_level: str | None = None
    if deployable_sol is not None:
        dep = max(0.0, float(deployable_sol))
        if dep <= 0.0:
            wallet_pressure = 0.0
            size = 0.0
            deployable_pressure_level = "high"
            reasons.append("deployable_zero")
            adjustments.append("deployable_zero_size_to_zero")
        else:
            wallet_pressure = min(1.0, dep / max(size, 1e-9))
            if dep < (1.0 * size):
                deployable_pressure_level = "high"
            elif dep < (2.0 * size):
                deployable_pressure_level = "medium"
            else:
                deployable_pressure_level = "low"

            if dep < (2.0 * size):
                # Leave headroom for reserve and avoid thrashing when wallet is tight.
                size = max(min_order, min(size, dep * 0.5))
                reasons.append("wallet_low_shrink_size")
                adjustments.append("wallet_low_shrink_size")
    else:
        deployable_pressure_level = None

    # Zombie pressure: tighten caps when there are stuck positions.
    zombie_positions = sum(1 for p in portfolio.open_positions.values() if getattr(p, "status", None) == "ZOMBIE")
    final_zombies = sum(1 for p in portfolio.open_positions.values() if getattr(p, "status", None) == POSITION_FINAL_ZOMBIE)
    exit_blocked = sum(1 for p in portfolio.open_positions.values() if getattr(p, "status", None) == "EXIT_BLOCKED")
    exit_stuck_total = int(zombie_positions + final_zombies + exit_blocked)
    zombie_pressure: float | None = None
    zombie_pressure_level: str | None = None
    if exit_stuck_total > 0:
        zombie_pressure = min(1.0, exit_stuck_total / 5.0)
        if zombie_pressure >= 0.8:
            zombie_pressure_level = "high"
        elif zombie_pressure >= 0.4:
            zombie_pressure_level = "medium"
        else:
            zombie_pressure_level = "low"
        # Small tightening: reduce max open + daily new by 1 when any stuck positions exist.
        reasons.append("zombie_pressure_tighten_caps")
        adjustments.append("zombie_pressure_tighten_caps")
        # Tighten entry filters modestly under zombie pressure.
        min_score += 2.0
        min_liq *= 1.10 if min_liq > 0 else 1.0
        adjustments.append("zombie_pressure_tighten_filters")
    else:
        zombie_pressure_level = "none"

    # Use existing engine capacity as baseline; policy returns deltas only.
    baseline_max_open = int(getattr(settings, "max_open_positions", 0) or 0)
    baseline_max_daily = int(getattr(settings, "max_daily_new_positions", 0) or 0)
    effective_max_open = baseline_max_open
    effective_max_daily = baseline_max_daily
    if exit_stuck_total > 0:
        effective_max_open = max(0, effective_max_open - 1)
        effective_max_daily = max(0, effective_max_daily - 1)

    # Entry enabled gate.
    blocked_reason = None
    if safe_mode_active:
        blocked_reason = "safe_mode_active"
    elif accounting_entries_blocked_reason:
        blocked_reason = accounting_entries_blocked_reason
    elif wallet_available_sol is None:
        blocked_reason = "wallet_snapshot_missing"
    elif size <= 0.0:
        blocked_reason = "effective_size_zero"

    entry_enabled = blocked_reason is None
    if blocked_reason:
        reasons.append(f"entries_blocked:{blocked_reason}")

    # Pressure level summary (human-friendly).
    wallet_pressure_level: str | None = None
    if wallet_pressure is None:
        wallet_pressure_level = None
    elif wallet_pressure <= 0.25:
        wallet_pressure_level = "high"
    elif wallet_pressure <= 0.6:
        wallet_pressure_level = "medium"
    else:
        wallet_pressure_level = "low"

    constrained = (deployable_pressure_level in {"high", "medium"}) or (zombie_pressure_level in {"high", "medium"})
    if not entry_enabled:
        posture = "recovery_only"
    elif constrained:
        posture = "constrained"
    else:
        posture = risk_mode

    # Exit / recovery derived knobs (computed + exposed; only some are enforced by engine).
    base_final_zombie_interval = int(getattr(settings, "final_zombie_recovery_probe_interval_cycles", 360) or 360)
    base_final_zombie_interval = max(1, base_final_zombie_interval)
    interval = base_final_zombie_interval
    if zombie_pressure_level == "high":
        interval = min(24 * 3600, int(interval * 4))
        adjustments.append("final_zombie_probe_interval_x4")
    elif zombie_pressure_level == "medium":
        interval = min(24 * 3600, int(interval * 2))
        adjustments.append("final_zombie_probe_interval_x2")
    if deployable_pressure_level == "high":
        interval = min(24 * 3600, int(interval * 2))
        adjustments.append("final_zombie_probe_interval_wallet_x2")

    exit_aggr = 1.0
    if posture in {"constrained", "recovery_only"}:
        exit_aggr = 0.6
    if zombie_pressure_level == "high":
        exit_aggr = min(exit_aggr, 0.5)

    dripper_enabled = bool(getattr(settings, "hachi_dripper_enabled", False))

    summary = ",".join(reasons) if reasons else "ok"
    return DerivedRuntimePolicy(
        runtime_risk_mode=risk_mode,
        policy_posture=posture,
        policy_adjustments_applied=tuple(adjustments),
        policy_reason_summary=summary,
        effective_position_size_sol=float(size),
        effective_max_open_positions=int(effective_max_open),
        effective_max_daily_new_positions=int(effective_max_daily),
        effective_min_score=float(min_score),
        effective_min_liquidity_usd=float(min_liq),
        effective_min_buy_sell_ratio=float(min_bsr),
        entry_enabled=bool(entry_enabled),
        entries_blocked_reason=blocked_reason,
        wallet_pressure_level=wallet_pressure_level,
        zombie_pressure_level=zombie_pressure_level,
        deployable_pressure_level=deployable_pressure_level,
        wallet_pressure_factor=wallet_pressure,
        zombie_pressure_factor=zombie_pressure,
        effective_final_zombie_recovery_probe_interval_cycles=int(interval),
        effective_exit_probe_aggressiveness=float(exit_aggr),
        effective_dripper_enabled=bool(dripper_enabled),
    )

