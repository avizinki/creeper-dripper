# Known issues (pre-existing / deferred)

These were **not fixed** in the 2026-03-25 freeze unless explicitly noted.

## Test: `tests/test_hachi_brain.py`
- **Symptoms:** Assertions expect `DRIPPER_CHUNK_SELECTED`, `DRIPPER_CHUNK_EXECUTED`, or selling in neutral zone; observed actions include `DRIPPER_WAIT` only in failing cases.
- **Likely cause:** Test expectations vs current `_run_hachi_dripper` policy (chunk selection / urgency gating) diverged.
- **Status:** Pre-existing relative to today’s Birdeye/venv work; **intentionally not fixed** in freeze.

## Test: `tests/test_run_id_observability.py` — `test_setup_logging_creates_per_run_logfile`
- **Symptoms:** `run_log.exists()` is False after `setup_logging(..., run_log_path=...)`.
- **Likely cause:** Logger setup does not create the run log file or parent directories for the given path.
- **Status:** Pre-existing; **intentionally not fixed** in freeze.

## Backlog doc staleness
- `docs/backlog/birdeye-and-runtime-followups.md` Issue 3 describes exit-liquidity 400 storm; **Solana skip is already implemented** — treat Issue 3 as historical / doc cleanup, not an open bug.

## Full suite count (reference)
- Last full run during this effort: **202 passed, 5 failed** (the failures above plus related Hachi cases).
