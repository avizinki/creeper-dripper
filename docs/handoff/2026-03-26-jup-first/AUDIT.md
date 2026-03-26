# JUP-FIRST Architecture Audit
**Date:** 2026-03-26
**Task:** JUP-FIRST-ARCH — audit, refactor plan, implementation

---

## Phase A — Birdeye / Non-Jupiter Usage Audit

Everything validated from code. No assumptions.

---

### 1. Discovery — Birdeye seed fetch
**File:** `engine/discovery.py` → called from `clients/birdeye.py`
**Calls:** `trending_tokens()` → `/defi/token_trending` and `new_listings()` → `/defi/v2/tokens/new_listing`
**Classification:** REQUIRED (for now) — only current source of token discovery seeds
**JUP-first impact:** Seeds are not used for valuation; they are just mint addresses to feed into Jupiter probes.
**Failure mode:** Discovery already has a try/except per source — if both fail, `seeds = []`, loop doesn't execute, `_last_discovery_candidates` is returned from cache.
**Risk:** If Birdeye seed call fails mid-loop, currently the cycle returns zero candidates. The TTL-cached candidates from the prior cycle (`_last_discovery_candidates`) ARE returned when the interval hasn't elapsed, but NOT when the interval has elapsed and seeds fail.
**Gap:** When discovery is triggered (interval elapsed) and both seed calls fail, the system returns `[]` candidates — equivalent to "blind cycle". This is already handled gracefully (no crash), but is not surfaced as `discovery_mode: degraded`.

---

### 2. Discovery — token_overview (per seed)
**File:** `clients/birdeye.py` → `build_candidate_light()` → `token_overview()`
**Call:** `/defi/token_overview` — 1 per seed, capped at `DISCOVERY_OVERVIEW_LIMIT`
**Classification:** REQUIRED for scoring — provides price, liquidity, volume, holder_count
**JUP-first impact:** Price/liquidity from overview are for scoring/filtering ONLY. Valuation is always Jupiter sell quote (`resolve_position_valuation`). This is already correct — Birdeye overview is an optional signal, not truth.
**Risk:** If overview fails, `build_candidate_light` raises → caught by outer try/except in `discover_candidates` → `REJECT_CANDIDATE_BUILD_FAILED`. Already graceful.

---

### 3. Discovery — token_creation_info (per accepted seed)
**File:** `clients/birdeye.py` → `enrich_candidate_heavy()` → `token_creation_info()`
**Call:** `/defi/token_creation_info` — only for seeds that pass prefilter
**Classification:** OPTIONAL — age gating. Age is a scoring factor and hard-cap filter.
**JUP-first impact:** If skipped, `age_hours = None`. Score still computes; age checks return None-safe defaults.
**Risk:** `should_skip_endpoint` already gates this in starved/constrained mode. Safe to skip.

---

### 4. Discovery — token_security (conditional)
**File:** `clients/birdeye.py` → `enrich_candidate_security_only()`
**Call:** `/defi/token_security` — only when score gate is reachable AND not skipped by budget
**Classification:** OPTIONAL — `BLOCK_MUTABLE_MINT` / `BLOCK_FREEZABLE` filter. If skipped, security fields default to `None` → treated as "unknown" (neither blocking nor approving).
**JUP-first impact:** None — security check is anti-rug, not valuation.
**Risk:** Safe to skip.

---

### 5. Discovery — token_holders (conditional)
**File:** `clients/birdeye.py` → `enrich_candidate_holders_only()`
**Call:** `/defi/v3/token/holder` — only when worst-case score can still swing acceptance
**Classification:** OPTIONAL — holder concentration scoring. If skipped, `top10_holder_percent` stays at pessimistic 100.0.
**JUP-first impact:** None.
**Risk:** Safe to skip.

---

### 6. Discovery — exit-liquidity (SKIPPED on Solana)
**File:** `clients/birdeye.py` → `build_candidate_light()`
**Status:** Already permanently skipped for Solana. `exit_liquidity_usd = None` for all Solana tokens.
**Classification:** NOT APPLICABLE (dead code path for Solana).

---

