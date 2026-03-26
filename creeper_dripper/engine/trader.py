from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from creeper_dripper.clients.birdeye import BirdeyeClient
from creeper_dripper.cache import TTLCache
from creeper_dripper.config import SOL_MINT, Settings
from creeper_dripper.errors import (
    EXEC_EXECUTE_FAILED,
    EXEC_EXECUTE_UNKNOWN,
    EXEC_NO_ROUTE,
    EXEC_ORDER_FAILED,
    EXEC_PROVIDER_UNAVAILABLE,
    EXEC_QUOTE_FAILED,
    EXEC_SKIPPED_DRY_RUN,
    EXEC_SKIPPED_LIVE_DISABLED,
    EXEC_SELL_PROCEEDS_UNAVAILABLE,
    EXEC_TX_BUILD_FAILED,
    EXEC_TX_CONFIRMED_FAILED,
    EXEC_TX_SEND_FAILED,
    EXEC_TX_SIGN_FAILED,
    EXEC_V2_EXECUTE_FAILED,
    EXEC_V2_ORDER_BUILD_FAILED,
    EXEC_V2_SIGN_FAILED,
    EXIT_UNKNOWN_PENDING_RECONCILE,
    JOURNAL_APPEND_FAILED,
    POSITION_FINAL_ZOMBIE,
    POSITION_RECONCILE_PENDING,
    SETTLEMENT_UNCONFIRMED,
    REJECT_ECONOMIC_SANITY_FAILED,
    REJECT_EXECUTION_ROUTE_MISSING,
    REJECT_NO_BUY_ROUTE,
    REJECT_NO_SELL_ROUTE,
    REJECT_QUOTE_OUTPUT_TOO_LOW,
    REJECT_QUOTE_PRICE_IMPACT_INVALID,
    SAFETY_DAILY_LOSS_CAP,
    SAFETY_MAX_CONSEC_EXEC_FAILURES,
    SAFETY_MAX_EXIT_BLOCKED,
    SAFETY_STALE_MARKET_DATA,
    SAFETY_UNKNOWN_EXIT_SATURATION,
    SELL_THRESHOLD_UNCOMPUTABLE,
    STATE_SAVE_FAILED,
)
from creeper_dripper.engine.discovery import discover_candidates
from creeper_dripper.engine.hachi_brain import (
    URGENCY_OVERRIDE_FULL,
    apply_urgency_to_chunk,
    chunk_wait_seconds,
    classify_momentum,
    classify_pnl_zone,
    compute_pnl_pct,
    override_reason,
    select_urgency,
)
from creeper_dripper.engine.position_pricing import (
    SOURCE_JUPITER_SELL,
    VALUATION_STATUS_NO_ROUTE,
    VALUATION_STATUS_OK,
    ensure_entry_sol_mark,
    extract_sell_quote_liquidity,
    is_valid_sol_mark,
    resolve_position_valuation,
)
from creeper_dripper.engine.runtime_policy import (
    DerivedRuntimePolicy,
    derive_age_band,
    derive_runtime_policy,
    liquidity_floor_for_age_band,
    survivability_required_buckets,
)
from creeper_dripper.execution.drip_chunker import select_drip_chunk
from creeper_dripper.execution.executor import TradeExecutor
from creeper_dripper.models import PortfolioState, PositionState, ProbeQuote, TakeProfitStep, TokenCandidate, TradeDecision
from creeper_dripper.observability import EventCollector
from creeper_dripper.storage.recovery import run_startup_recovery
from creeper_dripper.storage.state import save_portfolio, save_status_snapshot
from creeper_dripper.utils import append_jsonl, atomic_write_json, utc_now_iso

LOGGER = logging.getLogger(__name__)
MAX_EXIT_RETRIES = 5


def _is_system_execution_failure(classification: str | None) -> bool:
    """Return True only for system/execution failures (not market conditions)."""
    c = str(classification or "")
    if not c:
        return True
    if c in {EXEC_NO_ROUTE, SELL_THRESHOLD_UNCOMPUTABLE}:
        return False
    # Treat probe-only rejections as market conditions.
    if c.startswith("reject_"):
        return False
    if c in {EXEC_SKIPPED_DRY_RUN, EXEC_SKIPPED_LIVE_DISABLED}:
        return False
    return c in {
        EXEC_PROVIDER_UNAVAILABLE,
        EXEC_QUOTE_FAILED,
        EXEC_ORDER_FAILED,
        EXEC_EXECUTE_FAILED,
        EXEC_EXECUTE_UNKNOWN,
        EXEC_TX_BUILD_FAILED,
        EXEC_TX_SIGN_FAILED,
        EXEC_TX_SEND_FAILED,
        EXEC_TX_CONFIRMED_FAILED,
        EXEC_V2_ORDER_BUILD_FAILED,
        EXEC_V2_SIGN_FAILED,
        EXEC_V2_EXECUTE_FAILED,
        EXEC_SELL_PROCEEDS_UNAVAILABLE,
        SETTLEMENT_UNCONFIRMED,
    }

def _seed_sol_basis_on_open(
    position: PositionState,
    candidate: TokenCandidate,
    entry_sol_spent: float,
    qty_ui: float,
) -> None:
    """Primary cost basis: SOL per token at entry. USD fields are optional display only."""
    denom_ui = max(float(qty_ui), 1e-30)
    mark = float(entry_sol_spent) / denom_ui
    position.entry_mark_sol_per_token = mark
    position.last_mark_sol_per_token = mark
    position.peak_mark_sol_per_token = mark
    pu = candidate.price_usd
    try:
        uf = float(pu) if pu is not None else 0.0
    except (TypeError, ValueError):
        uf = 0.0
    if uf > 0.0:
        position.entry_price_usd = uf
        position.avg_entry_price_usd = uf
        position.last_price_usd = uf
        position.peak_price_usd = uf
        position.usd_mark_unavailable = False
    else:
        position.entry_price_usd = 0.0
        position.avg_entry_price_usd = 0.0
        position.last_price_usd = 0.0
        position.peak_price_usd = 0.0
        position.usd_mark_unavailable = True


EXIT_RETRY_BASE_SECONDS = 30
# Defensive bound for exponential backoff scheduling. This must never overflow datetime/timedelta.
# Kept intentionally conservative: long enough to avoid busy retries, short enough to remain observable.
MAX_EXIT_RETRY_DELAY_SECONDS = 60 * 60 * 24  # 24h
# Cap stored retry_count so it cannot grow without bound on terminally stuck positions.
MAX_EXIT_RETRY_COUNT = 256
# --- cash_sol SOURCE OF TRUTH ---
# cash_sol (PortfolioState.cash_sol) is the INTERNAL accounting ledger. It is the sole authoritative
# value used for entry capacity decisions and reserve enforcement.
#
# Invariants:
#   1. cash_sol is decremented on every confirmed-or-pending entry (buy), at the moment of execution.
#   2. cash_sol is incremented on every successful sell exit, using output_amount when available,
#      or the pre-execution sell quote (conservative lower-bound) when output_amount is absent.
#   3. cash_sol is NEVER written by the wallet RPC snapshot (_wallet_available_sol), which is a
#      visibility-only value used only for dynamic capacity scaling and observability.
#   4. Drift between cash_sol and the real wallet SOL balance is expected and bounded:
#      - Downward drift: sell proceeds estimated from quote (pessimistic), tx fees not modelled.
#      - Upward drift: unconfirmed-buy debit if tx later fails (see pending_proceeds_sol marker).
#   5. PositionState.pending_proceeds_sol > 0 flags a RECONCILE_PENDING(entry) position where
#      cash_sol was already debited but on-chain confirmation is not yet available.
# ---------------------------------
MIN_ROUNDTRIP_RETURN_RATIO = 0.02
MAX_ABS_PRICE_IMPACT_BPS = 5_000.0


def _seed_jsds_entry_baseline(position: PositionState, sell_probe: ProbeQuote) -> None:
    impact, hops, label = extract_sell_quote_liquidity(sell_probe)
    position.entry_sell_impact_bps = impact
    position.entry_sell_route_hops = hops
    position.entry_sell_route_label = label
    position.last_sell_impact_bps = impact
    position.last_sell_route_hops = hops
    position.last_sell_route_label = label
    position.quote_miss_streak = 0


