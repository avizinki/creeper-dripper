# creeper-dripper

Production-minded Solana momentum trader with:
- Jupiter-first execution and valuation truth
- budget-aware discovery/enrichment (degrades safely when constrained)
- wallet snapshot + reconciliation for deployable/safety truth
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
  - Jupiter (execution + sell-quote valuation truth)
  - Birdeye (optional discovery/enrichment signal layer; budget-aware and degradable)
  - Solana RPC (tx lifecycle only: broadcast + signature status; not wallet-balance settlement truth)

## Truth model (Jupiter-first)

- **Execution/settlement quantities**: Jupiter `/execute` response, with controlled fallbacks (order in/out metadata, then requested qty as last resort)
- **Valuation/sellability**: Jupiter sell-quote path is the primary truth for route/impact-driven exit behavior
- **Tx confirmation**: Solana RPC `getSignatureStatuses`
- **Wallet snapshot + reconciliation**: used for deployable capital truth and accounting safety checks
- **Wallet balances are not execution truth**: “dirty wallet” situations still require operator action (flatten wallet / import+reconcile holdings)

## Dashboard of Truth

`/truth` exposes operator state in one payload:
- accounting/capital truth (cash, deployable, reconciliation, drift warning)
- policy posture and entry gate status
- zombie pressure (recoverable vs dead/stuck exposure)
- discovery health (mode, failures, route/probe counters)
- API/budget state (Birdeye budget mode + reason summary)

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

Always run the CLI with the project interpreter so dependencies and behavior match this repo (Birdeye/Jupiter accounting, etc.):

```bash
.venv/bin/creeper-dripper doctor
# or: .venv/bin/python -m creeper_dripper.cli.main doctor
```

The CLI exits with an error if `sys.executable` is not the project `.venv` (unless `ALLOW_NON_VENV=1` is set for debugging only).

Then edit `.env`:
- set `BIRDEYE_API_KEY`
- set `JUPITER_API_KEY`
- set `SOLANA_KEYPAIR_PATH` (for live mode)

## Minimal operator config (preferred)

The project now prefers a **minimal operator `.env`**:
- **Secrets**: `BIRDEYE_API_KEY`, `JUPITER_API_KEY`
- **Wallet**: `SOLANA_KEYPAIR_PATH`
- **Mode**: `DRY_RUN`, `LIVE_TRADING_ENABLED`, `CHAIN`
- **Intent**: `BASE_POSITION_SIZE_SOL`, optional `RISK_MODE` (`conservative|balanced|aggressive`)

Most tuning is derived at runtime via a derived policy layer (wallet snapshot, deployable, zombie pressure, accounting safety),
while still supporting legacy env vars as optional overrides for backward compatibility.

## Derived Runtime Policy

The bot’s live behavior is primarily governed by a **derived runtime policy** computed each cycle from:
- wallet snapshot + deployable capital
- accounting safety/drift state
- zombie / FINAL_ZOMBIE pressure

It automatically adjusts:
- **effective position size**
- **effective entry caps (open slots, daily new entries)**
- **effective entry thresholds** (score/liquidity/buy-sell ratio)
- **discovery cadence** (slows down when constrained/recovery-only)

### Liquidity-aware entry gating (T-020)

Entries are gated using:
- **age-banded liquidity floors**: very young tokens can pass lower liquidity only with stronger route survivability;
  older tokens require materially higher liquidity.
- **route survivability checks** (cheap + controlled): 1–2 sell-quote size buckets, rejecting fragile routes.

These decisions are surfaced in runtime artifacts (`runtime/status.json`) under `derived_policy` and in
entry decision metadata for operator debugging.

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

See `.env.example` for the **minimal operator-level** environment variables (preferred). Legacy env vars are still supported as optional overrides.

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
