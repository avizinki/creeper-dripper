from __future__ import annotations

from creeper_dripper.errors import EXIT_RECONCILED_CLOSED, EXIT_RECONCILED_PARTIAL, EXIT_UNKNOWN_PENDING_RECONCILE
from creeper_dripper.models import PositionState


def reconcile_pending_exit(position: PositionState, tx_status: str | None) -> tuple[str, str]:
    """Reconcile a pending exit using Jupiter execution truth only.

    tx_status is sourced from executor.transaction_status() via getSignatureStatuses RPC.
    No wallet token balance reads; position qty is tracked internally from execution results.

    Returns (next_status, reason).
    """
    if tx_status == "success":
        # Transaction confirmed on-chain — treat as fully exited.
        return "CLOSED", EXIT_RECONCILED_CLOSED
    if tx_status == "failed":
        # Transaction reverted — safe to retry.
        return "EXIT_BLOCKED", EXIT_UNKNOWN_PENDING_RECONCILE
    # Unknown / not yet confirmed — leave pending.
    return "EXIT_PENDING", EXIT_UNKNOWN_PENDING_RECONCILE
