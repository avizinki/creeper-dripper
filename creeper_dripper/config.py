from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from creeper_dripper.utils import env_bool, env_csv_floats, env_float, env_int, env_str


SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


@dataclass(slots=True)
class Settings:
    birdeye_api_key: str
    jupiter_api_key: str
    bs58_private_key: str
    chain: str
    dry_run: bool
    live_trading_enabled: bool
    poll_interval_seconds: int
    runtime_dir: Path
    state_path: Path
    journal_path: Path
    discovery_limit: int
    discovery_max_candidates: int
    min_liquidity_usd: float
    min_exit_liquidity_usd: float
    min_volume_24h_usd: float
    min_buy_sell_ratio: float
    min_discovery_score: float
    max_token_age_hours: float
    block_mutable_mint: bool
    block_freezable: bool
    require_jup_sell_route: bool
    portfolio_start_sol: float
    max_open_positions: int
    base_position_size_sol: float
    max_position_size_sol: float
    cash_reserve_sol: float
    min_order_size_sol: float
    max_daily_new_positions: int
    cooldown_minutes_after_exit: int
    default_slippage_bps: int
    max_acceptable_price_impact_bps: int
    use_jupiter_managed_execution: bool
    stop_loss_pct: float
    trailing_stop_pct: float
    trailing_arm_pct: float
    time_stop_minutes: int
    take_profit_levels_pct: list[float]
    take_profit_fractions: list[float]
    force_full_exit_on_liquidity_break: bool
    liquidity_break_ratio: float
    exit_probe_fractions: list[float]
    log_level: str

    def validate(self) -> None:
        missing: list[str] = []
        if not self.birdeye_api_key:
            missing.append("BIRDEYE_API_KEY")
        if not self.jupiter_api_key:
            missing.append("JUPITER_API_KEY")
        if not self.bs58_private_key:
            missing.append("BS58_PRIVATE_KEY")
        if missing:
            raise RuntimeError(f"Missing required environment values: {', '.join(missing)}")
        if len(self.take_profit_levels_pct) != len(self.take_profit_fractions):
            raise RuntimeError("TAKE_PROFIT_LEVELS_PCT and TAKE_PROFIT_FRACTIONS must have the same length")
        if sum(self.take_profit_fractions) > 1.0:
            raise RuntimeError("TAKE_PROFIT_FRACTIONS sum must be <= 1.0")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)


def load_settings() -> Settings:
    project_root = Path.cwd()
    env_path = project_root / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=True)

    runtime_dir = Path(env_str("RUNTIME_DIR", "runtime"))
    settings = Settings(
        birdeye_api_key=env_str("BIRDEYE_API_KEY", ""),
        jupiter_api_key=env_str("JUPITER_API_KEY", ""),
        bs58_private_key=env_str("BS58_PRIVATE_KEY", ""),
        chain=env_str("CHAIN", "solana"),
        dry_run=env_bool("DRY_RUN", True),
        live_trading_enabled=env_bool("LIVE_TRADING_ENABLED", False),
        poll_interval_seconds=env_int("POLL_INTERVAL_SECONDS", 90),
        runtime_dir=runtime_dir,
        state_path=Path(env_str("STATE_PATH", str(runtime_dir / "state.json"))),
        journal_path=Path(env_str("JOURNAL_PATH", str(runtime_dir / "journal.jsonl"))),
        discovery_limit=env_int("DISCOVERY_LIMIT", 25),
        discovery_max_candidates=env_int("DISCOVERY_MAX_CANDIDATES", 8),
        min_liquidity_usd=env_float("MIN_LIQUIDITY_USD", 80_000),
        min_exit_liquidity_usd=env_float("MIN_EXIT_LIQUIDITY_USD", 40_000),
        min_volume_24h_usd=env_float("MIN_VOLUME_24H_USD", 125_000),
        min_buy_sell_ratio=env_float("MIN_BUY_SELL_RATIO", 1.05),
        min_discovery_score=env_float("MIN_DISCOVERY_SCORE", 55),
        max_token_age_hours=env_float("MAX_TOKEN_AGE_HOURS", 72),
        block_mutable_mint=env_bool("BLOCK_MUTABLE_MINT", True),
        block_freezable=env_bool("BLOCK_FREEZABLE", True),
        require_jup_sell_route=env_bool("REQUIRE_JUP_SELL_ROUTE", True),
        portfolio_start_sol=env_float("PORTFOLIO_START_SOL", 5.0),
        max_open_positions=env_int("MAX_OPEN_POSITIONS", 4),
        base_position_size_sol=env_float("BASE_POSITION_SIZE_SOL", 0.2),
        max_position_size_sol=env_float("MAX_POSITION_SIZE_SOL", 0.5),
        cash_reserve_sol=env_float("CASH_RESERVE_SOL", 0.25),
        min_order_size_sol=env_float("MIN_ORDER_SIZE_SOL", 0.03),
        max_daily_new_positions=env_int("MAX_DAILY_NEW_POSITIONS", 6),
        cooldown_minutes_after_exit=env_int("COOLDOWN_MINUTES_AFTER_EXIT", 20),
        default_slippage_bps=env_int("DEFAULT_SLIPPAGE_BPS", 250),
        max_acceptable_price_impact_bps=env_int("MAX_ACCEPTABLE_PRICE_IMPACT_BPS", 900),
        use_jupiter_managed_execution=env_bool("USE_JUPITER_MANAGED_EXECUTION", True),
        stop_loss_pct=env_float("STOP_LOSS_PCT", 20.0),
        trailing_stop_pct=env_float("TRAILING_STOP_PCT", 12.0),
        trailing_arm_pct=env_float("TRAILING_ARM_PCT", 25.0),
        time_stop_minutes=env_int("TIME_STOP_MINUTES", 240),
        take_profit_levels_pct=env_csv_floats("TAKE_PROFIT_LEVELS_PCT", [25.0, 60.0, 120.0, 250.0]),
        take_profit_fractions=env_csv_floats("TAKE_PROFIT_FRACTIONS", [0.15, 0.2, 0.25, 0.2]),
        force_full_exit_on_liquidity_break=env_bool("FORCE_FULL_EXIT_ON_LIQUIDITY_BREAK", True),
        liquidity_break_ratio=env_float("LIQUIDITY_BREAK_RATIO", 0.55),
        exit_probe_fractions=env_csv_floats("EXIT_PROBE_FRACTIONS", [0.1, 0.2, 0.35, 0.5, 1.0]),
        log_level=env_str("LOG_LEVEL", "INFO"),
    )
    settings.validate()
    return settings
