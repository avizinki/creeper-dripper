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
    exit_liquidity_available: bool = True
    exit_liquidity_reason: str | None = None
    birdeye_exit_liquidity_supported: bool = True
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
    age_source: str | None = None
    created_at_raw: str | None = None
    security_mint_mutable: bool | None = None
    security_freezable: bool | None = None
    jupiter_buy_out_amount: int | None = None
    jupiter_buy_price_impact_bps: float | None = None
    jupiter_sell_price_impact_bps: float | None = None
    sell_route_available: bool = False
    sell_quote_out_amount: int | None = None
    sell_quote_price_impact_bps: float | None = None
    sell_quote_success: bool = False
    sell_route_quality: str | None = None
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
class ExecutionResult:
    status: str
    requested_amount: int
    executed_amount: int | None = None
    output_amount: int | None = None
    diagnostic_code: str | None = None
    signature: str | None = None
    error: str | None = None
    is_partial: bool = False
    diagnostic_metadata: dict[str, Any] = field(default_factory=dict)


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
    position_id: str = ""
    realized_sol: float = 0.0
    stop_loss_pct: float = 20.0
    trailing_stop_pct: float = 12.0
    trailing_arm_pct: float = 25.0
    exit_liquidity_at_entry_usd: float | None = None
    last_exit_liquidity_usd: float | None = None
    take_profit_steps: list[TakeProfitStep] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    last_sell_signature: str | None = None
    pending_exit_reason: str | None = None
    pending_exit_qty_atomic: int | None = None
    pending_exit_signature: str | None = None
    exit_retry_count: int = 0
    last_exit_attempt_at: str | None = None
    next_exit_retry_at: str | None = None
    pending_proceeds_sol: float = 0.0
    # "entry" = buy settlement unclear; "exit" = sell settlement unclear (retry exit path).
    reconcile_context: str | None = None
    # SOL-first marks (see engine.position_pricing); USD fields above are display/risk metadata only.
    entry_mark_sol_per_token: float = 0.0
    last_mark_sol_per_token: float = 0.0
    peak_mark_sol_per_token: float = 0.0
    last_estimated_exit_value_sol: float | None = None
    unrealized_pnl_sol: float | None = None
    valuation_source: str | None = None
    valuation_status: str | None = None
    usd_mark_unavailable: bool = False
    # Jupiter sell-quote liquidity deterioration (JSDS); baseline at entry, refreshed each valuation cycle.
    entry_sell_impact_bps: float | None = None
    entry_sell_route_hops: int | None = None
    entry_sell_route_label: str | None = None
    last_sell_impact_bps: float | None = None
    last_sell_route_hops: int | None = None
    last_sell_route_label: str | None = None
    quote_miss_streak: int = 0
    # Drip exit state: chunked selling paced over multiple cycles.
    drip_exit_active: bool = False
    drip_exit_reason: str | None = None
    drip_qty_remaining_atomic: int | None = None
    drip_chunks_done: int = 0
    drip_next_chunk_at: str | None = None


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
    opened_today_date: str | None = None
    last_cycle_at: str | None = None
    safe_mode_active: bool = False
    safety_stop_reason: str | None = None
    consecutive_execution_failures: int = 0
    entries_skipped_dry_run: int = 0
    entries_skipped_live_disabled: int = 0
    run_id: str | None = None


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
