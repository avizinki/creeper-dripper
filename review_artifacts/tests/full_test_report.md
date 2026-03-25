# Full Test Report — Hachi Dripper Decision Layer

**Date:** 2026-03-25
**Base commit:** d277ed0 (feat: replace TP-ladder exit gate with Hachi-style Jupiter-quote dripper)
**This report covers:** All changes staged on top of d277ed0 (hachi brain + model fields + config keys + tests)

---

## Environment

| Item | Value |
|------|-------|
| Python | 3.11.15 |
| pytest | 9.0.2 |
| venv | `.venv/bin/python` / `.venv/bin/pytest` |
| Command | `.venv/bin/pytest -q` |

---

## Summary

| Status | Count |
|--------|-------|
| **Passed** | 140 |
| **Failed** | 13 |
| **Regressions (new failures from this PR)** | **0** |
| **Pre-existing failures (present on d277ed0)** | **13** |

---

## Pre-existing Failures (Category A — present before this PR)

Verified by running the same 13 tests against a `git stash` of our changes (i.e., bare d277ed0).
All 13 failures reproduce identically on the base commit.

| # | Test | File | Root Cause (pre-existing) |
|---|------|------|--------------------------|
| 1 | `test_jupiter_swap_endpoint_returns_transaction` | `test_execution_swap_path.py` | `JupiterClient.swap_transaction()` signature mismatch — test uses `input_mint=` kwarg that no longer exists on the live method |
| 2 | `test_discovery_strict_mode_rejects_when_exit_liquidity_unavailable` | `test_exit_liquidity_fallback.py` | `build_candidate` doesn't reject when `token_exit_liquidity` raises RuntimeError — strict-mode guard missing or bypassed |
| 3 | `test_jsds_missing_entry_baseline_only_streak_hard` | `test_jsds_liquidity.py` | Hachi dripper active in `.env` (`HACHI_DRIPPER_ENABLED=true`) emits `DRIPPER_WAIT` where test expects empty decisions — test predates Hachi and doesn't disable it |
| 4 | `test_jsds_deterioration_watch_logs_only` | `test_jsds_liquidity.py` | Same root cause as #3 |
| 5 | `test_jupiter_client_preserves_400_response_body` | `test_jupiter_probe_classification.py` | `AttributeError` on `JupiterClient` — method or attribute removed/renamed |
| 6 | `test_curl_proofed_success_params_match_app_params` | `test_jupiter_probe_classification.py` | Same `AttributeError` on `JupiterClient` |
| 7–12 | `test_scan_persistence.py` (6 tests) | `test_scan_persistence.py` | `RuntimeError: Configuration validation failed: Live mode requires wallet credentials` — test `_base_env` doesn't set `BS58_PRIVATE_KEY` and `.env` has `LIVE_TRADING_ENABLED=true` |
| 13 | `test_score_candidate_rewards_good_structure` | `test_scoring.py` | `passes_filters()` returns False — filter thresholds in `.env` are stricter than the test's hardcoded values |

---

## Regressions Introduced by This PR

**None.** Zero tests that passed on d277ed0 now fail.

---

## Fixes Applied

None required — no regressions to fix.

The pre-existing failures (A) are not touched; they predate the Hachi dripper work and require independent investigation/fixes unrelated to the brain layer being added here.

---

## New Tests Added by This PR

| File | Tests Added | All Pass? |
|------|------------|-----------|
| `tests/test_hachi_brain.py` | 38 | ✅ Yes |
| `tests/test_hachi_dripper.py` | 18 (updated) | ✅ Yes |
| `tests/test_drip_exit.py` | 6 (`.env`-isolation fix) | ✅ Yes |

Total new/updated tests passing: **62**

---

## Final Result

- **0 regressions** introduced by the hachi brain decision layer.
- **140 tests pass** on the full suite.
- **13 pre-existing failures** are unchanged from the base commit and require separate remediation.
- Repository test health is **not degraded** by this PR.
