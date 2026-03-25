from __future__ import annotations

import logging

from creeper_dripper.errors import (
    EXIT_RECONCILED_CLOSED,
    EXIT_UNKNOWN_PENDING_RECONCILE,
    POSITION_RECONCILE_PENDING,
)
from creeper_dripper.execution.reconcile import reconcile_pending_exit
from creeper_dripper.models import PortfolioState, TradeDecision

LOGGER = logging.getLogger(__name__)


def run_startup_recovery(portfolio: PortfolioState, executor, now: str) -> list[TradeDecision]:
    """Reconcile open positions on bot startup using Jupiter-execution truth only.

    No wallet RPC token balance reads.  For positions with a pending exit signature,
    transaction_status (getSignatureStatuses) is used to determine whether the exit
    confirmed on-chain.  All other quantity tracking comes from internally recorded
    execution results.
    """
    decisions: list[TradeDecision] = []
    for mint, position in list(portfolio.open_positions.items()):
        if position.status not in {"EXIT_PENDING", POSITION_RECONCILE_PENDING}:
            continue
        if position.status == POSITION_RECONCILE_PENDING and position.reconcile_context != "exit":
            # RECONCILE_PENDING(entry) can only be resolved by manual intervention now;
            # there is no wallet-balance fallback.
            LOGGER.critical(
                "startup_recovery_entry_reconcile_pending mint=%s position_id=%s "
                "— requires manual review (Jupiter-only mode has no wallet fallback)",
                mint,
                position.position_id or mint,
            )
            continue

        # For EXIT_PENDING or RECONCILE_PENDING(exit): check on-chain tx status.
        tx_status = (
            executor.transaction_status(position.pending_exit_signature)
            if position.pending_exit_signature
            else None
        )
        next_status, reason = reconcile_pending_exit(position, tx_status)

        if next_status == "CLOSED":
            position.status = "CLOSED"
            position.reconcile_context = None
            position.pending_exit_signature = None
            position.pending_exit_reason = None
            position.pending_exit_qty_atomic = None
            portfolio.closed_positions.append(position)
            portfolio.open_positions.pop(mint, None)
            portfolio.cooldowns[mint] = now
            decisions.append(
                TradeDecision(
                    action="RECOVERY_EXIT",
                    token_mint=mint,
                    symbol=position.symbol,
                    reason=EXIT_RECONCILED_CLOSED,
                )
            )
            LOGGER.info(
                "startup_recovery_exit_confirmed mint=%s position_id=%s signature=%s",
                mint,
                position.position_id or mint,
                position.pending_exit_signature,
            )

        elif next_status == "EXIT_BLOCKED":
            # Transaction reverted — re-queue as EXIT_PENDING so the normal retry path kicks in.
            position.status = "EXIT_PENDING"
            position.reconcile_context = None
            position.pending_exit_signature = None
            decisions.append(
                TradeDecision(
                    action="RECOVERY_EXIT",
                    token_mint=mint,
                    symbol=position.symbol,
                    reason=EXIT_UNKNOWN_PENDING_RECONCILE,
                    metadata={"startup_recovery": True, "tx_status": "failed"},
                )
            )
            LOGGER.warning(
                "startup_recovery_exit_reverted mint=%s position_id=%s — re-queued for retry",
                mint,
                position.position_id or mint,
            )

        else:
            # tx_status unknown / not yet confirmed — leave as EXIT_PENDING for next cycle.
            LOGGER.info(
                "startup_recovery_exit_pending mint=%s position_id=%s tx_status=%s",
                mint,
                position.position_id or mint,
                tx_status,
            )

    return decisions
