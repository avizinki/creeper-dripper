# creeper-dripper handoff

## what the bot does
- Discovers Solana token candidates from Birdeye.
- Probes buy/sell executability via Jupiter.
- Runs a guarded position lifecycle with stop/trailing/time/liquidity exits.
- Persists local state, journal, and cycle status snapshots.

## operator requirements
- Python 3.11+
- Birdeye API key
- Jupiter API key
- Solana wallet keypair file (`SOLANA_KEYPAIR_PATH`) for live execution

## first-run steps
1. `./bootstrap.sh`
2. `source .venv/bin/activate`
3. Edit `.env`:
   - `BIRDEYE_API_KEY`
   - `JUPITER_API_KEY`
   - `SOLANA_KEYPAIR_PATH` (for live mode)
4. `creeper-dripper doctor`
5. `creeper-dripper scan`
6. `creeper-dripper run --once`
7. If healthy, run loop: `creeper-dripper run`

## safe live-trading warning
- Live mode is only when:
  - `DRY_RUN=false`
  - `LIVE_TRADING_ENABLED=true`
- Start with dry-run and `run --once`.
- Do not enable live mode before `doctor` passes and wallet path is validated.

## runtime files
- `runtime/state.json` - current portfolio state
- `runtime/journal.jsonl` - decision and action journal
- `runtime/status.json` - last cycle summary snapshot
- `runtime/archive/` - archived corrupted state files

## safe mode meaning
- Safe mode disables new entries.
- Bot continues minimum-safe management of existing positions.
- Trigger reason is stored in state and visible in `creeper-dripper status`.
