# Claude Scheduled Tasks Plan — Creeper Dripper

**Created:** 2026-03-26
**Purpose:** Read-only monitoring and planning layer. Catch capital, cost, and workflow issues before they escalate.
**Constraint:** ALL tasks are READ-ONLY. No code changes. No commits. No command execution beyond reading existing files.

---

## Summary

| # | Task ID | Title | Frequency | Category | Priority |
|---|---------|-------|-----------|----------|----------|
| 1 | SCH-001 | Runtime Health Monitor | every 30 minutes | A. Runtime health | HIGH |
| 2 | SCH-002 | Birdeye CU Watchdog | every 2 hours | B. Birdeye / CU cost drift | HIGH |
| 3 | SCH-003 | Exit-Blocked & Zombie Monitor | every 30 minutes | D. Exit-blocked / zombie risk | HIGH |
| 4 | SCH-004 | Discovery Efficiency Monitor | every 2 hours | C. Discovery quality drift | HIGH |
| 5 | SCH-005 | Safe Mode & Safety Stop Alert | every 30 minutes | A. Runtime health | HIGH |
| 6 | SCH-006 | Open Tasks & Backlog Reminder | once per day | G. Audit / backlog | MEDIUM |
| 7 | SCH-007 | Workflow Hygiene Monitor | once per day | F. Repo / workflow hygiene | MEDIUM |
| 8 | SCH-008 | Test Health Planning Reminder | once per week | H. Test-health reminders | MEDIUM |
| 9 | SCH-009 | Observability Blind Spot Audit | once per week | E. Dashboard / observability | LOW |
| 10 | SCH-010 | Birdeye Endpoint Drift Audit | once per day | B. Birdeye / CU cost drift | MEDIUM |

**Total: 10 scheduled tasks**

---

## Tasks by Frequency

### Every 30 Minutes (Runtime-Critical)

---

### SCH-001 — Runtime Health Monitor

**Category:** A. Runtime health
**Priority:** HIGH
**Frequency:** every 30 minutes

**Why it matters:** The engine holds real capital. A stuck cycle, unexpected deployable SOL change, or anomalous position state can compound rapidly. This is the earliest possible tripwire before a problem becomes a loss.

**Files read:**
- `runtime/state.json`
- `runtime/status.json`
- `runtime/journal.jsonl` (last 20 lines)

**Task prompt:**
```
You are a read-only monitoring agent for a live Solana trading bot. Do NOT modify any files or run any commands.

Read these files:
1. runtime/state.json
2. runtime/status.json
3. runtime/journal.jsonl (last 20 lines only)

Then produce a concise health report with this exact structure:

## Runtime Health Report — [current timestamp]

### Portfolio State
- cash_sol: [value]
- reserved_sol: [value]
- open_positions: [count] (list symbols and status for each)
- closed_positions: [count]
- safe_mode_active: [true/false]
- safety_stop_reason: [value or NONE]
- consecutive_execution_failures: [value]

### Last Cycle
- last_cycle_at: [value or NEVER]
- cycle_in_run: [value]
- seeds_total: [value]
- candidates_accepted: [value]
- exits_attempted / exits_succeeded: [values]

### Alert Conditions
For each of the following conditions, state YES or NO and explain if YES:
1. STALE CYCLE: last_cycle_at is null OR more than 60 minutes ago → ALERT
2. SAFE MODE: safe_mode_active is true → ALERT
3. EXECUTION FAILURES: consecutive_execution_failures > 2 → ALERT
4. ZERO SEEDS: last known seeds_total is 0 (may indicate API or config failure) → ALERT
5. DEPLOYABLE SOL ANOMALY: cash_sol is 0 AND open_positions > 0 AND hachi_birth_wallet_sol is set → WARN
6. EXIT_PENDING WITH NO ACTIVITY: any position in status EXIT_PENDING with exit_retry_count > 3 → WARN
7. ZOMBIE DETECTED: any position has zombie_reason set (non-null) → ALERT

### Summary
One sentence: GREEN / YELLOW / RED and why.
```

**What it reports:** Portfolio snapshot, cycle freshness, execution failure streak, safe-mode activation, exit-pending stalls, zombie tokens.

