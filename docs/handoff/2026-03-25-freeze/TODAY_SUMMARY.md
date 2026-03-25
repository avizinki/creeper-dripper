# Today’s summary (2026-03-25)

Grouped by major thread. Outcomes and final state only.

## Birdeye audit and CU diagnostics
- Added request audit (jsonl + in-memory stats), one-shot credits measurement, CLI `audit-birdeye-once`, summary JSON with per-endpoint counts and CU delta.
- **Outcome:** Reproducible visibility into which endpoints fire, 200/400 split, and `delta_usage_api` per controlled run.

## Venv enforcement
- CLI fails fast unless running under the project `.venv` (uses `sys.prefix`, not fragile `sys.executable` symlinks). Override: `ALLOW_NON_VENV=1` for debugging.
- **Outcome:** Doctor/run show interpreter context; tests use autouse `ALLOW_NON_VENV` where needed.

## Exit-liquidity on Solana
- Direct curl + audit showed `400` / “Chain solana not supported” for `/defi/v3/token/exit-liquidity` while `token_overview` succeeded for the same mint.
- **Decision:** Skip that endpoint entirely on the Solana path before any HTTP; mark metadata/reason; non-retryable 400 class in client to avoid retry storms.

## Discovery pipeline and CU reduction
- Refactor: light build (`token_overview`) → capped overview stage (`DISCOVERY_OVERVIEW_LIMIT`) → heavy age (`token_creation_info`) for survivors → route probes.
- Further: conditional `token_security` / `token_holder` only when score bounds show they can affect acceptance vs `min_discovery_score` (flags `needs_security_check`, `needs_holder_check` on `TokenCandidate`).
- **CU progression observed (approximate, same audit command / config over time):** ~8581 → ~5011 → ~601 → **~131** API credits delta for a representative cycle.
- **Verdict:** Birdeye usage is now disciplined; endpoint pattern is overview + creation for vetted seeds, security/holder only when the score gate requires them.

## Tests and backlog
- Full suite still has **5 failing tests** (Hachi dripper expectations + per-run logfile); treated as pre-existing / deferred for this freeze.
- `docs/backlog/birdeye-and-runtime-followups.md` captures follow-up issues (Hachi tests, logging, exit-liquidity note superseded by skip, CU budget guard, 400-rate alert).

## Final state
- Repo frozen after: (1) backlog artifact commit, (2) this handoff pack commit. **No further feature work** in this freeze window.
