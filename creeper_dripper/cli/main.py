from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import shutil
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

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
    setup_logging(settings.log_level, runtime_dir=settings.runtime_dir)
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
    run_dir = _cycle_run_dir(settings.runtime_dir)
    observed_cycles: list[dict] = []
    seen_artifacts: set[str] = set()
    token_tracking: dict[str, dict] = {}
    notable_events: list[dict] = []
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
    next_run = time.monotonic()
    cycles = 0
    requested_cycles = 1 if args.once else (max(1, int(args.cycles)) if args.cycles is not None else None)
    prev_cache_debug_keys: list[str] = []
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
            settings.runtime_dir,
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
            break
        next_run += settings.poll_interval_seconds
        monotonic_sleep_until(next_run)

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
        checks.append({"check": "jupiter_probe_reachable_v1_quote", "ok": True, "endpoint": "GET /swap/v1/quote"})
    except Exception as exc:
        checks.append({"check": "jupiter_probe_reachable_v1_quote", "ok": False, "endpoint": "GET /swap/v1/quote", "error": str(exc)})
        ok = False

    try:
        jupiter.check_swap_reachability()
        checks.append({"check": "jupiter_execution_reachable_v1_swap", "ok": True, "endpoint": "POST /swap/v1/swap"})
    except Exception as exc:
        checks.append({"check": "jupiter_execution_reachable_v1_swap", "ok": False, "endpoint": "POST /swap/v1/swap", "error": str(exc)})
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
    blocked_positions = [p for p in open_positions if p.status == "EXIT_BLOCKED"]
    summary = {
        "dry_run": settings.dry_run,
        "live_trading_enabled": settings.live_trading_enabled,
        "open_positions": len(open_positions),
        "partial_positions": sum(1 for p in open_positions if p.status == "PARTIAL"),
        "exit_pending_positions": sum(1 for p in open_positions if p.status == "EXIT_PENDING"),
        "exit_blocked_positions": sum(1 for p in open_positions if p.status == "EXIT_BLOCKED"),
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


def main(argv: list[str] | None = None) -> int:
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

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