**Alert conditions:**
- last_cycle_at null or >60 min ago
- safe_mode_active = true
- consecutive_execution_failures > 2
- seeds_total = 0 (last known)
- Any position with zombie_reason set
- EXIT_PENDING position with exit_retry_count > 3

---

### SCH-003 — Exit-Blocked & Zombie Monitor

**Category:** D. Exit-blocked / zombie risk
**Priority:** HIGH
**Frequency:** every 30 minutes

**Why it matters:** A position stuck in EXIT_PENDING while the market moves against it is a capital loss in progress. The engine tracks `exit_blocked_cycles` and `zombie_since` — these fields are the early warning signal. If a position accumulates 5+ blocked cycles, that is a manual intervention signal.

**Files read:**
- `runtime/state.json`
- `runtime/journal.jsonl` (scan for SELL_BLOCKED, SELL_ATTEMPT failure, EXIT_PENDING events in last 50 lines)

**Task prompt:**
```
You are a read-only monitoring agent for a live Solana trading bot. Do NOT modify any files or run any commands.

Read these files:
1. runtime/state.json
2. runtime/journal.jsonl (last 50 lines)

Focus exclusively on exit health. Produce this report:

## Exit & Zombie Monitor — [current timestamp]

### Open Positions Summary
For EACH open position, report:
- symbol | mint (first 8 chars) | status | exit_blocked_cycles | first_blocked_at | zombie_since | zombie_reason | exit_retry_count | drip_exit_active | drip_chunks_done | pending_exit_reason

### Exit-Blocked Analysis
List any position where exit_blocked_cycles > 0. For each:
- How many cycles blocked?
- How long since first_blocked_at?
- Is this a zombie (zombie_since set)?
- Recommended operator attention level: WATCH / INVESTIGATE / URGENT

### Zombie Analysis
List any position where zombie_reason is not null. Report:
- Symbol, zombie_since, zombie_reason
- How long has it been a zombie?

### Journal Exit Events (last 50 lines)
Count and list: SELL_BLOCKED, SELL_ATTEMPT with status!=success, EXIT_PENDING, DRIPPER_WAIT

### Alert Conditions
1. ANY ZOMBIE: zombie_since is set on any position → ALERT
2. BLOCKED > 5 CYCLES: exit_blocked_cycles > 5 on any position → ALERT
3. REPEATED SELL FAILURES: >3 SELL_ATTEMPT failures for same mint in journal → ALERT
4. POSITION STUCK > 4 HOURS: EXIT_PENDING position where first_blocked_at is >4h ago → WARN

### Summary
GREEN / YELLOW / RED with one-line explanation.
```

**What it reports:** Per-position exit health, zombie detection, sell failure patterns from journal, blocked cycle counts.

**Alert conditions:**
- Any position with zombie_since set
- exit_blocked_cycles > 5
- >3 sell failures for same mint in recent journal
- EXIT_PENDING older than 4 hours

---

### SCH-005 — Safe Mode & Safety Stop Alert

**Category:** A. Runtime health
**Priority:** HIGH
**Frequency:** every 30 minutes

**Why it matters:** Safe mode freezes new entries. Safety stop means the engine has stopped itself for a reason. Both require operator awareness immediately. This is a lightweight, focused check that complements SCH-001 with more detail on the stop reason.

**Files read:**
- `runtime/state.json`
- `runtime/status.json`
- `runtime/journal.jsonl` (scan for SAFE_MODE, SAFETY_STOP, EXECUTION_FAILURE events in last 30 lines)

**Task prompt:**
```
You are a read-only monitoring agent for a live Solana trading bot. Do NOT modify any files or run any commands.

Read these files:
1. runtime/state.json
2. runtime/status.json
3. runtime/journal.jsonl (last 30 lines)

Produce a focused safety-state report:

## Safety State Monitor — [current timestamp]

### Safety Flags
- safe_mode_active: [true/false]
- safety_stop_reason: [value or NONE]
- consecutive_execution_failures: [value]
- entries_skipped_dry_run: [value]
- entries_skipped_live_disabled: [value]

### Safe Mode Assessment
If safe_mode_active is true:
- Report safety_stop_reason verbatim
- Scan journal for the most recent SAFE_MODE or SAFETY_STOP event and report its timestamp and metadata
- State: OPERATOR ACTION REQUIRED

If safe_mode_active is false:
- Report: CLEAR — no safe mode

### Execution Failure Assessment
If consecutive_execution_failures > 0:
- Report the count
- Scan journal for recent EXECUTION_FAILURE events (report symbol, reason, timestamp)
- If count > 2: WARN — approaching auto-halt threshold
- If count > 5: ALERT — engine may be in degraded state

### Dry Run / Live Disabled Check
If entries_skipped_live_disabled > 0 and live_trading_enabled should be true:
- ALERT: entries are being skipped due to live_trading_enabled=false — confirm config is intentional

### Summary
ONE LINE: SAFE / DEGRADED / HALTED and reason.
```

