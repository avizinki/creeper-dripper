from __future__ import annotations

import logging

from creeper_dripper.errors import EXIT_RECONCILED_CLOSED, EXIT_RECONCILED_PARTIAL, EXIT_UNKNOWN_PENDING_RECONCILE, RECOVERY_QTY_REDUCED_TO_WALLET, RECOVERY_WALLET_GT_STATE
from creeper_dripper.execution.reconcile import reconcile_pending_exit
from creeper_dripper.models import PortfolioState, TradeDecision

LOGGER = logging.getLogger(__name__)


def run_startup_recovery(portfolio: PortfolioState, executor, now: str) -> list[TradeDecision]:
    decisions: list[TradeDecision] = []
    for mint, position in list(portfolio.open_positions.items()):
        if position.status not in {"OPEN", "PARTIAL", "EXIT_PENDING", "EXIT_BLOCKED"}:
            continue
        wallet_qty = executor.wallet_token_balance_atomic(mint)
        if wallet_qty is None:
            continue
        original_state_qty = position.remaining_qty_atomic

        if wallet_qty < position.remaining_qty_atomic:
            decisions.append(
                TradeDecision(
                    action="RECOVERY_CORRECTION",
                    token_mint=mint,
                    symbol=position.symbol,
                    reason=RECOVERY_QTY_REDUCED_TO_WALLET,
                    qty_atomic=wallet_qty,
                    metadata={"state_qty": position.remaining_qty_atomic, "wallet_qty": wallet_qty},
                )
            )
            position.remaining_qty_atomic = wallet_qty
            denom = 10 ** max(position.decimals, 0)
            position.remaining_qty_ui = wallet_qty / denom if denom else 0.0

        elif wallet_qty > position.remaining_qty_atomic:
            decisions.append(
                TradeDecision(
                    action="RECOVERY_DISCREPANCY",
                    token_mint=mint,
                    symbol=position.symbol,
                    reason=RECOVERY_WALLET_GT_STATE,
                    metadata={"state_qty": position.remaining_qty_atomic, "wallet_qty": wallet_qty},
                )
            )
            LOGGER.warning("%s mint=%s state_qty=%s wallet_qty=%s", RECOVERY_WALLET_GT_STATE, mint, position.remaining_qty_atomic, wallet_qty)

        if wallet_qty <= 0:
            if position.status == "EXIT_PENDING":
                position.status = "CLOSED"
                position.pending_exit_qty_atomic = None
                position.pending_exit_reason = None
                position.pending_exit_signature = None
                portfolio.closed_positions.append(position)
                portfolio.open_positions.pop(mint, None)
                portfolio.cooldowns[mint] = now
                decisions.append(TradeDecision(action="RECOVERY_EXIT", token_mint=mint, symbol=position.symbol, reason=EXIT_RECONCILED_CLOSED))
            else:
                position.status = "EXIT_BLOCKED"
                decisions.append(TradeDecision(action="RECOVERY_DISCREPANCY", token_mint=mint, symbol=position.symbol, reason=EXIT_UNKNOWN_PENDING_RECONCILE, metadata={"wallet_qty": 0}))
            continue

        if position.status == "EXIT_PENDING":
            tx_status = executor.transaction_status(position.pending_exit_signature) if position.pending_exit_signature else None
            next_status, reason = reconcile_pending_exit(position, wallet_qty, tx_status)
            if wallet_qty < original_state_qty and next_status == "EXIT_PENDING":
                next_status = "PARTIAL"
                reason = EXIT_RECONCILED_PARTIAL
            if next_status == "PARTIAL":
                position.status = "PARTIAL"
                decisions.append(TradeDecision(action="RECOVERY_EXIT", token_mint=mint, symbol=position.symbol, reason=EXIT_RECONCILED_PARTIAL, qty_atomic=position.remaining_qty_atomic))
            elif next_status == "CLOSED":
                position.status = "CLOSED"
                portfolio.closed_positions.append(position)
                portfolio.open_positions.pop(mint, None)
                portfolio.cooldowns[mint] = now
                decisions.append(TradeDecision(action="RECOVERY_EXIT", token_mint=mint, symbol=position.symbol, reason=EXIT_RECONCILED_CLOSED))
            elif next_status == "EXIT_BLOCKED":
                position.status = "EXIT_BLOCKED"
                decisions.append(TradeDecision(action="RECOVERY_EXIT", token_mint=mint, symbol=position.symbol, reason=reason))
            else:
                decisions.append(TradeDecision(action="RECOVERY_EXIT", token_mint=mint, symbol=position.symbol, reason=EXIT_UNKNOWN_PENDING_RECONCILE))
    return decisions
