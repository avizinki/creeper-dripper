from __future__ import annotations

from dataclasses import asdict, dataclass

from creeper_dripper.errors import POSITION_FINAL_ZOMBIE


@dataclass(frozen=True, slots=True)
class DerivedRuntimePolicy:
    runtime_risk_mode: str
    effective_position_size_sol: float
    effective_max_open_positions: int
    effective_max_daily_new_positions: int
    effective_min_score: float
    effective_min_liquidity_usd: float
    entry_enabled: bool
    entries_blocked_reason: str | None
    policy_reason_summary: str
    wallet_pressure_factor: float | None = None
    zombie_pressure_factor: float | None = None

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

    # Risk-mode tuning (small deltas; safe defaults).
    if risk_mode == "conservative":
        min_score += 5.0
        min_liq *= 1.25 if min_liq > 0 else 1.0
        size_scale = 0.8
    elif risk_mode == "aggressive":
        min_score = max(0.0, min_score - 5.0)
        min_liq *= 0.9 if min_liq > 0 else 1.0
        size_scale = 1.1
    else:  # balanced
        size_scale = 1.0

    # Start from operator intent (BASE_POSITION_SIZE_SOL) then clamp.
    size = max(0.0, min(max_size, base_size * size_scale))
    size = max(min_order, size) if min_order > 0 else size

    # Wallet pressure: if deployable is low relative to target size, shrink size conservatively.
    reasons: list[str] = []
    wallet_pressure: float | None = None
    if deployable_sol is not None:
        dep = max(0.0, float(deployable_sol))
        if dep <= 0.0:
            wallet_pressure = 0.0
            size = 0.0
            reasons.append("deployable_zero")
        else:
            wallet_pressure = min(1.0, dep / max(size, 1e-9))
            if dep < (2.0 * size):
                # Leave headroom for reserve and avoid thrashing when wallet is tight.
                size = max(min_order, min(size, dep * 0.5))
                reasons.append("wallet_low_shrink_size")

    # Zombie pressure: tighten caps when there are stuck positions.
    zombie_positions = sum(1 for p in portfolio.open_positions.values() if getattr(p, "status", None) == "ZOMBIE")
    final_zombies = sum(1 for p in portfolio.open_positions.values() if getattr(p, "status", None) == POSITION_FINAL_ZOMBIE)
    exit_blocked = sum(1 for p in portfolio.open_positions.values() if getattr(p, "status", None) == "EXIT_BLOCKED")
    exit_stuck_total = int(zombie_positions + final_zombies + exit_blocked)
    zombie_pressure: float | None = None
    if exit_stuck_total > 0:
        zombie_pressure = min(1.0, exit_stuck_total / 5.0)
        # Small tightening: reduce max open + daily new by 1 when any stuck positions exist.
        reasons.append("zombie_pressure_tighten_caps")

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

    summary = ",".join(reasons) if reasons else "ok"
    return DerivedRuntimePolicy(
        runtime_risk_mode=risk_mode,
        effective_position_size_sol=float(size),
        effective_max_open_positions=int(effective_max_open),
        effective_max_daily_new_positions=int(effective_max_daily),
        effective_min_score=float(min_score),
        effective_min_liquidity_usd=float(min_liq),
        entry_enabled=bool(entry_enabled),
        entries_blocked_reason=blocked_reason,
        policy_reason_summary=summary,
        wallet_pressure_factor=wallet_pressure,
        zombie_pressure_factor=zombie_pressure,
    )