**What it reports:** Safe mode status, safety stop reason, execution failure streak, dry-run/live flag mismatches.

**Alert conditions:**
- safe_mode_active = true (any reason)
- consecutive_execution_failures > 2
- entries_skipped_live_disabled > 0 unexpectedly

---

## Every 2 Hours

---

### SCH-002 — Birdeye CU Watchdog

**Category:** B. Birdeye / CU cost drift
**Priority:** HIGH
**Frequency:** every 2 hours

**Why it matters:** The system was optimized from ~8581 CU/cycle to ~101-131 CU/cycle. This number can drift upward silently if: the overview limit is misconfigured, exit-liquidity somehow re-enables for Solana, or security/holder enrichment conditions expand. CU cost is money — drift here means direct financial waste that compounds over every cycle.

**Files read:**
- `runtime/birdeye_audit_summary.json`
- `runtime/birdeye_audit.jsonl` (last 20 lines, to check for recent entries)

**Task prompt:**
```
You are a read-only monitoring agent for a live Solana trading bot. Do NOT modify any files or run any commands.

Read these files:
1. runtime/birdeye_audit_summary.json
2. runtime/birdeye_audit.jsonl (last 20 lines if file exists)

Produce a Birdeye CU health report:

## Birdeye CU Watchdog — [current timestamp]

### CU Consumption (from audit summary)
- delta_usage_api: [value] (baseline: ~101-131 CU per controlled audit run)
- delta_usage_total: [value]
- estimated_cu_api_per_seed: [value] (baseline: ~2.525)
- estimated_cu_api_per_accepted_token: [value] (baseline: ~101.0)
- birdeye_candidate_build_calls: [value]
- seeds_total: [value]
- candidates_accepted: [value]

### Endpoint Call Counts
For each endpoint, report: total calls | 200 | 400 | other_non200
- /defi/token_overview
- /defi/token_creation_info
- /defi/token_security
- /defi/v3/token/holder
- /defi/token_trending
- /defi/v2/tokens/new_listing
- /utils/v1/credits
- /defi/v3/token/exit-liquidity [CRITICAL: should be called=false for Solana]
- /v1/wallet/token_list [should be called=false]

### Forbidden Endpoint Check
CRITICAL: If /defi/v3/token/exit-liquidity shows called=true or total_calls > 0:
→ ALERT: EXIT-LIQUIDITY REACTIVATED ON SOLANA — this was disabled at freeze checkpoint. Immediate investigation required.

### 400-Rate Assessment
For each endpoint with 400 > 0:
- Report endpoint, 400 count, total, rate (400/total as %)
- If any endpoint has 400-rate > 20%: WARN — investigate waste
- If any endpoint has 400-rate > 50%: ALERT — systematic failure, likely CU waste

### Audit File Freshness
- When was birdeye_audit_summary.json last modified?
- If older than 24 hours: NOTE — audit data is stale; watchdog is reading historical data

### CU Drift Assessment
- Is delta_usage_api within expected range (< 200 per audit run)?
- If delta_usage_api > 500: WARN — CU usage has drifted upward
- If delta_usage_api > 1000: ALERT — significant regression from freeze baseline of ~131

### Summary
GREEN / YELLOW / RED with one-line explanation.
```

**What it reports:** CU delta vs freeze baseline, forbidden endpoint check (exit-liquidity), per-endpoint 400 rates, audit file freshness.

**Alert conditions:**
- `/defi/v3/token/exit-liquidity` called=true (must remain false for Solana)
- delta_usage_api > 500 (drift from ~131 baseline)
- Any endpoint with >50% 400-rate
- Audit summary older than 24 hours (stale data)

---