### 7. CRITICAL: _mark_positions — birdeye.build_candidate() for open positions
**File:** `engine/trader.py` line 868
**Code:**
```python
candidate = self.birdeye.build_candidate(seed)
```
**When triggered:** Every cycle, for each open position whose mint is NOT in the current `candidates` list.
**What it calls:** `build_candidate()` → `build_candidate_light()` → `token_overview()` (1 CU per open position)
**What the result is used for:**
  - `position.last_exit_liquidity_usd = candidate.exit_liquidity_usd` → **always `None` on Solana** (exit-liquidity skipped)
  - Nothing else in that code path reads `candidate.price_usd` or other Birdeye fields for valuation
  - Actual valuation uses `resolve_position_valuation()` (Jupiter sell quote) — fully independent
**Classification:** WRONG — Birdeye call whose only used output (`exit_liquidity_usd`) is always `None` on Solana. This call costs CU every cycle for every open position not currently in the candidate list.
**Fix:** Replace `birdeye.build_candidate(seed)` with `TokenCandidate(address=mint, symbol=position.symbol, decimals=position.decimals)` directly. The fallback is already written below this call. The Birdeye call is wasteful and produces no actionable data on Solana.

---

### 8. Position valuation — Jupiter-only (CORRECT)
**File:** `engine/position_pricing.py` → `resolve_position_valuation()`
**Source:** `executor.quote_sell()` → Jupiter `/quote`
**Classification:** REQUIRED — this is already JUP-first and correct.
**Note:** Comment in file explicitly says "No wallet RPC, no Birdeye, no held/USD fallbacks."

---

### 9. Entry gating — Jupiter buy+sell probes (CORRECT)
**File:** `engine/discovery.py` → `jupiter.probe_quote()` (buy + sell)
**Classification:** REQUIRED — this is the JUP-first entry gate. Already correct.

---

### 10. Dashboard — Birdeye budget mode derivation
**File:** `dashboard/app.py` → `_derive_birdeye_budget_mode()`
**Source:** Log scan + audit file — no live Birdeye calls.
**Classification:** REQUIRED for observability — zero CU cost.

---

### 11. Discovery cadence — adaptive slowdown
**File:** `clients/birdeye.py` → `adjusted_discovery_limits()` + `adjusted_discovery_interval_seconds()`
**Classification:** REQUIRED — budget-aware throttle. Already works. No CU cost itself.

---

### Summary table

| # | Location | Call/Usage | Classification | CU Impact | Action |
|---|----------|-----------|---------------|-----------|--------|
| 1 | discovery.py | trending_tokens + new_listings | REQUIRED | medium | Wrap with cached_fallback |
| 2 | discovery.py | token_overview per seed | REQUIRED | medium | Existing cap OK |
| 3 | discovery.py | token_creation_info | OPTIONAL | low | Existing skip OK |
| 4 | discovery.py | token_security | OPTIONAL | low | Existing skip OK |
| 5 | discovery.py | token_holders | OPTIONAL | low | Existing skip OK |
| 6 | discovery.py | exit-liquidity | NOT APPLICABLE | 0 | Already skipped |
| 7 | **trader.py:868** | **build_candidate for open positions** | **WRONG** | **1 CU/open position/cycle** | **REMOVE** |
| 8 | position_pricing.py | Jupiter sell quote (valuation) | REQUIRED | 0 Birdeye | Correct, keep |
| 9 | discovery.py | Jupiter buy+sell probe | REQUIRED | 0 Birdeye | Correct, keep |
| 10 | dashboard/app.py | log scan for budget mode | REQUIRED | 0 | Correct, keep |
| 11 | birdeye.py | adaptive limits | REQUIRED | 0 | Correct, keep |

---

## Phase B — JUP-first Refactor Plan

### B1. Fix WRONG usage: remove birdeye.build_candidate() in _mark_positions (trader.py:868)

**Change:** Remove the `try: candidate = self.birdeye.build_candidate(seed)` block. Use the fallback directly.

**Before:**
```python
if candidate is None:
    try:
        seed = {"address": mint, "symbol": position.symbol, "decimals": position.decimals}
        candidate = self.birdeye.build_candidate(seed)
    except Exception as exc:
        LOGGER.warning("mark build failed for %s: %s — using minimal candidate for pricing fallback", mint, exc)
        candidate = TokenCandidate(address=mint, symbol=position.symbol, decimals=position.decimals)
```

