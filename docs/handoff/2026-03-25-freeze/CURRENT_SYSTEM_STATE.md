# Current system state (frozen checkpoint)

## Working (as of freeze)
- **Discovery:** Seed fetch (trending + new listings) → volume-sorted seeds → `token_overview` only up to `DISCOVERY_OVERVIEW_LIMIT` → prefilter/score → `token_creation_info` for survivors (age) → Jupiter buy/sell probes → conditional Birdeye `token_security` / `token_holder` when score logic requires them.
- **Solana:** `exit-liquidity` Birdeye call is **not** used (skipped before HTTP); `REQUIRE_BIRDEYE_EXIT_LIQUIDITY` behavior unchanged at the policy level.
- **CLI:** `doctor`, `run`, `audit-birdeye-once`, `debug-env`; **venv guard** active unless `ALLOW_NON_VENV=1`.
- **Observability:** Audit artifacts under `runtime/birdeye_audit.jsonl` and `runtime/birdeye_audit_summary.json` when audit paths are configured for a run.

## Intentionally frozen
- **No new architecture, refactors, or opportunistic fixes** after the two Birdeye-optimization commits referenced in the freeze (overview cap + conditional enrichment).
- Trading logic and exit logic were not redesigned in today’s work; discovery/Birdeye call pattern and guards were the focus.

## Known broken / deferred (not fixed in freeze)
- **Tests:** `tests/test_hachi_brain.py` (multiple failures: dripper expects chunk actions, engine returns wait) and `tests/test_run_id_observability.py` (per-run logfile path not created). See [KNOWN_ISSUES.md](./KNOWN_ISSUES.md).
- **Backlog ideas:** CU budget per cycle, 400-rate spike alert, any extra dashboard polish — listed in [OPEN_TASKS.md](./OPEN_TASKS.md) and `docs/backlog/birdeye-and-runtime-followups.md`.

## Operator note
- Always run the CLI from the project **`.venv`** (see README / doctor banner). Wrong interpreter breaks dependency assumptions and makes CU audit comparisons meaningless.