### SCH-004 — Discovery Efficiency Monitor

**Category:** C. Discovery quality drift
**Priority:** HIGH
**Frequency:** every 2 hours

**Why it matters:** Discovery is the funnel that feeds the whole system. If seeds collapse (API issue), if the overview limit is too aggressive (zero candidates built), or if the acceptance rate drops to near-zero (tuning drift), the engine runs but does nothing useful — burning CU on empty cycles while capital sits idle.

**Files read:**
- `runtime/birdeye_audit_summary.json` (discovery_summary section)
- `runtime/status.json` (last cycle summary)

**Task prompt:**
```
You are a read-only monitoring agent for a live Solana trading bot. Do NOT modify any files or run any commands.

Read these files:
1. runtime/birdeye_audit_summary.json (focus on discovery_summary section)
2. runtime/status.json

Produce a discovery efficiency report:

## Discovery Efficiency Monitor — [current timestamp]

### Discovery Funnel (from audit summary discovery_summary)
Report the full funnel with stage-by-stage efficiency:
- seeds_total: [value]
- discovered_candidates (from seeds): [value]
- seed_prefiltered_out (overview_limit rejections): [value]
- prefiltered_candidates (passed prefilter): [value]
- topn_candidates (after top-N): [value]
- route_checked_candidates: [value]
- candidates_built: [value]
- candidates_accepted: [value]
- candidates_rejected_total: [value]

Derived metrics:
- Seed→prefilter pass rate: prefiltered_candidates / seeds_total (%)
- Prefilter→accepted rate: candidates_accepted / prefiltered_candidates (%)
- Overview limit rejection share: reject_overview_limit / candidates_rejected_total (%)

### Birdeye Call Efficiency
- birdeye_candidate_build_calls: [value]
- jupiter_buy_probe_calls + jupiter_sell_probe_calls: [values]
- Are route-check calls proportional to candidates_built? (should be 2× candidates_built for buy+sell probes)

### Rejection Reason Breakdown
List all rejection_counts from the audit summary with counts. Flag if:
- reject_overview_limit is 100% of rejections → overview limit may be too tight
- reject_high_sell_impact is >50% of rejections → liquidity environment may have changed
- Any new rejection reason appears that was not in the freeze baseline

### From Last Cycle Status
- seeds_total from last cycle: [value]
- candidates_accepted from last cycle: [value]
- If both are 0: WARN — no candidates were processed at all this cycle

### Alert Conditions
1. ZERO SEEDS: seeds_total = 0 → ALERT — seed fetch may have failed
2. ZERO ACCEPTED AFTER BUILD: candidates_built > 0 but candidates_accepted = 0 for multiple consecutive reports → WARN — scoring or route filtering may be too strict
3. OVERVIEW LIMIT DOMINATING: reject_overview_limit > 95% of all rejections → NOTE — consider whether DISCOVERY_OVERVIEW_LIMIT is calibrated correctly
4. FUNNEL COLLAPSE: prefiltered_candidates = 0 consistently → ALERT — prefilter may be broken or too aggressive
5. PROBE MISMATCH: route_checked_candidates * 2 significantly exceeds candidates_built → NOTE — possible cache miss inflation

### Summary
GREEN / YELLOW / RED with one-line explanation of funnel health.
```

**What it reports:** Full discovery funnel metrics, stage-by-stage efficiency, rejection reason breakdown, Birdeye call proportionality.

**Alert conditions:**
- seeds_total = 0 (seed fetch failure)
- candidates_built > 0 but candidates_accepted = 0
- reject_overview_limit = 100% of rejections
- prefiltered_candidates = 0

---

### SCH-010 — Birdeye Endpoint Drift Audit

**Category:** B. Birdeye / CU cost drift
**Priority:** MEDIUM
**Frequency:** once per day

**Why it matters:** Distinct from the real-time CU watchdog (SCH-002), this is a daily diff check. It looks at whether the set of endpoints being called has changed — a new endpoint appearing or a previously-zero endpoint reactivating is the most dangerous sign of architectural drift in the Birdeye integration.

**Files read:**
- `runtime/birdeye_audit_summary.json`
- `runtime/birdeye_audit.jsonl` (full scan for endpoint diversity)

