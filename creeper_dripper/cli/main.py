from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time

from creeper_dripper.clients.birdeye import BirdeyeClient
from creeper_dripper.clients.jupiter import JupiterClient
from creeper_dripper.config import SOL_MINT, load_settings
from creeper_dripper.engine.discovery import discover_candidates
from creeper_dripper.engine.trader import CreeperDripper
from creeper_dripper.execution.executor import TradeExecutor
from creeper_dripper.execution.wallet import load_keypair_from_base58
from creeper_dripper.storage.state import load_portfolio
from creeper_dripper.utils import monotonic_sleep_until, setup_logging

STOP = False
LOGGER = logging.getLogger(__name__)


def _handle_sigint(_signum, _frame):
    global STOP
    STOP = True


def build_runtime():
    settings = load_settings()
    setup_logging(settings.log_level)
    owner = load_keypair_from_base58(settings.bs58_private_key)
    birdeye = BirdeyeClient(settings.birdeye_api_key, chain=settings.chain)
    jupiter = JupiterClient(settings.jupiter_api_key)
    executor = TradeExecutor(jupiter, owner, settings)
    portfolio = load_portfolio(settings.state_path, settings.portfolio_start_sol)
    engine = CreeperDripper(settings, birdeye, executor, portfolio)
    return settings, engine, executor, birdeye, owner


def cmd_scan(_args: argparse.Namespace) -> int:
    settings, _engine, executor, birdeye, _owner = build_runtime()
    candidates = discover_candidates(birdeye, executor.jupiter, settings)
    print(json.dumps([candidate.__dict__ for candidate in candidates], indent=2, default=str))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    settings, engine, *_rest = build_runtime()
    signal.signal(signal.SIGINT, _handle_sigint)
    signal.signal(signal.SIGTERM, _handle_sigint)
    next_run = time.monotonic()
    cycles = 0
    while not STOP:
        cycles += 1
        summary = engine.run_cycle()
        print(json.dumps(summary, indent=2, default=str))
        if args.once:
            break
        next_run += settings.poll_interval_seconds
        monotonic_sleep_until(next_run)
    return 0


def cmd_quote(args: argparse.Namespace) -> int:
    settings, _engine, executor, _birdeye, _owner = build_runtime()
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="creeper-dripper")
    sub = parser.add_subparsers(dest="cmd", required=True)

    scan = sub.add_parser("scan", help="Discover and rank candidates")
    scan.set_defaults(func=cmd_scan)

    run = sub.add_parser("run", help="Run trading loop")
    run.add_argument("--once", action="store_true", help="Run a single cycle")
    run.set_defaults(func=cmd_run)

    quote = sub.add_parser("quote", help="Probe Jupiter route")
    quote.add_argument("--side", choices=["buy", "sell"], required=True)
    quote.add_argument("--mint", required=True)
    quote.add_argument("--size-sol", type=float, default=0.1)
    quote.add_argument("--amount-atomic", type=int, default=0)
    quote.set_defaults(func=cmd_quote)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
