from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from creeper_dripper.clients.birdeye import BirdeyeClient
from creeper_dripper.config import Settings
from creeper_dripper.engine.discovery import discover_candidates
from creeper_dripper.execution.executor import TradeExecutor
from creeper_dripper.models import PortfolioState, PositionState, TakeProfitStep, TokenCandidate, TradeDecision
from creeper_dripper.storage.state import save_portfolio
from creeper_dripper.utils import append_jsonl, utc_now_iso

LOGGER = logging.getLogger(__name__)


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

    def run_cycle(self) -> dict:
        now = utc_now_iso()
        candidates = discover_candidates(self.birdeye, self.executor.jupiter, self.settings)
        decisions: list[TradeDecision] = []

        self._mark_positions(candidates, decisions, now)
        self._maybe_open_positions(candidates, decisions, now)
        self.portfolio.last_cycle_at = now
        save_portfolio(self.settings.state_path, self.portfolio)
        for decision in decisions:
            append_jsonl(self.settings.journal_path, {"ts": now, **asdict(decision)})
        return {
            "timestamp": now,
            "cash_sol": round(self.portfolio.cash_sol, 6),
            "open_positions": len(self.portfolio.open_positions),
            "candidate_symbols": [c.symbol for c in candidates],
            "decisions": [asdict(d) for d in decisions],
        }

    def _mark_positions(self, candidates: list[TokenCandidate], decisions: list[TradeDecision], now: str) -> None:
        by_mint = {c.address: c for c in candidates}
        for mint, position in list(self.portfolio.open_positions.items()):
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
            self._evaluate_exit_rules(position, candidate, decisions, now)

    def _evaluate_exit_rules(self, position: PositionState, candidate: TokenCandidate, decisions: list[TradeDecision], now: str) -> None:
        pnl_pct = ((position.last_price_usd - position.avg_entry_price_usd) / position.avg_entry_price_usd) * 100.0 if position.avg_entry_price_usd else 0.0
        age_minutes = _age_minutes(position.opened_at)
        liquidity_ratio = None
        if position.exit_liquidity_at_entry_usd and position.last_exit_liquidity_usd:
            liquidity_ratio = position.last_exit_liquidity_usd / max(position.exit_liquidity_at_entry_usd, 1.0)

        if self.settings.force_full_exit_on_liquidity_break and liquidity_ratio is not None and liquidity_ratio < self.settings.liquidity_break_ratio:
            self._sell_position(position, position.remaining_qty_atomic, "liquidity_break", decisions, now)
            return

        if pnl_pct <= -abs(position.stop_loss_pct):
            self._sell_position(position, position.remaining_qty_atomic, "stop_loss", decisions, now)
            return

        if pnl_pct >= position.trailing_arm_pct:
            trail_floor = position.peak_price_usd * (1.0 - position.trailing_stop_pct / 100.0)
            if position.last_price_usd <= trail_floor:
                self._sell_position(position, position.remaining_qty_atomic, "trailing_stop", decisions, now)
                return

        if age_minutes >= self.settings.time_stop_minutes and pnl_pct < 12.0:
            self._sell_position(position, position.remaining_qty_atomic, "time_stop", decisions, now)
            return

        for step in position.take_profit_steps:
            if step.done:
                continue
            if pnl_pct >= step.trigger_pct:
                qty = max(1, int(position.remaining_qty_atomic * step.fraction))
                self._sell_position(position, qty, f"take_profit_{int(step.trigger_pct)}", decisions, now)
                step.done = True
                if position.status != "OPEN":
                    return

    def _sell_position(self, position: PositionState, qty_atomic: int, reason: str, decisions: list[TradeDecision], now: str) -> None:
        qty_atomic = min(qty_atomic, position.remaining_qty_atomic)
        result, quote = self.executor.sell(position.token_mint, qty_atomic)
        if not quote.out_amount_atomic:
            decisions.append(TradeDecision(action="SELL_SKIP", token_mint=position.token_mint, symbol=position.symbol, reason=f"{reason}:no_route", qty_atomic=qty_atomic))
            return
        out_sol = (result.output_amount_result if result and result.output_amount_result is not None else quote.out_amount_atomic) / 1_000_000_000
        sold_fraction = qty_atomic / max(position.remaining_qty_atomic, 1)
        sold_ui = position.remaining_qty_ui * sold_fraction
        position.remaining_qty_atomic -= qty_atomic
        position.remaining_qty_ui = max(0.0, position.remaining_qty_ui - sold_ui)
        position.realized_sol += out_sol
        position.last_sell_signature = result.signature if result else None
        self.portfolio.cash_sol += out_sol
        decisions.append(TradeDecision(action="SELL", token_mint=position.token_mint, symbol=position.symbol, reason=reason, qty_atomic=qty_atomic, qty_ui=sold_ui, metadata={"out_sol": out_sol, "price_impact_bps": quote.price_impact_bps, "signature": result.signature if result else None}))
        if position.remaining_qty_atomic <= 0 or position.remaining_qty_ui <= 0.0:
            position.status = "CLOSED"
            self.portfolio.total_realized_sol += position.realized_sol - position.entry_sol
            self.portfolio.closed_positions.append(position)
            self.portfolio.open_positions.pop(position.token_mint, None)
            self.portfolio.cooldowns[position.token_mint] = now
        else:
            position.updated_at = now

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
            result, quote = self.executor.buy(candidate, size_sol)
            if not quote.out_amount_atomic:
                decisions.append(TradeDecision(action="BUY_SKIP", token_mint=candidate.address, symbol=candidate.symbol, reason="no_route", size_sol=size_sol))
                continue
            qty_atomic = result.output_amount_result if result and result.output_amount_result is not None else quote.out_amount_atomic
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
            decisions.append(TradeDecision(action="BUY", token_mint=candidate.address, symbol=candidate.symbol, reason="discovery_entry", size_sol=size_sol, qty_atomic=qty_atomic, qty_ui=qty_ui, metadata={"score": candidate.discovery_score, "price_impact_bps": quote.price_impact_bps, "signature": result.signature if result else None}))
            if len(self.portfolio.open_positions) >= self.settings.max_open_positions:
                break


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