class CreeperDripper:
    def __init__(
        self,
        settings: Settings,
        birdeye: BirdeyeClient,
        executor: TradeExecutor,
        portfolio: PortfolioState,
    ) -> None:
        self.settings = settings
        self.birdeye = birdeye
        self.executor = executor
        self.portfolio = portfolio
        self._startup_recovery_done = False
        self.events = EventCollector()
        self._safety_diagnostics: dict | None = None
        self._last_discovery_at: datetime | None = None
        self._last_discovery_candidates: list[TokenCandidate] = []
        self._last_discovery_summary: dict = {
            "seeds_total": 0,
            "candidates_built": 0,
            "candidates_accepted": 0,
            "candidates_rejected_total": 0,
            "rejection_counts": {},
        }
        self._candidate_cache = TTLCache[TokenCandidate](settings.candidate_cache_ttl_seconds)
        self._route_cache = TTLCache[ProbeQuote](settings.route_check_cache_ttl_seconds)
        self._cycle_in_run = 0
        # Wallet snapshot (visibility/bootstrap only; never settlement truth).
        self._wallet_available_sol: float | None = None
        self._wallet_snapshot_at: str | None = None
        # Per-cycle accounting guard (entries only).
        self._entries_blocked_reason: str | None = None
        self._last_deployable_sol: float | None = None
        self._last_accounting_drift_sol: float | None = None
        self._last_reconciled_cash_sol: float | None = None
        self._last_reconciliation_applied: bool = False
        self._last_reconciliation_delta_sol: float | None = None
        self._startup_wallet_drift_detected: bool = False
        self._last_runtime_policy: DerivedRuntimePolicy | None = None
        self._effective_discovery_interval_seconds: int | None = None
        if self.settings.run_id:
            self.portfolio.run_id = self.settings.run_id
        LOGGER.info(
            "cache_engine_init: candidate_cache_id=%s route_cache_id=%s candidate_ttl_s=%s route_ttl_s=%s",
            id(self._candidate_cache),
            id(self._route_cache),
            settings.candidate_cache_ttl_seconds,
            settings.route_check_cache_ttl_seconds,
        )

    def _compute_deployable_sol(self) -> float:
        cash_sol = float(self.portfolio.cash_sol)
        reserve = float(self.settings.cash_reserve_sol)
        wallet_available_sol = self._wallet_available_sol
        if wallet_available_sol is None:
            return 0.0
        return max(0.0, min(cash_sol, float(wallet_available_sol)) - reserve)

    def _pending_proceeds_total_sol(self) -> float:
        total = 0.0
        for p in self.portfolio.open_positions.values():
            try:
                pending = float(getattr(p, "pending_proceeds_sol", 0.0) or 0.0)
            except Exception:
                pending = 0.0
            if pending > 0.0:
                total += pending
        return max(0.0, float(total))

    def _reconciled_cash_from_wallet(self, wallet_total_sol: float) -> float:
        pending_proceeds = self._pending_proceeds_total_sol()
        reconciled_cash_sol = float(wallet_total_sol) - float(pending_proceeds)
        return max(0.0, min(float(reconciled_cash_sol), float(wallet_total_sol)))

    def _maybe_reconcile_cash_sol(self, *, now: str, trigger: str) -> bool:
        """
        Reconcile internal cash_sol against a visibility-only wallet snapshot.

        Purpose: eliminate dangerous upward drift that can freeze the bot (T-013) or allow unsafe entries.
        Hard rules:
        - cash_sol must never exceed wallet_total_sol
        - cash_sol must never be negative
        """
        self._last_reconciliation_applied = False
        self._last_reconciliation_delta_sol = None
        self._last_reconciled_cash_sol = None

        wallet_available_sol = self._wallet_available_sol
        if wallet_available_sol is None:
            return False

        wallet_total_sol = float(wallet_available_sol)
        old_cash = float(self.portfolio.cash_sol)
        new_cash = self._reconciled_cash_from_wallet(wallet_total_sol)
        self._last_reconciled_cash_sol = float(new_cash)
        delta = float(new_cash) - float(old_cash)

        epsilon = float(getattr(self.settings, "accounting_drift_epsilon_sol", 0.001) or 0.001)
        if abs(delta) <= epsilon:
            return False

        self.portfolio.cash_sol = float(new_cash)
        self._last_reconciliation_applied = True
        self._last_reconciliation_delta_sol = float(delta)
        self.events.emit(
            "accounting_reconciled",
            trigger,
            old_cash_sol=old_cash,
            new_cash_sol=new_cash,
            wallet_available_sol=wallet_total_sol,
            pending_proceeds_sol=self._pending_proceeds_total_sol(),
            drift_fixed=delta,
        )
        return True

    def _update_entry_accounting_guard(self) -> None:
        """Compute per-cycle entry guard + observability fields (entries only)."""
        self._entries_blocked_reason = None
        cash_sol = float(self.portfolio.cash_sol)
        wallet_available_sol = self._wallet_available_sol
        drift: float | None = None
        if wallet_available_sol is not None:
            drift = cash_sol - float(wallet_available_sol)
        self._last_accounting_drift_sol = drift
        self._last_deployable_sol = self._compute_deployable_sol()

        if wallet_available_sol is None:
            self._entries_blocked_reason = "blocked_wallet_snapshot_missing"
            return

        epsilon = float(getattr(self.settings, "accounting_drift_epsilon_sol", 0.001) or 0.001)
        # If startup snapshot detected an upward drift, block entries for at least one cycle
        # even if reconciliation already clamped cash_sol. This avoids immediately probing buys
        # on the same run-cycle where accounting correctness just changed.
        if self._startup_wallet_drift_detected:
            self._entries_blocked_reason = "blocked_accounting_drift_cash_gt_wallet"
            self._startup_wallet_drift_detected = False
            return
        if drift is not None and drift > epsilon:
            reconciled = self._maybe_reconcile_cash_sol(now=utc_now_iso(), trigger="cycle_drift_over_epsilon")
            if reconciled:
                self._entries_blocked_reason = "blocked_accounting_reconciled_this_cycle"
            else:
                self._entries_blocked_reason = "blocked_accounting_drift_cash_gt_wallet"
            # Refresh observability after reconciliation attempt.
            cash_sol = float(self.portfolio.cash_sol)
            drift = cash_sol - float(wallet_available_sol)
            self._last_accounting_drift_sol = drift
            self._last_deployable_sol = self._compute_deployable_sol()

    def set_wallet_snapshot(self, *, available_sol: float | None, snapshot_at: str) -> None:
        """Store visibility-only wallet snapshot for dynamic capacity decisions."""
        self._wallet_available_sol = None if available_sol is None else float(available_sol)
        self._wallet_snapshot_at = str(snapshot_at or "") or None
        try:
            if self._wallet_available_sol is not None:
                epsilon = float(getattr(self.settings, "accounting_drift_epsilon_sol", 0.001) or 0.001)
                if (float(self.portfolio.cash_sol) - float(self._wallet_available_sol)) > epsilon:
                    self._startup_wallet_drift_detected = True
        except Exception:
            self._startup_wallet_drift_detected = False
        # Startup reconciliation: after wallet snapshot becomes available, clamp cash_sol to a
        # truthful upper bound. Never runs without a wallet snapshot.
        self._maybe_reconcile_cash_sol(now=str(snapshot_at or "") or utc_now_iso(), trigger="startup_wallet_snapshot")

    def _effective_max_open_positions(self) -> int:
        """
        Derive dynamic max open positions from Hachi birth baseline + current wallet snapshot.

        Invariants:
        - reserve is always respected (deployable_sol = max(0, available_sol - cash_reserve_sol))
        - hard cap is always respected (<= HARD_MAX_OPEN_POSITIONS)
        - dynamic capacity affects entries only (exits/recovery are unchanged)
        - per-position sizing caps remain enforced elsewhere (BASE/MAX_POSITION_SIZE_SOL, MIN_ORDER_SIZE_SOL)

        This is visibility/bootstrap only: it never treats wallet balances as settlement truth.
        """
        baseline = int(self.settings.max_open_positions)
        hard_cap = int(getattr(self.settings, "hard_max_open_positions", baseline) or baseline)
        if not getattr(self.settings, "dynamic_capacity_enabled", True):
            return min(baseline, hard_cap)

        wallet_available_sol = self._wallet_available_sol
        if wallet_available_sol is None:
            available_sol = 0.0
        else:
            available_sol = min(float(self.portfolio.cash_sol), float(wallet_available_sol))
        reserve = float(self.settings.cash_reserve_sol)
        deployable = max(0.0, available_sol - reserve)

        birth_sol = getattr(self.settings, "hachi_birth_wallet_sol", None) or getattr(self.portfolio, "hachi_birth_wallet_sol", None)
        dynamic_from_birth = baseline
        try:
            if birth_sol is not None and float(birth_sol) > 0:
                scale = float(available_sol) / float(birth_sol)
                dynamic_from_birth = max(1, int(baseline * scale))
        except Exception:
            dynamic_from_birth = baseline

        denom = max(float(self.settings.base_position_size_sol), float(self.settings.min_order_size_sol), 1e-9)
        funding_cap = int(deployable / denom) if deployable > 0 else 0
        effective = min(hard_cap, max(baseline, min(dynamic_from_birth, funding_cap)))
        return max(0, int(effective))

    def _effective_max_daily_new_positions(self) -> int:
        """
        Derive max new entries per day from the same birth-baseline + deployable model as open slots.

        Invariants:
        - HARD_MAX_DAILY_NEW_POSITIONS is never exceeded
        - cannot exceed effective max open positions (cannot meaningfully exceed total slot count)
        - with deployable > 0, at least 1 new entry/day when dynamic capacity is enabled
        - when dynamic capacity is disabled or wallet snapshot unavailable, falls back to static MAX_DAILY_NEW_POSITIONS
          (same as using baseline + hard cap without birth scaling)
        """
        baseline = int(self.settings.max_daily_new_positions)
        hard_cap = int(getattr(self.settings, "hard_max_daily_new_positions", baseline) or baseline)
        if not getattr(self.settings, "dynamic_capacity_enabled", True):
            return min(baseline, hard_cap)

        wallet_available_sol = self._wallet_available_sol
        if wallet_available_sol is None:
            available_sol = 0.0
        else:
            available_sol = min(float(self.portfolio.cash_sol), float(wallet_available_sol))
        reserve = float(self.settings.cash_reserve_sol)
        deployable = max(0.0, available_sol - reserve)

        birth_sol = getattr(self.settings, "hachi_birth_wallet_sol", None) or getattr(self.portfolio, "hachi_birth_wallet_sol", None)
        dynamic_from_birth = baseline
        try:
            if birth_sol is not None and float(birth_sol) > 0:
                scale = float(available_sol) / float(birth_sol)
                dynamic_from_birth = max(1, int(baseline * scale))
        except Exception:
            dynamic_from_birth = baseline

        denom = max(float(self.settings.base_position_size_sol), float(self.settings.min_order_size_sol), 1e-9)
        funding_cap = int(deployable / denom) if deployable > 0 else 0

        effective_open = self._effective_max_open_positions()
        effective = min(hard_cap, max(baseline, min(dynamic_from_birth, funding_cap)))
        effective = min(effective, effective_open)
        effective = max(0, int(effective))
        if deployable > 0:
            effective = min(hard_cap, max(1, effective))
        return effective

    def run_cycle(self) -> dict:
        now = utc_now_iso()
        self._cycle_in_run += 1
        self._reset_daily_counters(now)
        decisions: list[TradeDecision] = []
        if not self._startup_recovery_done:
            recovery_decisions = run_startup_recovery(self.portfolio, self.executor, now)
            decisions.extend(recovery_decisions)
            for decision in recovery_decisions:
                self.events.emit("recovery_action", decision.reason, action=decision.action, token_mint=decision.token_mint)
            self._startup_recovery_done = True
        # Compute a pre-discovery policy to drive discovery cadence from current runtime pressures.
        self._update_entry_accounting_guard()
        pre_policy = derive_runtime_policy(
            settings=self.settings,
            portfolio=self.portfolio,
            wallet_available_sol=self._wallet_available_sol,
            deployable_sol=self._last_deployable_sol,
            accounting_entries_blocked_reason=self._entries_blocked_reason,
            safe_mode_active=bool(self.portfolio.safe_mode_active),
        )
        self._effective_discovery_interval_seconds = int(pre_policy.effective_discovery_interval_seconds or self.settings.discovery_interval_seconds)
        self.events.emit(
            "discovery_cadence_summary",
            "ok",
            effective_discovery_interval_seconds=self._effective_discovery_interval_seconds,
            discovery_cadence_reason=pre_policy.discovery_cadence_reason,
            policy_posture=pre_policy.policy_posture,
        )

        try:
            candidates, discovery_summary = self._discover_with_cadence()
        except Exception as exc:
            LOGGER.error("event=discovery_failed error_type=%s error=%s", type(exc).__name__, exc, exc_info=True)
            candidates = []
            discovery_summary = self._failed_discovery_summary()
            discovery_summary["discovery_failed"] = True
            discovery_summary["discovery_error_type"] = type(exc).__name__
            discovery_summary["discovery_error"] = str(exc)
            self.events.emit(
                "discovery_failed",
                "exception",
                error_type=discovery_summary["discovery_error_type"],
                error=discovery_summary["discovery_error"],
            )
        _checked_at_raw = discovery_summary.get("market_data_checked_at")
        if not _checked_at_raw:
            # Discovery failed — use last known successful timestamp so stale-data gate fires correctly.
            # Never substitute now() here: that would mask a discovery outage as "data is fresh".
            _checked_at_raw = self._last_discovery_summary.get("market_data_checked_at")
        market_data_checked_at = str(_checked_at_raw) if _checked_at_raw else None
        safe_mode_reason = self._evaluate_safety(now, market_data_checked_at=market_data_checked_at)
        if safe_mode_reason:
            self.portfolio.safe_mode_active = True
            self.portfolio.safety_stop_reason = safe_mode_reason
            metadata = {"open_positions": len(self.portfolio.open_positions)}
            if safe_mode_reason == SAFETY_STALE_MARKET_DATA and self._safety_diagnostics:
                metadata.update(self._safety_diagnostics)
            self.events.emit("safety_stop", safe_mode_reason, **metadata)
        else:
            self.portfolio.safe_mode_active = False
            self.portfolio.safety_stop_reason = None

        self._last_runtime_policy = derive_runtime_policy(
            settings=self.settings,
            portfolio=self.portfolio,
            wallet_available_sol=self._wallet_available_sol,
            deployable_sol=self._last_deployable_sol,
            accounting_entries_blocked_reason=self._entries_blocked_reason,
            safe_mode_active=bool(self.portfolio.safe_mode_active),
        )
        if self._entries_blocked_reason:
            self.events.emit(
                "entries_blocked",
                self._entries_blocked_reason,
                reason=self._entries_blocked_reason,
                cash_sol=self.portfolio.cash_sol,
                wallet_available_sol=self._wallet_available_sol,
                accounting_drift_sol=self._last_accounting_drift_sol,
                deployable_sol=self._last_deployable_sol,
                cash_reserve_sol=self.settings.cash_reserve_sol,
                wallet_snapshot_at=self._wallet_snapshot_at,
                reconciled_cash_sol=self._last_reconciled_cash_sol,
                reconciliation_applied=self._last_reconciliation_applied,
                reconciliation_delta_sol=self._last_reconciliation_delta_sol,
            )

        if (
            not self.portfolio.safe_mode_active
            and not self._entries_blocked_reason
            and self._last_runtime_policy is not None
            and self._last_runtime_policy.entry_enabled
        ):
            self._maybe_open_positions(candidates, decisions, now)
        self._mark_positions(candidates, decisions, now)
        self.portfolio.last_cycle_at = now
        cycle_summary = self._cycle_summary(now, discovery_summary, decisions)
        self._persist_cycle(now, decisions, cycle_summary)
        self.events.emit("cycle_summary", "ok", **cycle_summary)
        effective_max_open_positions = self._effective_max_open_positions()
        wallet_available_sol = self._wallet_available_sol
        deployable_sol = self._last_deployable_sol
        accounting_drift_sol = self._last_accounting_drift_sol
        effective_max_daily_new_positions = self._effective_max_daily_new_positions()
        policy = self._last_runtime_policy
        if policy is not None:
            effective_max_open_positions = min(int(effective_max_open_positions), int(policy.effective_max_open_positions))
            effective_max_daily_new_positions = min(
                int(effective_max_daily_new_positions), int(policy.effective_max_daily_new_positions)
            )
        self.events.emit(
            "entry_capacity_mode_summary",
            "ok",
            mode=self.settings.entry_capacity_mode,
            open_positions=len(self.portfolio.open_positions),
            max_open_positions=effective_max_open_positions,
            slots_available=max(0, effective_max_open_positions - len(self.portfolio.open_positions)),
            opened_today_count=self.portfolio.opened_today_count,
            max_daily_new_positions=self.settings.max_daily_new_positions,
            hard_max_daily_new_positions=int(
                getattr(self.settings, "hard_max_daily_new_positions", self.settings.max_daily_new_positions)
                or self.settings.max_daily_new_positions
            ),
            effective_max_daily_new_positions=effective_max_daily_new_positions,
            early_risk_bucket_enabled=self.settings.early_risk_bucket_enabled,
            cash_sol=self.portfolio.cash_sol,
            cash_reserve_sol=self.settings.cash_reserve_sol,
            wallet_available_sol=wallet_available_sol,
            wallet_snapshot_at=self._wallet_snapshot_at,
            hachi_birth_wallet_sol=getattr(self.settings, "hachi_birth_wallet_sol", None) or getattr(self.portfolio, "hachi_birth_wallet_sol", None),
            deployable_sol=deployable_sol,
            accounting_drift_sol=accounting_drift_sol,
            entries_blocked_reason=self._entries_blocked_reason,
            reconciled_cash_sol=self._last_reconciled_cash_sol,
            reconciliation_applied=self._last_reconciliation_applied,
            reconciliation_delta_sol=self._last_reconciliation_delta_sol,
            runtime_risk_mode=None if policy is None else policy.runtime_risk_mode,
            policy_posture=None if policy is None else policy.policy_posture,
            effective_position_size_sol=None if policy is None else policy.effective_position_size_sol,
            effective_policy_max_open_positions=None if policy is None else policy.effective_max_open_positions,
            effective_policy_max_daily_new_positions=None if policy is None else policy.effective_max_daily_new_positions,
            effective_min_score=None if policy is None else policy.effective_min_score,
            effective_min_liquidity_usd=None if policy is None else policy.effective_min_liquidity_usd,
            effective_min_buy_sell_ratio=None if policy is None else policy.effective_min_buy_sell_ratio,
            policy_reason_summary=None if policy is None else policy.policy_reason_summary,
            policy_adjustments_applied=None if policy is None else list(policy.policy_adjustments_applied),
            wallet_pressure_level=None if policy is None else policy.wallet_pressure_level,
            zombie_pressure_level=None if policy is None else policy.zombie_pressure_level,
            deployable_pressure_level=None if policy is None else policy.deployable_pressure_level,
            effective_final_zombie_recovery_probe_interval_cycles=None
            if policy is None
            else policy.effective_final_zombie_recovery_probe_interval_cycles,
            effective_exit_probe_aggressiveness=None if policy is None else policy.effective_exit_probe_aggressiveness,
            effective_dripper_enabled=None if policy is None else policy.effective_dripper_enabled,
            recovery_priority_level=None if policy is None else policy.recovery_priority_level,
            effective_discovery_interval_seconds=None if policy is None else policy.effective_discovery_interval_seconds,
            discovery_cadence_reason=None if policy is None else policy.discovery_cadence_reason,
        )
        blocked_positions = [p for p in self.portfolio.open_positions.values() if p.status == "EXIT_BLOCKED"]
        zombie_positions = [p for p in self.portfolio.open_positions.values() if p.status == "ZOMBIE"]
        final_zombie_positions = [p for p in self.portfolio.open_positions.values() if p.status == POSITION_FINAL_ZOMBIE]
        exit_stuck_total = len(blocked_positions) + len(zombie_positions) + len(final_zombie_positions)
        # T-019: capital visibility for stuck positions.
        zombie_locked_sol_estimate = 0.0
        recoverable_sol_estimate = 0.0
        dead_sol_estimate = 0.0
        class_counts: dict[str, int] = {}
        for p in zombie_positions + final_zombie_positions:
            est = float(getattr(p, "last_estimated_exit_value_sol", None) or 0.0)
            if est <= 0.0:
                est = max(0.0, float(getattr(p, "entry_sol", 0.0) or 0.0))
            zombie_locked_sol_estimate += est
            zc = str(getattr(p, "zombie_class", "") or "UNKNOWN")
            class_counts[zc] = class_counts.get(zc, 0) + 1
            if zc in {"SOFT_ZOMBIE", "FAKE_LIQUID"}:
                recoverable_sol_estimate += est
            elif zc in {"HARD_ZOMBIE"} or p.status == POSITION_FINAL_ZOMBIE:
                dead_sol_estimate += est
        self.events.emit(
            "zombie_capital_estimated",
            "ok",
            zombie_locked_sol_estimate=round(zombie_locked_sol_estimate, 6),
            recoverable_sol_estimate=round(recoverable_sol_estimate, 6),
            dead_sol_estimate=round(dead_sol_estimate, 6),
            zombie_class_counts=class_counts,
        )
        pending_proceeds_total = sum(
            float(getattr(p, "pending_proceeds_sol", 0.0) or 0.0)
            for p in self.portfolio.open_positions.values()
            if float(getattr(p, "pending_proceeds_sol", 0.0) or 0.0) > 0.0
        )
        self.events.emit(
            "exit_blocked_summary",
            "ok",
            exit_blocked_positions=len(blocked_positions),
            zombie_positions=len(zombie_positions),
            final_zombie_positions=len(final_zombie_positions),
            exit_stuck_total=exit_stuck_total,
            blocked_symbols=[p.symbol for p in blocked_positions],
            zombie_symbols=[p.symbol for p in zombie_positions],
            final_zombie_symbols=[p.symbol for p in final_zombie_positions],
            pending_proceeds_sol_total=round(pending_proceeds_total, 9),
        )
        return {
            "timestamp": now,
            "cash_sol": round(self.portfolio.cash_sol, 6),
            "open_positions": len(self.portfolio.open_positions),
            "candidate_symbols": [c.symbol for c in candidates],
            "decisions": [asdict(d) for d in decisions],
            "summary": cycle_summary,
            "events": self.events.to_dicts(),
            "zombie_locked_sol_estimate": round(zombie_locked_sol_estimate, 6),
            "recoverable_sol_estimate": round(recoverable_sol_estimate, 6),
            "dead_sol_estimate": round(dead_sol_estimate, 6),
        }

    def _discover_with_cadence(self) -> tuple[list[TokenCandidate], dict]:
        now_dt = datetime.now(timezone.utc)
        if self._last_discovery_at is not None:
            elapsed = (now_dt - self._last_discovery_at).total_seconds()
            interval = int(self._effective_discovery_interval_seconds or self.settings.discovery_interval_seconds)
            interval = max(1, interval)
            if elapsed < interval:
                cached_summary = dict(self._last_discovery_summary)
                cached_summary["discovery_cached"] = True
                return list(self._last_discovery_candidates), cached_summary
        candidates, summary = discover_candidates(
            self.birdeye,
            self.executor.jupiter,
            self.settings,
            candidate_cache=self._candidate_cache,
            route_cache=self._route_cache,
        )
        summary["discovery_cached"] = False
        summary["cache_engine_identity"] = {
            "candidate_cache_id": id(self._candidate_cache),
            "route_cache_id": id(self._route_cache),
        }
        self._last_discovery_at = now_dt
        self._last_discovery_candidates = list(candidates)
        self._last_discovery_summary = dict(summary)
        return candidates, summary

    def _failed_discovery_summary(self) -> dict:
        """Minimal discovery summary when discovery aborts; keeps cycle + summaries consistent."""
        cc, rc = self._candidate_cache, self._route_cache
        return {
            "seeds_total": 0,
            "discovered_candidates": 0,
            "prefiltered_candidates": 0,
            "seed_prefiltered_out": 0,
            "topn_candidates": 0,
            "route_checked_candidates": 0,
            "cache_hits": cc.stats.hits + rc.stats.hits,
            "cache_misses": cc.stats.misses + rc.stats.misses,
            "candidate_cache_hits": cc.stats.hits,
            "candidate_cache_misses": cc.stats.misses,
            "route_cache_hits": rc.stats.hits,
            "route_cache_misses": rc.stats.misses,
            "birdeye_candidate_build_calls": 0,
            "jupiter_buy_probe_calls": 0,
            "jupiter_sell_probe_calls": 0,
            "candidates_built": 0,
            "candidates_accepted": 0,
            "candidates_rejected_total": 0,
            "rejection_counts": {},
            "events": [],
            "market_data_checked_at": None,
            "cache_debug_first_keys": [],
            "cache_debug_identity": {"candidate_cache_id": id(cc), "route_cache_id": id(rc)},
            "cache_engine_identity": {"candidate_cache_id": id(cc), "route_cache_id": id(rc)},
            "cache_debug_trace": {"candidate": [], "route": []},
            "discovery_cached": False,
        }

    def run_startup_recovery(self) -> list[TradeDecision]:
        now = utc_now_iso()
        self._reset_daily_counters(now)
        decisions = run_startup_recovery(self.portfolio, self.executor, now)
        for decision in decisions:
            self.events.emit("recovery_action", decision.reason, action=decision.action, token_mint=decision.token_mint)
        self._startup_recovery_done = True
        if decisions:
            cycle_summary = self._cycle_summary(now, {"seeds_total": 0, "candidates_built": 0, "candidates_accepted": 0, "candidates_rejected_total": 0, "rejection_counts": {}}, decisions)
            self._persist_cycle(now, decisions, cycle_summary)
        return decisions

    def _mark_positions(self, candidates: list[TokenCandidate], decisions: list[TradeDecision], now: str) -> None:
        by_mint = {c.address: c for c in candidates}
        for mint, position in list(self.portfolio.open_positions.items()):
            if position.status in {"EXIT_BLOCKED", "ZOMBIE", POSITION_FINAL_ZOMBIE}:
                self._handle_exit_blocked_survival_layer(position, decisions, now, valuation_no_route=False)
                continue
            candidate = by_mint.get(mint)
            if candidate is None:
                try:
                    seed = {"address": mint, "symbol": position.symbol, "decimals": position.decimals}
                    candidate = self.birdeye.build_candidate(seed)
                except Exception as exc:
                    LOGGER.warning("mark build failed for %s: %s — using minimal candidate for pricing fallback", mint, exc)
                    candidate = TokenCandidate(address=mint, symbol=position.symbol, decimals=position.decimals)
            ensure_entry_sol_mark(position)
            v = resolve_position_valuation(
                mint=mint,
                symbol=position.symbol,
                position=position,
                executor=self.executor,
            )
            if v.status == VALUATION_STATUS_OK and v.value_sol is not None and v.mark_sol_per_token is not None:
                position.last_mark_sol_per_token = float(v.mark_sol_per_token)
                position.last_estimated_exit_value_sol = float(v.value_sol)
                position.unrealized_pnl_sol = float(v.value_sol) - float(position.entry_sol)
                position.valuation_source = SOURCE_JUPITER_SELL
                position.valuation_status = VALUATION_STATUS_OK
                if not is_valid_sol_mark(position.peak_mark_sol_per_token):
                    position.peak_mark_sol_per_token = position.last_mark_sol_per_token
                else:
                    position.peak_mark_sol_per_token = max(
                        float(position.peak_mark_sol_per_token),
                        float(position.last_mark_sol_per_token),
                    )
                LOGGER.info(
                    "event=position_valuation_sol mint=%s symbol=%s value_sol=%s pnl_sol=%s source=jupiter_sell status=ok size_bucket=%s",
                    mint,
                    position.symbol,
                    position.last_estimated_exit_value_sol,
                    position.unrealized_pnl_sol,
                    v.size_bucket,
                )
                position.last_sell_impact_bps = v.sell_quote_impact_bps
                position.last_sell_route_hops = v.sell_route_hops
                position.last_sell_route_label = v.sell_route_label
                position.quote_miss_streak = 0
            else:
                position.valuation_status = VALUATION_STATUS_NO_ROUTE
                position.quote_miss_streak = int(position.quote_miss_streak) + 1
                LOGGER.info(
                    "event=position_valuation_failed mint=%s symbol=%s reason=no_route size_bucket=%s detail=%s",
                    mint,
                    position.symbol,
                    v.size_bucket,
                    v.detail,
                )
                # Liquidity survival: track persistent no-route states even before exit triggers.
                self._handle_exit_blocked_survival_layer(position, decisions, now, valuation_no_route=True)
            position.updated_at = now
            position.last_exit_liquidity_usd = candidate.exit_liquidity_usd
            if position.status == "EXIT_PENDING" or (
                position.status == POSITION_RECONCILE_PENDING and position.reconcile_context == "exit"
            ):
                self._retry_pending_exit(position, decisions, now)
                continue
            if position.status == POSITION_RECONCILE_PENDING and position.reconcile_context == "entry":
                self._retry_entry_settlement_reconciliation(position, decisions, now)
                # Time stop must apply even to unconfirmed entries — no position holds indefinitely.
                if position.status == POSITION_RECONCILE_PENDING:
                    _entry_age = _age_minutes(position.opened_at)
                    if _entry_age >= self.settings.time_stop_minutes:
                        LOGGER.warning(
                            "reconcile_entry_time_stop mint=%s position_id=%s age_min=%.1f — forcing exit attempt",
                            position.token_mint,
                            position.position_id or position.token_mint,
                            _entry_age,
                        )
                        position.status = "OPEN"
                        self._start_exit(position, position.remaining_qty_atomic, "time_stop_reconcile_entry", decisions, now)
                continue
            self._evaluate_exit_rules(position, candidate, decisions, now)

    def _handle_exit_blocked_survival_layer(
        self,
        position: PositionState,
        decisions: list[TradeDecision],
        now: str,
        *,
        valuation_no_route: bool,
    ) -> None:
        """Liquidity Survival Layer for EXIT_BLOCKED / persistent no-route positions.

        Stages:
          - Stage 1 (cycles 1-3): retry normal blocked exit path (existing behavior)
          - Stage 2 (cycles 4-8): micro-probe sell quote to detect any route
          - Stage 3 (>= micro probe cycles): mark ZOMBIE and retry micro-probe at low frequency
        """
        status = position.status
        is_blocked_state = status in {"EXIT_BLOCKED", "ZOMBIE", POSITION_FINAL_ZOMBIE}
        if not is_blocked_state and not valuation_no_route:
            return

        # Initialize tracking.
        position.exit_blocked_cycles = int(getattr(position, "exit_blocked_cycles", 0) or 0) + 1
        if not getattr(position, "first_blocked_at", None) and is_blocked_state:
            position.first_blocked_at = now
        blocked_cycles = int(position.exit_blocked_cycles)

        self.events.emit(
            "exit_blocked_detected",
            "no_route" if valuation_no_route else "exit_blocked",
            mint=position.token_mint,
            symbol=position.symbol,
            blocked_cycles=blocked_cycles,
            status=position.status,
            valuation_status=position.valuation_status,
        )

        # Stage 1: existing retry behavior for known retryable blocked exits.
        if position.status != POSITION_FINAL_ZOMBIE and is_blocked_state and blocked_cycles <= int(self.settings.exit_blocked_retry_cycles):
            self._retry_blocked_exit_if_due(position, decisions, now)
            return

        # Compute micro-probe qty.
        remaining = int(position.remaining_qty_atomic or 0)
        if remaining <= 0:
            return
        qty_atomic = 1
        mark = float(position.last_mark_sol_per_token or 0.0)
        if mark > 0.0:
            target_sol = float(self.settings.min_order_size_sol) * 0.25
            qty_atomic = int((target_sol / mark) * (10 ** int(position.decimals)))
        if qty_atomic <= 0:
            qty_atomic = max(1, int(remaining * 0.001))
        qty_atomic = max(1, min(remaining, qty_atomic))

        micro_stage_max = int(self.settings.exit_blocked_micro_probe_cycles)
        in_micro_window = (blocked_cycles > int(self.settings.exit_blocked_retry_cycles)) and (blocked_cycles <= micro_stage_max)

        def _attempt_probe() -> bool:
            position.last_route_check_at = now
            position.last_recovery_attempt_at = now
            position.recovery_attempts = int(getattr(position, "recovery_attempts", 0) or 0) + 1
            self.events.emit(
                "exit_micro_probe_attempt",
                "ok",
                mint=position.token_mint,
                symbol=position.symbol,
                probe_size_atomic=qty_atomic,
                blocked_cycles=blocked_cycles,
            )
            try:
                probe = self.executor.quote_sell(position.token_mint, qty_atomic)
            except Exception as exc:
                self.events.emit(
                    "exit_micro_probe_attempt",
                    "exception",
                    mint=position.token_mint,
                    symbol=position.symbol,
                    probe_size_atomic=qty_atomic,
                    error=str(exc),
                )
                return False
            ok = bool(getattr(probe, "route_ok", False) and getattr(probe, "out_amount_atomic", None))
            if ok:
                position.zombie_class = "SOFT_ZOMBIE"
                position.zombie_age_cycles = int(getattr(position, "exit_blocked_cycles", 0) or 0)
                self.events.emit(
                    "zombie_classified",
                    "soft_zombie_route_found",
                    mint=position.token_mint,
                    symbol=position.symbol,
                    position_id=position.position_id or position.token_mint,
                    zombie_class=position.zombie_class,
                    zombie_age_cycles=position.zombie_age_cycles,
                )
                # Revive: clear zombie markers and attempt normal exit path if possible.
                if position.status == "ZOMBIE":
                    position.status = "EXIT_BLOCKED"
                position.zombie_reason = None
                position.zombie_since = None
                self.events.emit(
                    "zombie_recovered",
                    "route_found",
                    mint=position.token_mint,
                    symbol=position.symbol,
                    blocked_cycles=blocked_cycles,
                )
                if position.pending_exit_qty_atomic and position.pending_exit_qty_atomic > 0:
                    position.next_exit_retry_at = now
                    self._attempt_exit(position, decisions, now)
                return True
            return False

        # FINAL_ZOMBIE recovery-probe policy (rare + cheap), independent of the normal staged
        # retry/micro-probe windows.
        if position.status == POSITION_FINAL_ZOMBIE:
            position.zombie_class = "HARD_ZOMBIE"
            position.zombie_age_cycles = int(getattr(position, "exit_blocked_cycles", 0) or 0)
            interval = int(getattr(self.settings, "final_zombie_recovery_probe_interval_cycles", 360) or 360)
            if self._last_runtime_policy is not None and self._last_runtime_policy.effective_final_zombie_recovery_probe_interval_cycles:
                interval = int(self._last_runtime_policy.effective_final_zombie_recovery_probe_interval_cycles)
            interval = max(1, int(interval))
            due = (blocked_cycles % interval) == 0
            if not due:
                remaining_cycles = interval - (blocked_cycles % interval)
                self.events.emit(
                    "final_zombie_probe_skipped",
                    "not_due",
                    mint=position.token_mint,
                    symbol=position.symbol,
                    position_id=position.position_id or position.token_mint,
                    blocked_cycles=blocked_cycles,
                    probe_interval_cycles=interval,
                    next_probe_in_cycles=remaining_cycles,
                )
                return

            self.events.emit(
                "final_zombie_recovery_probe",
                "attempt",
                mint=position.token_mint,
                symbol=position.symbol,
                position_id=position.position_id or position.token_mint,
                blocked_cycles=blocked_cycles,
                probe_size_atomic=qty_atomic,
            )
            ok = _attempt_probe()
            if ok:
                self.events.emit(
                    "final_zombie_recovered_route",
                    "route_found",
                    mint=position.token_mint,
                    symbol=position.symbol,
                    position_id=position.position_id or position.token_mint,
                    blocked_cycles=blocked_cycles,
                )
                # Allow controlled exit path to resume under existing ZOMBIE logic.
                position.status = "ZOMBIE"
                position.zombie_reason = "final_zombie_recovered_route"
                position.zombie_since = now
            return

        # Stage 2: micro-probe window.
        if position.status != POSITION_FINAL_ZOMBIE and in_micro_window:
            _attempt_probe()
            return

        # Stage 3: zombie detection + low-frequency retries.
        if is_blocked_state and blocked_cycles >= micro_stage_max:
            if position.status not in {"ZOMBIE", POSITION_FINAL_ZOMBIE}:
                position.status = "ZOMBIE"
                position.zombie_reason = "no_route_persistent"
                position.zombie_since = now
                self.events.emit(
                    "position_zombie_detected",
                    "no_route_persistent",
                    mint=position.token_mint,
                    symbol=position.symbol,
                    blocked_cycles=blocked_cycles,
                )

            # Terminal promotion: if zombie_max_retry_cycles is configured (> 0) and the
            # position has been retrying that long, promote to FINAL_ZOMBIE and stop normal
            # retry/micro-probe loops. A FINAL_ZOMBIE is re-checked only rarely.
            zombie_max = int(getattr(self.settings, "zombie_max_retry_cycles", 0) or 0)
            if zombie_max > 0 and blocked_cycles >= zombie_max:
                if position.status != POSITION_FINAL_ZOMBIE:
                    position.status = POSITION_FINAL_ZOMBIE
                    position.final_zombie_at = now
                    LOGGER.critical(
                        "event=position_final_zombie mint=%s symbol=%s position_id=%s "
                        "blocked_cycles=%s zombie_max_retry_cycles=%s — terminal, no more retries",
                        position.token_mint,
                        position.symbol,
                        position.position_id or position.token_mint,
                        blocked_cycles,
                        zombie_max,
                    )
                    self.events.emit(
                        "position_final_zombie",
                        "max_retry_cycles_exceeded",
                        mint=position.token_mint,
                        symbol=position.symbol,
                        position_id=position.position_id or position.token_mint,
                        blocked_cycles=blocked_cycles,
                        zombie_max_retry_cycles=zombie_max,
                    )
                    self.events.emit(
                        "final_zombie_promoted",
                        "max_retry_cycles_exceeded",
                        mint=position.token_mint,
                        symbol=position.symbol,
                        position_id=position.position_id or position.token_mint,
                        blocked_cycles=blocked_cycles,
                        zombie_max_retry_cycles=zombie_max,
                    )
                    decisions.append(
                        TradeDecision(
                            action="FINAL_ZOMBIE",
                            token_mint=position.token_mint,
                            symbol=position.symbol,
                            reason="max_retry_cycles_exceeded",
                            metadata={
                                "blocked_cycles": blocked_cycles,
                                "zombie_max_retry_cycles": zombie_max,
                                "zombie_since": position.zombie_since,
                            },
                        )
                    )

            interval = int(self.settings.zombie_retry_interval_cycles)
            if interval > 0 and (blocked_cycles % interval) == 0:
                _attempt_probe()

    def _evaluate_jsds_liquidity(self, position: PositionState, decisions: list[TradeDecision], now: str) -> bool:
        """Jupiter sell-quote liquidity deterioration (JSDS). Returns True if an exit was started."""
        entry_imp = position.entry_sell_impact_bps
        last_imp = position.last_sell_impact_bps
        entry_hops = position.entry_sell_route_hops
        last_hops = position.last_sell_route_hops
        streak = int(position.quote_miss_streak)

        ACTIVE = {"OPEN", "PARTIAL"}
        active_positions = [p for p in self.portfolio.open_positions.values() if p.status in ACTIVE]

        n_active = len(active_positions)

        miss_count = sum(1 for p in active_positions if int(p.quote_miss_streak) >= 1)

        miss_ratio = (miss_count / n_active) if n_active else 0.0
        suppressed = n_active >= 2 and miss_ratio > 0.6

        impact_ratio: float | None = None
        if entry_imp is not None and last_imp is not None:
            try:
                e = max(float(entry_imp), 1.0)
                l = float(last_imp)
                impact_ratio = l / e
            except (TypeError, ValueError):
                pass

        hop_delta: int | None = None
        if entry_hops is not None and last_hops is not None:
            try:
                hop_delta = int(last_hops) - int(entry_hops)
            except (TypeError, ValueError):
                hop_delta = None

        pos_id = position.position_id or position.token_mint

        hard_from_streak = streak >= 5 if suppressed else streak >= 3
        hard_from_route = (
            entry_imp is not None
            and not suppressed
            and impact_ratio is not None
            and hop_delta is not None
            and impact_ratio >= 5.0
            and hop_delta >= 2
        )
        will_hard = hard_from_streak or hard_from_route

        soft_ok = (
            not suppressed
            and entry_imp is not None
            and impact_ratio is not None
            and hop_delta is not None
            and impact_ratio >= 4.0
            and hop_delta >= 1
        )

        if suppressed and not will_hard:
            blocked: list[str] = []
            if streak >= 3 and streak < 5:
                blocked.append("hard_streak")
            if entry_imp is not None and impact_ratio is not None and hop_delta is not None:
                if impact_ratio >= 5.0 and hop_delta >= 2:
                    blocked.append("hard_route")
                if impact_ratio >= 4.0 and hop_delta >= 1:
                    blocked.append("soft")
            if blocked:
                LOGGER.info(
                    "event=liquidity_signal_suppressed_platform_issue mint=%s position_id=%s quote_miss_streak=%s positions_miss_ratio=%s suppressed_triggers=%s",
                    position.token_mint,
                    pos_id,
                    streak,
                    round(miss_ratio, 4),
                    ",".join(blocked),
                )

        if will_hard:
            LOGGER.info(
                "event=liquidity_break mint=%s position_id=%s type=hard impact_ratio=%s hop_delta=%s quote_miss_streak=%s",
                position.token_mint,
                pos_id,
                impact_ratio,
                hop_delta,
                streak,
            )
            self._start_exit(position, position.remaining_qty_atomic, "liquidity_break_hard", decisions, now)
            return True

        if soft_ok:
            LOGGER.info(
                "event=liquidity_break mint=%s position_id=%s type=soft impact_ratio=%s hop_delta=%s quote_miss_streak=%s",
                position.token_mint,
                pos_id,
                impact_ratio,
                hop_delta,
                streak,
            )
            qty = max(1, int(position.remaining_qty_atomic * 0.5))
            self._start_exit(position, qty, "liquidity_break_soft", decisions, now)
            return True

        if entry_imp is not None and impact_ratio is not None and impact_ratio >= 2.5:
            LOGGER.info(
                "event=liquidity_deterioration_watch mint=%s position_id=%s impact_ratio=%s hop_delta=%s quote_miss_streak=%s",
                position.token_mint,
                pos_id,
                impact_ratio,
                hop_delta,
                streak,
            )

        return False

    def _evaluate_exit_rules(self, position: PositionState, candidate: TokenCandidate, decisions: list[TradeDecision], now: str) -> None:
        if position.status not in {"OPEN", "PARTIAL"}:
            return
        tp_thresholds = ",".join(f"{step.trigger_pct:.2f}%" for step in position.take_profit_steps)
        decision_taken = "none"
        ensure_entry_sol_mark(position)
        age_minutes = _age_minutes(position.opened_at)
        if not is_valid_sol_mark(position.entry_mark_sol_per_token) or not is_valid_sol_mark(position.last_mark_sol_per_token):
            # Valuation unavailable (Jupiter down, no route, first cycle).
            # Do NOT skip exit protection — apply time stop using age alone.
            LOGGER.warning(
                "exit_rules_no_valid_marks mint=%s valuation_status=%s age_min=%.1f — time_stop_only",
                position.token_mint,
                position.valuation_status,
                age_minutes,
            )
            if age_minutes >= self.settings.time_stop_minutes:
                decision_taken = "time_stop_no_valuation"
                self._start_exit(position, position.remaining_qty_atomic, "time_stop_no_valuation", decisions, now)
            LOGGER.info(
                "event=exit_eval_debug mint=%s symbol=%s entry_mark_sol_per_token=%s last_mark_sol_per_token=%s current_pnl_pct=%s computed_pnl_pct=%s take_profit_thresholds=%s triggered=%s decision=%s reason=%s",
                position.token_mint,
                position.symbol,
                position.entry_mark_sol_per_token,
                position.last_mark_sol_per_token,
                "n/a",
                "n/a",
                tp_thresholds or "none",
                str(decision_taken != "none").lower(),
                decision_taken,
                "invalid_marks",
            )
            return
        entry_s = float(position.entry_mark_sol_per_token)
        last_s = float(position.last_mark_sol_per_token)
        pnl_pct = (last_s / entry_s - 1.0) * 100.0
        current_pnl_pct = pnl_pct
        liquidity_ratio = None
        if position.exit_liquidity_at_entry_usd and position.last_exit_liquidity_usd:
            liquidity_ratio = position.last_exit_liquidity_usd / max(position.exit_liquidity_at_entry_usd, 1.0)

        if self.settings.force_full_exit_on_liquidity_break and liquidity_ratio is not None and liquidity_ratio < self.settings.liquidity_break_ratio:
            decision_taken = "liquidity_break"
            self._start_exit(position, position.remaining_qty_atomic, "liquidity_break", decisions, now)
            LOGGER.info(
                "event=exit_eval_debug mint=%s symbol=%s entry_mark_sol_per_token=%s last_mark_sol_per_token=%s current_pnl_pct=%s computed_pnl_pct=%s take_profit_thresholds=%s triggered=true decision=%s reason=liquidity_ratio_below_threshold",
                position.token_mint,
                position.symbol,
                position.entry_mark_sol_per_token,
                position.last_mark_sol_per_token,
                current_pnl_pct,
                pnl_pct,
                tp_thresholds or "none",
                decision_taken,
            )
            return

        if self._evaluate_jsds_liquidity(position, decisions, now):
            decision_taken = "liquidity_jsds"
            LOGGER.info(
                "event=exit_eval_debug mint=%s symbol=%s entry_mark_sol_per_token=%s last_mark_sol_per_token=%s current_pnl_pct=%s computed_pnl_pct=%s take_profit_thresholds=%s triggered=true decision=%s reason=jsds_liquidity_rule",
                position.token_mint,
                position.symbol,
                position.entry_mark_sol_per_token,
                position.last_mark_sol_per_token,
                current_pnl_pct,
                pnl_pct,
                tp_thresholds or "none",
                decision_taken,
            )
            return

        if pnl_pct <= -abs(position.stop_loss_pct):
            decision_taken = "stop_loss"
            self._start_exit(position, position.remaining_qty_atomic, "stop_loss", decisions, now)
            LOGGER.info(
                "event=exit_eval_debug mint=%s symbol=%s entry_mark_sol_per_token=%s last_mark_sol_per_token=%s current_pnl_pct=%s computed_pnl_pct=%s take_profit_thresholds=%s triggered=true decision=%s reason=stop_loss",
                position.token_mint,
                position.symbol,
                position.entry_mark_sol_per_token,
                position.last_mark_sol_per_token,
                current_pnl_pct,
                pnl_pct,
                tp_thresholds or "none",
                decision_taken,
            )
            return

        if pnl_pct >= position.trailing_arm_pct:
            peak_s = float(position.peak_mark_sol_per_token) if is_valid_sol_mark(position.peak_mark_sol_per_token) else last_s
            trail_floor = peak_s * (1.0 - position.trailing_stop_pct / 100.0)
            if last_s <= trail_floor:
                decision_taken = "trailing_stop"
                self._start_exit(position, position.remaining_qty_atomic, "trailing_stop", decisions, now)
                LOGGER.info(
                    "event=exit_eval_debug mint=%s symbol=%s entry_mark_sol_per_token=%s last_mark_sol_per_token=%s current_pnl_pct=%s computed_pnl_pct=%s take_profit_thresholds=%s triggered=true decision=%s reason=trailing_stop",
                    position.token_mint,
                    position.symbol,
                    position.entry_mark_sol_per_token,
                    position.last_mark_sol_per_token,
                    current_pnl_pct,
                    pnl_pct,
                    tp_thresholds or "none",
                    decision_taken,
                )
                return

        if age_minutes >= self.settings.time_stop_minutes and pnl_pct < 12.0:
            decision_taken = "time_stop"
            self._start_exit(position, position.remaining_qty_atomic, "time_stop", decisions, now)
            LOGGER.info(
                "event=exit_eval_debug mint=%s symbol=%s entry_mark_sol_per_token=%s last_mark_sol_per_token=%s current_pnl_pct=%s computed_pnl_pct=%s take_profit_thresholds=%s triggered=true decision=%s reason=time_stop",
                position.token_mint,
                position.symbol,
                position.entry_mark_sol_per_token,
                position.last_mark_sol_per_token,
                current_pnl_pct,
                pnl_pct,
                tp_thresholds or "none",
                decision_taken,
            )
            return

        # -------------------------------------------------------------------
        # Hachi-style dripper: primary sell controller when enabled.
        # Probes Jupiter sell quotes every cycle and executes small chunks
        # when route quality is acceptable.  No TP threshold gate — selling
        # can start immediately once a position is open.
        # TP ladder is bypassed entirely when hachi is active (it would race
        # with the dripper and cause double-selling).
        # -------------------------------------------------------------------
        if self.settings.hachi_dripper_enabled:
            self._run_hachi_dripper(position, decisions, now)
            return

        for step in position.take_profit_steps:
            if step.done:
                continue
            if pnl_pct >= step.trigger_pct:
                qty = max(1, int(position.remaining_qty_atomic * step.fraction))
                triggered = self._start_exit(position, qty, f"take_profit_{int(step.trigger_pct)}", decisions, now)
                if triggered:
                    decision_taken = "take_profit"
                    step.done = True
                    LOGGER.info(
                        "event=exit_eval_debug mint=%s symbol=%s entry_mark_sol_per_token=%s last_mark_sol_per_token=%s current_pnl_pct=%s computed_pnl_pct=%s take_profit_thresholds=%s triggered=true decision=%s reason=tp_threshold_hit tp_trigger=%s",
                        position.token_mint,
                        position.symbol,
                        position.entry_mark_sol_per_token,
                        position.last_mark_sol_per_token,
                        current_pnl_pct,
                        pnl_pct,
                        tp_thresholds or "none",
                        decision_taken,
                        f"{step.trigger_pct:.2f}%",
                    )
                    if position.status != "OPEN":
                        return
            else:
                LOGGER.info(
                    "event=exit_eval_debug mint=%s symbol=%s entry_mark_sol_per_token=%s last_mark_sol_per_token=%s current_pnl_pct=%s computed_pnl_pct=%s take_profit_thresholds=%s triggered=false decision=none reason=below_threshold tp_trigger=%s",
                    position.token_mint,
                    position.symbol,
                    position.entry_mark_sol_per_token,
                    position.last_mark_sol_per_token,
                    current_pnl_pct,
                    pnl_pct,
                    tp_thresholds or "none",
                    f"{step.trigger_pct:.2f}%",
                )

        if decision_taken == "none":
            LOGGER.info(
                "event=exit_eval_debug mint=%s symbol=%s entry_mark_sol_per_token=%s last_mark_sol_per_token=%s current_pnl_pct=%s computed_pnl_pct=%s take_profit_thresholds=%s triggered=false decision=none reason=no_exit_rule_triggered",
                position.token_mint,
                position.symbol,
                position.entry_mark_sol_per_token,
                position.last_mark_sol_per_token,
                current_pnl_pct,
                pnl_pct,
                tp_thresholds or "none",
            )

    def _run_hachi_dripper(self, position: PositionState, decisions: list[TradeDecision], now: str) -> bool:
        """Hachi dripper with PnL-zone + momentum decision brain.

        Every cycle for OPEN/PARTIAL positions:
          1. Classify current PnL into a zone (profit_harvest / neutral /
             deterioration / emergency).
          2. Classify cycle-over-cycle mark movement into a momentum state
             (improving / flat / weakening / collapsing).
          3. Map (zone, momentum) → urgency level.
          4. Emergency / collapse urgency bypasses the timing gate and fires a
             full-exit override immediately — no more blind dripping while
             deeply negative.
          5. For non-emergency urgency, probe Jupiter quotes and pick chunk size
             based on urgency (conservative → smallest, normal → near-equal
             largest, aggressive → biggest available).
          6. Scale next-chunk wait time with urgency (shorter when aggressive).
          7. Persist momentum state on position for next cycle.

        Jupiter quote is the sole source of truth for sellability.
        Birdeye liquidity is never consulted here.
        Hard exits from _evaluate_exit_rules always take priority over this method.

        Returns True if a sell chunk (or full exit) was dispatched this cycle.
        """
        pos_id = position.position_id or position.token_mint
        HACHI_MAX_DRIP_CHUNKS = 3
        remaining = position.remaining_qty_atomic
        if remaining <= 0:
            return False

        # ------------------------------------------------------------------
        # 1-3. Brain: PnL zone + momentum + urgency (always evaluated first,
        #      even before the timing gate, so emergencies can bypass it).
        # ------------------------------------------------------------------
        pnl_pct = compute_pnl_pct(position)
        momentum = classify_momentum(position, self.settings)
        if pnl_pct is not None:
            pnl_zone = classify_pnl_zone(pnl_pct, self.settings)
        else:
            pnl_zone = "unknown"
        urgency = select_urgency(pnl_zone, momentum) if pnl_pct is not None else "conservative"

        # Persist for observability / next-cycle comparison (even on wait cycles).
        position.last_hachi_pnl_pct = pnl_pct
        position.last_hachi_momentum_state = momentum

        LOGGER.info(
            "event=hachi_state_eval mint=%s position_id=%s pnl_pct=%s pnl_zone=%s "
            "momentum_state=%s urgency=%s remaining=%s entry_sol=%s",
            position.token_mint,
            pos_id,
            round(pnl_pct, 2) if pnl_pct is not None else "n/a",
            pnl_zone,
            momentum,
            urgency,
            remaining,
            round(position.entry_sol, 6),
        )

        # ---------------------------------------------------------------
        # Runner preservation gates:
        # - hard cap max chunks per position
        # - allow only one drip per TP level (profit milestones)
        # ---------------------------------------------------------------
        tp_levels = [float(x) for x in (self.settings.take_profit_levels_pct or [])]
        tp_levels = sorted(tp_levels)
        current_tp_level: int | None = None
        if pnl_pct is not None and tp_levels:
            current_tp_level = -1
            for idx, lvl in enumerate(tp_levels):
                if pnl_pct >= float(lvl):
                    current_tp_level = idx
        # If we've completed dripping, stay a runner unless a major event occurs.
        if getattr(position, "hachi_drip_completed", False):
            major_event = bool(urgency == URGENCY_OVERRIDE_FULL) or (
                current_tp_level is not None
                and (position.hachi_last_tp_level is None or current_tp_level > int(position.hachi_last_tp_level))
            )
            if not major_event:
                decisions.append(
                    TradeDecision(
                        action="DRIPPER_WAIT",
                        token_mint=position.token_mint,
                        symbol=position.symbol,
                        reason="hachi_drip_completed_runner_mode",
                    )
                )
                position.previous_mark_sol_per_token = float(position.last_mark_sol_per_token)
                return False
            # Major event resets the completion latch and drip pacing.
            position.hachi_drip_completed = False
            position.drip_chunks_done = 0
            position.drip_next_chunk_at = None

        # Stop dripping completely after max chunks (unless a major event resets above).
        if int(getattr(position, "drip_chunks_done", 0) or 0) >= HACHI_MAX_DRIP_CHUNKS:
            position.hachi_drip_completed = True
            position.drip_next_chunk_at = None
            LOGGER.info(
                "event=hachi_drip_stopped reason=max_chunks_reached mint=%s position_id=%s chunks_done=%s",
                position.token_mint,
                pos_id,
                position.drip_chunks_done,
            )
            decisions.append(
                TradeDecision(
                    action="DRIPPER_WAIT",
                    token_mint=position.token_mint,
                    symbol=position.symbol,
                    reason="max_chunks_reached",
                    metadata={"chunks_done": position.drip_chunks_done},
                )
            )
            position.previous_mark_sol_per_token = float(position.last_mark_sol_per_token)
            return False

        # Dripper should only trigger once per TP level (profit milestone).
        if urgency != URGENCY_OVERRIDE_FULL:
            last_tp = position.hachi_last_tp_level
            eligible_tp = (
                current_tp_level is not None
                and current_tp_level >= 0
                and (last_tp is None or int(current_tp_level) > int(last_tp))
            )
            if not eligible_tp:
                # Distinguish between "no TP threshold reached yet" and "same TP level already dripped".
                if current_tp_level is None or current_tp_level < 0:
                    not_armed_reason = "tp_threshold_not_reached"
                elif last_tp is not None and int(current_tp_level) <= int(last_tp):
                    not_armed_reason = "tp_level_already_dripped"
                else:
                    not_armed_reason = "tp_level_gate"
                LOGGER.info(
                    "event=dripper_not_armed mint=%s position_id=%s reason=%s "
                    "current_tp_level=%s last_tp_level=%s pnl_pct=%s urgency=%s",
                    position.token_mint,
                    pos_id,
                    not_armed_reason,
                    current_tp_level,
                    last_tp,
                    round(pnl_pct, 2) if pnl_pct is not None else "n/a",
                    urgency,
                )
                self.events.emit(
                    "dripper_not_armed",
                    not_armed_reason,
                    mint=position.token_mint,
                    symbol=position.symbol,
                    position_id=pos_id,
                    current_tp_level=current_tp_level,
                    last_tp_level=last_tp,
                    pnl_pct=round(pnl_pct, 2) if pnl_pct is not None else None,
                    urgency=urgency,
                )
                decisions.append(
                    TradeDecision(
                        action="DRIPPER_WAIT",
                        token_mint=position.token_mint,
                        symbol=position.symbol,
                        reason=not_armed_reason,
                        metadata={
                            "current_tp_level": current_tp_level,
                            "last_tp_level": last_tp,
                            "pnl_pct": round(pnl_pct, 2) if pnl_pct is not None else None,
                            "not_armed_reason": not_armed_reason,
                        },
                    )
                )
                position.previous_mark_sol_per_token = float(position.last_mark_sol_per_token)
                return False

        # ------------------------------------------------------------------
        # 4. Emergency / momentum-collapse override: bypass timing gate and
        #    sell everything immediately.
        # ------------------------------------------------------------------
        if urgency == URGENCY_OVERRIDE_FULL:
            exit_reason = override_reason(pnl_zone, momentum)
            LOGGER.info(
                "event=dripper_override_hard_exit mint=%s position_id=%s reason=%s "
                "pnl_pct=%s momentum_state=%s pnl_zone=%s chunks_done=%s",
                position.token_mint,
                pos_id,
                exit_reason,
                round(pnl_pct, 2) if pnl_pct is not None else "n/a",
                momentum,
                pnl_zone,
                position.drip_chunks_done,
            )
            decisions.append(
                TradeDecision(
                    action="DRIPPER_OVERRIDE_HARD_EXIT",
                    token_mint=position.token_mint,
                    symbol=position.symbol,
                    reason=exit_reason,
                    qty_atomic=remaining,
                    metadata={
                        "override_reason": exit_reason,
                        "pnl_pct": pnl_pct,
                        "momentum_state": momentum,
                        "pnl_zone": pnl_zone,
                        "chunks_done_before_override": position.drip_chunks_done,
                    },
                )
            )
            # Clear timing gate so _start_exit override-detection doesn't double-emit.
            position.drip_next_chunk_at = None
            # Update previous mark before exiting so state is clean if recovery occurs.
            position.previous_mark_sol_per_token = float(position.last_mark_sol_per_token)
            self._start_exit(position, remaining, exit_reason, decisions, now)
            return True

        # ------------------------------------------------------------------
        # Timing gate: only for non-emergency urgency.
        # ------------------------------------------------------------------
        if position.drip_next_chunk_at and not _retry_due(position.drip_next_chunk_at, now):
            LOGGER.info(
                "event=dripper_wait mint=%s position_id=%s drip_next_chunk_at=%s chunks_done=%s",
                position.token_mint,
                pos_id,
                position.drip_next_chunk_at,
                position.drip_chunks_done,
            )
            decisions.append(
                TradeDecision(
                    action="DRIPPER_WAIT",
                    token_mint=position.token_mint,
                    symbol=position.symbol,
                    reason="drip_timing_gate",
                    metadata={
                        "drip_next_chunk_at": position.drip_next_chunk_at,
                        "drip_chunks_done": position.drip_chunks_done,
                    },
                )
            )
            # Still update previous mark so momentum tracking stays accurate.
            position.previous_mark_sol_per_token = float(position.last_mark_sol_per_token)
            return False

        # ------------------------------------------------------------------
        # 5. Probe Jupiter sell quotes for each configured chunk fraction.
        # ------------------------------------------------------------------
        candidates: list[tuple[int, float, float | None]] = []  # (qty, sol_out/token, impact_bps)
        for pct in self.settings.drip_chunk_pcts:
            chunk_qty = max(1, int(remaining * pct))
            try:
                probe = self.executor.quote_sell(position.token_mint, chunk_qty)
            except Exception as exc:
                LOGGER.debug(
                    "dripper_eval: quote_sell failed mint=%s qty=%s err=%s",
                    position.token_mint,
                    chunk_qty,
                    exc,
                )
                continue
            if not probe.route_ok or not probe.out_amount_atomic or probe.out_amount_atomic <= 0:
                continue
            impact = probe.price_impact_bps
            if impact is not None and abs(float(impact)) > self.settings.hachi_max_price_impact_bps:
                continue
            efficiency = probe.out_amount_atomic / float(chunk_qty)
            candidates.append((chunk_qty, efficiency, impact))

        LOGGER.info(
            "event=dripper_eval mint=%s position_id=%s remaining=%s chunks_probed=%s viable=%s pnl_source=jupiter_quote",
            position.token_mint,
            pos_id,
            remaining,
            len(self.settings.drip_chunk_pcts),
            len(candidates),
        )

        # ------------------------------------------------------------------
        # Apply brain chunk policy: urgency → chosen chunk size.
        # ------------------------------------------------------------------
        chosen_qty, selection_reason = apply_urgency_to_chunk(
            urgency, candidates, remaining, self.settings
        )

        LOGGER.info(
            "event=hachi_chunk_policy mint=%s position_id=%s urgency=%s chosen_chunk=%s "
            "selection_reason=%s viable_candidates=%s pnl_zone=%s momentum=%s",
            position.token_mint,
            pos_id,
            urgency,
            chosen_qty,
            selection_reason,
            len(candidates),
            pnl_zone,
            momentum,
        )

        if chosen_qty is None:
            LOGGER.info(
                "event=dripper_wait mint=%s position_id=%s reason=no_executable_chunk remaining=%s",
                position.token_mint,
                pos_id,
                remaining,
            )
            decisions.append(
                TradeDecision(
                    action="DRIPPER_WAIT",
                    token_mint=position.token_mint,
                    symbol=position.symbol,
                    reason="no_executable_chunk",
                )
            )
            position.previous_mark_sol_per_token = float(position.last_mark_sol_per_token)
            return False

        # Derive chosen_eff / chosen_impact for logging from candidates (best match by qty).
        _cmap = {qty: (eff, imp) for qty, eff, imp in candidates}
        chosen_eff, chosen_impact = _cmap.get(chosen_qty, (0.0, None))

        LOGGER.info(
            "event=dripper_chunk_selected mint=%s position_id=%s chunk_qty=%s efficiency=%s "
            "impact_bps=%s chunks_done_so_far=%s urgency=%s selection_reason=%s",
            position.token_mint,
            pos_id,
            chosen_qty,
            round(chosen_eff, 8),
            chosen_impact,
            position.drip_chunks_done,
            urgency,
            selection_reason,
        )
        decisions.append(
            TradeDecision(
                action="DRIPPER_CHUNK_SELECTED",
                token_mint=position.token_mint,
                symbol=position.symbol,
                reason="hachi_dripper",
                qty_atomic=chosen_qty,
                metadata={
                    "efficiency": round(chosen_eff, 10),
                    "impact_bps": chosen_impact,
                    "urgency": urgency,
                    "pnl_zone": pnl_zone,
                    "momentum": momentum,
                    "selection_reason": selection_reason,
                },
            )
        )

        # ------------------------------------------------------------------
        # Execute the chunk; inspect decisions to detect whether sell settled.
        # ------------------------------------------------------------------
        decisions_before = len(decisions)
        self._start_exit(position, chosen_qty, "hachi_dripper", decisions, now)

        sell_executed = any(d.action == "SELL" for d in decisions[decisions_before:])
        if sell_executed:
            sold_qty = next(
                (d.qty_atomic for d in decisions[decisions_before:] if d.action == "SELL"),
                chosen_qty,
            )
            position.drip_chunks_done += 1
            # Record the TP level that triggered this drip so we only drip once per level.
            if current_tp_level is not None and current_tp_level >= 0:
                position.hachi_last_tp_level = int(current_tp_level)
            if position.status != "CLOSED":
                # Scale inter-chunk wait by urgency: conservative → longer, aggressive → shorter.
                wait_s = chunk_wait_seconds(urgency, self.settings.drip_min_chunk_wait_seconds)
                position.drip_next_chunk_at = _next_normal_retry_at(now, wait_s)
            # If we just hit the chunk cap, latch completion so this becomes a runner.
            if position.drip_chunks_done >= HACHI_MAX_DRIP_CHUNKS:
                position.hachi_drip_completed = True
                position.drip_next_chunk_at = None
                LOGGER.info(
                    "event=hachi_drip_stopped reason=max_chunks_reached mint=%s position_id=%s chunks_done=%s",
                    position.token_mint,
                    pos_id,
                    position.drip_chunks_done,
                )
            LOGGER.info(
                "event=dripper_chunk_executed mint=%s position_id=%s chunks_done=%s sold_qty=%s "
                "next_chunk_at=%s urgency=%s",
                position.token_mint,
                pos_id,
                position.drip_chunks_done,
                sold_qty,
                position.drip_next_chunk_at,
                urgency,
            )
            decisions.append(
                TradeDecision(
                    action="DRIPPER_CHUNK_EXECUTED",
                    token_mint=position.token_mint,
                    symbol=position.symbol,
                    reason="hachi_dripper",
                    qty_atomic=sold_qty,
                    metadata={
                        "chunks_done": position.drip_chunks_done,
                        "next_chunk_at": position.drip_next_chunk_at,
                        "urgency": urgency,
                        "pnl_zone": pnl_zone,
                        "momentum": momentum,
                    },
                )
            )
            self.events.emit(
                "dripper_chunk_executed",
                "hachi",
                position_id=pos_id,
                chunks_done=position.drip_chunks_done,
                sold_qty=sold_qty,
            )

        # ------------------------------------------------------------------
        # 7. Persist mark for next cycle's momentum comparison.
        # ------------------------------------------------------------------
        position.previous_mark_sol_per_token = float(position.last_mark_sol_per_token)
        return sell_executed

    def _start_exit(self, position: PositionState, qty_atomic: int, reason: str, decisions: list[TradeDecision], now: str) -> bool:
        # Detect when a hard exit overrides a pending Hachi dripper chunk.
        _is_hard = not reason.startswith("take_profit_") and reason != "hachi_dripper"
        if _is_hard and position.drip_next_chunk_at is not None:
            LOGGER.info(
                "event=dripper_override_hard_exit mint=%s position_id=%s override_reason=%s drip_next_chunk_at=%s chunks_done=%s",
                position.token_mint,
                position.position_id or position.token_mint,
                reason,
                position.drip_next_chunk_at,
                position.drip_chunks_done,
            )
            decisions.append(
                TradeDecision(
                    action="DRIPPER_OVERRIDE_HARD_EXIT",
                    token_mint=position.token_mint,
                    symbol=position.symbol,
                    reason=reason,
                    metadata={
                        "override_reason": reason,
                        "chunks_done_before_override": position.drip_chunks_done,
                    },
                )
            )
            position.drip_next_chunk_at = None

        if position.status == "EXIT_PENDING":
            # If a drip is in progress and a hard (non-TP) exit arrives, override it.
            hard_exit = not reason.startswith("take_profit_")
            if position.drip_exit_active and hard_exit:
                LOGGER.info(
                    "drip_override mint=%s reason=%s drip_reason=%s",
                    position.token_mint,
                    reason,
                    position.drip_exit_reason,
                )
                _clear_drip_state(position)
                qty_atomic = position.remaining_qty_atomic
                position.pending_exit_reason = reason
                position.pending_exit_qty_atomic = qty_atomic
                position.updated_at = now
                decisions.append(TradeDecision(action="EXIT_PENDING", token_mint=position.token_mint, symbol=position.symbol, reason=reason, qty_atomic=qty_atomic))
                self._attempt_exit(position, decisions, now)
                return True
            return False
        qty_atomic = min(max(1, qty_atomic), position.remaining_qty_atomic)
        # Decide whether to use drip exit for this signal.
        if self.settings.drip_exit_enabled and _is_drip_eligible(reason):
            position.drip_exit_active = True
            position.drip_exit_reason = reason
            position.drip_qty_remaining_atomic = qty_atomic
            position.drip_chunks_done = 0
            position.drip_next_chunk_at = None
        position.status = "EXIT_PENDING"
        position.pending_exit_reason = reason
        position.pending_exit_qty_atomic = qty_atomic
        position.updated_at = now
        decisions.append(TradeDecision(action="EXIT_PENDING", token_mint=position.token_mint, symbol=position.symbol, reason=reason, qty_atomic=qty_atomic))
        self._attempt_exit(position, decisions, now)
        return True

    def _retry_entry_settlement_reconciliation(self, position: PositionState, decisions: list[TradeDecision], now: str) -> None:
        """Entry reconciliation: Jupiter is truth — no wallet RPC. Position stays RECONCILE_PENDING for manual review."""
        LOGGER.critical(
            "entry_reconcile_no_rpc mint=%s position_id=%s "
            "— buy settlement was unknown; Jupiter-only mode cannot auto-resolve. "
            "Position remains RECONCILE_PENDING for manual intervention.",
            position.token_mint,
            position.position_id or position.token_mint,
        )

    def _retry_pending_exit(self, position: PositionState, decisions: list[TradeDecision], now: str) -> None:
        if position.exit_retry_count >= MAX_EXIT_RETRIES:
            position.status = "EXIT_BLOCKED"
            position.pending_exit_reason = None
            position.pending_exit_qty_atomic = None
            decisions.append(TradeDecision(action="SELL_BLOCKED", token_mint=position.token_mint, symbol=position.symbol, reason="retries_exhausted"))
            return
        if position.next_exit_retry_at and not _retry_due(position.next_exit_retry_at, now):
            return
        self._attempt_exit(position, decisions, now)

    def _retry_blocked_exit_if_due(self, position: PositionState, decisions: list[TradeDecision], now: str) -> None:
        if position.pending_exit_reason != SELL_THRESHOLD_UNCOMPUTABLE:
            return
        if position.next_exit_retry_at and not _retry_due(position.next_exit_retry_at, now):
            return
        # Keep blocked-state retries on normal cycle cadence for threshold failures.
        self._attempt_exit(position, decisions, now)

    def _attempt_exit(self, position: PositionState, decisions: list[TradeDecision], now: str) -> None:
        if position.pending_exit_qty_atomic is None or position.pending_exit_qty_atomic <= 0:
            position.status = "EXIT_BLOCKED"
            _clear_drip_state(position)
            decisions.append(TradeDecision(action="SELL_BLOCKED", token_mint=position.token_mint, symbol=position.symbol, reason="missing_pending_qty"))
            return
        # Drip timing gate: hold until the next chunk window is due.
        if position.drip_exit_active and position.drip_next_chunk_at and not _retry_due(position.drip_next_chunk_at, now):
            decisions.append(
                TradeDecision(
                    action="DRIP_CHUNK_WAITING",
                    token_mint=position.token_mint,
                    symbol=position.symbol,
                    reason=position.drip_exit_reason or "drip",
                    metadata={
                        "drip_next_chunk_at": position.drip_next_chunk_at,
                        "drip_chunks_done": position.drip_chunks_done,
                    },
                )
            )
            return
        # Drip: select best chunk size; non-drip: use full pending qty.
        if position.drip_exit_active:
            chunk_qty = select_drip_chunk(position, self.executor, self.settings)
            requested_qty = (
                min(chunk_qty, position.remaining_qty_atomic)
                if chunk_qty is not None and chunk_qty > 0
                else min(position.pending_exit_qty_atomic, position.remaining_qty_atomic)
            )
        else:
            requested_qty = min(position.pending_exit_qty_atomic, position.remaining_qty_atomic)
        # T-019: FAKE_LIQUID partial liquidation — when route exists but executions repeatedly fail,
        # reduce requested size to a small fraction to increase odds of recovering anything.
        if str(getattr(position, "zombie_class", "") or "") == "FAKE_LIQUID":
            reduced = max(1, int(requested_qty * 0.25))
            if reduced < requested_qty:
                self.events.emit(
                    "partial_exit_attempt",
                    "fake_liquid_reduce_qty",
                    position_id=position.position_id or position.token_mint,
                    token_mint=position.token_mint,
                    requested_before=requested_qty,
                    requested_after=reduced,
                )
                requested_qty = reduced
        # Jupiter is truth — no pre-sell wallet read. Proceed with internally tracked qty.
        if requested_qty <= 0:
            _clear_drip_state(position)
            position.status = "EXIT_BLOCKED"
            decisions.append(TradeDecision(action="SELL_BLOCKED", token_mint=position.token_mint, symbol=position.symbol, reason="remaining_qty_zero"))
            return

        result, quote = self.executor.sell(position.token_mint, requested_qty)
        self.events.emit("exit_attempt", position.pending_exit_reason or "unknown", position_id=position.position_id or position.token_mint, requested=requested_qty)
        LOGGER.info(
            "sell_attempt position_id=%s reason=%s status=%s requested=%s executed=%s signature=%s",
            position.position_id or position.token_mint,
            position.pending_exit_reason or "unknown",
            result.status,
            requested_qty,
            result.executed_amount,
            result.signature,
        )
        decisions.append(
            TradeDecision(
                action="SELL_ATTEMPT",
                token_mint=position.token_mint,
                symbol=position.symbol,
                reason=position.pending_exit_reason or "unknown",
                qty_atomic=requested_qty,
                metadata={
                    "status": result.status,
                    "requested_amount": requested_qty,
                    "executed_amount": result.executed_amount,
                    "signature": result.signature,
                    "error": result.error,
                    "price_impact_bps": quote.price_impact_bps,
                    "classification": result.diagnostic_metadata.get("classification") or result.diagnostic_code,
                    "jupiter_error_code": _extract_jupiter_error_code(result.diagnostic_metadata.get("response_body")),
                },
            )
        )

        if result.status == "success":
            sett = result.diagnostic_metadata.get("post_sell_settlement")
            if not isinstance(sett, dict) or not sett.get("settlement_confirmed"):
                LOGGER.error(
                    "sell_settlement_metadata_missing position_id=%s mint=%s",
                    position.position_id or position.token_mint,
                    position.token_mint,
                )
                position.status = POSITION_RECONCILE_PENDING
                position.reconcile_context = "exit"
                position.exit_retry_count = min(
                    MAX_EXIT_RETRY_COUNT, max(0, int(getattr(position, "exit_retry_count", 0) or 0)) + 1
                )
                position.last_exit_attempt_at = now
                position.next_exit_retry_at = _next_retry_at(now, position.exit_retry_count)
                position.pending_exit_signature = result.signature
                decisions.append(
                    TradeDecision(
                        action="SELL_SETTLEMENT_PENDING",
                        token_mint=position.token_mint,
                        symbol=position.symbol,
                        reason="sell_settlement_metadata_missing",
                        qty_atomic=requested_qty,
                        metadata={"classification": SETTLEMENT_UNCONFIRMED},
                    )
                )
                return
            sold_reconciled = int(sett.get("sold_atomic_settled") or result.executed_amount or 0)
            if sold_reconciled < 0:
                raise RuntimeError("invariant violated: negative sold amount")
            if sold_reconciled > position.remaining_qty_atomic:
                LOGGER.warning(
                    "sell_settlement sold_exceeds_remaining position_id=%s sold=%s remaining=%s",
                    position.position_id or position.token_mint,
                    sold_reconciled,
                    position.remaining_qty_atomic,
                )
            denom = 10 ** max(position.decimals, 0)
            new_remaining = max(0, position.remaining_qty_atomic - sold_reconciled)
            position.remaining_qty_atomic = new_remaining
            position.remaining_qty_ui = (new_remaining / denom) if denom else float(new_remaining)
            out_sol = None
            proceeds_source = None
            if result.output_amount is not None:
                out_sol = max(0.0, float(result.output_amount) / 1_000_000_000.0)
                proceeds_source = "output_amount"
                position.realized_sol += out_sol
                self.portfolio.cash_sol += out_sol
            else:
                # SOL proceeds unavailable from Jupiter — do NOT credit cash_sol and do NOT use
                # the pre-execution quote as a substitute. The quote is not settlement truth.
                # Mark RECONCILE_PENDING so the operator can investigate; position stays visible.
                LOGGER.critical(
                    "sell_proceeds_unknown mint=%s position_id=%s signature=%s "
                    "sold_atomic=%s remaining_after=%s — RECONCILE_PENDING, SOL not credited",
                    position.token_mint,
                    position.position_id or position.token_mint,
                    result.signature,
                    sold_reconciled,
                    new_remaining,
                )
                self.events.emit(
                    "sell_proceeds_unknown",
                    SETTLEMENT_UNCONFIRMED,
                    token_mint=position.token_mint,
                    position_id=position.position_id or position.token_mint,
                    signature=result.signature,
                    sold_atomic=sold_reconciled,
                    remaining_after=new_remaining,
                )
                _clear_drip_state(position)
                position.last_sell_signature = result.signature
                position.status = POSITION_RECONCILE_PENDING
                position.reconcile_context = "exit"
                position.exit_retry_count = min(
                    MAX_EXIT_RETRY_COUNT, max(0, int(getattr(position, "exit_retry_count", 0) or 0)) + 1
                )
                position.last_exit_attempt_at = now
                position.next_exit_retry_at = _next_retry_at(now, position.exit_retry_count)
                position.pending_exit_signature = result.signature
                # Set qty to 0 so _attempt_exit will not re-sell on the next retry cycle.
                # Tokens are already gone; EXIT_BLOCKED is the correct visible end-state.
                position.pending_exit_qty_atomic = 0
                decisions.append(
                    TradeDecision(
                        action="SELL_SETTLEMENT_PENDING",
                        token_mint=position.token_mint,
                        symbol=position.symbol,
                        reason="sell_proceeds_unknown",
                        qty_atomic=sold_reconciled,
                        metadata={
                            "classification": SETTLEMENT_UNCONFIRMED,
                            "signature": result.signature,
                            "sold_atomic": sold_reconciled,
                            "remaining_after": new_remaining,
                        },
                    )
                )
                return
            position.last_sell_signature = result.signature
            position.exit_retry_count = 0
            position.last_exit_attempt_at = now
            position.next_exit_retry_at = None
            position.pending_exit_qty_atomic = None
            position.pending_exit_reason = None
            position.pending_exit_signature = None
            position.reconcile_context = None
            sold_ui = sold_reconciled / denom if denom else float(sold_reconciled)
            decisions.append(
                TradeDecision(
                    action="SELL",
                    token_mint=position.token_mint,
                    symbol=position.symbol,
                    reason="exit_success",
                    qty_atomic=sold_reconciled,
                    qty_ui=sold_ui,
                    metadata={
                        "out_sol": out_sol,
                        "proceeds_source": proceeds_source,
                        "signature": result.signature,
                        "partial": result.is_partial,
                        "proceeds_pending_reconcile": out_sol is None,
                        "post_sell_settlement": sett,
                        "sold_atomic_settled": sold_reconciled,
                        "remaining_after_sell_atomic": new_remaining,
                    },
                )
            )
            self.events.emit("exit_success", result.diagnostic_code or "success", position_id=position.position_id or position.token_mint, qty=sold_reconciled)
            if str(getattr(position, "zombie_class", "") or "") == "FAKE_LIQUID":
                self.events.emit(
                    "partial_exit_success",
                    "fake_liquid",
                    position_id=position.position_id or position.token_mint,
                    token_mint=position.token_mint,
                    sold_atomic=sold_reconciled,
                )
            if out_sol is not None:
                self.events.emit(
                    "cash_sol_credited",
                    "sell_proceeds",
                    token_mint=position.token_mint,
                    position_id=position.position_id or position.token_mint,
                    credited_sol=out_sol,
                    proceeds_source=proceeds_source,
                    cash_sol_after=self.portfolio.cash_sol,
                )
            # Drip lifecycle: schedule next chunk or complete the drip.
            if position.drip_exit_active and position.remaining_qty_atomic > 0:
                drip_remaining = max(0, (position.drip_qty_remaining_atomic or 0) - sold_reconciled)
                position.drip_chunks_done += 1
                if drip_remaining > 0:
                    position.drip_qty_remaining_atomic = drip_remaining
                    position.drip_next_chunk_at = _next_normal_retry_at(now, self.settings.drip_min_chunk_wait_seconds)
                    position.pending_exit_qty_atomic = drip_remaining
                    position.pending_exit_reason = position.drip_exit_reason
                    position.pending_exit_signature = None
                    position.status = "EXIT_PENDING"
                    position.updated_at = now
                    decisions.append(
                        TradeDecision(
                            action="DRIP_CHUNK_EXECUTED",
                            token_mint=position.token_mint,
                            symbol=position.symbol,
                            reason=position.drip_exit_reason or "drip",
                            qty_atomic=sold_reconciled,
                            metadata={
                                "drip_chunks_done": position.drip_chunks_done,
                                "drip_qty_remaining_atomic": drip_remaining,
                                "drip_next_chunk_at": position.drip_next_chunk_at,
                            },
                        )
                    )
                    self.events.emit("drip_chunk_executed", "chunk_done", position_id=position.position_id or position.token_mint, chunks_done=position.drip_chunks_done)
                    return
                else:
                    # All drip qty is sold; fall through to CLOSED/PARTIAL.
                    _clear_drip_state(position)
            elif position.drip_exit_active:
                # remaining_qty_atomic is 0 — position will close; clear drip state.
                _clear_drip_state(position)
            if position.remaining_qty_atomic <= 0:
                position.status = "CLOSED"
                self.portfolio.total_realized_sol += position.realized_sol - position.entry_sol
                self.portfolio.closed_positions.append(position)
                self.portfolio.open_positions.pop(position.token_mint, None)
                self.portfolio.cooldowns[position.token_mint] = now
            else:
                position.status = "PARTIAL"
                position.updated_at = now
            return

        if result.status == "unknown" and result.diagnostic_code == SETTLEMENT_UNCONFIRMED:
            _clear_drip_state(position)
            position.status = POSITION_RECONCILE_PENDING
            position.reconcile_context = "exit"
            position.pending_exit_signature = result.signature
            position.exit_retry_count = min(
                MAX_EXIT_RETRY_COUNT, max(0, int(getattr(position, "exit_retry_count", 0) or 0)) + 1
            )
            position.last_exit_attempt_at = now
            position.next_exit_retry_at = _next_retry_at(now, position.exit_retry_count)
            decisions.append(
                TradeDecision(
                    action="SELL_SETTLEMENT_PENDING",
                    token_mint=position.token_mint,
                    symbol=position.symbol,
                    reason="sell_settlement_unconfirmed",
                    qty_atomic=requested_qty,
                    metadata={
                        "classification": SETTLEMENT_UNCONFIRMED,
                        "signature": result.signature,
                        "post_sell_settlement": result.diagnostic_metadata.get("post_sell_settlement"),
                    },
                )
            )
            self.events.emit(
                "exit_failed",
                SETTLEMENT_UNCONFIRMED,
                position_id=position.position_id or position.token_mint,
                error=result.error or SETTLEMENT_UNCONFIRMED,
            )
            return

        if result.status == "failed":
            _clear_drip_state(position)
            classification = str(result.diagnostic_metadata.get("classification") or result.diagnostic_code or "failed")
            jupiter_error_code = _extract_jupiter_error_code(result.diagnostic_metadata.get("response_body"))
            position.status = "EXIT_BLOCKED"
            if classification == SELL_THRESHOLD_UNCOMPUTABLE:
                position.pending_exit_reason = SELL_THRESHOLD_UNCOMPUTABLE
                position.pending_exit_qty_atomic = requested_qty
                position.last_exit_attempt_at = now
                position.next_exit_retry_at = _next_normal_retry_at(now, self.settings.poll_interval_seconds)
            else:
                position.exit_retry_count = min(
                    MAX_EXIT_RETRY_COUNT, max(0, int(getattr(position, "exit_retry_count", 0) or 0)) + 1
                )
                position.last_exit_attempt_at = now
                position.next_exit_retry_at = _next_retry_at(now, position.exit_retry_count)
                if _is_system_execution_failure(classification):
                    self.portfolio.consecutive_execution_failures += 1
                    # If sell route appears present (quote is ok) but execution fails repeatedly,
                    # classify as FAKE_LIQUID to enable partial liquidation.
                    if bool(getattr(quote, "route_ok", False) and getattr(quote, "out_amount_atomic", None)):
                        position.zombie_class = "FAKE_LIQUID"
                        position.zombie_age_cycles = int(getattr(position, "exit_blocked_cycles", 0) or 0)
                        self.events.emit(
                            "zombie_classified",
                            "fake_liquid_execution_failures",
                            mint=position.token_mint,
                            symbol=position.symbol,
                            position_id=position.position_id or position.token_mint,
                            zombie_class=position.zombie_class,
                            zombie_age_cycles=position.zombie_age_cycles,
                            classification=classification,
                        )
                        self.events.emit(
                            "partial_exit_failed",
                            "fake_liquid",
                            position_id=position.position_id or position.token_mint,
                            token_mint=position.token_mint,
                            classification=classification,
                        )
            decisions.append(
                TradeDecision(
                    action="SELL_BLOCKED",
                    token_mint=position.token_mint,
                    symbol=position.symbol,
                    reason=f"execution_failed:{result.error or 'unknown'}",
                    qty_atomic=requested_qty,
                    metadata={
                        "classification": classification,
                        "jupiter_error_code": jupiter_error_code,
                        "requested_amount": requested_qty,
                    },
                )
            )
            self.events.emit("exit_failed", result.diagnostic_code or "failed", position_id=position.position_id or position.token_mint, error=result.error or "unknown")
        else:
            _clear_drip_state(position)
            if result.signature:
                position.status = "EXIT_PENDING"
                position.pending_exit_signature = result.signature
                decisions.append(TradeDecision(action="SELL_PENDING", token_mint=position.token_mint, symbol=position.symbol, reason=f"execution_unknown:{result.error or 'unknown'}", qty_atomic=requested_qty))
                LOGGER.warning("%s position_id=%s reason=%s", EXIT_UNKNOWN_PENDING_RECONCILE, position.position_id or position.token_mint, result.error or "unknown")
                self.events.emit("exit_failed", result.diagnostic_code or EXIT_UNKNOWN_PENDING_RECONCILE, position_id=position.position_id or position.token_mint, error=result.error or "unknown")
            else:
                position.status = "EXIT_BLOCKED"
                decisions.append(TradeDecision(action="SELL_BLOCKED", token_mint=position.token_mint, symbol=position.symbol, reason=f"execution_no_signature:{result.status}", qty_atomic=requested_qty))
                LOGGER.warning("sell_no_signature_blocked position_id=%s status=%s", position.position_id or position.token_mint, result.status)
                # Signature failure is a system failure signal.
                self.portfolio.consecutive_execution_failures += 1

    def _maybe_open_positions(self, candidates: list[TokenCandidate], decisions: list[TradeDecision], now: str) -> None:
        mode = str(getattr(self.settings, "entry_capacity_mode", "strict") or "strict")
        effective_max_open_positions = self._effective_max_open_positions()
        effective_max_daily_new_positions = self._effective_max_daily_new_positions()
        # Invariants:
        # - dynamic capacity affects entries only (this function)
        # - reserve floor is enforced before execution (blocked_cash_reserve)
        # - per-position sizing caps are enforced via size_sol=min(base,max,early_risk_cap)

        def _emit_capacity_decision(*, candidate: TokenCandidate, allowed: bool, reason: str) -> None:
            candidate_type = "early_risk" if bool((candidate.raw or {}).get("early_risk_bucket")) else "standard"
            self.events.emit(
                "entry_capacity_decision",
                reason,
                mode=mode,
                candidate_type=candidate_type,
                allowed=bool(allowed),
                blocked=not bool(allowed),
                reason=reason,
                mint=candidate.address,
                symbol=candidate.symbol,
                open_positions=len(self.portfolio.open_positions),
                max_open_positions=effective_max_open_positions,
                opened_today_count=self.portfolio.opened_today_count,
                max_daily_new_positions=self.settings.max_daily_new_positions,
                effective_max_daily_new_positions=effective_max_daily_new_positions,
            )

        # FINAL_ZOMBIE positions occupy an open_positions slot but can never exit on their own.
        # They must not consume effective entry capacity; subtract them from the occupancy count.
        final_zombie_count = sum(
            1 for p in self.portfolio.open_positions.values() if p.status == POSITION_FINAL_ZOMBIE
        )
        effective_open_count = len(self.portfolio.open_positions) - final_zombie_count
        if effective_open_count >= effective_max_open_positions:
            return
        if self.portfolio.opened_today_count >= effective_max_daily_new_positions:
            return

        def _is_early(c: TokenCandidate) -> bool:
            return bool((c.raw or {}).get("early_risk_bucket"))

        ordered = [*([c for c in candidates if not _is_early(c)]), *([c for c in candidates if _is_early(c)])]

        for candidate in ordered:
            final_zombie_count = sum(
                1 for p in self.portfolio.open_positions.values() if p.status == POSITION_FINAL_ZOMBIE
            )
            if len(self.portfolio.open_positions) - final_zombie_count >= effective_max_open_positions:
                break
            if candidate.address in self.portfolio.open_positions:
                _emit_capacity_decision(candidate=candidate, allowed=False, reason="blocked_already_open")
                continue
            if _cooldown_active(self.portfolio.cooldowns.get(candidate.address), self.settings.cooldown_minutes_after_exit):
                _emit_capacity_decision(candidate=candidate, allowed=False, reason="blocked_cooldown_active")
                continue
            size_sol = min(self.settings.base_position_size_sol, self.settings.max_position_size_sol)
            if self._last_runtime_policy is not None:
                size_sol = float(self._last_runtime_policy.effective_position_size_sol)
            early_risk = bool((candidate.raw or {}).get("early_risk_bucket"))

            # T-020: age-banded liquidity gating (pre-execution).
            age_band = derive_age_band(getattr(candidate, "age_hours", None))
            effective_liq_floor = float(liquidity_floor_for_age_band(age_band))
            liq_usd = float(candidate.liquidity_usd or 0.0)
            if liq_usd < effective_liq_floor:
                self.events.emit(
                    "entry_liquidity_gate",
                    "blocked",
                    mint=candidate.address,
                    symbol=candidate.symbol,
                    effective_age_band=age_band,
                    effective_liquidity_floor=effective_liq_floor,
                    liquidity_floor_reason="age_banded_floor",
                    liquidity_usd=liq_usd,
                )
                _emit_capacity_decision(candidate=candidate, allowed=False, reason="blocked_policy_age_band_liquidity")
                continue

            if early_risk:
                if mode == "strict" and len(self.portfolio.open_positions) > 1:
                    _emit_capacity_decision(candidate=candidate, allowed=False, reason="blocked_strict_early_risk_open_positions_gt_1")
                    continue
                # balanced/fill_slots: early-risk allowed only after standard ordering (handled by ordered list)
                if self.settings.early_risk_bucket_enabled:
                    size_sol = min(size_sol, float(self.settings.early_risk_position_size_sol))

            _emit_capacity_decision(candidate=candidate, allowed=True, reason="allowed")

            if early_risk and self.settings.early_risk_bucket_enabled:
                size_sol = min(size_sol, float(self.settings.early_risk_position_size_sol))
            if self._last_runtime_policy is not None:
                if float(candidate.discovery_score or 0.0) < float(self._last_runtime_policy.effective_min_score):
                    _emit_capacity_decision(candidate=candidate, allowed=False, reason="blocked_policy_min_score")
                    continue
                if float(candidate.liquidity_usd or 0.0) < float(self._last_runtime_policy.effective_min_liquidity_usd):
                    _emit_capacity_decision(candidate=candidate, allowed=False, reason="blocked_policy_min_liquidity")
                    continue
                if float(candidate.buy_sell_ratio_1h or 0.0) < float(self._last_runtime_policy.effective_min_buy_sell_ratio):
                    _emit_capacity_decision(candidate=candidate, allowed=False, reason="blocked_policy_min_buy_sell_ratio")
                    continue
            if self.portfolio.cash_sol - size_sol < self.settings.cash_reserve_sol:
                _emit_capacity_decision(candidate=candidate, allowed=False, reason="blocked_cash_reserve")
                continue
            buy_probe = self.executor.quote_buy(candidate, size_sol)
            if not buy_probe.route_ok or not buy_probe.out_amount_atomic:
                self._write_entry_probe_artifact(candidate, now, buy_probe, None, REJECT_NO_BUY_ROUTE)
                decisions.append(
                    TradeDecision(
                        action="BUY_SKIP",
                        token_mint=candidate.address,
                        symbol=candidate.symbol,
                        reason=REJECT_NO_BUY_ROUTE,
                        size_sol=size_sol,
                        metadata={
                            "classification": REJECT_NO_BUY_ROUTE,
                            "request_params": {
                                "inputMint": SOL_MINT,
                                "outputMint": candidate.address,
                                "amount": max(1, int(size_sol * 1_000_000_000)),
                                "slippageBps": self.settings.default_slippage_bps,
                            },
                            "probe_raw": buy_probe.raw,
                            "phase": "pre_entry_probe",
                        },
                    )
                )
                self.events.emit("entry_failed", REJECT_NO_BUY_ROUTE, token_mint=candidate.address, symbol=candidate.symbol)
                continue
            buy_sanity_reason = _probe_sanity_reason(buy_probe)
            if buy_sanity_reason:
                self._write_entry_probe_artifact(candidate, now, buy_probe, None, buy_sanity_reason)
                decisions.append(
                    TradeDecision(
                        action="BUY_SKIP",
                        token_mint=candidate.address,
                        symbol=candidate.symbol,
                        reason=REJECT_ECONOMIC_SANITY_FAILED,
                        size_sol=size_sol,
                        metadata={"phase": "pre_entry_probe", "classification": buy_sanity_reason, "probe_raw": buy_probe.raw},
                    )
                )
                self.events.emit("entry_failed", buy_sanity_reason, token_mint=candidate.address, symbol=candidate.symbol, phase="pre_entry_probe")
                continue
            sell_probe = self.executor.quote_sell(candidate.address, max(1, buy_probe.out_amount_atomic))
            if not sell_probe.route_ok or not sell_probe.out_amount_atomic:
                self._write_entry_probe_artifact(candidate, now, buy_probe, sell_probe, REJECT_EXECUTION_ROUTE_MISSING)
                decisions.append(
                    TradeDecision(
                        action="BUY_SKIP",
                        token_mint=candidate.address,
                        symbol=candidate.symbol,
                        reason=REJECT_EXECUTION_ROUTE_MISSING,
                        size_sol=size_sol,
                        metadata={
                            "classification": REJECT_NO_SELL_ROUTE,
                            "request_params": {
                                "inputMint": candidate.address,
                                "outputMint": SOL_MINT,
                                "amount": max(1, buy_probe.out_amount_atomic),
                                "slippageBps": self.settings.default_slippage_bps,
                            },
                            "probe_raw": sell_probe.raw,
                            "phase": "pre_entry_probe",
                        },
                    )
                )
                self.events.emit("entry_failed", REJECT_EXECUTION_ROUTE_MISSING, token_mint=candidate.address, symbol=candidate.symbol)
                continue
            # T-020: route survivability gating (cheap, controlled).
            required_buckets = survivability_required_buckets(age_band)
            survivability_ok = 1
            frag = False
            score = 1.0
            if required_buckets >= 2:
                small_qty = max(1, int(max(1, buy_probe.out_amount_atomic) * 0.25))
                try:
                    sell_probe_small = self.executor.quote_sell(candidate.address, small_qty)
                except Exception:
                    sell_probe_small = None
                ok_small = bool(sell_probe_small and sell_probe_small.route_ok and sell_probe_small.out_amount_atomic)
                survivability_ok = (1 if ok_small else 0) + 1
                score = survivability_ok / 2.0
                if not ok_small:
                    frag = True
            # Impact stability: if impact missing or extreme, mark fragile (does not add calls).
            imp = sell_probe.price_impact_bps
            if imp is None or (isinstance(imp, (int, float)) and float(imp) > float(self.settings.max_acceptable_price_impact_bps)):
                frag = True

            self.events.emit(
                "route_survivability_scored",
                "ok",
                mint=candidate.address,
                symbol=candidate.symbol,
                effective_age_band=age_band,
                route_survivability_score=round(float(score), 3),
                route_fragile=bool(frag),
                buckets_required=required_buckets,
            )
            if frag or score < (1.0 if required_buckets >= 2 else 0.5):
                decisions.append(
                    TradeDecision(
                        action="BUY_SKIP",
                        token_mint=candidate.address,
                        symbol=candidate.symbol,
                        reason="reject_route_survivability",
                        size_sol=size_sol,
                        metadata={
                            "phase": "pre_entry_probe",
                            "classification": "reject_route_survivability",
                            "effective_age_band": age_band,
                            "effective_liquidity_floor": effective_liq_floor,
                            "liquidity_floor_reason": "age_banded_floor",
                            "route_survivability_score": score,
                            "route_fragile": frag,
                        },
                    )
                )
                _emit_capacity_decision(candidate=candidate, allowed=False, reason="blocked_route_survivability")
                continue
            sell_sanity_reason = _probe_sanity_reason(sell_probe)
            roundtrip_reason = _roundtrip_sanity_reason(buy_probe, sell_probe)
            sanity_reason = sell_sanity_reason or roundtrip_reason
            if sanity_reason:
                self._write_entry_probe_artifact(candidate, now, buy_probe, sell_probe, sanity_reason)
                decisions.append(
                    TradeDecision(
                        action="BUY_SKIP",
                        token_mint=candidate.address,
                        symbol=candidate.symbol,
                        reason=REJECT_ECONOMIC_SANITY_FAILED,
                        size_sol=size_sol,
                        metadata={
                            "phase": "pre_entry_probe",
                            "classification": sanity_reason,
                            "buy_probe_raw": buy_probe.raw,
                            "sell_probe_raw": sell_probe.raw,
                        },
                    )
                )
                self.events.emit("entry_failed", sanity_reason, token_mint=candidate.address, symbol=candidate.symbol, phase="pre_entry_probe")
                continue
            self._write_entry_probe_artifact(candidate, now, buy_probe, sell_probe, "entry_probe_passed")
            execution, quote = self.executor.buy(candidate, size_sol)
            self.events.emit("entry_attempt", "discovery_entry", token_mint=candidate.address, size_sol=size_sol)
            if execution.status == "unknown" and execution.diagnostic_code == SETTLEMENT_UNCONFIRMED:
                pbs = execution.diagnostic_metadata.get("post_buy_settlement") or {}
                provisional = pbs.get("provisional_quote_atomic") or quote.out_amount_atomic
                if provisional is None or int(provisional) <= 0:
                    decisions.append(
                        TradeDecision(
                            action="BUY_SKIP",
                            token_mint=candidate.address,
                            symbol=candidate.symbol,
                            reason="buy_settlement_unconfirmed_no_provisional_qty",
                            size_sol=size_sol,
                            metadata={"classification": SETTLEMENT_UNCONFIRMED, "signature": execution.signature},
                        )
                    )
                    self.portfolio.consecutive_execution_failures += 1
                    continue
                qty_atomic = int(provisional)
                if not candidate.decimals:
                    continue
                qty_ui = qty_atomic / (10 ** candidate.decimals)
                position = PositionState(
                    token_mint=candidate.address,
                    symbol=candidate.symbol,
                    decimals=candidate.decimals,
                    status=POSITION_RECONCILE_PENDING,
                    opened_at=now,
                    updated_at=now,
                    entry_price_usd=0.0,
                    avg_entry_price_usd=0.0,
                    entry_sol=size_sol,
                    remaining_qty_atomic=qty_atomic,
                    remaining_qty_ui=qty_ui,
                    peak_price_usd=0.0,
                    last_price_usd=0.0,
                    position_id=f"{candidate.address}:{now}",
                    stop_loss_pct=self.settings.stop_loss_pct,
                    trailing_stop_pct=self.settings.trailing_stop_pct,
                    trailing_arm_pct=self.settings.trailing_arm_pct,
                    exit_liquidity_at_entry_usd=candidate.exit_liquidity_usd,
                    last_exit_liquidity_usd=candidate.exit_liquidity_usd,
                    take_profit_steps=[TakeProfitStep(trigger_pct=lvl, fraction=frac) for lvl, frac in zip(self.settings.take_profit_levels_pct, self.settings.take_profit_fractions)],
                    notes=[f"score={candidate.discovery_score}", "entry_settlement_unconfirmed", *candidate.reasons],
                    reconcile_context="entry",
                )
                _seed_sol_basis_on_open(position, candidate, size_sol, qty_ui)
                _seed_jsds_entry_baseline(position, sell_probe)
                self.portfolio.open_positions[candidate.address] = position
                self.portfolio.cash_sol -= size_sol
                # Mark the debit as pending confirmation so it is visible in state if the tx
                # fails on-chain. pending_proceeds_sol > 0 flags a RECONCILE_PENDING(entry)
                # position where cash_sol was already debited but on-chain outcome is unknown.
                # This does NOT reverse the debit — that requires operator intervention via
                # startup recovery — but makes the phantom-debit risk observable and auditable.
                position.pending_proceeds_sol = float(size_sol)
                LOGGER.warning(
                    "buy_settlement_unconfirmed_cash_debited mint=%s position_id=%s "
                    "size_sol=%.9f signature=%s — cash_sol debited; reversal requires manual recovery if tx fails",
                    candidate.address,
                    position.position_id,
                    size_sol,
                    execution.signature,
                )
                self.events.emit(
                    "cash_sol_debited_pending",
                    SETTLEMENT_UNCONFIRMED,
                    token_mint=candidate.address,
                    position_id=position.position_id,
                    debited_sol=size_sol,
                    cash_sol_after=self.portfolio.cash_sol,
                    signature=execution.signature,
                )
                self.portfolio.opened_today_count += 1
                decisions.append(
                    TradeDecision(
                        action="BUY",
                        token_mint=candidate.address,
                        symbol=candidate.symbol,
                        reason="discovery_entry_settlement_pending",
                        size_sol=size_sol,
                        qty_atomic=qty_atomic,
                        qty_ui=qty_ui,
                        metadata={
                            "classification": SETTLEMENT_UNCONFIRMED,
                            "signature": execution.signature,
                            "provisional_qty_atomic": qty_atomic,
                            "price_impact_bps": quote.price_impact_bps,
                            "cash_sol_debited": size_sol,
                        },
                    )
                )
                self.events.emit("entry_failed", SETTLEMENT_UNCONFIRMED, token_mint=candidate.address, symbol=candidate.symbol, phase="post_execute_settlement")
                if len(self.portfolio.open_positions) >= self._effective_max_open_positions():
                    break
                continue

            if execution.status != "success":
                phase = str(execution.diagnostic_metadata.get("phase") or "execute")
                classification = str(execution.diagnostic_metadata.get("classification") or execution.diagnostic_code or f"execution_{execution.status}")
                self.events.emit(
                    "entry_failed",
                    classification,
                    symbol=candidate.symbol,
                    mint=candidate.address,
                    side="buy",
                    phase=phase,
                    endpoint=execution.diagnostic_metadata.get("endpoint"),
                    request_params=execution.diagnostic_metadata.get("request_params"),
                    status_code=execution.diagnostic_metadata.get("status_code"),
                    response_body=execution.diagnostic_metadata.get("response_body"),
                    classification=classification,
                )
                decisions.append(
                    TradeDecision(
                        action="BUY_SKIP",
                        token_mint=candidate.address,
                        symbol=candidate.symbol,
                        reason=f"execution_{execution.status}",
                        size_sol=size_sol,
                        metadata={
                            "error": execution.error,
                            "price_impact_bps": quote.price_impact_bps,
                            "phase": phase,
                            "endpoint": execution.diagnostic_metadata.get("endpoint"),
                            "request_params": execution.diagnostic_metadata.get("request_params"),
                            "status_code": execution.diagnostic_metadata.get("status_code"),
                            "response_body": execution.diagnostic_metadata.get("response_body"),
                            "classification": classification,
                        },
                    )
                )
                if classification == EXEC_SKIPPED_DRY_RUN:
                    self.portfolio.entries_skipped_dry_run += 1
                elif classification == EXEC_SKIPPED_LIVE_DISABLED:
                    self.portfolio.entries_skipped_live_disabled += 1
                else:
                    if _is_system_execution_failure(classification):
                        self.portfolio.consecutive_execution_failures += 1
                continue
            qty_atomic = execution.executed_amount
            if qty_atomic is None or qty_atomic <= 0:
                decisions.append(TradeDecision(action="BUY_SKIP", token_mint=candidate.address, symbol=candidate.symbol, reason="missing_executed_amount", size_sol=size_sol))
                continue
            assert qty_atomic > 0, "position cannot be created with zero quantity"
            if not candidate.decimals:
                continue
            qty_ui = qty_atomic / (10 ** candidate.decimals)
            position = PositionState(
                token_mint=candidate.address,
                symbol=candidate.symbol,
                decimals=candidate.decimals,
                status="OPEN",
                opened_at=now,
                updated_at=now,
                entry_price_usd=0.0,
                avg_entry_price_usd=0.0,
                entry_sol=size_sol,
                remaining_qty_atomic=qty_atomic,
                remaining_qty_ui=qty_ui,
                peak_price_usd=0.0,
                last_price_usd=0.0,
                position_id=f"{candidate.address}:{now}",
                stop_loss_pct=self.settings.stop_loss_pct,
                trailing_stop_pct=self.settings.trailing_stop_pct,
                trailing_arm_pct=self.settings.trailing_arm_pct,
                exit_liquidity_at_entry_usd=candidate.exit_liquidity_usd,
                last_exit_liquidity_usd=candidate.exit_liquidity_usd,
                take_profit_steps=[TakeProfitStep(trigger_pct=lvl, fraction=frac) for lvl, frac in zip(self.settings.take_profit_levels_pct, self.settings.take_profit_fractions)],
                notes=[f"score={candidate.discovery_score}", *candidate.reasons],
            )
            _seed_sol_basis_on_open(position, candidate, size_sol, qty_ui)
            _seed_jsds_entry_baseline(position, sell_probe)
            self.portfolio.open_positions[candidate.address] = position
            self.portfolio.cash_sol -= size_sol
            self.portfolio.opened_today_count += 1
            decisions.append(TradeDecision(action="BUY", token_mint=candidate.address, symbol=candidate.symbol, reason="discovery_entry", size_sol=size_sol, qty_atomic=qty_atomic, qty_ui=qty_ui, metadata={"score": candidate.discovery_score, "price_impact_bps": quote.price_impact_bps, "signature": execution.signature}))
            self.portfolio.consecutive_execution_failures = 0
            self.events.emit("entry_success", execution.diagnostic_code or "success", token_mint=candidate.address, qty_atomic=qty_atomic)
            if len(self.portfolio.open_positions) >= self._effective_max_open_positions():
                break

    def _write_entry_probe_artifact(
        self,
        candidate: TokenCandidate,
        now: str,
        buy_probe,
        sell_probe,
        decision: str,
    ) -> None:
        # Keep lightweight per-candidate proof artifacts for fast route debugging.
        safe_symbol = "".join(ch if ch.isalnum() else "_" for ch in (candidate.symbol or "unknown"))[:24]
        timestamp = now.replace(":", "").replace("-", "").replace(".", "").replace("+", "_")
        artifact_dir = self.settings.run_dir or self.settings.runtime_dir
        path = artifact_dir / f"entry_probe_{safe_symbol}_{timestamp}.json"
        payload = {
            "timestamp": now,
            "run_id": self.settings.run_id,
            "cycle_in_run": self._cycle_in_run,
            "candidate": {
                "symbol": candidate.symbol,
                "mint": candidate.address,
                "score": candidate.discovery_score,
                "liquidity_usd": candidate.liquidity_usd,
                "volume_24h_usd": candidate.volume_24h_usd,
                "buy_sell_ratio": candidate.buy_sell_ratio_1h,
            },
            "buy_probe": asdict(buy_probe) if buy_probe is not None else None,
            "sell_probe": asdict(sell_probe) if sell_probe is not None else None,
            "final_decision": decision,
        }
        try:
            atomic_write_json(path, payload)
        except Exception as exc:
            LOGGER.warning("entry probe artifact write failed path=%s error=%s", path, exc)

    def _persist_cycle(self, now: str, decisions: list[TradeDecision], cycle_summary: dict) -> None:
        if self.settings.run_id:
            self.portfolio.run_id = self.settings.run_id
        try:
            save_portfolio(self.settings.state_path, self.portfolio)
        except Exception as exc:
            LOGGER.error("%s path=%s error=%s", STATE_SAVE_FAILED, self.settings.state_path, exc)
            self.events.emit("persistence_issue", STATE_SAVE_FAILED, path=str(self.settings.state_path), error=str(exc))
            raise
        for decision in decisions:
            try:
                append_jsonl(
                    self.settings.journal_path,
                    {
                        "ts": now,
                        "run_id": self.settings.run_id,
                        "cycle_in_run": self._cycle_in_run,
                        **asdict(decision),
                    },
                )
                if self.settings.run_dir:
                    append_jsonl(
                        self.settings.run_dir / "journal.jsonl",
                        {
                            "ts": now,
                            "run_id": self.settings.run_id,
                            "cycle_in_run": self._cycle_in_run,
                            **asdict(decision),
                        },
                    )
            except Exception as exc:
                LOGGER.error("%s path=%s error=%s", JOURNAL_APPEND_FAILED, self.settings.journal_path, exc)
                self.events.emit("persistence_issue", JOURNAL_APPEND_FAILED, path=str(self.settings.journal_path), error=str(exc))
                raise
        status_path = self.settings.runtime_dir / "status.json"
        policy_payload = None if self._last_runtime_policy is None else self._last_runtime_policy.to_dict()
        status_payload = {
            "run_id": self.settings.run_id,
            "run_dir": str(self.settings.run_dir) if self.settings.run_dir else None,
            "cycle_in_run": self._cycle_in_run,
            "cycle_timestamp": now,
            "safe_mode_active": self.portfolio.safe_mode_active,
            "safety_stop_reason": self.portfolio.safety_stop_reason,
            "summary": cycle_summary,
            # Dedicated, operator-friendly derived policy payload for dashboard/status consumers.
            "derived_policy": policy_payload,
        }
        try:
            top_rejections = sorted(
                cycle_summary.get("rejection_counts", {}).items(),
                key=lambda kv: kv[1],
                reverse=True,
            )[:5]
            status_payload["top_rejection_reasons"] = [{"reason": k, "count": v} for k, v in top_rejections]
            save_status_snapshot(status_path, status_payload)
            if self.settings.run_dir:
                save_status_snapshot(self.settings.run_dir / "status.json", status_payload)
                append_jsonl(
                    self.settings.run_dir / "cycle_summaries.jsonl",
                    {
                        "ts": now,
                        "run_id": self.settings.run_id,
                        "cycle_in_run": self._cycle_in_run,
                        "summary": cycle_summary,
                    },
                )
        except Exception as exc:
            LOGGER.error("%s path=%s error=%s", STATE_SAVE_FAILED, status_path, exc)
            self.events.emit("persistence_issue", STATE_SAVE_FAILED, path=str(status_path), error=str(exc))

    def _reset_daily_counters(self, now: str) -> None:
        day = now[:10]
        if self.portfolio.opened_today_date != day:
            self.portfolio.opened_today_date = day
            self.portfolio.opened_today_count = 0

    def _evaluate_safety(self, now: str, *, market_data_checked_at: str | None = None) -> str | None:
        self._safety_diagnostics = None
        if self.portfolio.total_realized_sol <= -abs(self.settings.daily_realized_loss_cap_sol):
            return SAFETY_DAILY_LOSS_CAP
        if self.portfolio.consecutive_execution_failures >= self.settings.max_consecutive_execution_failures:
            return SAFETY_MAX_CONSEC_EXEC_FAILURES
        # Stale-market safety is only meaningful during an active run loop.
        # Do not persistently "lock" the bot into safe mode when no run is active.
        if market_data_checked_at and self.settings.run_id and self.portfolio.last_cycle_at is not None:
            age_seconds = _age_seconds_between(now, market_data_checked_at)
            threshold_seconds = max(1, int(self.settings.stale_market_data_minutes * 60))
            if age_seconds is not None and age_seconds > threshold_seconds:
                self._safety_diagnostics = {
                    "current_time": now,
                    "checked_timestamp": market_data_checked_at,
                    "data_age_seconds": age_seconds,
                    "stale_threshold_seconds": threshold_seconds,
                    "data_source_name": "discovery_cycle_market_data",
                }
                return SAFETY_STALE_MARKET_DATA
        unknown_exits = sum(
            1
            for p in self.portfolio.open_positions.values()
            if p.status == "EXIT_PENDING"
            or (p.status == POSITION_RECONCILE_PENDING and p.reconcile_context == "exit")
        )
        if unknown_exits >= self.settings.unknown_exit_saturation_limit:
            return SAFETY_UNKNOWN_EXIT_SATURATION
        blocked = sum(1 for p in self.portfolio.open_positions.values() if p.status == "EXIT_BLOCKED")
        if blocked >= self.settings.max_exit_blocked_positions:
            return SAFETY_MAX_EXIT_BLOCKED
        return None

    def _cycle_summary(self, now: str, discovery_summary: dict, decisions: list[TradeDecision]) -> dict:
        open_positions = list(self.portfolio.open_positions.values())
        partial_positions = sum(1 for p in open_positions if p.status == "PARTIAL")
        exit_pending_positions = sum(
            1
            for p in open_positions
            if p.status == "EXIT_PENDING"
            or (p.status == POSITION_RECONCILE_PENDING and p.reconcile_context == "exit")
        )
        exit_blocked_positions = sum(1 for p in open_positions if p.status == "EXIT_BLOCKED")
        zombie_positions_count = sum(1 for p in open_positions if p.status == "ZOMBIE")
        final_zombie_positions_count = sum(1 for p in open_positions if p.status == POSITION_FINAL_ZOMBIE)
        exit_stuck_total = exit_blocked_positions + zombie_positions_count + final_zombie_positions_count
        zombie_locked_sol_estimate = 0.0
        recoverable_sol_estimate = 0.0
        dead_sol_estimate = 0.0
        for p in open_positions:
            if p.status not in {"ZOMBIE", POSITION_FINAL_ZOMBIE}:
                continue
            est = float(getattr(p, "last_estimated_exit_value_sol", None) or 0.0)
            if est <= 0.0:
                est = max(0.0, float(getattr(p, "entry_sol", 0.0) or 0.0))
            zombie_locked_sol_estimate += est
            zc = str(getattr(p, "zombie_class", "") or "UNKNOWN")
            if zc in {"SOFT_ZOMBIE", "FAKE_LIQUID"}:
                recoverable_sol_estimate += est
            elif zc in {"HARD_ZOMBIE"} or p.status == POSITION_FINAL_ZOMBIE:
                dead_sol_estimate += est
        entries_attempted = sum(1 for d in decisions if d.action == "BUY_SKIP" or d.action == "BUY")
        entries_succeeded = sum(1 for d in decisions if d.action == "BUY")
        exits_attempted = sum(1 for d in decisions if d.action == "SELL_ATTEMPT")
        exits_succeeded = sum(1 for d in decisions if d.action == "SELL")
        def _decision_classification(d: TradeDecision) -> str | None:
            try:
                return str((d.metadata or {}).get("classification") or "")
            except Exception:
                return None

        execution_failures = sum(
            1
            for d in decisions
            if d.action in {"SELL_BLOCKED", "BUY_SKIP"} and _is_system_execution_failure(_decision_classification(d))
        )
        entries_skipped_dry_run = sum(1 for d in decisions if d.action == "BUY_SKIP" and d.metadata.get("classification") == EXEC_SKIPPED_DRY_RUN)
        entries_skipped_live_disabled = sum(1 for d in decisions if d.action == "BUY_SKIP" and d.metadata.get("classification") == EXEC_SKIPPED_LIVE_DISABLED)
        entries_blocked_pre_execution = sum(1 for d in decisions if d.action == "BUY_SKIP" and d.metadata.get("phase") == "pre_entry_probe")
        entries_order_failed = sum(1 for d in decisions if d.action == "BUY_SKIP" and d.metadata.get("phase") == "order_build")
        entries_execute_failed = sum(1 for d in decisions if d.action == "BUY_SKIP" and d.metadata.get("phase") == "execute")
        # execution_failures already excludes mode-gated skips.
        unknown_exits = sum(1 for d in decisions if d.action == "SELL_PENDING")
        wallet_available_sol = self._wallet_available_sol
        if wallet_available_sol is None:
            available_sol = 0.0
        else:
            available_sol = min(float(self.portfolio.cash_sol), float(wallet_available_sol))
        birth_sol = getattr(self.settings, "hachi_birth_wallet_sol", None) or getattr(self.portfolio, "hachi_birth_wallet_sol", None)
        hachi_capacity_scale: float | None = None
        try:
            if birth_sol is not None and float(birth_sol) > 0:
                hachi_capacity_scale = float(available_sol) / float(birth_sol)
        except Exception:
            hachi_capacity_scale = None
        effective_max_daily_new_positions = self._effective_max_daily_new_positions()
        hard_max_daily_new_positions = int(
            getattr(self.settings, "hard_max_daily_new_positions", self.settings.max_daily_new_positions)
            or self.settings.max_daily_new_positions
        )
        return {
            "run_id": self.settings.run_id,
            "cycle_in_run": self._cycle_in_run,
            "timestamp": now,
            "discovery_failed": bool(discovery_summary.get("discovery_failed", False)),
            "discovery_error_type": discovery_summary.get("discovery_error_type"),
            "discovery_error": discovery_summary.get("discovery_error"),
            "seeds_total": discovery_summary.get("seeds_total", 0),
            "discovered_candidates": discovery_summary.get("discovered_candidates", 0),
            "prefiltered_candidates": discovery_summary.get("prefiltered_candidates", 0),
            "topn_candidates": discovery_summary.get("topn_candidates", 0),
            "route_checked_candidates": discovery_summary.get("route_checked_candidates", 0),
            "cache_hits": discovery_summary.get("cache_hits", 0),
            "cache_misses": discovery_summary.get("cache_misses", 0),
            "candidate_cache_hits": discovery_summary.get("candidate_cache_hits", 0),
            "candidate_cache_misses": discovery_summary.get("candidate_cache_misses", 0),
            "route_cache_hits": discovery_summary.get("route_cache_hits", 0),
            "route_cache_misses": discovery_summary.get("route_cache_misses", 0),
            "cache_debug_first_keys": discovery_summary.get("cache_debug_first_keys", []),
            "cache_debug_identity": discovery_summary.get("cache_debug_identity", {}),
            "cache_engine_identity": discovery_summary.get("cache_engine_identity", {}),
            "cache_debug_trace": discovery_summary.get("cache_debug_trace", {}),
            "birdeye_candidate_build_calls": discovery_summary.get("birdeye_candidate_build_calls", 0),
            "jupiter_buy_probe_calls": discovery_summary.get("jupiter_buy_probe_calls", 0),
            "jupiter_sell_probe_calls": discovery_summary.get("jupiter_sell_probe_calls", 0),
            "discovery_cached": bool(discovery_summary.get("discovery_cached", False)),
            "candidates_built": discovery_summary.get("candidates_built", 0),
            "candidates_accepted": discovery_summary.get("candidates_accepted", 0),
            "candidates_rejected_total": discovery_summary.get("candidates_rejected_total", 0),
            "rejection_counts": discovery_summary.get("rejection_counts", {}),
            "open_positions": len(open_positions),
            "partial_positions": partial_positions,
            "exit_pending_positions": exit_pending_positions,
            "exit_blocked_positions": exit_blocked_positions,
            "zombie_positions": zombie_positions_count,
            "final_zombie_positions": final_zombie_positions_count,
            "exit_stuck_total": exit_stuck_total,
            "zombie_locked_sol_estimate": round(zombie_locked_sol_estimate, 6),
            "recoverable_sol_estimate": round(recoverable_sol_estimate, 6),
            "dead_sol_estimate": round(dead_sol_estimate, 6),
            "entries_attempted": entries_attempted,
            "entries_succeeded": entries_succeeded,
            "entries_blocked_pre_execution": entries_blocked_pre_execution,
            "entries_order_failed": entries_order_failed,
            "entries_execute_failed": entries_execute_failed,
            "entries_skipped_dry_run": entries_skipped_dry_run,
            "entries_skipped_live_disabled": entries_skipped_live_disabled,
            "exits_attempted": exits_attempted,
            "exits_succeeded": exits_succeeded,
            "execution_failures": execution_failures,
            "unknown_exits": unknown_exits,
            "max_daily_new_positions": self.settings.max_daily_new_positions,
            "hard_max_daily_new_positions": hard_max_daily_new_positions,
            "effective_max_daily_new_positions": effective_max_daily_new_positions,
            "opened_today_count": self.portfolio.opened_today_count,
            "hachi_capacity_scale": hachi_capacity_scale,
            "safe_mode_active": self.portfolio.safe_mode_active,
            "cash_sol": round(float(self.portfolio.cash_sol), 6),
            "wallet_available_sol": wallet_available_sol,
            "deployable_sol": None if self._last_deployable_sol is None else round(float(self._last_deployable_sol), 6),
            "accounting_drift_sol": (
                None if self._last_accounting_drift_sol is None else round(float(self._last_accounting_drift_sol), 6)
            ),
            "entries_blocked_reason": self._entries_blocked_reason,
            "reconciled_cash_sol": (
                None if self._last_reconciled_cash_sol is None else round(float(self._last_reconciled_cash_sol), 6)
            ),
            "reconciliation_applied": bool(self._last_reconciliation_applied),
            "reconciliation_delta_sol": (
                None if self._last_reconciliation_delta_sol is None else round(float(self._last_reconciliation_delta_sol), 6)
            ),
            "derived_policy": None if self._last_runtime_policy is None else self._last_runtime_policy.to_dict(),
        }


