# Creeper-Dripper Full System Audit

Repo reviewed at: `main` (`539bcd5`)
Audit date: 2026-03-25

## 1. Executive verdict
- **Verdict**: **BLOCKER** for a cautious first live run.
- **Confidence**: **High** (blocker is a concrete correctness/accounting flaw on a core recovery path).
- **Top 5 risks**:
  1. **Recovery can silently “close” positions based on tx confirmation alone** (no settled qty/proceeds), breaking operator trust and capital accounting.
  2. **Exit reconciliation semantics are internally inconsistent**: `EXIT_PENDING`/`RECONCILE_PENDING(exit)` can transition to `CLOSED` without a trustworthy settlement quantity/proceeds.
  3. **Docs drift / architecture lock contradictions** can mislead operators and future changes (especially around execution model).
  4. **Stale legacy CLI reporting paths** reference wallet-based discrepancy reasons that no longer exist in production code, risking false operator conclusions.
  5. **Ambiguous unit naming in settlement parsing** (“lamports” vs token atomic) increases risk of future semantic errors and mis-review.

## 2. Architecture truth model
- **What the system treats as truth (as implemented)**:
  - **Execution & settlement quantities**: Jupiter `/swap/v2/order` + `/swap/v2/execute` response fields (Tiered fallbacks).
    - **Sell**: settled sold qty prefers `/execute` input amount, then order input amount; otherwise settlement is **unknown** and position moves to `RECONCILE_PENDING(exit)`.
    - **Buy**: settled bought qty prefers `/execute` output amount, then order out amount, then quote probe out amount.
  - **Tx lifecycle confirmation**: Solana RPC `getSignatureStatuses` (only used for tx status).
  - **Wallet balances**: not used for automatic reconciliation in the production codepaths reviewed.
- **Internal consistency**:
  - The **engine/executor** code mostly matches “Jupiter-only settlement truth” and is careful to avoid crediting SOL proceeds when unknown.
  - **Contradiction exists in recovery/reconcile**: startup recovery promotes positions to `CLOSED` based on **signature confirmation alone**, which is *not* settlement truth under the stated model and can destroy accounting truth.
- **Contradictions**:
  - `docs/ARCHITECTURE_LOCK.md` states “No regression to `/order -> /execute` as the primary execution model”, but production execution **is** `/swap/v2/order -> /swap/v2/execute` (`creeper_dripper/execution/executor.py`).
  - CLI prints/aggregates `recovery_wallet_gt_state` (“dirty wallet”) but production recovery no longer produces those reasons (`creeper_dripper/storage/recovery.py`).

## 3. Codepath review
Review by area.

### execution
- **What is good**
  - Clear mode gates: `DRY_RUN` and `LIVE_TRADING_ENABLED` both required for live execution (`creeper_dripper/execution/executor.py`).
  - Jupiter-only settlement chain is explicit and tested (tiered fallbacks) (`creeper_dripper/execution/executor.py`, `tests/test_execution_swap_path.py`).
  - Careful handling of “proceeds unknown”: does **not** credit cash and prevents immediate re-sell by zeroing pending qty (`creeper_dripper/engine/trader.py`).
  - Tx failure artifacts are persisted (`tx_failure_*.json`) for RPC send failures (`creeper_dripper/execution/executor.py`).
- **What is risky**
  - Settlement parsing helper `_extract_execute_response_output_lamports` is used for both buys and sells; naming implies lamports, but for buys it’s token atomic output. This is a maintainability/trust hazard.
  - Probe error classification in `_probe_error_details` uses substring heuristics; may misclassify and impact decision logs (operator trust).
- **What is stale/dead**
  - `TradeExecutor.sign_and_send()` and `/swap/v1/swap` transaction path appears legacy; primary path is v2 order/execute.
- **What should be tightened**
  - Align naming of settlement extraction functions/vars to avoid unit confusion.
  - Ensure every “sell blocked” path emits a consistent, searchable reason tag (see Observability section).

