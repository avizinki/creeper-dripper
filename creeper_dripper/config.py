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
    solana_keypair_path: Path | None
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
    discovery_interval_seconds: int
    max_active_candidates: int
    candidate_cache_ttl_seconds: int
    route_check_cache_ttl_seconds: int
    prefilter_min_liquidity_usd: float
    prefilter_max_age_hours: float
    prefilter_min_recent_volume_usd: float
    min_liquidity_usd: float
    min_exit_liquidity_usd: float
    require_birdeye_exit_liquidity: bool
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
    daily_realized_loss_cap_sol: float
    max_consecutive_execution_failures: int
    stale_market_data_minutes: int
    unknown_exit_saturation_limit: int
    max_exit_blocked_positions: int
    log_level: str
    drip_exit_enabled: bool
    drip_chunk_pcts: list[float]
    drip_near_equal_band: float
    drip_min_chunk_wait_seconds: int
    # Hachi-style dripper: replaces TP-ladder as the primary sell controller.
    # When True, every cycle probes Jupiter sell quotes for small chunks and
    # executes immediately when route quality is acceptable — no TP threshold
    # required before first sell activity.
    hachi_dripper_enabled: bool = False
    hachi_max_price_impact_bps: int = 900
    run_id: str | None = None
    run_dir: Path | None = None
    run_log_path: Path | None = None

    def validate(self) -> None:
        errors: list[str] = []
        if not self.birdeye_api_key:
            errors.append("BIRDEYE_API_KEY is required")
        if not self.jupiter_api_key:
            errors.append("JUPITER_API_KEY is required")
        if self.solana_keypair_path:
            if not self.solana_keypair_path.exists():
                errors.append(f"SOLANA_KEYPAIR_PATH does not exist: {self.solana_keypair_path}")
            if not self.solana_keypair_path.is_file():
                errors.append(f"SOLANA_KEYPAIR_PATH is not a file: {self.solana_keypair_path}")
            if not os.access(self.solana_keypair_path, os.R_OK):
                errors.append(f"SOLANA_KEYPAIR_PATH is not readable: {self.solana_keypair_path}")
        if self.live_trading_enabled and not self.dry_run and not self.solana_keypair_path and not self.bs58_private_key:
            errors.append("Live mode requires wallet credentials: set SOLANA_KEYPAIR_PATH (preferred) or BS58_PRIVATE_KEY")
        if len(self.take_profit_levels_pct) != len(self.take_profit_fractions):
            errors.append("TAKE_PROFIT_LEVELS_PCT and TAKE_PROFIT_FRACTIONS must have the same length")
        if sum(self.take_profit_fractions) > 1.0:
            errors.append("TAKE_PROFIT_FRACTIONS sum must be <= 1.0")
        if self.daily_realized_loss_cap_sol <= 0:
            errors.append("DAILY_REALIZED_LOSS_CAP_SOL must be > 0")
        if self.max_consecutive_execution_failures <= 0:
            errors.append("MAX_CONSECUTIVE_EXECUTION_FAILURES must be > 0")
        if self.max_exit_blocked_positions <= 0:
            errors.append("MAX_EXIT_BLOCKED_POSITIONS must be > 0")
        if self.discovery_interval_seconds <= 0:
            errors.append("DISCOVERY_INTERVAL_SECONDS must be > 0")
        if self.max_active_candidates <= 0:
            errors.append("MAX_ACTIVE_CANDIDATES must be > 0")
        if self.candidate_cache_ttl_seconds <= 0:
            errors.append("CANDIDATE_CACHE_TTL_SECONDS must be > 0")
        if self.route_check_cache_ttl_seconds <= 0:
            errors.append("ROUTE_CHECK_CACHE_TTL_SECONDS must be > 0")
        if errors:
            raise RuntimeError("Configuration validation failed:\n- " + "\n- ".join(errors))
        self.runtime_dir.mkdir(parents=True, exist_ok=True)


def load_settings() -> Settings:
    project_root = Path.cwd()
    env_path = project_root / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=True)

    runtime_dir = Path(env_str("RUNTIME_DIR", "runtime"))

    discovery_interval_seconds = _required_env_int("DISCOVERY_INTERVAL_SECONDS")
    max_active_candidates = _required_env_int("MAX_ACTIVE_CANDIDATES")
    candidate_cache_ttl_seconds = _optional_env_int("CANDIDATE_CACHE_TTL_SECONDS", 120)
    route_check_cache_ttl_seconds = _optional_env_int("ROUTE_CHECK_CACHE_TTL_SECONDS", 90)

    settings = Settings(
        birdeye_api_key=env_str("BIRDEYE_API_KEY", ""),
        jupiter_api_key=env_str("JUPITER_API_KEY", ""),
        solana_keypair_path=Path(path_raw) if (path_raw := env_str("SOLANA_KEYPAIR_PATH", "")) else None,
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
        discovery_interval_seconds=discovery_interval_seconds,
        max_active_candidates=max_active_candidates,
        candidate_cache_ttl_seconds=candidate_cache_ttl_seconds,
        route_check_cache_ttl_seconds=route_check_cache_ttl_seconds,
        prefilter_min_liquidity_usd=env_float("PREFILTER_MIN_LIQUIDITY_USD", 50_000),
        prefilter_max_age_hours=env_float("PREFILTER_MAX_AGE_HOURS", 48),
        prefilter_min_recent_volume_usd=env_float("PREFILTER_MIN_RECENT_VOLUME_USD", 30_000),
        min_liquidity_usd=env_float("MIN_LIQUIDITY_USD", 80_000),
        min_exit_liquidity_usd=env_float("MIN_EXIT_LIQUIDITY_USD", 40_000),
        require_birdeye_exit_liquidity=env_bool("REQUIRE_BIRDEYE_EXIT_LIQUIDITY", False),
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
        daily_realized_loss_cap_sol=env_float("DAILY_REALIZED_LOSS_CAP_SOL", 1.0),
        max_consecutive_execution_failures=env_int("MAX_CONSECUTIVE_EXECUTION_FAILURES", 6),
        stale_market_data_minutes=env_int("STALE_MARKET_DATA_MINUTES", 10),
        unknown_exit_saturation_limit=env_int("UNKNOWN_EXIT_SATURATION_LIMIT", 6),
        max_exit_blocked_positions=env_int("MAX_EXIT_BLOCKED_POSITIONS", 5),
        log_level=env_str("LOG_LEVEL", "INFO"),
        drip_exit_enabled=env_bool("DRIP_EXIT_ENABLED", False),
        drip_chunk_pcts=env_csv_floats("DRIP_CHUNK_PCTS", [0.10, 0.25, 0.50]),
        drip_near_equal_band=env_float("DRIP_NEAR_EQUAL_BAND", 0.002),
        drip_min_chunk_wait_seconds=env_int("DRIP_MIN_CHUNK_WAIT_SECONDS", 30),
        hachi_dripper_enabled=env_bool("HACHI_DRIPPER_ENABLED", False),
        hachi_max_price_impact_bps=env_int("HACHI_MAX_PRICE_IMPACT_BPS", 900),
        run_id=None,
        run_dir=None,
        run_log_path=None,
    )
    settings.validate()
    return settings


def _required_env_int(name: str) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        raise RuntimeError(f"Missing required environment value: {name}")
    try:
        return int(float(raw.strip()))
    except ValueError as exc:
        raise RuntimeError(f"Invalid integer environment value for {name}: {raw}") from exc


def _optional_env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(float(raw.strip()))
    except ValueError as exc:
        raise RuntimeError(f"Invalid integer environment value for {name}: {raw}") from exc