def _age_minutes(ts: str) -> float:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return 0.0
    return max(0.0, (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() / 60.0)


def _age_seconds_between(current_ts: str, checked_ts: str) -> int | None:
    try:
        current = datetime.fromisoformat(current_ts.replace("Z", "+00:00")).astimezone(timezone.utc)
        checked = datetime.fromisoformat(checked_ts.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None
    return max(0, int((current - checked).total_seconds()))


def _cooldown_active(ts: str | None, cooldown_minutes: int) -> bool:
    if not ts:
        return False
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return False
    return datetime.now(timezone.utc) - dt.astimezone(timezone.utc) < timedelta(minutes=cooldown_minutes)


def _retry_due(ts: str, now: str) -> bool:
    try:
        due = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        now_dt = datetime.fromisoformat(now.replace("Z", "+00:00"))
    except Exception:
        return True
    return now_dt.astimezone(timezone.utc) >= due.astimezone(timezone.utc)


def _next_retry_at(now: str, retry_count: int) -> str:
    try:
        now_dt = datetime.fromisoformat(now.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        now_dt = datetime.now(timezone.utc)
    try:
        rc = int(retry_count)
    except Exception:
        rc = 1
    rc = max(1, min(MAX_EXIT_RETRY_COUNT, rc))
    # Bound exponentiation to avoid gigantic integers, then cap the final delay.
    exp = min(60, max(0, rc - 1))
    try:
        delay_seconds = int(EXIT_RETRY_BASE_SECONDS) * (2**exp)
    except Exception:
        delay_seconds = int(EXIT_RETRY_BASE_SECONDS)
    delay_seconds = max(1, min(int(MAX_EXIT_RETRY_DELAY_SECONDS), int(delay_seconds)))
    try:
        return (now_dt + timedelta(seconds=delay_seconds)).isoformat()
    except OverflowError:
        # Final safety net: never crash the runtime due to scheduling math.
        return (now_dt + timedelta(seconds=int(MAX_EXIT_RETRY_DELAY_SECONDS))).isoformat()


def _next_normal_retry_at(now: str, interval_seconds: int) -> str:
    try:
        now_dt = datetime.fromisoformat(now.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        now_dt = datetime.now(timezone.utc)
    return (now_dt + timedelta(seconds=max(1, int(interval_seconds)))).isoformat()


def _extract_jupiter_error_code(response_body: object) -> str | None:
    if response_body is None:
        return None
    if isinstance(response_body, dict):
        val = response_body.get("errorCode")
        return str(val) if val is not None else None
    if isinstance(response_body, str):
        try:
            parsed = json.loads(response_body)
            if isinstance(parsed, dict):
                val = parsed.get("errorCode")
                return str(val) if val is not None else None
        except Exception:
            return None
    return None


def _probe_sanity_reason(probe) -> str | None:
    out = probe.out_amount_atomic
    if out is None or out <= 0:
        return REJECT_QUOTE_OUTPUT_TOO_LOW
    impact = probe.price_impact_bps
    if impact is not None and abs(float(impact)) >= MAX_ABS_PRICE_IMPACT_BPS:
        return REJECT_QUOTE_PRICE_IMPACT_INVALID
    return None


def _clear_drip_state(position: PositionState) -> None:
    """Reset drip control fields on a position (call on reconcile, failure, or completion).

    ``drip_chunks_done`` is intentionally preserved here so callers can inspect how
    many chunks fired before the drip ended.  It is reset to 0 only when a *new*
    drip is started in ``_start_exit``.
    """
    position.drip_exit_active = False
    position.drip_exit_reason = None
    position.drip_qty_remaining_atomic = None
    position.drip_next_chunk_at = None


def _is_drip_eligible(reason: str) -> bool:
    """Return True if the exit reason should use drip (chunked) selling.

    Only take-profit exits use drip by default; stop-loss / time-stop / trailing-stop
    / liquidity exits are hard exits that should clear as quickly as possible.
    """
    return reason.startswith("take_profit_")


def _roundtrip_sanity_reason(buy_probe, sell_probe) -> str | None:
    buy_in = int(buy_probe.input_amount_atomic or 0)
    sell_out = int(sell_probe.out_amount_atomic or 0)
    if buy_in <= 0 or sell_out <= 0:
        return REJECT_QUOTE_OUTPUT_TOO_LOW
    ratio = sell_out / max(buy_in, 1)
    if ratio < MIN_ROUNDTRIP_RETURN_RATIO:
        return REJECT_ECONOMIC_SANITY_FAILED
    return None
