# creeper-dripper

Production-minded Solana momentum trader with:
- discovery from Birdeye
- execution through Jupiter Swap v2
- local file-based state, journal, and status snapshots
- safety rails and structured observability

## Architecture (short)

- `scan` discovers and scores candidates (no trading).
- `run` executes cycle logic: recovery -> discovery -> exits -> entries -> persistence.
- State and journal are file-based:
  - `runtime/state.json`
  - `runtime/journal.jsonl`
  - `runtime/status.json`
- Providers are fixed:
  - Birdeye
  - Jupiter
  - Solana RPC (wallet and tx/balance truth only)

## Requirements

- Python `>=3.11`
- API keys:
  - Birdeye
  - Jupiter
- Solana wallet JSON file (64-byte keypair array)

## Setup

```bash
./bootstrap.sh
```

Manual equivalent:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
pip install -e .
cp .env.example .env
```

Then edit `.env`:
- set `BIRDEYE_API_KEY`
- set `JUPITER_API_KEY`
- set `SOLANA_KEYPAIR_PATH` (for live mode)

## Wallet file format

`SOLANA_KEYPAIR_PATH` points to a JSON file containing exactly 64 integers in `[0,255]`, for example:

```json
[12,34,56,78,90,123,45,67,89,10,11,12,13,14,15,16,17,18,19,20,21,22,23,24,25,26,27,28,29,30,31,32,33,44,55,66,77,88,99,100,101,102,103,104,105,106,107,108,109,110,111,112,113,114,115,116,117,118,119,120,121,122,123,124]
```

## Commands

- `creeper-dripper doctor`  
  Non-trading health checks: config/env, mode flags (`DRY_RUN`, `LIVE_TRADING_ENABLED`), wallet path, runtime write access, Birdeye auth, Jupiter reachability, and safe-mode state.

- `creeper-dripper status`  
  Local runtime summary: mode flags, open/partial/pending/blocked/closed counts, cash, safe mode, last cycle, failure counters, mode-skip counters, daily counters.

- `creeper-dripper scan`  
  Discover and rank candidates, no trades.

- `creeper-dripper run --once`  
  Single full cycle.
  Prints effective mode and explicitly reports when entry execution is skipped by mode.

- `creeper-dripper run`  
  Continuous loop.

- `creeper-dripper quote --side buy|sell ...`  
  Route probe only.

## Dry-run vs live mode

- Default is safe:
  - `DRY_RUN=true`
  - `LIVE_TRADING_ENABLED=false`
- Live trading requires both:
  - `DRY_RUN=false`
  - `LIVE_TRADING_ENABLED=true`
  - valid wallet credentials (`SOLANA_KEYPAIR_PATH` preferred)

## Safe mode behavior

When a safety rail triggers, bot enters safe mode:
- stops opening new entries
- continues minimum position management/exits
- emits structured `safety_stop` with exact reason
- reason is visible in `status` and `runtime/status.json`

## Environment variables

See `.env.example` for full list and defaults.

Required:
- `BIRDEYE_API_KEY`
- `JUPITER_API_KEY`

Wallet:
- `SOLANA_KEYPAIR_PATH` (preferred)
- `BS58_PRIVATE_KEY` (deprecated fallback)

Core runtime:
- `RUNTIME_DIR`, `STATE_PATH`, `JOURNAL_PATH`, `POLL_INTERVAL_SECONDS`

Discovery filters:
- `DISCOVERY_LIMIT`, `DISCOVERY_MAX_CANDIDATES`, `MIN_LIQUIDITY_USD`, `MIN_EXIT_LIQUIDITY_USD`, `MIN_VOLUME_24H_USD`, `MIN_BUY_SELL_RATIO`, `MIN_DISCOVERY_SCORE`, `MAX_TOKEN_AGE_HOURS`, `BLOCK_MUTABLE_MINT`, `BLOCK_FREEZABLE`, `REQUIRE_JUP_SELL_ROUTE`

Sizing/execution:
- `PORTFOLIO_START_SOL`, `MAX_OPEN_POSITIONS`, `BASE_POSITION_SIZE_SOL`, `MAX_POSITION_SIZE_SOL`, `CASH_RESERVE_SOL`, `MIN_ORDER_SIZE_SOL`, `MAX_DAILY_NEW_POSITIONS`, `COOLDOWN_MINUTES_AFTER_EXIT`, `DEFAULT_SLIPPAGE_BPS`, `MAX_ACCEPTABLE_PRICE_IMPACT_BPS`

Risk/ladder:
- `STOP_LOSS_PCT`, `TRAILING_STOP_PCT`, `TRAILING_ARM_PCT`, `TIME_STOP_MINUTES`, `TAKE_PROFIT_LEVELS_PCT`, `TAKE_PROFIT_FRACTIONS`, `FORCE_FULL_EXIT_ON_LIQUIDITY_BREAK`, `LIQUIDITY_BREAK_RATIO`, `EXIT_PROBE_FRACTIONS`

Drip exit (optional chunked sells):
- `DRIP_EXIT_ENABLED`, `DRIP_CHUNK_PCTS`, `DRIP_NEAR_EQUAL_BAND`, `DRIP_MIN_CHUNK_WAIT_SECONDS` (see `.env.example`)

Safety rails:
- `DAILY_REALIZED_LOSS_CAP_SOL`
- `MAX_CONSECUTIVE_EXECUTION_FAILURES`
- `STALE_MARKET_DATA_MINUTES`
- `UNKNOWN_EXIT_SATURATION_LIMIT`
- `MAX_EXIT_BLOCKED_POSITIONS`

## Recommended first run

```bash
source .venv/bin/activate
creeper-dripper doctor
creeper-dripper scan
creeper-dripper run --once
```

## Troubleshooting

- Missing keys:
  - run `creeper-dripper doctor`
  - ensure `BIRDEYE_API_KEY` and `JUPITER_API_KEY` are set

- Wallet path invalid:
  - verify `SOLANA_KEYPAIR_PATH` exists/readable and JSON has 64 ints

- Birdeye 401:
  - API key invalid/expired or wrong account scope

- No candidates:
  - check doctor output
  - relax strict discovery filters in `.env`
  - inspect `runtime/status.json` rejection counts

- Safe mode triggered:
  - run `creeper-dripper status`
  - inspect `safety_stop_reason` and failure counters

- State recovery behavior:
  - corrupted `state.json` is archived under `runtime/archive/`
  - fresh safe portfolio is initialized automatically
