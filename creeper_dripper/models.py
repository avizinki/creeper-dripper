from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class TokenCandidate:
    address: str
    symbol: str
    name: str | None = None
    decimals: int | None = None
    price_usd: float | None = None
    liquidity_usd: float | None = None
    exit_liquidity_usd: float | None = None
    volume_24h_usd: float | None = None
    volume_1h_usd: float | None = None
    change_1h_pct: float | None = None
    change_24h_pct: float | None = None
    buy_1h: int | None = None
    sell_1h: int | None = None
    buy_sell_ratio_1h: float | None = None
    holder_count: int | None = None
    top10_holder_percent: float | None = None
    age_hours: float | None = None
    security_mint_mutable: bool | None = None
    security_freezable: bool | None = None
    jupiter_buy_out_amount: int | None = None
    jupiter_buy_price_impact_bps: float | None = None
    jupiter_sell_price_impact_bps: float | None = None
    discovery_score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class JupiterOrder:
    request_id: str
    transaction_b64: str | None
    out_amount: int | None
    router: str | None
    mode: str | None
    raw: dict[str, Any]


@dataclass(slots=True)
class JupiterExecuteResult:
    status: str
    signature: str | None
    code: int
    input_amount_result: int | None
    output_amount_result: int | None
    error: str | None
    raw: dict[str, Any]


@dataclass(slots=True)
class ProbeQuote:
    input_amount_atomic: int
    out_amount_atomic: int | None
    price_impact_bps: float | None
    route_ok: bool
    raw: dict[str, Any]


@dataclass(slots=True)
class TakeProfitStep:
    trigger_pct: float
    fraction: float
    done: bool = False


@dataclass(slots=True)
class PositionState:
    token_mint: str
    symbol: str
    decimals: int
    status: str
    opened_at: str
    updated_at: str
    entry_price_usd: float
    avg_entry_price_usd: float
    entry_sol: float
    remaining_qty_atomic: int
    remaining_qty_ui: float
    peak_price_usd: float
    last_price_usd: float
    realized_sol: float = 0.0
    stop_loss_pct: float = 20.0
    trailing_stop_pct: float = 12.0
    trailing_arm_pct: float = 25.0
    exit_liquidity_at_entry_usd: float | None = None
    last_exit_liquidity_usd: float | None = None
    take_profit_steps: list[TakeProfitStep] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    last_sell_signature: str | None = None


@dataclass(slots=True)
class PortfolioState:
    version: int
    cash_sol: float
    reserved_sol: float
    total_realized_sol: float
    open_positions: dict[str, PositionState]
    closed_positions: list[PositionState]
    cooldowns: dict[str, str]
    opened_today_count: int
    last_cycle_at: str | None = None


@dataclass(slots=True)
class TradeDecision:
    action: str
    token_mint: str
    symbol: str
    reason: str
    qty_atomic: int | None = None
    qty_ui: float | None = None
    size_sol: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
