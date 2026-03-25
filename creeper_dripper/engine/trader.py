from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from creeper_dripper.clients.birdeye import BirdeyeClient
from creeper_dripper.cache import TTLCache
from creeper_dripper.config import SOL_MINT, Settings
from creeper_dripper.errors import (
    EXEC_SKIPPED_DRY_RUN,
    EXEC_SKIPPED_LIVE_DISABLED,
    EXIT_UNKNOWN_PENDING_RECONCILE,
    JOURNAL_APPEND_FAILED,
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
from creeper_dripper.engine.position_pricing import (
    SOURCE_JUPITER_SELL,
    VALUATION_STATUS_NO_ROUTE,
    VALUATION_STATUS_OK,
    ensure_entry_sol_mark,
    extract_sell_quote_liquidity,
    is_valid_sol_mark,
    resolve_position_valuation,
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
        if self.settings.run_id:
            self.portfolio.run_id = self.settings.run_id
        LOGGER.info(
            "cache_engine_init: candidate_cache_id=%s route_cache_id=%s candidate_ttl_s=%s route_ttl_s=%s",
            id(self._candidate_cache),
            id(self._route_cache),
            settings.candidate_cache_ttl_seconds,
            settings.route_check_cache_ttl_seconds,
        )

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
        try:
            candidates, discovery_summary = self._discover_with_cadence()
        except Exception as exc:
            LOGGER.error("event=discovery_failed error=%s", exc, exc_info=True)
            candidates = []
            discovery_summary = self._failed_discovery_summary()
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

        if not self.portfolio.safe_mode_active:
            self._maybe_open_positions(candidates, decisions, now)
        self._mark_positions(candidates, decisions, now)
        self.portfolio.last_cycle_at = now
        cycle_summary = self._cycle_summary(now, discovery_summary, decisions)
        self._persist_cycle(now, decisions, cycle_summary)
        self.events.emit("cycle_summary", "ok", **cycle_summary)
        return {
            "timestamp": now,
            "cash_sol": round(self.portfolio.cash_sol, 6),
            "open_positions": len(self.portfolio.open_positions),
            "candidate_symbols": [c.symbol for c in candidates],
            "decisions": [asdict(d) for d in decisions],
            "summary": cycle_summary,
            "events": self.events.to_dicts(),
        }

    def _discover_with_cadence(self) -> tuple[list[TokenCandidate], dict]:
        now_dt = datetime.now(timezone.utc)
        if self._last_discovery_at is not None:
            elapsed = (now_dt - self._last_discovery_at).total_seconds()
            if elapsed < self.settings.discovery_interval_seconds:
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
            if position.status == "EXIT_BLOCKED":
                self._retry_blocked_exit_if_due(position, decisions, now)
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

    def _start_exit(self, position: PositionState, qty_atomic: int, reason: str, decisions: list[TradeDecision], now: str) -> bool:
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
        """Resolve ENTRY_RECONCILE_PENDING using wallet RPC as secondary truth (never zero out blindly)."""
        bal = self.executor.wallet_token_balance_atomic(position.token_mint)
        if bal is not None and bal > 0:
            denom = 10 ** max(position.decimals, 0)
            position.remaining_qty_atomic = bal
            position.remaining_qty_ui = (bal / denom) if denom else float(bal)
            if position.entry_sol > 0.0 and position.remaining_qty_ui > 0.0:
                position.entry_mark_sol_per_token = position.entry_sol / max(position.remaining_qty_ui, 1e-30)
                if not is_valid_sol_mark(position.last_mark_sol_per_token):
                    position.last_mark_sol_per_token = position.entry_mark_sol_per_token
                if not is_valid_sol_mark(position.peak_mark_sol_per_token):
                    position.peak_mark_sol_per_token = position.entry_mark_sol_per_token
                else:
                    position.peak_mark_sol_per_token = max(
                        float(position.peak_mark_sol_per_token),
                        float(position.entry_mark_sol_per_token),
                    )
            position.status = "OPEN"
            position.reconcile_context = None
            position.updated_at = now
            decisions.append(
                TradeDecision(
                    action="RECOVERY_CORRECTION",
                    token_mint=position.token_mint,
                    symbol=position.symbol,
                    reason="entry_settlement_resolved_wallet",
                    qty_atomic=bal,
                    metadata={"classification": "entry_settlement_resolved_wallet"},
                )
            )
            return
        if bal == 0:
            LOGGER.warning(
                "entry_reconcile_still_zero mint=%s position_id=%s (keeping RECONCILE_PENDING)",
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
        wallet_balance = self.executor.wallet_token_balance_atomic(position.token_mint)
        if wallet_balance is not None and wallet_balance < requested_qty:
            LOGGER.warning("sell balance discrepancy position_id=%s mint=%s expected=%s wallet=%s", position.position_id or position.token_mint, position.token_mint, requested_qty, wallet_balance)
            requested_qty = max(0, wallet_balance)
            if not position.drip_exit_active:
                # Drip: pending_exit_qty_atomic holds the total drip target; do not clobber it.
                position.pending_exit_qty_atomic = requested_qty
            decisions.append(TradeDecision(action="SELL_BALANCE_ADJUSTED", token_mint=position.token_mint, symbol=position.symbol, reason="wallet_balance_below_expected", qty_atomic=requested_qty))
        if requested_qty <= 0:
            _clear_drip_state(position)
            position.status = POSITION_RECONCILE_PENDING
            position.reconcile_context = "exit"
            position.exit_retry_count += 1
            position.last_exit_attempt_at = now
            position.next_exit_retry_at = _next_retry_at(now, position.exit_retry_count)
            decisions.append(
                TradeDecision(
                    action="SELL_SETTLEMENT_PENDING",
                    token_mint=position.token_mint,
                    symbol=position.symbol,
                    reason="wallet_balance_zero_reconcile_pending",
                    qty_atomic=position.remaining_qty_atomic,
                    metadata={"classification": SETTLEMENT_UNCONFIRMED},
                )
            )
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
                position.exit_retry_count += 1
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
            if result.output_amount is not None:
                out_sol = max(0.0, float(result.output_amount) / 1_000_000_000.0)
                position.realized_sol += out_sol
                self.portfolio.cash_sol += out_sol
            else:
                # SOL proceeds unavailable from both Jupiter and RPC — do NOT credit cash,
                # do NOT close the position. Mark RECONCILE_PENDING so the operator can
                # investigate and so the position remains visible in state.
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
                position.exit_retry_count += 1
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
            position.exit_retry_count += 1
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
                position.exit_retry_count += 1
                position.last_exit_attempt_at = now
                position.next_exit_retry_at = _next_retry_at(now, position.exit_retry_count)
                self.portfolio.consecutive_execution_failures += 1
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
            position.status = "EXIT_PENDING"
            position.pending_exit_signature = result.signature
            decisions.append(TradeDecision(action="SELL_PENDING", token_mint=position.token_mint, symbol=position.symbol, reason=f"execution_unknown:{result.error or 'unknown'}", qty_atomic=requested_qty))
            LOGGER.warning("%s position_id=%s reason=%s", EXIT_UNKNOWN_PENDING_RECONCILE, position.position_id or position.token_mint, result.error or "unknown")
            self.events.emit("exit_failed", result.diagnostic_code or EXIT_UNKNOWN_PENDING_RECONCILE, position_id=position.position_id or position.token_mint, error=result.error or "unknown")

    def _maybe_open_positions(self, candidates: list[TokenCandidate], decisions: list[TradeDecision], now: str) -> None:
        if len(self.portfolio.open_positions) >= self.settings.max_open_positions:
            return
        if self.portfolio.opened_today_count >= self.settings.max_daily_new_positions:
            return
        for candidate in candidates:
            if candidate.address in self.portfolio.open_positions:
                continue
            if _cooldown_active(self.portfolio.cooldowns.get(candidate.address), self.settings.cooldown_minutes_after_exit):
                continue
            size_sol = min(self.settings.base_position_size_sol, self.settings.max_position_size_sol)
            if self.portfolio.cash_sol - size_sol < self.settings.cash_reserve_sol:
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
                        },
                    )
                )
                self.events.emit("entry_failed", SETTLEMENT_UNCONFIRMED, token_mint=candidate.address, symbol=candidate.symbol, phase="post_execute_settlement")
                if len(self.portfolio.open_positions) >= self.settings.max_open_positions:
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
            if len(self.portfolio.open_positions) >= self.settings.max_open_positions:
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
        status_payload = {
            "run_id": self.settings.run_id,
            "run_dir": str(self.settings.run_dir) if self.settings.run_dir else None,
            "cycle_in_run": self._cycle_in_run,
            "cycle_timestamp": now,
            "safe_mode_active": self.portfolio.safe_mode_active,
            "safety_stop_reason": self.portfolio.safety_stop_reason,
            "summary": cycle_summary,
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
        if market_data_checked_at:
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
        entries_attempted = sum(1 for d in decisions if d.action == "BUY_SKIP" or d.action == "BUY")
        entries_succeeded = sum(1 for d in decisions if d.action == "BUY")
        exits_attempted = sum(1 for d in decisions if d.action == "SELL_ATTEMPT")
        exits_succeeded = sum(1 for d in decisions if d.action == "SELL")
        execution_failures = sum(1 for d in decisions if d.action in {"SELL_BLOCKED", "BUY_SKIP"})
        entries_skipped_dry_run = sum(1 for d in decisions if d.action == "BUY_SKIP" and d.metadata.get("classification") == EXEC_SKIPPED_DRY_RUN)
        entries_skipped_live_disabled = sum(1 for d in decisions if d.action == "BUY_SKIP" and d.metadata.get("classification") == EXEC_SKIPPED_LIVE_DISABLED)
        entries_blocked_pre_execution = sum(1 for d in decisions if d.action == "BUY_SKIP" and d.metadata.get("phase") == "pre_entry_probe")
        entries_order_failed = sum(1 for d in decisions if d.action == "BUY_SKIP" and d.metadata.get("phase") == "order_build")
        entries_execute_failed = sum(1 for d in decisions if d.action == "BUY_SKIP" and d.metadata.get("phase") == "execute")
        execution_failures = max(
            0,
            execution_failures - entries_skipped_dry_run - entries_skipped_live_disabled,
        )
        unknown_exits = sum(1 for d in decisions if d.action == "SELL_PENDING")
        return {
            "run_id": self.settings.run_id,
            "cycle_in_run": self._cycle_in_run,
            "timestamp": now,
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
            "safe_mode_active": self.portfolio.safe_mode_active,
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
    delay_seconds = EXIT_RETRY_BASE_SECONDS * (2 ** max(0, retry_count - 1))
    return (now_dt + timedelta(seconds=delay_seconds)).isoformat()


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
