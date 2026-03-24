from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from creeper_dripper.models import PortfolioState, PositionState, TakeProfitStep
from creeper_dripper.utils import atomic_write_json


STATE_VERSION = 1


def new_portfolio(initial_cash_sol: float) -> PortfolioState:
    return PortfolioState(
        version=STATE_VERSION,
        cash_sol=initial_cash_sol,
        reserved_sol=0.0,
        total_realized_sol=0.0,
        open_positions={},
        closed_positions=[],
        cooldowns={},
        opened_today_count=0,
        last_cycle_at=None,
    )


def load_portfolio(path: Path, initial_cash_sol: float) -> PortfolioState:
    if not path.exists():
        return new_portfolio(initial_cash_sol)
    data = json.loads(path.read_text(encoding="utf-8"))
    open_positions: dict[str, PositionState] = {}
    for mint, raw in data.get("open_positions", {}).items():
        steps = [TakeProfitStep(**step) for step in raw.get("take_profit_steps", [])]
        open_positions[mint] = PositionState(**{**raw, "take_profit_steps": steps})
    closed_positions = []
    for raw in data.get("closed_positions", []):
        steps = [TakeProfitStep(**step) for step in raw.get("take_profit_steps", [])]
        closed_positions.append(PositionState(**{**raw, "take_profit_steps": steps}))
    return PortfolioState(
        version=int(data.get("version", STATE_VERSION)),
        cash_sol=float(data.get("cash_sol", initial_cash_sol)),
        reserved_sol=float(data.get("reserved_sol", 0.0)),
        total_realized_sol=float(data.get("total_realized_sol", 0.0)),
        open_positions=open_positions,
        closed_positions=closed_positions,
        cooldowns={str(k): str(v) for k, v in data.get("cooldowns", {}).items()},
        opened_today_count=int(data.get("opened_today_count", 0)),
        last_cycle_at=data.get("last_cycle_at"),
    )


def save_portfolio(path: Path, portfolio: PortfolioState) -> None:
    atomic_write_json(path, asdict(portfolio))
