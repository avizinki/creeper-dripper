# Backlog (follow-ups from Birdeye audit validation)

GitHub-style issues for tracking. Created after `audit-birdeye-once` strict validation (2026-03-25).

---

## Issue 1: Fix Hachi dripper test mismatch (DRIPPER_WAIT vs DRIPPER_CHUNK_SELECTED)

**Type:** bug / tests  
**Summary:** `tests/test_hachi_brain.py` expects dripper chunk selection/execution; engine returns `DRIPPER_WAIT` only.  
**Scope:** Align `CreeperDripper._run_hachi_dripper` behavior with tests or update tests to match current policy.

---

## Issue 2: Fix per-run logfile creation (`test_setup_logging_creates_per_run_logfile`)

**Type:** bug  
**Summary:** `setup_logging` does not create `runtime/runs/<rid>/logfile.log` when passed that path.  
**Scope:** Ensure file (and parent dirs) exist when logging is configured for a run.

---

## Issue 3: Minimize Birdeye `/defi/v3/token/exit-liquidity` usage (borderline: **FAIL** in audit)

**Type:** performance / cost  
**Summary:** Audit showed 120 HTTP calls to exit-liquidity in one discovery cycle, **all 400**, body `Chain solana not supported`. Retries (3×) multiply waste.  
**Scope:** Skip this endpoint when `birdeye_exit_liquidity_supported` is already false for the account/chain, or short-circuit after first 400 without retries for known unsupported-chain messages.

---

## Issue 4: Add CU budget guard per discovery/trading cycle

**Type:** feature / risk  
**Summary:** Compare `GET /utils/v1/credits` delta to a configurable max; pause or alert when exceeded.

---

## Issue 5: Alert when Birdeye HTTP 400 rate spikes

**Type:** observability  
**Summary:** Track rolling 400/total per endpoint; log `SELL_BLOCKED_REASON`-style tag for API waste; optional dashboard alert.
