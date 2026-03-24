from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from creeper_dripper.clients.birdeye import BirdeyeClient
from creeper_dripper.config import Settings
from creeper_dripper.errors import (
    EXIT_UNKNOWN_PENDING_RECONCILE,
    JOURNAL_APPEND_FAILED,
    SAFETY_DAILY_LOSS_CAP,
    SAFETY_MAX_CONSEC_EXEC_FAILURES,
    SAFETY_MAX_EXIT_BLOCKED,
    SAFETY_STALE_MARKET_DATA,
    SAFETY_UNKNOWN_EXIT_SATURATION,
    STATE_SAVE_FAILED,
)
from creeper_dripper.engine.discovery import discover_candidates
from creeper_dripper.execution.executor import TradeExecutor
from creeper_dripper.models import PortfolioState, PositionState, TakeProfitStep, TokenCandidate, TradeDecision
from creeper_dripper.observability import EventCollector
from creeper_dripper.storage.recovery import run_startup_recovery
from creeper_dripper.storage.state import save_portfolio, save_status_snapshot
from creeper_dripper.utils import append_jsonl, utc_now_iso

LOGGER = logging.getLogger(__name__)
MAX_EXIT_RETRIES = 5
EXIT_RETRY_BASE_SECONDS = 30


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

    def run_cycle(self) -> dict:
        now = utc_now_iso()
        self._reset_daily_counters(now)
        decisions: list[TradeDecision] = []
        if not self._startup_recovery_done:
            recovery_decisions = run_startup_recovery(self.portfolio, self.executor, now)
            decisions.extend(recovery_decisions)
            for decision in recovery_decisions:
                self.events.emit("recovery_action", decision.reason, action=decision.action, token_mint=decision.token_mint)
            self._startup_recovery_done = True
        candidates, discovery_summary = discover_candidates(self.birdeye, self.executor.jupiter, self.settings)
        safe_mode_reason = self._evaluate_safety(now)
        if safe_mode_reason:
            self.portfolio.safe_mode_active = True
            self.portfolio.safety_stop_reason = safe_mode_reason
            self.events.emit("safety_stop", safe_mode_reason, open_positions=len(self.portfolio.open_positions))
        else:
            self.portfolio.safe_mode_active = False
            self.portfolio.safety_stop_reason = None

        self._mark_positions(candidates, decisions, now)
        if not self.portfolio.safe_mode_active:
            self._maybe_open_positions(candidates, decisions, now)
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
                continue
            candidate = by_mint.get(mint)
            if candidate is None:
                try:
                    seed = {"address": mint, "symbol": position.symbol, "decimals": position.decimals}
                    candidate = self.birdeye.build_candidate(seed)
                except Exception as exc:
                    LOGGER.warning("mark build failed for %s: %s", mint, exc)
                    continue
            price = candidate.price_usd or position.last_price_usd
            position.last_price_usd = price
            position.updated_at = now
            position.last_exit_liquidity_usd = candidate.exit_liquidity_usd
            position.peak_price_usd = max(position.peak_price_usd, price)
            if position.status == "EXIT_PENDING":
                self._retry_pending_exit(position, decisions, now)
                continue
            self._evaluate_exit_rules(position, candidate, decisions, now)

    def _evaluate_exit_rules(self, position: PositionState, candidate: TokenCandidate, decisions: list[TradeDecision], now: str) -> None:
        if position.status not in {"OPEN", "PARTIAL"}:
            return
        pnl_pct = ((position.last_price_usd - position.avg_entry_price_usd) / position.avg_entry_price_usd) * 100.0 if position.avg_entry_price_usd else 0.0
        age_minutes = _age_minutes(position.opened_at)
        liquidity_ratio = None
        if position.exit_liquidity_at_entry_usd and position.last_exit_liquidity_usd:
            liquidity_ratio = position.last_exit_liquidity_usd / max(position.exit_liquidity_at_entry_usd, 1.0)

        if self.settings.force_full_exit_on_liquidity_break and liquidity_ratio is not None and liquidity_ratio < self.settings.liquidity_break_ratio:
            self._start_exit(position, position.remaining_qty_atomic, "liquidity_break", decisions, now)
            return

        if pnl_pct <= -abs(position.stop_loss_pct):
            self._start_exit(position, position.remaining_qty_atomic, "stop_loss", decisions, now)
            return

        if pnl_pct >= position.trailing_arm_pct:
            trail_floor = position.peak_price_usd * (1.0 - position.trailing_stop_pct / 100.0)
            if position.last_price_usd <= trail_floor:
                self._start_exit(position, position.remaining_qty_atomic, "trailing_stop", decisions, now)
                return

        if age_minutes >= self.settings.time_stop_minutes and pnl_pct < 12.0:
            self._start_exit(position, position.remaining_qty_atomic, "time_stop", decisions, now)
            return

        for step in position.take_profit_steps:
            if step.done:
                continue
            if pnl_pct >= step.trigger_pct:
                qty = max(1, int(position.remaining_qty_atomic * step.fraction))
                triggered = self._start_exit(position, qty, f"take_profit_{int(step.trigger_pct)}", decisions, now)
                if triggered:
                    step.done = True
                    if position.status != "OPEN":
                        return

    def _start_exit(self, position: PositionState, qty_atomic: int, reason: str, decisions: list[TradeDecision], now: str) -> bool:
        if position.status == "EXIT_PENDING":
            return False
        qty_atomic = min(max(1, qty_atomic), position.remaining_qty_atomic)
        position.status = "EXIT_PENDING"
        position.pending_exit_reason = reason
        position.pending_exit_qty_atomic = qty_atomic
        position.updated_at = now
        decisions.append(TradeDecision(action="EXIT_PENDING", token_mint=position.token_mint, symbol=position.symbol, reason=reason, qty_atomic=qty_atomic))
        self._attempt_exit(position, decisions, now)
        return True

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

    def _attempt_exit(self, position: PositionState, decisions: list[TradeDecision], now: str) -> None:
        if position.pending_exit_qty_atomic is None or position.pending_exit_qty_atomic <= 0:
            position.status = "EXIT_BLOCKED"
            decisions.append(TradeDecision(action="SELL_BLOCKED", token_mint=position.token_mint, symbol=position.symbol, reason="missing_pending_qty"))
            return
        requested_qty = min(position.pending_exit_qty_atomic, position.remaining_qty_atomic)
        wallet_balance = self.executor.wallet_token_balance_atomic(position.token_mint)
        if wallet_balance is not None and wallet_balance < requested_qty:
            LOGGER.warning("sell balance discrepancy position_id=%s mint=%s expected=%s wallet=%s", position.position_id or position.token_mint, position.token_mint, requested_qty, wallet_balance)
            requested_qty = max(0, wallet_balance)
            position.pending_exit_qty_atomic = requested_qty
            decisions.append(TradeDecision(action="SELL_BALANCE_ADJUSTED", token_mint=position.token_mint, symbol=position.symbol, reason="wallet_balance_below_expected", qty_atomic=requested_qty))
        if requested_qty <= 0:
            position.status = "EXIT_BLOCKED"
            position.exit_retry_count += 1
            position.last_exit_attempt_at = now
            position.next_exit_retry_at = _next_retry_at(now, position.exit_retry_count)
            decisions.append(TradeDecision(action="SELL_BLOCKED", token_mint=position.token_mint, symbol=position.symbol, reason="wallet_balance_zero", qty_atomic=0))
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
                },
            )
        )

        if result.status == "success":
            sold_atomic = min(result.executed_amount or requested_qty, position.remaining_qty_atomic)
            if sold_atomic <= 0:
                position.status = "EXIT_BLOCKED"
                position.exit_retry_count += 1
                position.last_exit_attempt_at = now
                position.next_exit_retry_at = _next_retry_at(now, position.exit_retry_count)
                decisions.append(TradeDecision(action="SELL_BLOCKED", token_mint=position.token_mint, symbol=position.symbol, reason="success_without_executed_amount", qty_atomic=requested_qty))
                return
            sold_qty_atomic = sold_atomic
            sold_fraction = sold_qty_atomic / max(position.remaining_qty_atomic, 1)
            sold_ui = position.remaining_qty_ui * sold_fraction
            position.remaining_qty_atomic -= sold_qty_atomic
            position.remaining_qty_ui = max(0.0, position.remaining_qty_ui - sold_ui)
            out_sol = None
            if result.output_amount is not None:
                out_sol = result.output_amount / 1_000_000_000
                position.realized_sol += out_sol
                self.portfolio.cash_sol += out_sol
            else:
                position.pending_proceeds_sol += 0.0
            position.last_sell_signature = result.signature
            position.exit_retry_count = 0
            position.last_exit_attempt_at = now
            position.next_exit_retry_at = None
            position.pending_exit_qty_atomic = None
            position.pending_exit_reason = None
            position.pending_exit_signature = None
            decisions.append(
                TradeDecision(
                    action="SELL",
                    token_mint=position.token_mint,
                    symbol=position.symbol,
                    reason="exit_success",
                    qty_atomic=sold_qty_atomic,
                    qty_ui=sold_ui,
                    metadata={"out_sol": out_sol, "signature": result.signature, "partial": result.is_partial, "proceeds_pending_reconcile": out_sol is None},
                )
            )
            self.events.emit("exit_success", result.diagnostic_code or "success", position_id=position.position_id or position.token_mint, qty=sold_qty_atomic)
            if position.remaining_qty_atomic <= 0 or position.remaining_qty_ui <= 0.0:
                position.status = "CLOSED"
                self.portfolio.total_realized_sol += position.realized_sol - position.entry_sol
                self.portfolio.closed_positions.append(position)
                self.portfolio.open_positions.pop(position.token_mint, None)
                self.portfolio.cooldowns[position.token_mint] = now
            else:
                position.status = "PARTIAL"
                position.updated_at = now
            return

        position.exit_retry_count += 1
        position.last_exit_attempt_at = now
        position.next_exit_retry_at = _next_retry_at(now, position.exit_retry_count)
        if result.status == "failed":
            position.status = "EXIT_BLOCKED"
            decisions.append(TradeDecision(action="SELL_BLOCKED", token_mint=position.token_mint, symbol=position.symbol, reason=f"execution_failed:{result.error or 'unknown'}", qty_atomic=requested_qty))
            self.portfolio.consecutive_execution_failures += 1
            self.events.emit("exit_failed", result.diagnostic_code or "failed", position_id=position.position_id or position.token_mint, error=result.error or "unknown")
        else:
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
            execution, quote = self.executor.buy(candidate, size_sol)
            self.events.emit("entry_attempt", "discovery_entry", token_mint=candidate.address, size_sol=size_sol)
            if execution.status != "success":
                decisions.append(
                    TradeDecision(
                        action="BUY_SKIP",
                        token_mint=candidate.address,
                        symbol=candidate.symbol,
                        reason=f"execution_{execution.status}",
                        size_sol=size_sol,
                        metadata={"error": execution.error, "price_impact_bps": quote.price_impact_bps},
                    )
                )
                self.portfolio.consecutive_execution_failures += 1
                self.events.emit("entry_failed", execution.diagnostic_code or f"execution_{execution.status}", token_mint=candidate.address, error=execution.error or "unknown")
                continue
            qty_atomic = execution.executed_amount
            if qty_atomic is None or qty_atomic <= 0:
                decisions.append(TradeDecision(action="BUY_SKIP", token_mint=candidate.address, symbol=candidate.symbol, reason="missing_executed_amount", size_sol=size_sol))
                continue
            entry_price = candidate.price_usd or 0.0
            if entry_price <= 0 or not candidate.decimals:
                continue
            qty_ui = qty_atomic / (10 ** candidate.decimals)
            position = PositionState(
                token_mint=candidate.address,
                symbol=candidate.symbol,
                decimals=candidate.decimals,
                status="OPEN",
                opened_at=now,
                updated_at=now,
                entry_price_usd=entry_price,
                avg_entry_price_usd=entry_price,
                entry_sol=size_sol,
                remaining_qty_atomic=qty_atomic,
                remaining_qty_ui=qty_ui,
                peak_price_usd=entry_price,
                last_price_usd=entry_price,
                position_id=f"{candidate.address}:{now}",
                stop_loss_pct=self.settings.stop_loss_pct,
                trailing_stop_pct=self.settings.trailing_stop_pct,
                trailing_arm_pct=self.settings.trailing_arm_pct,
                exit_liquidity_at_entry_usd=candidate.exit_liquidity_usd,
                last_exit_liquidity_usd=candidate.exit_liquidity_usd,
                take_profit_steps=[TakeProfitStep(trigger_pct=lvl, fraction=frac) for lvl, frac in zip(self.settings.take_profit_levels_pct, self.settings.take_profit_fractions)],
                notes=[f"score={candidate.discovery_score}", *candidate.reasons],
            )
            self.portfolio.open_positions[candidate.address] = position
            self.portfolio.cash_sol -= size_sol
            self.portfolio.opened_today_count += 1
            decisions.append(TradeDecision(action="BUY", token_mint=candidate.address, symbol=candidate.symbol, reason="discovery_entry", size_sol=size_sol, qty_atomic=qty_atomic, qty_ui=qty_ui, metadata={"score": candidate.discovery_score, "price_impact_bps": quote.price_impact_bps, "signature": execution.signature}))
            self.portfolio.consecutive_execution_failures = 0
            self.events.emit("entry_success", execution.diagnostic_code or "success", token_mint=candidate.address, qty_atomic=qty_atomic)
            if len(self.portfolio.open_positions) >= self.settings.max_open_positions:
                break

    def _persist_cycle(self, now: str, decisions: list[TradeDecision], cycle_summary: dict) -> None:
        try:
            save_portfolio(self.settings.state_path, self.portfolio)
        except Exception as exc:
            LOGGER.error("%s path=%s error=%s", STATE_SAVE_FAILED, self.settings.state_path, exc)
            self.events.emit("persistence_issue", STATE_SAVE_FAILED, path=str(self.settings.state_path), error=str(exc))
            raise
        for decision in decisions:
            try:
                append_jsonl(self.settings.journal_path, {"ts": now, **asdict(decision)})
            except Exception as exc:
                LOGGER.error("%s path=%s error=%s", JOURNAL_APPEND_FAILED, self.settings.journal_path, exc)
                self.events.emit("persistence_issue", JOURNAL_APPEND_FAILED, path=str(self.settings.journal_path), error=str(exc))
                raise
        status_path = self.settings.runtime_dir / "status.json"
        try:
            top_rejections = sorted(
                cycle_summary.get("rejection_counts", {}).items(),
                key=lambda kv: kv[1],
                reverse=True,
            )[:5]
            save_status_snapshot(
                status_path,
                {
                    "cycle_timestamp": now,
                    "safe_mode_active": self.portfolio.safe_mode_active,
                    "safety_stop_reason": self.portfolio.safety_stop_reason,
                    "summary": cycle_summary,
                    "top_rejection_reasons": [{"reason": k, "count": v} for k, v in top_rejections],
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

    def _evaluate_safety(self, now: str) -> str | None:
        if self.portfolio.total_realized_sol <= -abs(self.settings.daily_realized_loss_cap_sol):
            return SAFETY_DAILY_LOSS_CAP
        if self.portfolio.consecutive_execution_failures >= self.settings.max_consecutive_execution_failures:
            return SAFETY_MAX_CONSEC_EXEC_FAILURES
        if self.portfolio.last_cycle_at and _age_minutes(self.portfolio.last_cycle_at) > self.settings.stale_market_data_minutes:
            return SAFETY_STALE_MARKET_DATA
        unknown_exits = sum(1 for p in self.portfolio.open_positions.values() if p.status == "EXIT_PENDING")
        if unknown_exits >= self.settings.unknown_exit_saturation_limit:
            return SAFETY_UNKNOWN_EXIT_SATURATION
        blocked = sum(1 for p in self.portfolio.open_positions.values() if p.status == "EXIT_BLOCKED")
        if blocked >= self.settings.max_exit_blocked_positions:
            return SAFETY_MAX_EXIT_BLOCKED
        return None

    def _cycle_summary(self, now: str, discovery_summary: dict, decisions: list[TradeDecision]) -> dict:
        open_positions = list(self.portfolio.open_positions.values())
        partial_positions = sum(1 for p in open_positions if p.status == "PARTIAL")
        exit_pending_positions = sum(1 for p in open_positions if p.status == "EXIT_PENDING")
        exit_blocked_positions = sum(1 for p in open_positions if p.status == "EXIT_BLOCKED")
        entries_attempted = sum(1 for d in decisions if d.action == "BUY_SKIP" or d.action == "BUY")
        entries_succeeded = sum(1 for d in decisions if d.action == "BUY")
        exits_attempted = sum(1 for d in decisions if d.action == "SELL_ATTEMPT")
        exits_succeeded = sum(1 for d in decisions if d.action == "SELL")
        execution_failures = sum(1 for d in decisions if d.action in {"SELL_BLOCKED", "BUY_SKIP"})
        unknown_exits = sum(1 for d in decisions if d.action == "SELL_PENDING")
        return {
            "timestamp": now,
            "seeds_total": discovery_summary.get("seeds_total", 0),
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