**After:**
```python
if candidate is None:
    candidate = TokenCandidate(address=mint, symbol=position.symbol, decimals=position.decimals)
```

**Safety:** Valuation is `resolve_position_valuation()` (Jupiter-only) — unaffected. `last_exit_liquidity_usd` will always be `None` (same as before on Solana, since exit-liquidity is skipped). The liquidity_break_ratio check guards itself with `if position.exit_liquidity_at_entry_usd and position.last_exit_liquidity_usd:` — setting `None` is safe.

**CU impact:** Removes 1 Birdeye API call per open position per cycle when that position is not in the current candidate list.

---

### B2. Harden discovery seed failure — return cached candidates when Birdeye seeds fail

**Current behavior:** When discovery is triggered (interval elapsed) and `trending_tokens()` + `new_listings()` both fail → `seeds = []` → `candidates = []`. The `_last_discovery_candidates` list is NOT used.

**Desired:** When seeds fail entirely, return last known good candidates (with a degraded mode signal).

**Change:** In `_discover_with_cadence()` in trader.py, after a discovery call that yields zero candidates AND `discovery_failed` is True or `seeds_total == 0`, fall back to `_last_discovery_candidates`.

**Why this is JUP-first:** Cached candidates already passed Jupiter buy+sell probes. They are pre-validated by Jupiter. Returning them doesn't violate any JUP-first principle.

---

### B3. Add data_source_mode and discovery_mode to status snapshot

**Change:** Derive and include two new fields in `_cycle_summary()` (trader.py) and expose them in the dashboard `/api/status` endpoint:

- `data_source_mode`: `"jup_only"` | `"mixed"` | `"enrichment_enabled"`
  - `"jup_only"` when birdeye_budget_mode is `"starved"` OR discovery yielded zero seeds
  - `"mixed"` when birdeye_budget_mode is `"constrained"`
  - `"enrichment_enabled"` when birdeye_budget_mode is `"healthy"`
- `discovery_mode`: `"active"` | `"degraded"` | `"cached_only"`
  - `"active"` when discovery ran with real seeds
  - `"cached_only"` when discovery_cached is True
  - `"degraded"` when seeds_total == 0 but cycle ran, OR discovery_failed

---

## Phase C — Free-tier Adaptive Loop

**Assessment:** The free-tier adaptive loop described in the task is **already substantially implemented**:

1. `_discover_with_cadence()` already respects `effective_discovery_interval_seconds` — which is dynamically extended under `constrained`/`starved` budget modes
2. `adjusted_discovery_limits()` already cuts seed and overview limits under pressure
3. `should_skip_endpoint()` already gates security, holder, and creation enrichment

**What is missing:** Explicit visibility into which mode the system is in, and a graceful fallback when seeds fail (B2 above).

**Phase C implementation:** The adaptive loop is the existing system. No new infrastructure needed. The only additions are:
- B2 (cached fallback when seeds fail)
- B3 (mode fields in status)
- Dashboard rendering of `data_source_mode` and `discovery_mode`

---

## Deliverable Plan

### Implementation (minimal diff)

1. **trader.py** — Remove birdeye.build_candidate() in _mark_positions (B1)
2. **trader.py** — Add cached-candidate fallback on zero-seed discovery failure (B2)
3. **trader.py** — Add data_source_mode + discovery_mode to _cycle_summary() (B3)
4. **dashboard/app.py** — Expose data_source_mode + discovery_mode in /api/status payload (B3)

### Files NOT touched
- clients/birdeye.py (no change needed)
- clients/jupiter.py (no change needed)
- engine/discovery.py (no change needed)
- engine/position_pricing.py (no change needed)
- engine/runtime_policy.py (no change needed)
- config.py (no new env vars)

### Test changes
- Existing tests pass (B1 change makes _mark_positions deterministic for test mocks)
- Add 1 test for B2 (seed failure → cached fallback)
- Add 1 test for B3 (data_source_mode derivation)
