from __future__ import annotations

import json
import logging
import shutil
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from creeper_dripper.errors import STATE_FILE_CORRUPTED
from creeper_dripper.models import PortfolioState, PositionState, TakeProfitStep
from creeper_dripper.utils import atomic_write_json

LOGGER = logging.getLogger(__name__)

STATE_VERSION = 2


def new_portfolio(initial_cash_sol: float) -> PortfolioState:
    today = datetime.now(timezone.utc).date().isoformat()
    return PortfolioState(
        version=STATE_VERSION,
        cash_sol=initial_cash_sol,
        reserved_sol=0.0,
        total_realized_sol=0.0,
        open_positions={},
        closed_positions=[],
        cooldowns={},
        opened_today_count=0,
        opened_today_date=today,
        last_cycle_at=None,
    )


def load_portfolio(path: Path, initial_cash_sol: float) -> PortfolioState:
    if not path.exists():
        return new_portfolio(initial_cash_sol)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        _archive_corrupted_state(path)
        LOGGER.error("%s path=%s error=%s", STATE_FILE_CORRUPTED, path, exc)
        return new_portfolio(initial_cash_sol)
    open_positions: dict[str, PositionState] = {}
    closed_positions = []
    try:
        for mint, raw in data.get("open_positions", {}).items():
            steps = [TakeProfitStep(**step) for step in raw.get("take_profit_steps", [])]
            open_positions[mint] = PositionState(**{**raw, "take_profit_steps": steps})
        for raw in data.get("closed_positions", []):
            steps = [TakeProfitStep(**step) for step in raw.get("take_profit_steps", [])]
            closed_positions.append(PositionState(**{**raw, "take_profit_steps": steps}))
    except Exception as exc:
        _archive_corrupted_state(path)
        LOGGER.error("%s path=%s error=%s", STATE_FILE_CORRUPTED, path, exc)
        return new_portfolio(initial_cash_sol)
    return PortfolioState(
        version=int(data.get("version", STATE_VERSION)),
        cash_sol=float(data.get("cash_sol", initial_cash_sol)),
        reserved_sol=float(data.get("reserved_sol", 0.0)),
        total_realized_sol=float(data.get("total_realized_sol", 0.0)),
        open_positions=open_positions,
        closed_positions=closed_positions,
        cooldowns={str(k): str(v) for k, v in data.get("cooldowns", {}).items()},
        opened_today_count=int(data.get("opened_today_count", 0)),
        opened_today_date=data.get("opened_today_date"),
        last_cycle_at=data.get("last_cycle_at"),
        safe_mode_active=bool(data.get("safe_mode_active", False)),
        safety_stop_reason=data.get("safety_stop_reason"),
        consecutive_execution_failures=int(data.get("consecutive_execution_failures", 0)),
        entries_skipped_dry_run=int(data.get("entries_skipped_dry_run", 0)),
        entries_skipped_live_disabled=int(data.get("entries_skipped_live_disabled", 0)),
    )


def save_portfolio(path: Path, portfolio: PortfolioState) -> None:
    portfolio.version = STATE_VERSION
    atomic_write_json(path, asdict(portfolio))


def save_status_snapshot(path: Path, payload: dict) -> None:
    atomic_write_json(path, payload)


def _archive_corrupted_state(path: Path) -> None:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_dir = path.parent / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    archived_path = archive_dir / f"{path.stem}.{timestamp}.corrupted{path.suffix}"
    shutil.move(str(path), str(archived_path))
