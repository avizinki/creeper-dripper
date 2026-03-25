from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import signal
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from creeper_dripper.clients.birdeye import BirdeyeClient
from creeper_dripper.clients.birdeye_audit import build_birdeye_audit_summary_dict
from creeper_dripper.clients.jupiter import JupiterClient
from creeper_dripper.config import SOL_MINT, USDC_MINT, load_settings
from creeper_dripper.engine.discovery import discover_candidates, serialize_candidates
from creeper_dripper.engine.trader import CreeperDripper
from creeper_dripper.errors import SAFETY_STALE_MARKET_DATA
from creeper_dripper.execution.executor import TradeExecutor
from creeper_dripper.execution.wallet import load_keypair_from_base58, load_keypair_from_file
from creeper_dripper.storage.state import load_portfolio, save_portfolio
from creeper_dripper.utils import atomic_write_json, monotonic_sleep_until, setup_logging

STOP = False
LOGGER = logging.getLogger(__name__)

_MASK_TOKENS = ("KEY", "SECRET", "PRIVATE", "TOKEN")


def _project_root() -> Path:
    """Repository / install root containing the `creeper_dripper` package directory."""
    return Path(__file__).resolve().parent.parent.parent


def running_from_project_venv(project_root: Path | None = None) -> bool:
    """
    True if this process uses the project virtualenv.

    Uses ``sys.prefix`` (venv root), not ``Path(sys.executable).resolve()`` — on some installs
    the latter resolves through symlinks to a Homebrew/Frameworks path outside ``.venv/bin``.
    """
    root = project_root if project_root is not None else _project_root()
    expected = (root / ".venv").resolve()
    try:
        return Path(sys.prefix).resolve() == expected
    except OSError:
        return False


def _ensure_project_venv() -> None:
    """Fail fast unless running from project `.venv` or ALLOW_NON_VENV=1 (debug only)."""
    if os.environ.get("ALLOW_NON_VENV") == "1":
        print("WARNING: running outside .venv (unsafe)", file=sys.stderr)
        return
    project_root = _project_root()
    if running_from_project_venv(project_root):
        return
    expected_prefix = (project_root / ".venv").resolve()
    print("FATAL: creeper-dripper must run from project .venv", file=sys.stderr)
    print(f"Current interpreter: {sys.executable}", file=sys.stderr)
    print(f"Current sys.prefix: {sys.prefix}", file=sys.stderr)
    print(f"Expected sys.prefix: {expected_prefix}", file=sys.stderr)
    print(
        "Run using: .venv/bin/creeper-dripper or .venv/bin/python -m creeper_dripper.cli.main",
        file=sys.stderr,
    )
    raise SystemExit(1)


def _print_interpreter_banner() -> None:
    active = running_from_project_venv()
    print(f"interpreter={sys.executable}")
    print(f"venv_active={str(active).lower()}")


def _print_doctor_cli_hints() -> None:
    """Visible resolution hints (editable install + which creeper-dripper)."""
    cli_path = shutil.which("creeper-dripper") or sys.argv[0] or "n/a"
    print(f"cli_path={cli_path}")
    print("install_hint=from project root with venv activated: pip install -e .")


def cmd_debug_env(_args: argparse.Namespace) -> int:
    """Print interpreter, prefix, PATH, and venv detection (diagnostic)."""
    pr = _project_root()
    active = running_from_project_venv(pr)
    which_cli = shutil.which("creeper-dripper")
    print(f"sys.executable={sys.executable}")
    print(f"sys.prefix={sys.prefix}")
    print(f"which_creeper_dripper={which_cli or 'n/a'}")
    print(f"PATH={os.environ.get('PATH', '')}")
    print(f"venv_detected={str(active).lower()}")
    print(f"project_root={pr}")
    return 0


def mask_value(value: str) -> str:
    value = str(value or "")
    n = len(value)
    if n == 0:
        return "(len=0)"
    return f"{value[:4]}...{value[-3:]} (len={n})"


def _should_mask_key(key: str) -> bool:
    # Do not mask paths even if they include "KEY" (e.g. SOLANA_KEYPAIR_PATH).
    k = str(key or "")
    if "PATH" in k:
        return False
    return any(token in k for token in _MASK_TOKENS)


def _format_env_snapshot_line(key: str, value: object) -> str:
    raw = "" if value is None else str(value)
    if _should_mask_key(key):
        return f"{key}={mask_value(raw)}"
    return f"{key}={raw}"


