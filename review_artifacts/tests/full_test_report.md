# Full Test Report — Hachi Dripper Decision Layer (Stabilized)

**Date:** 2026-03-25
**Branch:** live-hachi-stabilize
**Base branch:** live-jsds-test @ 3aaec46
**Command:** `.venv/bin/pytest -q`

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
| **Passed** | 153 |
| **Failed** | 0 |
| **Skipped** | 0 |
| **Regressions** | 0 |
| **Pre-existing failures fixed** | 13 |

**Full suite: 153 passed, 0 failed.**

---

## Fixes Applied

### A — JupiterClient signature drift (3 tests)

| Test | File | Fix |
|------|------|-----|
| `test_jupiter_swap_endpoint_returns_transaction` | `test_execution_swap_path.py` | Updated call from old positional mint/amount params to current `swap_transaction(quote_response=..., user_public_key=...)` signature |
| `test_jupiter_client_preserves_400_response_body` | `test_jupiter_probe_classification.py` | Changed `client.order(...)` to `client.execution_order_v2(..., taker="fake_taker")` — the current method name for `/order` endpoint |
| `test_curl_proofed_success_params_match_app_params` | `test_jupiter_probe_classification.py` | Changed `JupiterClient.build_order_params(...)` to `JupiterClient.build_quote_params(...)` — the current static method name |

No runtime behavior changed — tests now call the live API surface correctly.

### B — JSDS tests conflicting with Hachi (2 tests)

| Test | Fix |
|------|-----|
| `test_jsds_missing_entry_baseline_only_streak_hard` | Added `settings.hachi_dripper_enabled = False` in `_settings` helper |
| `test_jsds_deterioration_watch_logs_only` | Same fix (shared `_settings` helper) |

Root cause: `.env` file has `HACHI_DRIPPER_ENABLED=true`; `load_dotenv(override=True)` inside `load_settings()` overwrote the test's monkeypatched value. Force-assigning `settings.hachi_dripper_enabled = False` after `load_settings()` closes the gap. JSDS tests are about JSDS-specific logic and must run with Hachi disabled.

### C — test_scan_persistence env pollution (6 tests)

| Fix | Detail |
|-----|--------|
| Added `monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")` and `monkeypatch.setenv("DRY_RUN", "true")` to `_base_env` | `load_dotenv(override=True)` in earlier test runs leaked `LIVE_TRADING_ENABLED=true` into `os.environ`. `monkeypatch.chdir(tmp_path)` prevented `.env` re-load but didn't undo the leaked value. The tests now explicitly declare their live-mode stance. |

### D — scoring test env mismatch (1 test)

| Fix | Detail |
|-----|--------|
| Added `sell_route_available=True` to the `TokenCandidate` in `test_score_candidate_rewards_good_structure` | The `.env` has `REQUIRE_JUP_SELL_ROUTE=true`. The test candidate is meant to represent a "good structure" token that should pass all filters — providing a sell route is part of that contract. `sell_route_available` defaults to `False` in `TokenCandidate`, so it was silently failing the route filter. |

### E — build_candidate exit liquidity RuntimeError mismatch (1 test)

| Fix | Detail |
|-----|--------|
| Added `settings.require_birdeye_exit_liquidity = require_exit` force-assign in `_settings` helper of `test_exit_liquidity_fallback.py` | Same `load_dotenv(override=True)` stomping issue. `.env` has `REQUIRE_BIRDEYE_EXIT_LIQUIDITY=false`, which overwrote the test's monkeypatched `true`. Force-assigning after `load_settings()` ensures strict mode tests actually run in strict mode. |

---

## Remaining Failures

**None.** All 153 tests pass.

---

## Files Changed

| File | Change type |
|------|------------|
| `tests/test_jsds_liquidity.py` | Added `settings.hachi_dripper_enabled = False` to `_settings` helper |
| `tests/test_scan_persistence.py` | Added `LIVE_TRADING_ENABLED=false` and `DRY_RUN=true` to `_base_env` |
| `tests/test_scoring.py` | Added `sell_route_available=True` to good-structure candidate |
| `tests/test_exit_liquidity_fallback.py` | Force-assign `settings.require_birdeye_exit_liquidity = require_exit` after `load_settings()` |
| `tests/test_execution_swap_path.py` | Updated `swap_transaction` call to current signature |
| `tests/test_jupiter_probe_classification.py` | Updated `client.order` → `client.execution_order_v2` and `build_order_params` → `build_quote_params` |
| `review_artifacts/tests/full_test_report.md` | This file |
| `review_artifacts/tests/full_test_report.json` | Updated JSON version |