### trader/engine
- **What is good**
  - Cycle ordering is disciplined: recovery → discovery → mode-gated entries → marking/valuation → exits → persistence (`creeper_dripper/engine/trader.py`).
  - Safety rails exist and are enforced via safe mode: daily loss cap, consecutive execution failures, stale market data, unknown-exit saturation, max exit-blocked (`creeper_dripper/engine/trader.py`).
  - Exit logic prioritizes hard exits over Hachi dripper and drip exits; explicitly avoids ladder+dripper race (“double-selling”) (`creeper_dripper/engine/trader.py`).
  - When valuation marks are invalid, time stop still applies (exit protection not skipped).
- **What is risky**
  - **Blocker**: exit recovery path can close a position from signature confirmation alone (see Critical Findings).
  - Entry settlement unknown (`RECONCILE_PENDING(entry)`) is permanently manual — acceptable by design, but operator UX/docs must be extremely explicit (what to do, how to recover).
- **What is stale/dead**
  - CLI’s “dirty wallet detected” aggregator is stale relative to engine reality (no producer of those decisions).
- **What should be tightened**
  - Make state transition invariants explicit: when can a position become `CLOSED` in Jupiter-only truth?
  - Ensure “pending exit signature confirmed” cannot destroy accounting truth.

### storage/recovery
- **What is good**
  - Corrupt state handling archives the broken file and bootstraps a fresh portfolio (`creeper_dripper/storage/state.py`).
  - Persistence guard drops non-pubkey “mint placeholders” to avoid polluted state (`creeper_dripper/storage/state.py`).
- **What is risky**
  - **Blocker**: startup recovery closes positions without proceeds reconciliation (see Critical Findings).
  - `reconcile_pending_exit` has no “partial” concept; it maps confirmed tx → `CLOSED` unconditionally.
- **What is stale/dead**
  - Test suite currently asserts the risky recovery behavior; tests encode an unsafe assumption.
- **What should be tightened**
  - Recovery must preserve unresolved settlement/proceeds truth and keep the position visible until operator action.

### CLI/operator flow
- **What is good**
  - `doctor` checks: env load, mode flags, runtime dir writable, Birdeye/Jupiter reachability, safe-mode state (`creeper_dripper/cli/main.py`).
  - `run` creates per-run directories with snapshots and journals; emits `run_id` boundaries (good for forensics).
- **What is risky**
  - Stale “dirty wallet” warnings and counters reference `recovery_wallet_gt_state` which production recovery no longer emits; can cause operator confusion.
- **What is stale/dead**
  - References to wallet-balance discrepancy reasons (`recovery_wallet_gt_state`) in `cmd_run` token tracking/summary.
- **What should be tightened**
  - Remove or rework stale counters/warnings so operator output matches production truth model (Jupiter-only settlement).

### config/env
- **What is good**
  - `Settings.validate()` enforces required keys and validates wallet path only when live execution is actually enabled (`creeper_dripper/config.py`).
  - Mode defaults are safe (`DRY_RUN=true`, `LIVE_TRADING_ENABLED=false` in `.env.example`).
- **What is risky**
  - `BS58_PRIVATE_KEY` exists as deprecated fallback; ensure it never leaks via logs (currently not logged).
- **What should be tightened**
  - Ensure `.env.example` and README remain aligned with code semantics after each truth-model change (see Docs drift).

### docs
- **What is good**
  - README/HANDOFF clearly state Jupiter-only settlement model and mode gates.
- **What is risky / drift**
  - `docs/ARCHITECTURE_LOCK.md` contradicts current code (execution model).
  - README claims recovery detects “dirty wallet” via wallet balances, but production codepaths reviewed do not implement that check.
- **What should be tightened**
  - Update `ARCHITECTURE_LOCK.md` to reflect *current* approved model, or explicitly mark it stale.
  - Align recovery docs with actual recovery behavior (and fix recovery behavior first if blocker stands).

### tests
- **What is good**
  - Extensive targeted unit tests exist for settlement parsing, hachi dripper behavior, CLI operator outputs.
- **What is weak**
  - Tests encode the unsafe recovery invariant: confirmed signature ⇒ closed position (`tests/test_recovery_and_state.py`).
  - There are no tests asserting “recovery does not destroy accounting truth” (e.g., never closes without proceeds/qty truth).