**Task prompt:**
```
You are a read-only monitoring agent for a live Solana trading bot. Do NOT modify any files or run any commands.

Read these files:
1. runtime/birdeye_audit_summary.json
2. runtime/birdeye_audit.jsonl (full file if < 500 lines, otherwise last 200 lines)

Produce a daily Birdeye endpoint drift audit:

## Birdeye Endpoint Drift Audit — [current timestamp]

### Canonical Endpoint Set (freeze baseline 2026-03-25)
The ONLY endpoints that should be called are:
ALLOWED (discovery path):
  - /defi/token_trending
  - /defi/v2/tokens/new_listing
  - /defi/token_overview (capped by DISCOVERY_OVERVIEW_LIMIT)
  - /defi/token_creation_info (survivors only)
  - /defi/token_security (conditional: only when score gate still reachable)
  - /defi/v3/token/holder (conditional: only when score gate still reachable)
  - /utils/v1/credits (metering: before + after)

FORBIDDEN (must never appear as called=true):
  - /defi/v3/token/exit-liquidity (Solana unsupported — skip implemented at freeze)
  - /v1/wallet/token_list (not used in current architecture)
  - Any other endpoint not in the ALLOWED list above

### Actual Endpoint Report
For each endpoint in the audit summary, report:
  - called: [true/false]
  - total_calls: [value]
  - 200 / 400 / other_non200: [counts]
  - Status vs baseline: EXPECTED / UNEXPECTED / FORBIDDEN

### Drift Detection
Report any endpoint where:
1. It appears in the FORBIDDEN list and called=true → CRITICAL DRIFT
2. It appears in the ALLOWED list but was not called (conditional endpoints are OK if not triggered)
3. Any NEW endpoint appears that is NOT in either list above → UNKNOWN ENDPOINT — review immediately

### 400 Waste Summary
For each endpoint with 400 > 0, compute waste_calls = 400 + other_non200.
Total waste_calls across all endpoints: [sum]
If total waste > 20% of all calls: WARN

### Top 10 Mints Causing 400s
Report top_10_mints_causing_400 from audit summary.
If any mint appears repeatedly across different endpoints: flag as systematic bad-seed problem.

### Trend vs Yesterday (if prior audit data visible in jsonl)
Compare today's endpoint set to any prior entry in birdeye_audit.jsonl.
Note any changes in call counts or endpoint activation.

### Summary
List: [N] endpoints called, [N] forbidden endpoints active (should be 0), [N] unknown endpoints, total 400-waste rate.
```

**What it reports:** Full endpoint set vs freeze baseline, forbidden endpoint detection, unknown endpoint detection, 400-waste rate, bad-seed patterns.

**Alert conditions:**
- Any forbidden endpoint shows called=true
- Any unknown endpoint appears
- Total 400-waste > 20% of all calls
- Same mint appearing in top-10-400s across multiple endpoints

---

## Once Per Day

---

### SCH-006 — Open Tasks & Backlog Reminder

**Category:** G. Audit / backlog follow-up reminders
**Priority:** MEDIUM
**Frequency:** once per day

**Why it matters:** Deferred tasks in OPEN_TASKS.md and the backlog accumulate. Without a daily review, "later" items become permanent technical debt. This task surfaces the prioritized list without requiring a human to remember to check it.

**Files read:**
- `docs/handoff/2026-03-25-freeze/OPEN_TASKS.md`
- `docs/handoff/2026-03-25-freeze/KNOWN_ISSUES.md`
- `docs/backlog/birdeye-and-runtime-followups.md`
- `docs/workflow/claude_task_result.json` (check if any task is ready_for_cursor_validation)

