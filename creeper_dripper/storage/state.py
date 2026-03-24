from __future__ import annotations

import json
import logging
import shutil
from dataclasses import MISSING, asdict, fields
from datetime import datetime, timezone
from pathlib import Path

from solders.pubkey import Pubkey

from creeper_dripper.errors import STATE_FILE_CORRUPTED, STATE_NON_PUBKEY_MINT_DROPPED
from creeper_dripper.models import PortfolioState, PositionState, TakeProfitStep
from creeper_dripper.utils import atomic_write_json

LOGGER = logging.getLogger(__name__)

STATE_VERSION = 2


def _position_state_from_raw(raw: dict) -> PositionState:
    steps = [TakeProfitStep(**step) for step in raw.get("take_profit_steps", [])]
    m = {k: v for k, v in raw.items() if k != "take_profit_steps"}
    old_mp = m.pop("mark_price_source", None)
    if not m.get("valuation_source") and old_mp is not None:
        m["valuation_source"] = old_mp
    old_ms = m.pop("mark_price_status", None)
    if not m.get("valuation_status") and old_ms is not None:
        m["valuation_status"] = old_ms
    for key, default in (
        ("entry_mark_sol_per_token", 0.0),
        ("last_mark_sol_per_token", 0.0),
        ("peak_mark_sol_per_token", 0.0),
        ("last_estimated_exit_value_sol", None),
        ("unrealized_pnl_sol", None),
        ("usd_mark_unavailable", False),
        ("valuation_source", None),
        ("valuation_status", None),
        ("entry_sell_impact_bps", None),
        ("entry_sell_route_hops", None),
        ("entry_sell_route_label", None),
        ("last_sell_impact_bps", None),
        ("last_sell_route_hops", None),
        ("last_sell_route_label", None),
        ("quote_miss_streak", 0),
        ("drip_exit_active", False),
        ("drip_exit_reason", None),
        ("drip_qty_remaining_atomic", None),
        ("drip_chunks_done", 0),
        ("drip_next_chunk_at", None),
    ):
        m.setdefault(key, default)
    for f in fields(PositionState):
        if f.name == "take_profit_steps":
            continue
        if f.name not in m:
            if f.default_factory is not MISSING:
                m[f.name] = f.default_factory()
            elif f.default is not MISSING:
                m[f.name] = f.default
    kwargs = {f.name: m[f.name] for f in fields(PositionState) if f.name != "take_profit_steps"}
    kwargs["take_profit_steps"] = steps
    return PositionState(**kwargs)


def _is_valid_solana_token_mint(mint: str) -> bool:
    """True if `mint` parses as a Solana public key (filters test placeholders like mint1)."""
    if not mint or not isinstance(mint, str):
        return False
    raw = mint.strip()
    if not raw:
        return False
    try:
        Pubkey.from_string(raw)
    except Exception:
        return False
    return True


def _drop_positions_with_invalid_mints(portfolio: PortfolioState, *, context: str) -> None:
    """Remove open/closed positions whose token_mint is not a valid Solana pubkey (persistence guard)."""
    for map_key in list(portfolio.open_positions.keys()):
        pos = portfolio.open_positions[map_key]
        if _is_valid_solana_token_mint(pos.token_mint):
            continue
        del portfolio.open_positions[map_key]
        LOGGER.warning(
            "%s context=%s map_key=%s token_mint=%s symbol=%s",
            STATE_NON_PUBKEY_MINT_DROPPED,
            context,
            map_key,
            pos.token_mint,
            pos.symbol,
        )
    kept_closed: list[PositionState] = []
    for pos in portfolio.closed_positions:
        if _is_valid_solana_token_mint(pos.token_mint):
            kept_closed.append(pos)
            continue
        LOGGER.warning(
            "%s context=%s map_key=n/a token_mint=%s symbol=%s",
            STATE_NON_PUBKEY_MINT_DROPPED,
            context,
            pos.token_mint,
            pos.symbol,
        )
    portfolio.closed_positions = kept_closed


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
            open_positions[mint] = _position_state_from_raw(raw)
        for raw in data.get("closed_positions", []):
            closed_positions.append(_position_state_from_raw(raw))
    except Exception as exc:
        _archive_corrupted_state(path)
        LOGGER.error("%s path=%s error=%s", STATE_FILE_CORRUPTED, path, exc)
        return new_portfolio(initial_cash_sol)
    portfolio = PortfolioState(
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
    _drop_positions_with_invalid_mints(portfolio, context="load")
    return portfolio


def save_portfolio(path: Path, portfolio: PortfolioState) -> None:
    _drop_positions_with_invalid_mints(portfolio, context="save")
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