- **What should be tightened**
  - Add tests for the corrected recovery semantics (see Recommended next actions).

## 4. Critical findings

### Finding 1 — **BLOCKER**: Startup recovery can silently close positions based on tx confirmation alone
- **Severity**: **blocker**
- **Exact file**: `creeper_dripper/storage/recovery.py`, `creeper_dripper/execution/reconcile.py`
- **Exact function(s)**:
  - `run_startup_recovery(...)` in `creeper_dripper/storage/recovery.py`
  - `reconcile_pending_exit(...)` in `creeper_dripper/execution/reconcile.py`
- **Exact risk**
  - For `EXIT_PENDING` or `RECONCILE_PENDING(exit)`, recovery calls `getSignatureStatuses`, and if it returns `"success"`, it transitions the position to `CLOSED` and removes it from `open_positions` **without**:
    - verifying sold quantity from Jupiter settlement truth,
    - verifying proceeds were credited to `portfolio.cash_sol`,
    - preserving a “settlement/proceeds unknown” state that requires manual operator reconciliation.
  - This can permanently hide unresolved capital outcomes and produce a misleading “closed” history.
- **Why it matters in live trading**
  - A confirmed signature is **not** sufficient to reconstruct execution quantities and proceeds under this system’s stated truth model.
  - Operators may believe the portfolio is flat or the system safely exited, while cash/proceeds are missing from state.
  - This is exactly the kind of failure that destroys operator trust and can mask capital loss or stuck funds.
- **Recommended minimal fix**
  - Change recovery semantics: **do not set `CLOSED` solely from signature success**.
  - Minimal safe behavior in Jupiter-only mode:
    - If a position is `EXIT_PENDING`/`RECONCILE_PENDING(exit)` and the signature is confirmed, transition to **`RECONCILE_PENDING(exit)`** (or a new explicit status) and keep it visible with `SELL_BLOCKED_REASON`-style audit tags, until operator provides settlement/proceeds truth.
    - Alternatively, require persisted settlement metadata at time of sell (sold qty + proceeds) before allowing `CLOSED`.
  - Update tests (`tests/test_recovery_and_state.py`) to reflect the corrected invariant.

### Finding 2 — **HIGH**: `docs/ARCHITECTURE_LOCK.md` contradicts production execution model
- **Severity**: **high**
- **Exact file**: `docs/ARCHITECTURE_LOCK.md`
- **Exact function(s)**: N/A (docs)
- **Exact risk**
  - The doc asserts “No regression to `/order -> /execute` as the primary execution model”, but production uses `/swap/v2/order` + `/swap/v2/execute` (`creeper_dripper/execution/executor.py`).
- **Why it matters in live trading**
  - Operators and future reviewers may rely on this doc to reason about correctness/safety, leading to wrong runbooks and wrong assumptions during incidents.
- **Recommended minimal fix**
  - Update the lock doc to match current code, or mark it explicitly stale with a pointer to the code entrypoints that define truth.

### Finding 3 — **MEDIUM**: CLI “dirty wallet” warning path is stale vs production recovery
- **Severity**: **medium**
- **Exact file**: `creeper_dripper/cli/main.py`
- **Exact function(s)**: `cmd_run(...)`
- **Exact risk**
  - `cmd_run` searches for recovery decisions with reason `"recovery_wallet_gt_state"` and prints `DIRTY_WALLET_DETECTED_NOT_CLEAN_START`.
  - Production recovery (`creeper_dripper/storage/recovery.py`) does not emit that reason; wallet-balance truth is removed.
- **Why it matters in live trading**
  - Operators may interpret the absence/presence of this warning incorrectly, or the code may suggest checks that aren’t actually happening.
- **Recommended minimal fix**
  - Remove this warning/counter or replace with a Jupiter-only “dirty state” detector that matches current truth boundaries (e.g., `RECONCILE_PENDING(entry/exit)` counts).

### Finding 4 — **LOW/MEDIUM**: Settlement parsing naming invites unit confusion
- **Severity**: **low** (can become medium if future changes touch it)
- **Exact file**: `creeper_dripper/execution/executor.py`
- **Exact function(s)**: `_extract_execute_response_output_lamports(...)`, `_settle_buy_after_execute(...)`
- **Exact risk**
  - A helper named “output_lamports” is used to parse Jupiter `/execute` output for both SOL proceeds (sell) and token output (buy). This is correct today by coincidence of shared field names, but highly error-prone.
