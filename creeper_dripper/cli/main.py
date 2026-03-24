from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from dataclasses import asdict

from creeper_dripper.clients.birdeye import BirdeyeClient
from creeper_dripper.clients.jupiter import JupiterClient
from creeper_dripper.config import SOL_MINT, USDC_MINT, load_settings
from creeper_dripper.engine.discovery import discover_candidates, serialize_candidates
from creeper_dripper.engine.trader import CreeperDripper
from creeper_dripper.execution.executor import TradeExecutor
from creeper_dripper.execution.wallet import load_keypair_from_base58, load_keypair_from_file
from creeper_dripper.storage.state import load_portfolio
from creeper_dripper.utils import atomic_write_json, monotonic_sleep_until, setup_logging

STOP = False
LOGGER = logging.getLogger(__name__)


def _candidate_score(candidate) -> float:
    if isinstance(candidate, dict):
        return float(candidate.get("discovery_score") or 0.0)
    return float(getattr(candidate, "discovery_score", 0.0) or 0.0)


def _handle_sigint(_signum, _frame):
    global STOP
    STOP = True


def _load_owner_if_configured(settings):
    if settings.solana_keypair_path:
        LOGGER.info("Loading Solana keypair from path: %s", settings.solana_keypair_path)
        return load_keypair_from_file(settings.solana_keypair_path)
    if settings.bs58_private_key:
        LOGGER.warning("BS58_PRIVATE_KEY is deprecated, use SOLANA_KEYPAIR_PATH")
        return load_keypair_from_base58(settings.bs58_private_key)
    return None


def build_runtime(*, require_owner: bool, load_owner: bool):
    settings = load_settings()
    setup_logging(settings.log_level)
    owner = _load_owner_if_configured(settings) if load_owner else None
    if require_owner and owner is None:
        raise RuntimeError("Missing wallet credentials: set SOLANA_KEYPAIR_PATH (preferred) or BS58_PRIVATE_KEY")
    birdeye = BirdeyeClient(settings.birdeye_api_key, chain=settings.chain)
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
    settings = load_settings()
    require_owner = settings.live_trading_enabled and not settings.dry_run
    _settings, engine, *_rest = build_runtime(require_owner=require_owner, load_owner=require_owner)
    print(
        json.dumps(
            {
                "mode": {
                    "dry_run": settings.dry_run,
                    "live_trading_enabled": settings.live_trading_enabled,
                }
            },
            indent=2,
            default=str,
        )
    )
    recovery = engine.run_startup_recovery()
    if recovery:
        print(json.dumps({"startup_recovery": [asdict(d) for d in recovery]}, indent=2, default=str))
    signal.signal(signal.SIGINT, _handle_sigint)
    signal.signal(signal.SIGTERM, _handle_sigint)
    next_run = time.monotonic()
    cycles = 0
    while not STOP:
        cycles += 1
        summary = engine.run_cycle()
        print(json.dumps(summary, indent=2, default=str))
        s = summary.get("summary", {})
        if (s.get("entries_skipped_dry_run", 0) or 0) > 0 or (s.get("entries_skipped_live_disabled", 0) or 0) > 0:
            print(
                "entry execution skipped by mode: "
                f"dry_run={s.get('entries_skipped_dry_run', 0)}, "
                f"live_disabled={s.get('entries_skipped_live_disabled', 0)}"
            )
        if args.once:
            break
        next_run += settings.poll_interval_seconds
        monotonic_sleep_until(next_run)
    return 0


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
    checks: list[dict] = []
    ok = True
    settings = None
    try:
        settings = load_settings()
        checks.append({"check": "config_load", "ok": True})
        checks.append(
            {
                "check": "mode_flags",
                "ok": True,
                "dry_run": settings.dry_run,
                "live_trading_enabled": settings.live_trading_enabled,
            }
        )
    except Exception as exc:
        checks.append({"check": "config_load", "ok": False, "error": str(exc)})
        print(json.dumps({"ok": False, "checks": checks}, indent=2, default=str))
        return 1

    if settings.solana_keypair_path:
        wallet_ok = settings.solana_keypair_path.exists() and settings.solana_keypair_path.is_file() and os.access(settings.solana_keypair_path, os.R_OK)
        checks.append({"check": "wallet_path", "ok": wallet_ok, "path": str(settings.solana_keypair_path)})
        ok = ok and wallet_ok
    else:
        checks.append({"check": "wallet_path", "ok": True, "note": "not configured (allowed for doctor/scan/quote)"})

    runtime_ok = os.access(settings.runtime_dir, os.W_OK)
    checks.append({"check": "runtime_dir_writable", "ok": runtime_ok, "path": str(settings.runtime_dir)})
    ok = ok and runtime_ok

    birdeye = BirdeyeClient(settings.birdeye_api_key, chain=settings.chain)
    try:
        birdeye.trending_tokens(limit=1)
        checks.append({"check": "birdeye_auth", "ok": True})
    except Exception as exc:
        checks.append({"check": "birdeye_auth", "ok": False, "error": str(exc)})
        ok = False

    jupiter = JupiterClient(settings.jupiter_api_key)
    try:
        jupiter.probe_quote(input_mint=SOL_MINT, output_mint=USDC_MINT, amount_atomic=1_000_000, slippage_bps=settings.default_slippage_bps)
        checks.append({"check": "jupiter_reachable", "ok": True})
    except Exception as exc:
        checks.append({"check": "jupiter_reachable", "ok": False, "error": str(exc)})
        ok = False

    try:
        portfolio = load_portfolio(settings.state_path, settings.portfolio_start_sol)
        checks.append(
            {
                "check": "safe_mode_state",
                "ok": True,
                "safe_mode_active": portfolio.safe_mode_active,
                "safety_stop_reason": portfolio.safety_stop_reason,
            }
        )
    except Exception as exc:
        checks.append({"check": "safe_mode_state", "ok": False, "error": str(exc)})
        ok = False

    print(json.dumps({"ok": ok, "checks": checks}, indent=2, default=str))
    return 0 if ok else 1


def cmd_status(_args: argparse.Namespace) -> int:
    settings = load_settings()
    portfolio = load_portfolio(settings.state_path, settings.portfolio_start_sol)
    open_positions = list(portfolio.open_positions.values())
    summary = {
        "dry_run": settings.dry_run,
        "live_trading_enabled": settings.live_trading_enabled,
        "open_positions": len(open_positions),
        "partial_positions": sum(1 for p in open_positions if p.status == "PARTIAL"),
        "exit_pending_positions": sum(1 for p in open_positions if p.status == "EXIT_PENDING"),
        "exit_blocked_positions": sum(1 for p in open_positions if p.status == "EXIT_BLOCKED"),
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="creeper-dripper")
    sub = parser.add_subparsers(dest="cmd", required=True)

    scan = sub.add_parser("scan", help="Discover and rank candidates (no trades)")
    scan.set_defaults(func=cmd_scan)

    run = sub.add_parser("run", help="Run trading engine cycle/loop")
    run.add_argument("--once", action="store_true", help="Run one cycle and exit")
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

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
