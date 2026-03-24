from __future__ import annotations

from creeper_dripper.errors import EXIT_RECONCILED_CLOSED, EXIT_RECONCILED_PARTIAL, EXIT_UNKNOWN_PENDING_RECONCILE
from creeper_dripper.models import PositionState


def reconcile_pending_exit(position: PositionState, wallet_balance_atomic: int | None, tx_status: str | None) -> tuple[str, str]:
    if wallet_balance_atomic is None:
        return "EXIT_PENDING", EXIT_UNKNOWN_PENDING_RECONCILE

    if wallet_balance_atomic <= 0:
        return "CLOSED", EXIT_RECONCILED_CLOSED

    if wallet_balance_atomic < position.remaining_qty_atomic:
        return "PARTIAL", EXIT_RECONCILED_PARTIAL

    if tx_status == "failed":
        return "EXIT_BLOCKED", EXIT_UNKNOWN_PENDING_RECONCILE
    return "EXIT_PENDING", EXIT_UNKNOWN_PENDING_RECONCILE