- **Why it matters in live trading**
  - Unit confusion in settlement code is a classic source of silent accounting bugs.
- **Recommended minimal fix**
  - Rename extraction helper(s) to unit-neutral names (e.g. `_extract_execute_total_output_amount`) and rename vars accordingly.

## 5. Test coverage assessment
- **Already well covered**
  - Jupiter settlement tier chain for buy/sell (`tests/test_execution_swap_path.py`).
  - Hachi dripper behavior, chunk timing gate, overrides, and SOL crediting on successful chunks (`tests/test_hachi_dripper.py`).
  - State corruption archive and non-pubkey mint dropping (`tests/test_recovery_and_state.py`).
  - CLI `doctor` and `status` smoke behavior (`tests/test_cli_operator.py`).
- **Weak / risky gaps**
  - Recovery behavior tests currently **validate unsafe invariants** (confirmed signature ⇒ closed).
  - No tests for “accounting invariants” across recovery:
    - if `portfolio.cash_sol` is not credited and proceeds are unknown, position must not disappear from state.
    - `CLOSED` must imply a settled sold qty and final accounting consistency (per this truth model).
- **Exact missing tests worth adding**
  - `startup_recovery_confirmed_signature_does_not_close_without_settlement_truth`
  - `startup_recovery_preserves_reconcile_pending_exit_visibility_and_reason`
  - `position_cannot_transition_to_closed_without_exit_settlement_metadata`
- **Which missing tests matter before live vs later**
  - **Before live**: all recovery/accounting invariant tests above (they guard capital safety + operator trust).
  - **Later**: maintainability naming tests, doc drift checks.

## 6. Operator-readiness assessment
- **Docs drift**
  - `docs/ARCHITECTURE_LOCK.md` contradicts execution model.
  - CLI mentions wallet discrepancy reasons that production does not emit.
- **Env/config surprises**
  - `DISCOVERY_INTERVAL_SECONDS` and `MAX_ACTIVE_CANDIDATES` are required env vars (no defaults). That’s fine but must stay visible in docs/runbooks.
- **Log clarity**
  - Strong structured event emission exists, but “why no exits/trades” can still be obscured if a position disappears via recovery close.
- **Dangerous manual steps**
  - Manual resolution required for `RECONCILE_PENDING(entry)` and should be explicitly documented (step-by-step runbook).
- **First-live watch items**
  - Any `RECONCILE_PENDING(exit)` or `EXIT_PENDING` with a signature should be treated as high-attention until recovery semantics are fixed.
  - Monitor safe-mode triggers: stale market data, unknown exit saturation, max exit blocked.

## 7. Recommended next actions

### before first live run
- **Fix recovery semantics** so positions cannot become `CLOSED` purely from signature confirmation (see Finding 1).
- Update tests to enforce the corrected invariant and prevent regression.
- Update `docs/ARCHITECTURE_LOCK.md` to match actual execution model or mark it stale.
- Remove or correct stale CLI “dirty wallet” messaging to avoid operator confusion.

### after first live run
- Add a short operator runbook for `RECONCILE_PENDING(entry/exit)`:
  - how to identify,
  - how to resolve,
  - what evidence to capture (`run_dir`, artifacts).
- Consider adding a `status` output section: “positions requiring operator action” (counts + reasons).

### later / optional cleanup
- Rename settlement parsing helpers/vars to remove unit ambiguity.
- Consolidate “sell blocked” observability tags so every blocker produces a consistent `SELL_BLOCKED_REASON`-style signal.

## 8. Final verdict
Right now I **do not recommend** a cautious first live run: the startup recovery path can **silently close** unresolved exit positions based only on signature confirmation, which conflicts with the Jupiter-only settlement truth model and can permanently corrupt operator-visible accounting. Fixing that recovery invariant (and the tests/docs around it) is the minimal, surgical change required to restore correctness and operator trust without reintroducing wallet-balance truth.