def _print_env_snapshot(settings) -> None:
    lines: list[str] = []
    # Execution flags first
    lines.extend(
        [
            _format_env_snapshot_line("DRY_RUN", str(bool(settings.dry_run)).lower()),
            _format_env_snapshot_line("LIVE_TRADING_ENABLED", str(bool(settings.live_trading_enabled)).lower()),
            _format_env_snapshot_line("HACHI_DRIPPER_ENABLED", str(bool(settings.hachi_dripper_enabled)).lower()),
            _format_env_snapshot_line("HACHI_MAX_PRICE_IMPACT_BPS", settings.hachi_max_price_impact_bps),
            _format_env_snapshot_line("DRIP_EXIT_ENABLED", str(bool(settings.drip_exit_enabled)).lower()),
            _format_env_snapshot_line("USE_JUPITER_MANAGED_EXECUTION", str(bool(settings.use_jupiter_managed_execution)).lower()),
        ]
    )
    # Paths
    lines.extend(
        [
            _format_env_snapshot_line("RUNTIME_DIR", settings.runtime_dir),
            _format_env_snapshot_line("STATE_PATH", settings.state_path),
            _format_env_snapshot_line("JOURNAL_PATH", settings.journal_path),
            _format_env_snapshot_line("SOLANA_KEYPAIR_PATH", settings.solana_keypair_path or ""),
            _format_env_snapshot_line("WALLET_ADDRESS", getattr(settings, "wallet_address", None) or ""),
            _format_env_snapshot_line("SOLANA_RPC_URL", os.getenv("SOLANA_RPC_URL", "")),
        ]
    )
    # API keys / credentials (masked)
    lines.extend(
        [
            _format_env_snapshot_line("BIRDEYE_API_KEY", settings.birdeye_api_key),
            _format_env_snapshot_line("JUPITER_API_KEY", settings.jupiter_api_key),
            _format_env_snapshot_line("BS58_PRIVATE_KEY", settings.bs58_private_key),
        ]
    )
    # Rest (effective settings)
    lines.extend(
        [
            _format_env_snapshot_line("CHAIN", settings.chain),
            _format_env_snapshot_line("POLL_INTERVAL_SECONDS", settings.poll_interval_seconds),
            _format_env_snapshot_line("LOG_LEVEL", settings.log_level),
            _format_env_snapshot_line("DISCOVERY_LIMIT", settings.discovery_limit),
            _format_env_snapshot_line("DISCOVERY_MAX_CANDIDATES", settings.discovery_max_candidates),
            _format_env_snapshot_line("DISCOVERY_INTERVAL_SECONDS", settings.discovery_interval_seconds),
            _format_env_snapshot_line("MAX_ACTIVE_CANDIDATES", settings.max_active_candidates),
            _format_env_snapshot_line("CANDIDATE_CACHE_TTL_SECONDS", settings.candidate_cache_ttl_seconds),
            _format_env_snapshot_line("ROUTE_CHECK_CACHE_TTL_SECONDS", settings.route_check_cache_ttl_seconds),
            _format_env_snapshot_line("MIN_LIQUIDITY_USD", settings.min_liquidity_usd),
            _format_env_snapshot_line("MIN_EXIT_LIQUIDITY_USD", settings.min_exit_liquidity_usd),
            _format_env_snapshot_line("REQUIRE_BIRDEYE_EXIT_LIQUIDITY", str(bool(settings.require_birdeye_exit_liquidity)).lower()),
            _format_env_snapshot_line("MIN_VOLUME_24H_USD", settings.min_volume_24h_usd),
            _format_env_snapshot_line("MIN_BUY_SELL_RATIO", settings.min_buy_sell_ratio),
            _format_env_snapshot_line("MIN_DISCOVERY_SCORE", settings.min_discovery_score),
            _format_env_snapshot_line("MAX_TOKEN_AGE_HOURS", settings.max_token_age_hours),
            _format_env_snapshot_line("BLOCK_MUTABLE_MINT", str(bool(settings.block_mutable_mint)).lower()),
            _format_env_snapshot_line("BLOCK_FREEZABLE", str(bool(settings.block_freezable)).lower()),
            _format_env_snapshot_line("REQUIRE_JUP_SELL_ROUTE", str(bool(settings.require_jup_sell_route)).lower()),
            _format_env_snapshot_line("PORTFOLIO_START_SOL", settings.portfolio_start_sol),
            _format_env_snapshot_line("MAX_OPEN_POSITIONS", settings.max_open_positions),
            _format_env_snapshot_line("HARD_MAX_OPEN_POSITIONS", getattr(settings, "hard_max_open_positions", "")),
            _format_env_snapshot_line("ENTRY_CAPACITY_MODE", settings.entry_capacity_mode),
            _format_env_snapshot_line("BASE_POSITION_SIZE_SOL", settings.base_position_size_sol),
            _format_env_snapshot_line("MAX_POSITION_SIZE_SOL", settings.max_position_size_sol),
            _format_env_snapshot_line("CASH_RESERVE_SOL", settings.cash_reserve_sol),
            _format_env_snapshot_line("MIN_ORDER_SIZE_SOL", settings.min_order_size_sol),
            _format_env_snapshot_line("MAX_DAILY_NEW_POSITIONS", settings.max_daily_new_positions),
            _format_env_snapshot_line("HARD_MAX_DAILY_NEW_POSITIONS", getattr(settings, "hard_max_daily_new_positions", "")),
            _format_env_snapshot_line("COOLDOWN_MINUTES_AFTER_EXIT", settings.cooldown_minutes_after_exit),
            _format_env_snapshot_line("DEFAULT_SLIPPAGE_BPS", settings.default_slippage_bps),
            _format_env_snapshot_line("MAX_ACCEPTABLE_PRICE_IMPACT_BPS", settings.max_acceptable_price_impact_bps),
            _format_env_snapshot_line("STOP_LOSS_PCT", settings.stop_loss_pct),
            _format_env_snapshot_line("TRAILING_STOP_PCT", settings.trailing_stop_pct),
            _format_env_snapshot_line("TRAILING_ARM_PCT", settings.trailing_arm_pct),
            _format_env_snapshot_line("TIME_STOP_MINUTES", settings.time_stop_minutes),
            _format_env_snapshot_line("TAKE_PROFIT_LEVELS_PCT", ",".join(str(x) for x in settings.take_profit_levels_pct)),
            _format_env_snapshot_line("TAKE_PROFIT_FRACTIONS", ",".join(str(x) for x in settings.take_profit_fractions)),
            _format_env_snapshot_line("FORCE_FULL_EXIT_ON_LIQUIDITY_BREAK", str(bool(settings.force_full_exit_on_liquidity_break)).lower()),
            _format_env_snapshot_line("LIQUIDITY_BREAK_RATIO", settings.liquidity_break_ratio),
            _format_env_snapshot_line("EXIT_PROBE_FRACTIONS", ",".join(str(x) for x in settings.exit_probe_fractions)),
            _format_env_snapshot_line("DAILY_REALIZED_LOSS_CAP_SOL", settings.daily_realized_loss_cap_sol),
            _format_env_snapshot_line("MAX_CONSECUTIVE_EXECUTION_FAILURES", settings.max_consecutive_execution_failures),
            _format_env_snapshot_line("STALE_MARKET_DATA_MINUTES", settings.stale_market_data_minutes),
            _format_env_snapshot_line("UNKNOWN_EXIT_SATURATION_LIMIT", settings.unknown_exit_saturation_limit),
            _format_env_snapshot_line("MAX_EXIT_BLOCKED_POSITIONS", settings.max_exit_blocked_positions),
            _format_env_snapshot_line("EXIT_BLOCKED_RETRY_CYCLES", settings.exit_blocked_retry_cycles),
            _format_env_snapshot_line("EXIT_BLOCKED_MICRO_PROBE_CYCLES", settings.exit_blocked_micro_probe_cycles),
            _format_env_snapshot_line("ZOMBIE_RETRY_INTERVAL_CYCLES", settings.zombie_retry_interval_cycles),
            _format_env_snapshot_line("DRIP_CHUNK_PCTS", ",".join(str(x) for x in settings.drip_chunk_pcts)),
            _format_env_snapshot_line("DRIP_NEAR_EQUAL_BAND", settings.drip_near_equal_band),
            _format_env_snapshot_line("DRIP_MIN_CHUNK_WAIT_SECONDS", settings.drip_min_chunk_wait_seconds),
            _format_env_snapshot_line("HACHI_PROFIT_HARVEST_MIN_PCT", settings.hachi_profit_harvest_min_pct),
            _format_env_snapshot_line("HACHI_NEUTRAL_FLOOR_PCT", settings.hachi_neutral_floor_pct),
            _format_env_snapshot_line("HACHI_EMERGENCY_PNL_PCT", settings.hachi_emergency_pnl_pct),
            _format_env_snapshot_line("HACHI_WEAKENING_DROP_PCT", settings.hachi_weakening_drop_pct),
            _format_env_snapshot_line("HACHI_COLLAPSE_DROP_PCT", settings.hachi_collapse_drop_pct),
            _format_env_snapshot_line("DYNAMIC_CAPACITY_ENABLED", str(bool(getattr(settings, "dynamic_capacity_enabled", True))).lower()),
            _format_env_snapshot_line("HACHI_BIRTH_WALLET_SOL", getattr(settings, "hachi_birth_wallet_sol", None)),
            _format_env_snapshot_line("HACHI_BIRTH_TIMESTAMP", getattr(settings, "hachi_birth_timestamp", None)),
        ]
    )
    print("\n=== ENV SNAPSHOT (masked) ===")
    for line in lines:
        print(line)


def _wallet_address_for_snapshot(settings) -> str | None:
    """
    Best-effort wallet address discovery for visibility-only snapshots.

    Never used as settlement/execution truth. This is operator visibility/bootstrap only.
    """
    try:
        if getattr(settings, "wallet_address", None):
            return str(settings.wallet_address)
        owner = _load_owner_if_configured(settings)
        if owner is None:
            return None
        pubkey = getattr(owner, "pubkey", None)
        if callable(pubkey):
            return str(owner.pubkey())
        return None
    except Exception:
        return None


def _print_wallet_snapshot(*, settings, birdeye: BirdeyeClient, executor: TradeExecutor, wallet: str | None, header: str) -> None:
    print(f"\n=== {header} ===")
    print("note: visibility/bootstrap only (NOT settlement truth; NOT execution truth; does NOT modify state)")
    if not wallet:
        print("wallet_snapshot: n/a (wallet not configured)")
        return
    lamports = executor.native_sol_balance_lamports(wallet)
    sol = None if lamports is None else (float(lamports) / 1_000_000_000.0)
    try:
        snap = birdeye.wallet_token_list(wallet)
    except Exception as exc:
        print(f"wallet_snapshot: failed wallet={wallet} error={type(exc).__name__}:{exc}")
        return
    items = snap.get("items") or []
    total_usd = snap.get("totalUsd")
    # Prefer excluding SOL/USDC from "holdings summary" so the output focuses on leftovers.
    filtered = []
    for it in items:
        addr = str((it or {}).get("address") or "").strip()
        if addr in {SOL_MINT, USDC_MINT}:
            continue
        filtered.append(it)
    def _value_usd(it: dict) -> float:
        try:
            return float(it.get("valueUsd") or 0.0)
        except Exception:
            return 0.0
    top = sorted(filtered, key=_value_usd, reverse=True)[:8]
    print(f"wallet={wallet}")
    print(f"wallet_native_sol_snapshot_rpc={('n/a' if sol is None else round(sol, 6))}")
    print(f"wallet_token_count_birdeye={len(items)}")
    print(f"wallet_total_usd_birdeye={('n/a' if total_usd is None else total_usd)}")
    print("holdings_top:")
    if not top:
        print("  (none)")
    for it in top:
        sym = str(it.get('symbol') or '?')
        addr = str(it.get('address') or '?')
        ui = it.get("uiAmount")
        vu = it.get("valueUsd")
        print(f"  - {sym} {addr} ui={ui} usd={vu}")


def _visibility_effective_max_daily_new_positions(
    *,
    settings,
    wallet_available_sol: float | None,
    portfolio_cash_sol: float,
    birth_sol: float | None,
    effective_max_open: int | None,
) -> int | None:
    """Match CreeperDripper._effective_max_daily_new_positions when engine is unavailable."""
    try:
        baseline = int(settings.max_daily_new_positions)
        hard_cap = int(getattr(settings, "hard_max_daily_new_positions", baseline) or baseline)
        if not bool(getattr(settings, "dynamic_capacity_enabled", True)):
            return min(baseline, hard_cap)

        available_sol = wallet_available_sol
        if available_sol is None:
            available_sol = float(portfolio_cash_sol)
        reserve = float(settings.cash_reserve_sol)
        deployable = max(0.0, float(available_sol) - reserve)

        bsol = birth_sol
        dynamic_from_birth = baseline
        if bsol is not None and float(bsol) > 0:
            dynamic_from_birth = max(1, int(baseline * (float(available_sol) / float(bsol))))

        denom = max(float(settings.base_position_size_sol), float(settings.min_order_size_sol), 1e-9)
        funding_cap = int(deployable / denom) if deployable > 0 else 0

        eo = int(effective_max_open) if effective_max_open is not None else None
        effective = min(hard_cap, max(baseline, min(dynamic_from_birth, funding_cap)))
        if eo is not None:
            effective = min(effective, eo)
        effective = max(0, int(effective))
        if deployable > 0:
            effective = min(hard_cap, max(1, effective))
        return effective
    except Exception:
        return None


