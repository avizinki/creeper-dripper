from __future__ import annotations

from creeper_dripper.errors import (
    EXIT_RECONCILED_CLOSED,
    EXIT_RECONCILED_PARTIAL,
    EXIT_TX_CONFIRMED_NEEDS_SETTLEMENT,
    EXIT_UNKNOWN_PENDING_RECONCILE,
    POSITION_RECONCILE_PENDING,
)
from creeper_dripper.models import PositionState


def reconcile_pending_exit(position: PositionState, tx_status: str | None) -> tuple[str, str]:
    """Reconcile a pending exit using Jupiter execution truth only.

    tx_status is sourced from executor.transaction_status() via getSignatureStatuses RPC.
    No wallet token balance reads; position qty is tracked internally from execution results.

    Returns (next_status, reason).
    """
    if tx_status == "success":
        # Tx lifecycle success is NOT settlement truth in Jupiter-only mode.
        # Only close if the position already contains internally recorded "fully exited" truth.
        has_internal_full_exit_truth = (
            int(position.remaining_qty_atomic) <= 0
            and (position.pending_exit_qty_atomic in {None, 0})
            and bool(position.last_sell_signature)
        )
        if has_internal_full_exit_truth:
            return "CLOSED", EXIT_RECONCILED_CLOSED
        return POSITION_RECONCILE_PENDING, EXIT_TX_CONFIRMED_NEEDS_SETTLEMENT
    if tx_status == "failed":
        # Transaction reverted — safe to retry.
        return "EXIT_BLOCKED", EXIT_UNKNOWN_PENDING_RECONCILE
    # Unknown / not yet confirmed — leave pending.
    return "EXIT_PENDING", EXIT_UNKNOWN_PENDING_RECONCILE
