# Open tasks (for Claude continuation)

Only items discussed today or listed in today’s backlog doc. Grouped; each entry has priority and status.

## Immediate follow-ups

| Title | Why | Priority | Status | Notes |
|-------|-----|----------|--------|-------|
| Align Hachi dripper tests vs engine | `test_hachi_brain.py` expects `DRIPPER_CHUNK_SELECTED` / execution; engine emits `DRIPPER_WAIT` | now | todo | Decide: update tests to current policy or adjust `_run_hachi_dripper` — see KNOWN_ISSUES |
| Fix per-run logfile creation | `test_setup_logging_creates_per_run_logfile` fails; `setup_logging` does not create `runtime/runs/<rid>/logfile.log` | now | todo | Create parent dirs + file when `run_log_path` set |

## Research ideas

| Title | Why | Priority | Status | Notes |
|-------|-----|----------|--------|-------|
| CU budget guard per cycle | Compare `GET /utils/v1/credits` delta to configurable max; pause or alert | research | todo | From backlog; not implemented |
| Birdeye 400-rate spike alert | Rolling 400/total per endpoint; waste visibility | research | todo | From backlog |

## Later fixes

| Title | Why | Priority | Status | Notes |
|-------|-----|----------|--------|-------|
| Exit-liquidity backlog note cleanup | `docs/backlog` Issue 3 describes pre-skip waste; Solana skip is now implemented | later | todo | Doc hygiene only — behavior already fixed |

## Observability / dashboard

| Title | Why | Priority | Status | Notes |
|-------|-----|----------|--------|-------|
| Optional dashboard polish | Grouped events, cycle snapshot, timeline toggles — improve when prioritized | later | frozen | No changes in this freeze |

## Strategy / discovery / cost

| Title | Why | Priority | Status | Notes |
|-------|-----|----------|--------|-------|
| Further conditional Birdeye ideas | Only if new waste appears; current pattern is disciplined (~131 CU delta in audit) | later | frozen | Do not expand scope without new evidence |
| Clean-wallet validation idea | HANDOFF mentions manual resolution if holdings disagree with state | research | todo | Operational, not coded today |

## Explicitly frozen

- No new discovery architecture, no broad refactors, no opportunistic fixes until the next non-freeze plan.
- Birdeye Solana exit-liquidity skip and conditional security/holder enrichment: **do not revert** without founder/risk review.