def _print_dynamic_capacity(
    *,
    settings,
    engine: CreeperDripper | None,
    wallet_available_sol: float | None,
    header: str,
    portfolio=None,
) -> None:
    print(f"\n=== {header} ===")
    print("note: affects entries only (never exits; never settlement truth)")
    birth_sol = None
    birth_ts = None
    effective_max = None
    if engine is not None:
        birth_sol = engine.portfolio.hachi_birth_wallet_sol
        birth_ts = engine.portfolio.hachi_birth_timestamp
        try:
            effective_max = engine._effective_max_open_positions()
        except Exception:
            effective_max = None
    reserve = float(settings.cash_reserve_sol)
    deployable = None if wallet_available_sol is None else max(0.0, float(wallet_available_sol) - reserve)

    # When doctor runs, we may not have an engine instance. Compute the same effective limit locally.
    if engine is None:
        try:
            baseline = int(settings.max_open_positions)
            hard_cap = int(getattr(settings, "hard_max_open_positions", baseline) or baseline)
            if not bool(getattr(settings, "dynamic_capacity_enabled", True)):
                effective_max = min(baseline, hard_cap)
            else:
                available = wallet_available_sol
                bsol = getattr(settings, "hachi_birth_wallet_sol", None) or birth_sol
                dynamic_from_birth = baseline
                if available is not None and bsol is not None and float(bsol) > 0:
                    dynamic_from_birth = max(1, int(baseline * (float(available) / float(bsol))))
                denom = max(float(settings.base_position_size_sol), float(settings.min_order_size_sol), 1e-9)
                funding_cap = int(float(deployable or 0.0) / denom) if deployable and deployable > 0 else 0
                effective_max = min(hard_cap, max(baseline, min(dynamic_from_birth, funding_cap)))
        except Exception:
            effective_max = None
    pf = portfolio if portfolio is not None else (engine.portfolio if engine is not None else None)
    pcash = float(pf.cash_sol) if pf is not None else None
    birth_for_daily = getattr(settings, "hachi_birth_wallet_sol", None) or birth_sol
    effective_daily = None
    if engine is not None:
        try:
            effective_daily = engine._effective_max_daily_new_positions()
        except Exception:
            effective_daily = None
    elif pcash is not None:
        effective_daily = _visibility_effective_max_daily_new_positions(
            settings=settings,
            wallet_available_sol=wallet_available_sol,
            portfolio_cash_sol=pcash,
            birth_sol=birth_for_daily,
            effective_max_open=effective_max,
        )
    print(f"dynamic_capacity_enabled={str(bool(getattr(settings, 'dynamic_capacity_enabled', True))).lower()}")
    print(f"hard_max_open_positions={getattr(settings, 'hard_max_open_positions', None)}")
    print(f"effective_max_open_positions={effective_max}")
    print(f"max_daily_new_positions={settings.max_daily_new_positions}")
    print(f"hard_max_daily_new_positions={getattr(settings, 'hard_max_daily_new_positions', None)}")
    print(f"effective_max_daily_new_positions={effective_daily}")
    if pf is not None:
        print(f"opened_today_count={pf.opened_today_count}")
        print(f"opened_today_date={pf.opened_today_date}")
    try:
        av = wallet_available_sol
        if av is None and pf is not None:
            av = float(pf.cash_sol)
        bs = birth_for_daily
        if av is not None and bs is not None and float(bs) > 0:
            print(f"hachi_capacity_scale={float(av) / float(bs)}")
        else:
            print("hachi_capacity_scale=n/a")
    except Exception:
        print("hachi_capacity_scale=n/a")
    print(f"hachi_birth_wallet_sol={getattr(settings, 'hachi_birth_wallet_sol', None) or birth_sol}")
    print(f"hachi_birth_timestamp={getattr(settings, 'hachi_birth_timestamp', None) or birth_ts}")
    print(f"current_wallet_sol_snapshot={wallet_available_sol}")
    print(f"deployable_sol={deployable}")



def run_doctor_checks(settings, birdeye_client: BirdeyeClient | None = None) -> tuple[bool, list[dict], object | None]:
    """
    Core health checks shared by `doctor` and `run` preflight.

    Returns (all_ok, checks, portfolio_or_none). May mutate `settings` (Hachi birth
    fields from portfolio) and persist portfolio when clearing stale-market safe mode.
    """
    checks: list[dict] = []
    ok = True

    checks.append({"check": "config_load", "ok": True})
    checks.append(
        {
            "check": "mode_flags",
            "ok": True,
            "dry_run": settings.dry_run,
            "live_trading_enabled": settings.live_trading_enabled,
        }
    )

    if settings.solana_keypair_path:
        wallet_ok = (
            settings.solana_keypair_path.exists()
            and settings.solana_keypair_path.is_file()
            and os.access(settings.solana_keypair_path, os.R_OK)
        )
        checks.append({"check": "wallet_path", "ok": wallet_ok, "path": str(settings.solana_keypair_path)})
        ok = ok and wallet_ok
    else:
        checks.append({"check": "wallet_path", "ok": True, "note": "not configured (allowed for doctor/scan/quote)"})

    runtime_ok = os.access(settings.runtime_dir, os.W_OK)
    checks.append({"check": "runtime_dir_writable", "ok": runtime_ok, "path": str(settings.runtime_dir)})
    ok = ok and runtime_ok

    birdeye = birdeye_client or BirdeyeClient(settings.birdeye_api_key, chain=settings.chain)
    try:
        birdeye.trending_tokens(limit=1)
        checks.append({"check": "birdeye_auth", "ok": True})
    except Exception as exc:
        checks.append({"check": "birdeye_auth", "ok": False, "error": str(exc)})
        ok = False

    jupiter = JupiterClient(settings.jupiter_api_key)
    try:
        jupiter.probe_quote(
            input_mint=SOL_MINT,
            output_mint=USDC_MINT,
            amount_atomic=1_000_000,
            slippage_bps=settings.default_slippage_bps,
        )
        checks.append({"check": "jupiter_probe_reachable_v1_quote", "ok": True, "endpoint": "GET /swap/v1/quote"})
    except Exception as exc:
        checks.append(
            {
                "check": "jupiter_probe_reachable_v1_quote",
                "ok": False,
                "endpoint": "GET /swap/v1/quote",
                "error": str(exc),
            }
        )
        ok = False

    try:
        jupiter.check_swap_reachability()
        checks.append({"check": "jupiter_execution_reachable_v1_swap", "ok": True, "endpoint": "POST /swap/v1/swap"})
    except Exception as exc:
        checks.append(
            {
                "check": "jupiter_execution_reachable_v1_swap",
                "ok": False,
                "endpoint": "POST /swap/v1/swap",
                "error": str(exc),
            }
        )
        ok = False

    portfolio = None
    try:
        portfolio = load_portfolio(settings.state_path, settings.portfolio_start_sol)
        ignored_stale = False
        if portfolio.safe_mode_active and portfolio.safety_stop_reason == SAFETY_STALE_MARKET_DATA:
            portfolio.safe_mode_active = False
            portfolio.safety_stop_reason = None
            ignored_stale = True
            try:
                save_portfolio(settings.state_path, portfolio)
            except Exception as exc:
                LOGGER.warning("doctor_clear_stale_market_safe_mode_failed: %s", exc)
        if getattr(portfolio, "hachi_birth_wallet_sol", None) is not None:
            try:
                settings.hachi_birth_wallet_sol = float(portfolio.hachi_birth_wallet_sol)
                settings.hachi_birth_timestamp = portfolio.hachi_birth_timestamp
            except Exception:
                pass
        checks.append(
            {
                "check": "safe_mode_state",
                "ok": True,
                "safe_mode_active": portfolio.safe_mode_active,
                "safety_stop_reason": portfolio.safety_stop_reason,
                "ignored_stale_market_data_outside_run": ignored_stale,
            }
        )
    except Exception as exc:
        checks.append({"check": "safe_mode_state", "ok": False, "error": str(exc)})
        ok = False

    return ok, checks, portfolio