**Task prompt:**
```
You are a read-only planning agent for a live Solana trading bot. Do NOT modify any files or run any commands.

Read these files:
1. docs/handoff/2026-03-25-freeze/OPEN_TASKS.md
2. docs/handoff/2026-03-25-freeze/KNOWN_ISSUES.md
3. docs/backlog/birdeye-and-runtime-followups.md
4. docs/workflow/claude_task_result.json (if it exists)

Produce a daily task status report:

## Daily Task & Backlog Review — [current timestamp]

### Tasks Ready for Execution (Immediate Priority)
List all tasks with status "todo" and priority "now" from OPEN_TASKS.md.
For each: title | why | what's needed to start | estimated risk (HIGH/MED/LOW capital risk)

### Pending Task Handoffs
Check docs/workflow/claude_task_result.json.
If status is "ready_for_cursor_validation":
  → REMINDER: Task [task_id] is ready for Cursor validation. It has been pending since [check file modification date if inferrable].
  → Report the required_validation commands verbatim so an operator can act immediately.

### Research Items (Next to Prioritize)
List all "research" priority items from OPEN_TASKS.md with a one-line summary of what investigation would look like.

### Backlog Issue Status
From docs/backlog/birdeye-and-runtime-followups.md, list each issue:
- Issue number | title | current status (open/resolved/historical)
- NOTE: Issue 3 (exit-liquidity 400 storm) was resolved at the 2026-03-25 freeze — mark as historical if still listed as open.

### Known Issues (Test Failures)
From KNOWN_ISSUES.md, list each pre-existing failure with:
- Test name | root cause summary | decision made | still relevant?

### Suggested Next Action
Based on the above, state the single highest-value task that could be done in the next Claude session. Include: what to read, what to produce, estimated session scope.

### Summary
[N] tasks ready for execution, [N] awaiting handoff validation, [N] research items, [N] known test failures unresolved.
```

**What it reports:** Prioritized task queue, pending Cursor handoffs, backlog status with resolved-item flagging, test failure tracker, suggested next action.

**Alert conditions:**
- Any "now" priority task has been in OPEN_TASKS for >3 days without status change
- claude_task_result.json is ready_for_cursor_validation but has not been acted on
- A backlog issue marked "open" is actually resolved (needs doc cleanup)

---

### SCH-007 — Workflow Hygiene Monitor

**Category:** F. Repo / workflow hygiene
**Priority:** MEDIUM
**Frequency:** once per day

**Why it matters:** The system depends on clean handoff artifacts, up-to-date freeze docs, and consistent workflow tooling. Stale task result files, mismatched validation statuses, or missing handoff docs create confusion and increase the risk of incorrect Claude sessions (reading outdated context).

**Files read:**
- `docs/workflow/` (list all files, check modification dates where inferrable from content)
- `docs/handoff/2026-03-25-freeze/` (check all expected files exist)
- `docs/backlog/` (check for stale items)
- `docs/audit/` (check for recent audit artifacts)

**Task prompt:**
```
You are a read-only monitoring agent for a live Solana trading bot. Do NOT modify any files or run any commands.

Read the following files and directory listings:
1. docs/workflow/ — list all files present
2. docs/handoff/2026-03-25-freeze/ — list all files present, read README.md
3. docs/backlog/ — list all files present, read birdeye-and-runtime-followups.md
4. docs/audit/ — list all files present

Then read:
- docs/workflow/claude_task_result.json (if present)
- docs/workflow/cursor_validation_result.json (if present)

Produce a workflow hygiene report:

## Workflow Hygiene Monitor — [current timestamp]

### Freeze Checkpoint Completeness
Expected files in docs/handoff/2026-03-25-freeze/:
  README.md, CURRENT_SYSTEM_STATE.md, OPEN_TASKS.md, KNOWN_ISSUES.md, TODAY_CHAT_CONTEXT.md, TODAY_SUMMARY.md, DISCUSSED_TOPICS.md

For each expected file: PRESENT / MISSING

### Task Handoff State
From docs/workflow/claude_task_result.json (if present):
- task_id: [value]
- status: [value]
- If status is "ready_for_cursor_validation": PENDING VALIDATION — task is waiting for Cursor
- If status is "validated" or "committed": COMPLETE — task can be archived

From docs/workflow/cursor_validation_result.json (if present):
- Does it match the task_id in claude_task_result.json?
- If yes: what is the validation outcome?
- If no: MISMATCH — the two handoff files refer to different tasks

### Backlog File Staleness Check
From docs/backlog/birdeye-and-runtime-followups.md:
- List each issue and assess: OPEN / RESOLVED / HISTORICAL based on known system state
- Issue 3 (exit-liquidity) should be HISTORICAL — flag if still documented as open
- Are there any issues in the backlog that appear to be fully resolved but not yet closed?

### Audit Artifact Freshness
From docs/audit/:
- List all audit files present
- Note the most recent audit file (by filename date)
- If no audit file is dated within the last 7 days: NOTE — consider running a fresh audit

### Workflow Doc Consistency
Check docs/workflow/ for:
- Any files that appear to be stale task results (old claude_task_result.json that was superseded)
- Any files that should not be in the workflow directory

### Summary
[N] freeze files present/missing, handoff state: [state], [N] stale backlog items, audit freshness: [days since last audit].
```

