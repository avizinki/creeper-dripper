# creeper-dripper

A production-minded **Birdeye ↔ Jupiter only** Solana trader built for your workflow.

What it does:
- discovers tokens from **Birdeye trending + new listings**
- enriches them with **overview, security, holders, creation info, and exit liquidity**
- verifies both **buy and sell routes** through **Jupiter Swap API v2 Order/Execute**
- opens only small, controlled positions
- manages exits with **take-profit ladder, stop loss, trailing stop, time stop, and liquidity-break exit**
- persists state locally so you can stop/start without losing context

## Important reality check

This repo is designed to be **usable immediately**, but no unattended trading system is magically risk-free.

It includes guardrails on purpose:
- `DRY_RUN=true` by default
- `LIVE_TRADING_ENABLED=false` by default
- size limits and cash reserve
- sell-route probing before entry
- liquidity-break forced exit support

That is deliberate.

## Current architecture

### Discovery
Birdeye endpoints used:
- `/defi/token_trending`
- `/defi/v2/tokens/new_listing`
- `/defi/token_overview`
- `/defi/token_security`
- `/defi/v3/token/holder`
- `/defi/v3/token/exit-liquidity`
- `/defi/token_creation_info`

Birdeye documents those endpoints and their rate limits, including trending, token overview, holders, creation info, OHLCV, and exit-liquidity. citeturn680647search0turn536634view0turn536634view1turn536634view2turn536634view3

### Execution
Jupiter execution uses **Swap API v2** with the recommended **`/order` + `/execute`** flow. Jupiter says this path is the default happy path, requires an API key, returns an assembled transaction from `/order`, and expects the signed transaction plus `requestId` at `/execute`. citeturn599097view0turn898121view0

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
cp .env.example .env
```

Then edit `.env` and set:
- `BIRDEYE_API_KEY`
- `JUPITER_API_KEY`
- `BS58_PRIVATE_KEY`

## First safe run

```bash
creeper-dripper scan
creeper-dripper run --once
```

## Live run

Change in `.env`:

```env
DRY_RUN=false
LIVE_TRADING_ENABLED=true
```

Then:

```bash
creeper-dripper run
```

## Useful commands

Scan candidates:

```bash
creeper-dripper scan
```

Run one cycle:

```bash
creeper-dripper run --once
```

Probe a buy route:

```bash
creeper-dripper quote --side buy --mint <TOKEN_MINT> --size-sol 0.1
```

Probe a sell route:

```bash
creeper-dripper quote --side sell --mint <TOKEN_MINT> --amount-atomic 1000000
```

## Runtime files

- `runtime/state.json` — portfolio state
- `runtime/journal.jsonl` — decision journal

## Strategy defaults

Entry bias:
- fresh enough
- enough 24h volume
- enough visible spot liquidity
- enough **exit liquidity**
- buy/sell flow above threshold
- no mutable/freezable token if blocked in config
- Jupiter buy route exists
- Jupiter sell route exists

Exit bias:
- ladder take-profits
- hard stop
- trailing stop after arming
- time stop for weak laggards
- full exit when exit liquidity collapses under configured ratio

## Production notes

### Why exit liquidity matters
Birdeye’s exit-liquidity endpoint is specifically meant to validate whether there is real on-chain liquidity before user trades. That directly supports the main weakness we identified in the earlier project version: price without executable exit capacity. citeturn536634view0

### Why Jupiter v2 instead of old quote-only paths
Jupiter’s current docs say the Swap API is unified at `api.jup.ag/swap/v2`, that `/order` + `/execute` is the recommended path, and older flows like Ultra are deprecated in favor of Swap API v2. citeturn599097view0turn821747search5turn821747search6

## Limitations you should know

- this is intentionally **Birdeye + Jupiter only**
- it does not use websockets yet
- it does not do wallet reconciliation from chain history yet
- it does not do portfolio hedging or correlated exposure control yet
- it trusts current Birdeye/Jupiter response shapes and handles many variations, but APIs can evolve

## Recommended next hardening steps

- add dedicated healthcheck command
- add max-daily-loss kill switch
- add per-token blacklist / allowlist
- add Telegram or Slack alerts
- add OHLCV-based volatility gating from Birdeye
- add wallet balance sync against chain before each live cycle