def _print_preflight_summary_lines(checks: list[dict]) -> None:
    """Compact operator-facing lines for the standard doctor checks."""
    print("Preflight summary:")
    for row in checks:
        name = row.get("check", "?")
        row_ok = bool(row.get("ok"))
        label = "ok" if row_ok else "FAIL"
        bits: list[str] = [f"  {name}: {label}"]
        if name == "mode_flags" and row_ok:
            bits.append(f"dry_run={row.get('dry_run')} live_trading_enabled={row.get('live_trading_enabled')}")
        elif name == "wallet_path":
            if row.get("path"):
                bits.append(f"path={row['path']}")
            if row.get("note"):
                bits.append(str(row["note"]))
        elif name == "runtime_dir_writable" and row.get("path"):
            bits.append(f"path={row['path']}")
        elif name == "safe_mode_state" and row_ok:
            bits.append(f"safe_mode_active={row.get('safe_mode_active')} safety_stop_reason={row.get('safety_stop_reason')}")
        if not row_ok and row.get("error"):
            bits.append(f"error={row['error']}")
        elif name in ("jupiter_probe_reachable_v1_quote", "jupiter_execution_reachable_v1_swap") and row.get("endpoint"):
            bits.append(f"endpoint={row['endpoint']}")
        print(" | ".join(bits))


def _print_preflight_failure(checks: list[dict]) -> None:
    failed = [c for c in checks if not c.get("ok")]
    print("Preflight doctor: FAILED")
    print("Failed checks:")
    for c in failed:
        name = c.get("check", "?")
        err = c.get("error")
        if err:
            print(f"  - {name}: {err}")
        else:
            print(f"  - {name}: {c}")


def _print_run_capacity_config_line(settings) -> None:
    """Hard caps + baselines before engine/wallet snapshot (effective limits printed in STARTUP DYNAMIC CAPACITY)."""
    ho = getattr(settings, "hard_max_open_positions", None)
    hd = getattr(settings, "hard_max_daily_new_positions", None)
    print(
        "[preflight] capacity (config): "
        f"hard_max_open_positions={ho} "
        f"hard_max_daily_new_positions={hd} "
        f"max_open_positions={settings.max_open_positions} "
        f"max_daily_new_positions={settings.max_daily_new_positions} "
        "(effective_max_* printed after wallet snapshot)"
    )