**What it reports:** Freeze checkpoint file completeness, task handoff state and cross-file consistency, backlog staleness, audit artifact freshness.

**Alert conditions:**
- Any expected freeze checkpoint file is missing
- claude_task_result.json and cursor_validation_result.json refer to different task IDs
- A backlog issue that is resolved is still documented as open
- No audit file dated within 7 days

---

## Once Per Week

---

### SCH-008 — Test Health Planning Reminder

**Category:** H. Test-health reminders
**Priority:** MEDIUM
**Frequency:** once per week

**Why it matters:** The freeze baseline is 202 passed / 5 failed. These failures are known and deferred, but they cannot be deferred indefinitely — especially the Hachi dripper test mismatch, which is the highest-risk deferred issue (tests that don't match live engine behavior are dangerous). This weekly reminder keeps the test debt visible.

**Files read:**
- `docs/handoff/2026-03-25-freeze/KNOWN_ISSUES.md`
- `docs/handoff/2026-03-25-freeze/OPEN_TASKS.md`
- `docs/backlog/birdeye-and-runtime-followups.md`

**Task prompt:**
```
You are a read-only planning agent for a live Solana trading bot. Do NOT modify any files or run any commands.

Read these files:
1. docs/handoff/2026-03-25-freeze/KNOWN_ISSUES.md
2. docs/handoff/2026-03-25-freeze/OPEN_TASKS.md
3. docs/backlog/birdeye-and-runtime-followups.md

Produce a weekly test health planning report:

## Weekly Test Health Planning — [current timestamp]

### Known Test Failures (freeze baseline: 202 passed / 5 failed)
List each known failing test from KNOWN_ISSUES.md:
For each:
- Test name
- Root cause (one sentence)
- Risk assessment: does this test failure indicate a gap between test expectations and live engine behavior?
- Fix approach (from OPEN_TASKS.md if documented)
- Priority: how soon should this be fixed?

### Highest-Risk Failure: Hachi Dripper Tests
The Hachi dripper tests (test_hachi_brain.py) expect DRIPPER_CHUNK_SELECTED but the engine emits DRIPPER_WAIT.
Assess:
1. Does this mismatch mean the tests are wrong, or the engine is wrong?
2. What is the capital risk of leaving this unresolved? (Tests not catching real behavior drift = risk)
3. What is the minimum investigation needed to decide the answer?

### Logging Test Failure
test_setup_logging_creates_per_run_logfile: assess current status.
If claude_task_result.json in docs/workflow/ has task_id "T-001-fix-per-run-logfile-creation":
- Report its status. If "ready_for_cursor_validation": the fix is written, just needs validation and commit.

### Suggested Testing Work for Next Sprint
Based on priority and risk:
1. Which failing test should be fixed first?
2. What is the safest way to approach it? (Update test vs fix engine)
3. What files would need to be read to plan the fix?

### Summary
[N] known failures, [N] are capital-risk relevant, next recommended test action.
```

**What it reports:** Known test failures with risk assessment, Hachi dripper mismatch analysis, logging fix status, suggested testing priorities.

**Alert conditions:**
- Hachi dripper test failures have been unresolved for >2 weeks (risk of silent engine-test divergence compounding)
- T-001 fix is still "ready_for_cursor_validation" after >1 week (should have been committed by now)

---

### SCH-009 — Observability Blind Spot Audit

**Category:** E. Dashboard / observability blind spots
**Priority:** LOW
**Frequency:** once per week

**Why it matters:** Over time, the observability layer can fall behind the engine. Events get emitted but not surfaced, new fields appear in state.json but aren't monitored, or the journal grows with patterns that no alert catches. This weekly review keeps the monitoring surface current without requiring code changes.

**Files read:**
- `runtime/state.json`
- `runtime/status.json`
- `runtime/journal.jsonl` (last 100 lines)
- `docs/handoff/2026-03-25-freeze/CURRENT_SYSTEM_STATE.md`

**Task prompt:**
```
You are a read-only audit agent for a live Solana trading bot. Do NOT modify any files or run any commands.

Read these files:
1. runtime/state.json
2. runtime/status.json
3. runtime/journal.jsonl (last 100 lines)
4. docs/handoff/2026-03-25-freeze/CURRENT_SYSTEM_STATE.md

Produce a weekly observability audit:

## Observability Blind Spot Audit — [current timestamp]

### State Field Coverage
Review runtime/state.json. For each top-level field and each field within open_positions entries:
- Is this field monitored by any known scheduled task (SCH-001 through SCH-010)?
- Is any field consistently null or 0 when it should have meaningful values?
- Are there any fields whose values look anomalous but would not trigger any current alert?

Flag up to 5 fields that appear to be unmonitored or under-reported.

### Journal Event Coverage
Review the last 100 lines of runtime/journal.jsonl.
List all distinct action types seen (e.g., BUY, SELL, DRIPPER_CHUNK_SELECTED, DRIPPER_WAIT, EXIT_PENDING, etc.).
For each action type:
- Is it covered by a current scheduled task alert condition?
- Does it appear with anomalous frequency (e.g., many DRIPPER_WAIT but no DRIPPER_CHUNK_SELECTED)?

### Cycle Summary Coverage
Review runtime/status.json summary fields.
Flag any summary counter that is always 0 (may indicate dead code path or broken counter):
- exits_attempted vs exits_succeeded mismatch?
- execution_failures counter growing?
- unknown_exits > 0?

### Missing Alert Conditions
Based on the above, identify up to 3 gaps in the current monitoring set:
For each gap:
- What condition is not currently detected?
- What file/field would surface it?
- What would a monitoring prompt look like? (one paragraph)

### Hachi Dripper Observability
Check journal for Hachi-specific events:
- DRIPPER_CHUNK_SELECTED vs DRIPPER_WAIT ratio
- drip_chunks_done values in state.json open_positions
- hachi_last_tp_level values (are they advancing, or stuck at null?)
- hachi_birth_wallet_sol: is it set? (required for dynamic capacity)

### Summary
[N] unmonitored state fields identified, [N] journal action types without alert coverage, [N] monitoring gaps proposed.
```

**What it reports:** State field coverage gaps, journal event diversity, dead counter detection, Hachi dripper observability, proposed new monitoring conditions.

**Alert conditions (meta):** This task produces recommendations, not alerts. If it finds >3 critical unmonitored conditions, flag to operator for next planning session.

---

## Design Notes

### What was deliberately excluded

- No task that reads `.env` or config files that might expose secrets
- No task that counts git commits, checks git status, or touches version control
- No task that runs tests or validation commands
- No task that checks external APIs (Birdeye, Jupiter) directly — only reads local audit artifacts
- No task that modifies `runtime/`, `docs/`, or any file on disk
- No task with ambiguous "fix it" language — every task ends with a report

### Frequency rationale

30-minute tasks cover the live-capital risk surface: is the engine running, are exits processing, is safe mode active. These cannot wait 2 hours.

2-hour tasks cover cost and discovery efficiency. These drift slowly enough that a 2-hour lag is acceptable, but fast enough that daily-only would miss an intraday CU spike.

Daily tasks cover workflow state and task queue. These change at human pace.

Weekly tasks cover structural debt (test failures, observability gaps). These require planning, not reaction.

### Task priority rationale

HIGH = capital or cost impact detectable in <30 minutes if unmonitored
MEDIUM = workflow or quality impact; missed detection costs hours, not minutes
LOW = architectural/structural insight; missed detection costs days to weeks

### CU conservatism

All tasks read local files only. Zero Birdeye or Jupiter API calls. The only "cost" is Claude input tokens for reading the files. Most files are small (<1KB each). The largest file is `birdeye_audit_summary.json` (~10KB). All tasks are within the "cheap, precise" category per the claude-usage-audit-2026-03-26.md operating policy.

---

*Plan created: 2026-03-26*
*Based on: freeze checkpoint 2026-03-25, CURRENT_SYSTEM_STATE.md, OPEN_TASKS.md, KNOWN_ISSUES.md, backlog docs, runtime/state.json, runtime/status.json, runtime/birdeye_audit_summary.json*