def _safe_snapshot_copy(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _cycle_run_dir(runtime_dir: Path) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = runtime_dir / "cycle_runs" / ts
    out.mkdir(parents=True, exist_ok=True)
    return out


def _generate_run_id() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    return f"{ts}_{secrets.token_hex(3)}"


def _git_commit_short() -> str | None:
    try:
        out = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
        return out or None
    except Exception:
        return None


def _copy_new_runtime_artifacts(runtime_dir: Path, run_dir: Path, cycle_id: int, seen: set[str], cycle_started_at: float) -> list[str]:
    copied: list[str] = []
    patterns = [
        "entry_probe_*.json",
        "tx_failure_*.json",
        "exit_*.json",
    ]
    for pattern in patterns:
        for src in runtime_dir.glob(pattern):
            key = str(src.resolve())
            if key in seen:
                continue
            try:
                if src.stat().st_mtime < cycle_started_at:
                    continue
            except OSError:
                continue
            dst_name = f"cycle_{cycle_id}_{src.name}"
            dst = run_dir / dst_name
            _safe_snapshot_copy(src, dst)
            seen.add(key)
            copied.append(dst_name)
    return copied


def _candidate_score(candidate) -> float:
    if isinstance(candidate, dict):
        return float(candidate.get("discovery_score") or 0.0)
    return float(getattr(candidate, "discovery_score", 0.0) or 0.0)


def _handle_sigint(_signum, _frame):
    global STOP
    STOP = True


def _tcp_port_in_use(host: str, port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(0.25)
        return sock.connect_ex((host, port)) == 0
    except OSError:
        return False
    finally:
        sock.close()


def _start_dashboard_process(settings) -> subprocess.Popen | None:
    """Spawn local read-only dashboard (uvicorn); never raises."""
    host, port = "127.0.0.1", 8765
    try:
        if _tcp_port_in_use(host, port):
            print("Dashboard already running on port 8765")
            LOGGER.warning("event=dashboard_start_skipped reason=port_in_use port=%s", port)
            return None
        venv_uvicorn = Path.cwd() / ".venv" / "bin" / "uvicorn"
        if venv_uvicorn.is_file():
            cmd = [
                str(venv_uvicorn),
                "creeper_dripper.dashboard.app:app",
                "--host",
                host,
                "--port",
                str(port),
            ]
        else:
            cmd = [
                sys.executable,
                "-m",
                "uvicorn",
                "creeper_dripper.dashboard.app:app",
                "--host",
                host,
                "--port",
                str(port),
            ]
        env = os.environ.copy()
        env["RUNTIME_DIR"] = str(settings.runtime_dir)
        env["STATE_PATH"] = str(settings.state_path)
        env["JOURNAL_PATH"] = str(settings.journal_path)
        env["STATUS_PATH"] = str(settings.runtime_dir / "status.json")
        env["LOGFILE_PATH"] = str(settings.run_log_path)
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        time.sleep(0.6)
        if proc.poll() is not None:
            LOGGER.warning(
                "event=dashboard_start_failed returncode=%s",
                proc.returncode,
            )
            return None
        print("Dashboard running at http://127.0.0.1:8765")
        LOGGER.info("event=dashboard_started host=%s port=%s pid=%s", host, port, proc.pid)
        return proc
    except Exception as exc:
        LOGGER.warning("event=dashboard_start_failed error=%s", exc)
        return None


def _stop_dashboard_process(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    try:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                LOGGER.warning("dashboard_stop_kill_timeout pid=%s", proc.pid)
        LOGGER.info("event=dashboard_stopped pid=%s", proc.pid)
    except Exception as exc:
        LOGGER.warning("event=dashboard_stopped error=%s pid=%s", exc, getattr(proc, "pid", None))


def _load_owner_if_configured(settings):
    if settings.solana_keypair_path:
        LOGGER.info("Loading Solana keypair from path: %s", settings.solana_keypair_path)
        return load_keypair_from_file(settings.solana_keypair_path)
    if settings.bs58_private_key:
        LOGGER.warning("BS58_PRIVATE_KEY is deprecated, use SOLANA_KEYPAIR_PATH")
        return load_keypair_from_base58(settings.bs58_private_key)
    return None


def build_runtime(
    *,
    require_owner: bool,
    load_owner: bool,
    settings=None,
    configure_logging: bool = True,
    birdeye: BirdeyeClient | None = None,
):
    settings = settings or load_settings()
    if configure_logging:
        setup_logging(settings.log_level, runtime_dir=settings.runtime_dir, run_log_path=settings.run_log_path)
    owner = _load_owner_if_configured(settings) if load_owner else None
    if require_owner and owner is None:
        raise RuntimeError("Missing wallet credentials: set SOLANA_KEYPAIR_PATH (preferred) or BS58_PRIVATE_KEY")
    birdeye = birdeye or BirdeyeClient(settings.birdeye_api_key, chain=settings.chain)
    jupiter = JupiterClient(settings.jupiter_api_key)
    executor = TradeExecutor(jupiter, owner, settings)
    portfolio = load_portfolio(settings.state_path, settings.portfolio_start_sol)
    engine = CreeperDripper(settings, birdeye, executor, portfolio)
    return settings, engine, executor, birdeye, owner


def cmd_scan(_args: argparse.Namespace) -> int:
    settings, _engine, executor, birdeye, _owner = build_runtime(require_owner=False, load_owner=False)
    latest_path = settings.runtime_dir / "scan_latest.json"
    summary_path = settings.runtime_dir / "scan_summary.json"
    atomic_write_json(latest_path, [])
    progress_state = {
        "seeds_total": 0,
        "processed_total": 0,
        "built_total": 0,
        "accepted_total": 0,
        "rejection_counts": {},
        "last_processed_symbol": None,
        "last_processed_mint": None,
        "partial_progress": True,
        "interrupted": False,
        "top_candidates_seen": [],
    }
    atomic_write_json(summary_path, progress_state)

    def _on_progress(summary_payload: dict, accepted_payload: list[dict]) -> None:
        progress_state.update(summary_payload)
        payload = {
            **summary_payload,
            "partial_progress": True,
            "interrupted": False,
        }
        atomic_write_json(summary_path, payload)
        atomic_write_json(latest_path, accepted_payload or [])

    try:
        candidates, summary = discover_candidates(birdeye, executor.jupiter, settings, progress_callback=_on_progress)
        final_latest = serialize_candidates(candidates)
        # Persist accepted candidates first so summary issues cannot erase them.
        atomic_write_json(latest_path, final_latest)
        try:
            top_candidates = serialize_candidates(sorted(candidates, key=_candidate_score, reverse=True)[:5])
            final_summary = {
                "seeds_total": summary.get("seeds_total", 0),
                "processed_total": summary.get("candidates_built", 0) + summary.get("candidates_rejected_total", 0),
                "built_total": summary.get("candidates_built", 0),
                "accepted_total": summary.get("candidates_accepted", 0),
                "rejection_counts": summary.get("rejection_counts", {}),
                "last_processed_symbol": None,
                "last_processed_mint": None,
                "partial_progress": False,
                "interrupted": False,
                "top_candidates_seen": top_candidates,
            }
        except Exception as exc:
            LOGGER.exception("failed to serialize scan summary; writing minimal summary: %s", exc)
            final_summary = {
                "seeds_total": summary.get("seeds_total", 0),
                "processed_total": summary.get("candidates_built", 0) + summary.get("candidates_rejected_total", 0),
                "built_total": summary.get("candidates_built", 0),
                "accepted_total": len(final_latest),
                "rejection_counts": summary.get("rejection_counts", {}),
                "last_processed_symbol": None,
                "last_processed_mint": None,
                "partial_progress": False,
                "interrupted": False,
                "top_candidates_seen": [],
                "summary_serialization_error": str(exc),
            }
        atomic_write_json(summary_path, final_summary)
        print(json.dumps(final_latest, indent=2, default=str))
        return 0
    except KeyboardInterrupt:
        interrupted_summary = {
            **progress_state,
            "interrupted": True,
            "partial_progress": True,
        }
        try:
            existing_latest = json.loads(latest_path.read_text(encoding="utf-8")) if latest_path.exists() else []
        except Exception:
            existing_latest = []
        atomic_write_json(latest_path, existing_latest)
        atomic_write_json(summary_path, interrupted_summary)
        print("scan interrupted; partial results written to runtime/scan_latest.json and runtime/scan_summary.json")
        return 130


def cmd_run(args: argparse.Namespace) -> int:
    _print_interpreter_banner()
    settings = load_settings()
    run_id = _generate_run_id()
    settings.run_id = run_id
    settings.run_dir = settings.runtime_dir / "runs" / run_id
    settings.run_dir.mkdir(parents=True, exist_ok=True)
    settings.run_log_path = settings.run_dir / "logfile.log"
    setup_logging(settings.log_level, runtime_dir=settings.runtime_dir, run_log_path=settings.run_log_path)

    ok, checks, _pf_portfolio = run_doctor_checks(settings)
    _print_preflight_summary_lines(checks)
    if not ok:
        _print_preflight_failure(checks)
        LOGGER.error("preflight_doctor_failed checks=%s", checks)
        return 1
    print("Preflight doctor: ok")
    _print_run_capacity_config_line(settings)

    require_owner = settings.live_trading_enabled and not settings.dry_run
    _settings, engine, executor, birdeye, owner = build_runtime(
        require_owner=require_owner,
        load_owner=require_owner,
        settings=settings,
        configure_logging=False,
    )
    # Startup guard: ignore any persisted stale-market safe-mode from a previous run.
    # Stale-market safety applies only during an active run loop.
    if engine.portfolio.safe_mode_active and engine.portfolio.safety_stop_reason == SAFETY_STALE_MARKET_DATA:
        engine.portfolio.safe_mode_active = False
        engine.portfolio.safety_stop_reason = None
        try:
            save_portfolio(settings.state_path, engine.portfolio)
        except Exception as exc:
            LOGGER.warning("startup_clear_stale_market_safe_mode_failed: %s", exc)
    run_dir = _cycle_run_dir(settings.run_dir)
    observed_cycles: list[dict] = []
    seen_artifacts: set[str] = set()
    token_tracking: dict[str, dict] = {}
    notable_events: list[dict] = []
    run_mode = "run_once" if args.once else ("run_cycles" if args.cycles is not None else "run_loop")
    git_commit = _git_commit_short()
    startup_payload = {
        "event": "run_started",
        "run_id": settings.run_id,
        "mode": run_mode,
        "pid": os.getpid(),
        "git_commit": git_commit,
        "run_dir": str(settings.run_dir),
    }
    LOGGER.warning(
        "event=run_started run_id=%s mode=%s pid=%s git_commit=%s run_dir=%s",
        settings.run_id,
        run_mode,
        os.getpid(),
        git_commit,
        settings.run_dir,
    )
    print(json.dumps(startup_payload, indent=2, default=str))
    print(
        json.dumps(
            {
                "mode": {
                    "dry_run": settings.dry_run,
                    "live_trading_enabled": settings.live_trading_enabled,
                },
                "run_id": settings.run_id,
            },
            indent=2,
            default=str,
        )
    )
    # One-time wallet snapshot on startup (visibility only; never used as settlement truth)
    wallet = None
    try:
        wallet = str(owner.pubkey()) if owner is not None else None
    except Exception:
        wallet = None
    _print_wallet_snapshot(
        settings=settings,
        birdeye=birdeye,
        executor=executor,
        wallet=wallet,
        header="STARTUP WALLET SNAPSHOT (visibility only)",
    )
    # Seed dynamic capacity snapshot + Hachi birth baseline (visibility/bootstrap only).
    try:
        now = datetime.now(timezone.utc).isoformat()
        lamports = executor.native_sol_balance_lamports(wallet) if wallet else None
        available_sol = None if lamports is None else float(lamports) / 1_000_000_000.0
        engine.set_wallet_snapshot(available_sol=available_sol, snapshot_at=now)
        birth_sol = settings.hachi_birth_wallet_sol
        birth_ts = settings.hachi_birth_timestamp
        if engine.portfolio.hachi_birth_wallet_sol is None and birth_sol is not None:
            engine.portfolio.hachi_birth_wallet_sol = float(birth_sol)
            engine.portfolio.hachi_birth_timestamp = birth_ts or now
            save_portfolio(settings.state_path, engine.portfolio)
        elif engine.portfolio.hachi_birth_wallet_sol is None and available_sol is not None:
            engine.portfolio.hachi_birth_wallet_sol = float(available_sol)
            engine.portfolio.hachi_birth_timestamp = now
            save_portfolio(settings.state_path, engine.portfolio)
    except Exception as exc:
        LOGGER.warning("startup_wallet_snapshot_seed_failed: %s", exc)
    try:
        _print_dynamic_capacity(
            settings=settings,
            engine=engine,
            wallet_available_sol=locals().get("available_sol", None),
            header="STARTUP DYNAMIC CAPACITY (entries only)",
            portfolio=engine.portfolio,
        )
    except Exception as exc:
        LOGGER.warning("startup_dynamic_capacity_print_failed: %s", exc)
    recovery = engine.run_startup_recovery()
    if recovery:
        print(json.dumps({"startup_recovery": [asdict(d) for d in recovery]}, indent=2, default=str))
    recovery_discrepancies = [d for d in recovery if d.reason == "recovery_wallet_gt_state"]
    if recovery_discrepancies:
        warning_payload = {
            "warning": "DIRTY_WALLET_DETECTED_NOT_CLEAN_START",
            "details": {
                "recovery_wallet_gt_state_count": len(recovery_discrepancies),
                "tokens": [
                    {
                        "symbol": d.symbol,
                        "mint": d.token_mint,
                        "state_qty": (d.metadata or {}).get("state_qty"),
                        "wallet_qty": (d.metadata or {}).get("wallet_qty"),
                    }
                    for d in recovery_discrepancies
                ],
                "operator_actions": [
                    "flatten wallet first for a true clean start",
                    "or import/reconcile holdings into state before continuing",
                ],
            },
        }
        print(json.dumps(warning_payload, indent=2, default=str))
        LOGGER.warning(
            "DIRTY_WALLET_DETECTED_NOT_CLEAN_START recovery_wallet_gt_state_count=%s",
            len(recovery_discrepancies),
        )
    signal.signal(signal.SIGINT, _handle_sigint)
    signal.signal(signal.SIGTERM, _handle_sigint)
    dashboard_process: subprocess.Popen | None = None
    try:
        dashboard_process = _start_dashboard_process(settings)
        next_run = time.monotonic()
        cycles = 0
        requested_cycles = 1 if args.once else (max(1, int(args.cycles)) if args.cycles is not None else None)
        prev_cache_debug_keys: list[str] = []
        stop_reason = "unknown"
        while not STOP:
            cycles += 1
            cycle_started_mono = time.monotonic()
            cycle_start_ts = datetime.now(timezone.utc).isoformat()
            prev_positions = {
                mint: {
                    "symbol": p.symbol,
                    "status": p.status,
                    "last_price_usd": p.last_price_usd,
                    "peak_price_usd": p.peak_price_usd,
                    "last_mark_sol_per_token": p.last_mark_sol_per_token,
                    "peak_mark_sol_per_token": p.peak_mark_sol_per_token,
                    "current_estimated_value_sol": p.last_estimated_exit_value_sol,
                    "unrealized_pnl_sol": p.unrealized_pnl_sol,
                    "valuation_source": p.valuation_source,
                    "usd_mark_unavailable": p.usd_mark_unavailable,
                }
                for mint, p in engine.portfolio.open_positions.items()
            }
            summary = engine.run_cycle()
            cycle_end_ts = datetime.now(timezone.utc).isoformat()
            print(json.dumps(summary, indent=2, default=str))
            s = summary.get("summary", {})
            runtime_cost_line = (
                "runtime_cost: "
                f"cache_hits={s.get('cache_hits', 0)} "
                f"cache_misses={s.get('cache_misses', 0)} "
                f"candidate_cache_hits={s.get('candidate_cache_hits', 0)} "
                f"candidate_cache_misses={s.get('candidate_cache_misses', 0)} "
                f"route_cache_hits={s.get('route_cache_hits', 0)} "
                f"route_cache_misses={s.get('route_cache_misses', 0)} "
                f"discovered={s.get('discovered_candidates', 0)} "
                f"prefiltered={s.get('prefiltered_candidates', 0)} "
                f"built={s.get('candidates_built', 0)} "
                f"topN={s.get('topn_candidates', 0)} "
                f"route_checked={s.get('route_checked_candidates', 0)} "
                f"accepted={s.get('candidates_accepted', 0)} "
                f"discovery_reused={s.get('discovery_cached', False)}"
            )
            print(runtime_cost_line)
            LOGGER.info(runtime_cost_line)
            cache_debug_first_keys = list(s.get("cache_debug_first_keys") or [])
            repeated_with_prev_cycle = bool(set(cache_debug_first_keys) & set(prev_cache_debug_keys))
            cache_debug_line = (
                "cache_debug: "
                f"first_keys={cache_debug_first_keys} "
                f"repeat_with_prev_cycle={repeated_with_prev_cycle}"
            )
            print(cache_debug_line)
            LOGGER.info(cache_debug_line)
            identity = s.get("cache_debug_identity") or {}
            engine_identity = s.get("cache_engine_identity") or {}
            cache_identity_line = (
                "cache_identity: "
                f"engine_candidate_id={engine_identity.get('candidate_cache_id')} "
                f"discover_candidate_id={identity.get('candidate_cache_id')} "
                f"engine_route_id={engine_identity.get('route_cache_id')} "
                f"discover_route_id={identity.get('route_cache_id')}"
            )
            print(cache_identity_line)
            LOGGER.info(cache_identity_line)
            cache_trace = s.get("cache_debug_trace") or {}
            candidate_trace = list(cache_trace.get("candidate") or [])
            route_trace = list(cache_trace.get("route") or [])
            cache_trace_line = (
                "cache_trace: "
                f"candidate_first_ops={candidate_trace[:6]} "
                f"route_first_ops={route_trace[:6]}"
            )
            print(cache_trace_line)
            LOGGER.info(cache_trace_line)
            prev_cache_debug_keys = cache_debug_first_keys
            if (s.get("entries_skipped_dry_run", 0) or 0) > 0 or (s.get("entries_skipped_live_disabled", 0) or 0) > 0:
                print(
                    "entry execution skipped by mode: "
                    f"dry_run={s.get('entries_skipped_dry_run', 0)}, "
                    f"live_disabled={s.get('entries_skipped_live_disabled', 0)}"
                )
            cycle_id = cycles
            _safe_snapshot_copy(settings.state_path, run_dir / f"state_cycle_{cycle_id}.json")
            _safe_snapshot_copy(settings.runtime_dir / "status.json", run_dir / f"status_cycle_{cycle_id}.json")
            _safe_snapshot_copy(settings.journal_path, run_dir / f"journal_cycle_{cycle_id}.jsonl")
            atomic_write_json(run_dir / f"events_cycle_{cycle_id}.json", summary.get("events", []))
            copied_artifacts = _copy_new_runtime_artifacts(
                settings.run_dir or settings.runtime_dir,
                run_dir,
                cycle_id,
                seen_artifacts,
                cycle_started_mono,
            )
    
            decisions = summary.get("decisions", [])
            for mint, pos in engine.portfolio.open_positions.items():
                symbol = pos.symbol
                track = token_tracking.setdefault(
                    mint,
                    {
                        "symbol": symbol,
                        "status_transitions": [pos.status],
                        "start_price_usd": pos.last_price_usd,
                        "end_price_usd": pos.last_price_usd,
                        "peak_updates": 0,
                        "exit_attempts": 0,
                        "exit_outcomes": [],
                        "recovery_discrepancies": 0,
                    },
                )
                old = prev_positions.get(mint)
                if old and old.get("status") != pos.status:
                    track["status_transitions"].append(pos.status)
                track["end_price_usd"] = pos.last_price_usd
                if old and (pos.peak_price_usd or 0.0) > (old.get("peak_price_usd") or 0.0):
                    track["peak_updates"] += 1
    
            for d in decisions:
                action = d.get("action")
                mint = d.get("token_mint")
                if action == "SELL_ATTEMPT" and mint in token_tracking:
                    token_tracking[mint]["exit_attempts"] += 1
                if action in {"SELL", "SELL_BLOCKED", "SELL_PENDING"} and mint in token_tracking:
                    token_tracking[mint]["exit_outcomes"].append(action)
                if d.get("reason") == "recovery_wallet_gt_state" and mint in token_tracking:
                    token_tracking[mint]["recovery_discrepancies"] += 1
    
                if action in {"SELL_BLOCKED", "SELL", "SELL_PENDING", "RECOVERY_DISCREPANCY", "BUY"}:
                    notable_events.append(
                        {
                            "cycle_id": cycle_id,
                            "action": action,
                            "symbol": d.get("symbol"),
                            "reason": d.get("reason"),
                            "classification": (d.get("metadata") or {}).get("classification"),
                        }
                    )
    
            observed_cycles.append(
                {
                    "cycle_id": cycle_id,
                    "start_ts": cycle_start_ts,
                    "end_ts": cycle_end_ts,
                    "entries_attempted": s.get("entries_attempted", 0),
                    "entries_succeeded": s.get("entries_succeeded", 0),
                    "exits_attempted": s.get("exits_attempted", 0),
                    "exits_succeeded": s.get("exits_succeeded", 0),
                    "exit_blocked_positions": s.get("exit_blocked_positions", 0),
                    "execution_failures": s.get("execution_failures", 0),
                    "recovery_discrepancies": sum(1 for d in decisions if d.get("reason") == "recovery_wallet_gt_state"),
                    "cache_hits": s.get("cache_hits", 0),
                    "cache_misses": s.get("cache_misses", 0),
                    "candidate_cache_hits": s.get("candidate_cache_hits", 0),
                    "candidate_cache_misses": s.get("candidate_cache_misses", 0),
                    "route_cache_hits": s.get("route_cache_hits", 0),
                    "route_cache_misses": s.get("route_cache_misses", 0),
                    "discovered": s.get("discovered_candidates", 0),
                    "prefiltered": s.get("prefiltered_candidates", 0),
                    "built": s.get("candidates_built", 0),
                    "topN": s.get("topn_candidates", 0),
                    "route_checked": s.get("route_checked_candidates", 0),
                    "accepted": s.get("candidates_accepted", 0),
                    "discovery_reused": s.get("discovery_cached", False),
                    "cache_debug_first_keys": cache_debug_first_keys,
                    "cache_debug_repeat_with_prev_cycle": repeated_with_prev_cycle,
                    "copied_artifacts": copied_artifacts,
                }
            )
    
            if requested_cycles is not None and cycles >= requested_cycles:
                stop_reason = "requested_cycles_reached"
                break
            next_run += settings.poll_interval_seconds
            monotonic_sleep_until(next_run)
        if STOP and stop_reason == "unknown":
            stop_reason = "signal"
        # Operator stop guard: stopping manually must not leave the system in stale-market safe-mode.
        if stop_reason == "signal" and engine.portfolio.safety_stop_reason == SAFETY_STALE_MARKET_DATA:
            engine.portfolio.safe_mode_active = False
            engine.portfolio.safety_stop_reason = None
            try:
                save_portfolio(settings.state_path, engine.portfolio)
            except Exception as exc:
                LOGGER.warning("stop_clear_stale_market_safe_mode_failed: %s", exc)
    
        open_positions = list(engine.portfolio.open_positions.values())
        blocked_positions = [p for p in open_positions if p.status == "EXIT_BLOCKED"]
        blocked_symbols = [p.symbol for p in blocked_positions]
    
        per_token = []
        for mint, info in token_tracking.items():
            start_price = info.get("start_price_usd")
            end_price = info.get("end_price_usd")
            price_change = None
            if isinstance(start_price, (int, float)) and isinstance(end_price, (int, float)) and start_price:
                price_change = ((end_price - start_price) / start_price) * 100.0
            per_token.append(
                {
                    "mint": mint,
                    "symbol": info.get("symbol"),
                    "status_transitions": info.get("status_transitions", []),
                    "price_change_pct": price_change,
                    "peak_updates": info.get("peak_updates", 0),
                    "exit_attempts": info.get("exit_attempts", 0),
                    "exit_outcomes": info.get("exit_outcomes", []),
                    "recovery_discrepancies": info.get("recovery_discrepancies", 0),
                }
            )
    
        summary_payload = {
            "run_dir": str(run_dir),
            "run_id": settings.run_id,
            "total_cycles": len(observed_cycles),
            "entries_attempted": sum(c.get("entries_attempted", 0) for c in observed_cycles),
            "entries_succeeded": sum(c.get("entries_succeeded", 0) for c in observed_cycles),
            "exits_attempted": sum(c.get("exits_attempted", 0) for c in observed_cycles),
            "exits_succeeded": sum(c.get("exits_succeeded", 0) for c in observed_cycles),
            "exit_blocked_count": len(blocked_positions),
            "blocked_symbols": blocked_symbols,
            "recovery_discrepancies": sum(c.get("recovery_discrepancies", 0) for c in observed_cycles),
            "execution_failures": sum(c.get("execution_failures", 0) for c in observed_cycles),
            "cycles": observed_cycles,
            "per_token_tracking": per_token,
            "focus_tracking": {
                "69": next((x for x in per_token if x.get("symbol") == "69"), None),
                "PRl": next((x for x in per_token if x.get("symbol") == "PRl"), None),
                "Sandwich": next((x for x in per_token if x.get("symbol") == "Sandwich"), None),
            },
            "notable_events": notable_events[:50],
        }
        atomic_write_json(run_dir / "summary.json", summary_payload)
        print(json.dumps({"cycle_run_summary_path": str(run_dir / "summary.json")}, indent=2, default=str))
        LOGGER.warning(
            "event=run_stopped run_id=%s reason=%s total_cycles=%s run_dir=%s",
            settings.run_id,
            stop_reason,
            len(observed_cycles),
            run_dir,
        )
        print(
            json.dumps(
                {
                    "event": "run_stopped",
                    "run_id": settings.run_id,
                    "reason": stop_reason,
                    "total_cycles": len(observed_cycles),
                    "run_dir": str(run_dir),
                },
                indent=2,
                default=str,
            )
        )
        return 0
    finally:
        _stop_dashboard_process(dashboard_process)


def cmd_quote(args: argparse.Namespace) -> int:
    settings, _engine, executor, _birdeye, _owner = build_runtime(require_owner=False, load_owner=False)
    if args.side == "buy":
        probe = executor.jupiter.probe_quote(
            input_mint=SOL_MINT,
            output_mint=args.mint,
            amount_atomic=max(1, int(args.size_sol * 1_000_000_000)),
            slippage_bps=settings.default_slippage_bps,
        )
    else:
        probe = executor.jupiter.probe_quote(
            input_mint=args.mint,
            output_mint=SOL_MINT,
            amount_atomic=args.amount_atomic,
            slippage_bps=settings.default_slippage_bps,
        )
    print(json.dumps(probe.__dict__, indent=2, default=str))
    return 0


def cmd_doctor(_args: argparse.Namespace) -> int:
    _print_interpreter_banner()
    _print_doctor_cli_hints()
    settings = None
    try:
        settings = load_settings()
    except Exception as exc:
        checks = [{"check": "config_load", "ok": False, "error": str(exc)}]
        print(json.dumps({"ok": False, "checks": checks}, indent=2, default=str))
        return 1

    ok, checks, portfolio = run_doctor_checks(settings)
    print(json.dumps({"ok": ok, "checks": checks}, indent=2, default=str))
    birdeye = BirdeyeClient(settings.birdeye_api_key, chain=settings.chain)
    jupiter = JupiterClient(settings.jupiter_api_key)
    # Wallet snapshot (visibility only; never used as settlement truth)
    wallet = _wallet_address_for_snapshot(settings)
    executor = TradeExecutor(jupiter, owner=None, settings=settings)
    _print_wallet_snapshot(
        settings=settings,
        birdeye=birdeye,
        executor=executor,
        wallet=wallet,
        header="WALLET SNAPSHOT (visibility only)",
    )
    # Dynamic capacity (computed from current visibility-only wallet snapshot)
    lamports = executor.native_sol_balance_lamports(wallet) if wallet else None
    wallet_sol = None if lamports is None else float(lamports) / 1_000_000_000.0
    # Hachi birth baseline initialization (first interaction wins: doctor or run startup).
    # Never overwrite if already set.
    try:
        if portfolio is not None and portfolio.hachi_birth_wallet_sol is None and wallet_sol is not None:
            now = datetime.now(timezone.utc).isoformat()
            portfolio.hachi_birth_wallet_sol = float(wallet_sol)
            portfolio.hachi_birth_timestamp = now
            save_portfolio(settings.state_path, portfolio)
            # Also surface in current settings instance for this doctor output.
            settings.hachi_birth_wallet_sol = float(wallet_sol)
            settings.hachi_birth_timestamp = now
            LOGGER.warning(
                "event=hachi_birth_initialized source=doctor wallet_sol=%s timestamp=%s",
                portfolio.hachi_birth_wallet_sol,
                portfolio.hachi_birth_timestamp,
            )
    except Exception as exc:
        LOGGER.warning("doctor_hachi_birth_init_failed: %s", exc)
    _print_dynamic_capacity(
        settings=settings,
        engine=None,
        wallet_available_sol=wallet_sol,
        header="DYNAMIC CAPACITY (entries only; visibility-derived)",
        portfolio=portfolio,
    )
    _print_env_snapshot(settings)
    return 0 if ok else 1


def cmd_status(_args: argparse.Namespace) -> int:
    settings = load_settings()
    portfolio = load_portfolio(settings.state_path, settings.portfolio_start_sol)
    open_positions = list(portfolio.open_positions.values())
    blocked_positions = [p for p in open_positions if p.status == "EXIT_BLOCKED"]
    zombie_positions = [p for p in open_positions if p.status == "ZOMBIE"]
    blocked_or_zombie = [p for p in open_positions if p.status in {"EXIT_BLOCKED", "ZOMBIE"}]
    blocked_or_zombie_symbols = [p.symbol for p in blocked_or_zombie]
    summary = {
        "dry_run": settings.dry_run,
        "live_trading_enabled": settings.live_trading_enabled,
        "open_positions": len(open_positions),
        "partial_positions": sum(1 for p in open_positions if p.status == "PARTIAL"),
        "exit_pending_positions": sum(1 for p in open_positions if p.status == "EXIT_PENDING"),
        "exit_blocked_positions": sum(1 for p in open_positions if p.status == "EXIT_BLOCKED"),
        "zombie_positions": len(zombie_positions),
        "blocked_or_zombie_symbols": blocked_or_zombie_symbols,
        "blocked_or_zombie_positions": [
            {
                "symbol": p.symbol,
                "mint": p.token_mint,
                "status": p.status,
                "blocked_cycles": getattr(p, "exit_blocked_cycles", 0),
                "zombie_reason": getattr(p, "zombie_reason", None),
                "zombie_since": getattr(p, "zombie_since", None),
                "valuation_status": getattr(p, "valuation_status", None),
            }
            for p in blocked_or_zombie
        ],
        "blocked_positions": [
            {
                "symbol": p.symbol,
                "mint": p.token_mint,
                "blocked_reason_classification": p.pending_exit_reason,
                "next_retry_at": p.next_exit_retry_at,
            }
            for p in blocked_positions
        ],
        "closed_positions": len(portfolio.closed_positions),
        "cash_sol": round(portfolio.cash_sol, 6),
        "safe_mode_active": portfolio.safe_mode_active,
        "safety_stop_reason": portfolio.safety_stop_reason,
        "last_cycle_at": portfolio.last_cycle_at,
        "consecutive_execution_failures": portfolio.consecutive_execution_failures,
        "entries_skipped_dry_run": portfolio.entries_skipped_dry_run,
        "entries_skipped_live_disabled": portfolio.entries_skipped_live_disabled,
        "opened_today_count": portfolio.opened_today_count,
        "opened_today_date": portfolio.opened_today_date,
    }
    print(json.dumps(summary, indent=2, default=str))
    return 0


def cmd_cleanup_dust(_args: argparse.Namespace) -> int:
    """
    Cleanup dust / leftovers in the wallet.

    Visibility/bootstrap only: uses Birdeye wallet snapshot to enumerate holdings.
    Execution truth remains Jupiter-only (no wallet balance settlement truth).
    """
    settings = load_settings()
    # We only require owner if this command can actually execute sells.
    require_owner = settings.live_trading_enabled and not settings.dry_run
    settings, engine, executor, birdeye, owner = build_runtime(require_owner=require_owner, load_owner=True, settings=settings)
    wallet = None
    try:
        wallet = str(owner.pubkey()) if owner is not None else _wallet_address_for_snapshot(settings)
    except Exception:
        wallet = _wallet_address_for_snapshot(settings)
    if not wallet:
        raise RuntimeError("cleanup-dust requires a configured wallet (SOLANA_KEYPAIR_PATH preferred)")

    snap = birdeye.wallet_token_list(wallet)
    items = list(snap.get("items") or [])
    portfolio = engine.portfolio
    open_mints = set(portfolio.open_positions.keys())

    leftovers: list[dict] = []
    for it in items:
        addr = str((it or {}).get("address") or "").strip()
        if not addr or addr in {SOL_MINT, USDC_MINT}:
            continue
        ui_amt = it.get("uiAmount") or 0.0
        try:
            ui_amt = float(ui_amt)
        except Exception:
            ui_amt = 0.0
        if ui_amt <= 0:
            continue
        if addr in open_mints:
            continue
        leftovers.append(it)

    # Safety: keep cleanup conservative; do not liquidate large holdings automatically.
    DUST_VALUE_USD = 1.0
    MAX_AUTO_SELL_VALUE_USD = 50.0

    sold: list[dict] = []
    archived: list[dict] = []
    still_blocked: list[dict] = []

    for it in leftovers:
        mint = str(it.get("address") or "").strip()
        symbol = str(it.get("symbol") or "?").strip() or "?"
        balance = it.get("balance")
        try:
            amount_atomic = int(balance)
        except Exception:
            amount_atomic = 0
        value_usd = it.get("valueUsd")
        try:
            value_usd_f = float(value_usd) if value_usd is not None else 0.0
        except Exception:
            value_usd_f = 0.0

        if value_usd_f > 0.0 and value_usd_f <= DUST_VALUE_USD:
            archived.append({"mint": mint, "symbol": symbol, "reason": "dust", "value_usd": value_usd_f, "amount_atomic": amount_atomic})
            continue

        # One final route check + optional one-time sell.
        try:
            probe = executor.quote_sell(mint, max(1, amount_atomic))
        except Exception as exc:
            still_blocked.append({"mint": mint, "symbol": symbol, "reason": "quote_exception", "error": str(exc), "value_usd": value_usd_f})
            continue

        if not probe.route_ok or not probe.out_amount_atomic:
            archived.append({"mint": mint, "symbol": symbol, "reason": "no_route", "value_usd": value_usd_f, "amount_atomic": amount_atomic})
            continue

        if value_usd_f > MAX_AUTO_SELL_VALUE_USD:
            still_blocked.append({"mint": mint, "symbol": symbol, "reason": "tradable_but_too_large_for_auto_cleanup", "value_usd": value_usd_f, "amount_atomic": amount_atomic})
            continue

        result, _q = executor.sell(mint, max(1, amount_atomic))
        if result.status == "success" or result.status == "skipped":
            sold.append({"mint": mint, "symbol": symbol, "status": result.status, "diagnostic_code": result.diagnostic_code, "signature": result.signature, "value_usd": value_usd_f, "amount_atomic": amount_atomic})
        else:
            still_blocked.append({"mint": mint, "symbol": symbol, "status": result.status, "diagnostic_code": result.diagnostic_code, "error": result.error, "value_usd": value_usd_f, "amount_atomic": amount_atomic})

    # Archive report for operator evidence (non-destructive; no retries).
    try:
        ts = datetime.now(timezone.utc).isoformat()
        archive_path = settings.runtime_dir / "dust_cleanup_archive.json"
        payload = {
            "timestamp": ts,
            "wallet": wallet,
            "sold": sold,
            "archived": archived,
            "still_blocked": still_blocked,
        }
        atomic_write_json(archive_path, payload)
    except Exception as exc:
        LOGGER.warning("dust_cleanup_archive_write_failed: %s", exc)

    summary = {
        "wallet": wallet,
        "leftovers_seen": len(leftovers),
        "sold": len(sold),
        "archived": len(archived),
        "still_blocked": len(still_blocked),
        "sold_items": sold,
        "archived_items": archived,
        "still_blocked_items": still_blocked,
    }
    print(json.dumps(summary, indent=2, default=str))
    return 0


def cmd_audit_birdeye_once(_args: argparse.Namespace) -> int:
    """
    Diagnostic: doctor/preflight, then one discovery cycle with Birdeye request audit + credits delta.
    Does not execute live trades (same discovery path as `scan`).
    """
    settings = load_settings()
    setup_logging(settings.log_level, runtime_dir=settings.runtime_dir, run_log_path=settings.run_log_path)
    audit_jsonl = settings.runtime_dir / "birdeye_audit.jsonl"
    summary_path = settings.runtime_dir / "birdeye_audit_summary.json"
    audit_jsonl.parent.mkdir(parents=True, exist_ok=True)
    audit_jsonl.write_text("", encoding="utf-8")

    birdeye = BirdeyeClient(settings.birdeye_api_key, chain=settings.chain, audit_jsonl_path=audit_jsonl)
    birdeye.audit_reset()
    birdeye.audit_set_phase("doctor")
    ok, checks, _portfolio = run_doctor_checks(settings, birdeye_client=birdeye)
    _print_preflight_summary_lines(checks)
    if not ok:
        _print_preflight_failure(checks)
        LOGGER.error("audit_birdeye_preflight_failed checks=%s", checks)
        payload = build_birdeye_audit_summary_dict(
            birdeye.audit_snapshot(),
            credits_before=None,
            credits_after=None,
            discovery_summary={},
            doctor_ok=False,
        )
        payload["preflight_checks"] = checks
        payload["audit_artifacts"] = {"jsonl": str(audit_jsonl), "summary": str(summary_path)}
        atomic_write_json(summary_path, payload)
        print(json.dumps(payload, indent=2, default=str))
        print(f"Wrote {summary_path}")
        return 1

    print("Preflight doctor: ok")
    birdeye.audit_set_phase("credits_meter")
    credits_before: dict | None = None
    credits_after: dict | None = None
    try:
        credits_before = birdeye.credits_usage()
    except Exception as exc:
        LOGGER.warning("birdeye_credits_before_failed: %s", exc)

    birdeye.audit_set_phase("discovery")
    _settings, _engine, executor, _b, _owner = build_runtime(
        require_owner=False,
        load_owner=False,
        settings=settings,
        configure_logging=False,
        birdeye=birdeye,
    )
    _candidates, summary = discover_candidates(birdeye, executor.jupiter, settings)

    birdeye.audit_set_phase("credits_meter")
    try:
        credits_after = birdeye.credits_usage()
    except Exception as exc:
        LOGGER.warning("birdeye_credits_after_failed: %s", exc)

    payload = build_birdeye_audit_summary_dict(
        birdeye.audit_snapshot(),
        credits_before=credits_before,
        credits_after=credits_after,
        discovery_summary=summary,
        doctor_ok=True,
    )
    payload["preflight_checks"] = checks
    payload["audit_artifacts"] = {"jsonl": str(audit_jsonl), "summary": str(summary_path)}
    atomic_write_json(summary_path, payload)
    print(json.dumps(payload, indent=2, default=str))
    print(f"Wrote {audit_jsonl}")
    print(f"Wrote {summary_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    _ensure_project_venv()
    parser = argparse.ArgumentParser(prog="creeper-dripper")
    sub = parser.add_subparsers(dest="cmd", required=True)

    scan = sub.add_parser("scan", help="Discover and rank candidates (no trades)")
    scan.set_defaults(func=cmd_scan)

    run = sub.add_parser("run", help="Run trading engine cycle/loop")
    run.add_argument("--once", action="store_true", help="Run one cycle and exit")
    run.add_argument("--cycles", type=int, default=None, help="Run this many cycles; omit to run continuously")
    run.set_defaults(func=cmd_run)

    quote = sub.add_parser("quote", help="Probe Jupiter buy/sell route")
    quote.add_argument("--side", choices=["buy", "sell"], required=True)
    quote.add_argument("--mint", required=True)
    quote.add_argument("--size-sol", type=float, default=0.1)
    quote.add_argument("--amount-atomic", type=int, default=0)
    quote.set_defaults(func=cmd_quote)

    doctor = sub.add_parser("doctor", help="Run non-trading health checks")
    doctor.set_defaults(func=cmd_doctor)

    status = sub.add_parser("status", help="Show concise local runtime status")
    status.set_defaults(func=cmd_status)

    cleanup = sub.add_parser("cleanup-dust", help="Cleanup wallet leftovers/dust (one-shot, conservative)")
    cleanup.set_defaults(func=cmd_cleanup_dust)

    audit_be = sub.add_parser(
        "audit-birdeye-once",
        help="Doctor + one discovery cycle with Birdeye HTTP audit and credits delta (diagnostic, no trades)",
    )
    audit_be.set_defaults(func=cmd_audit_birdeye_once)

    dbg = sub.add_parser("debug-env", help="Print interpreter, PATH, and venv detection (diagnostic)")
    dbg.set_defaults(func=cmd_debug_env)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
