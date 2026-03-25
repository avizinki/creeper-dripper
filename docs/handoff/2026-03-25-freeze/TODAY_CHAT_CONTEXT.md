# Today’s chat context (by topic)

Not full transcripts. Each section: topic → discovered → changed → decision → frozen / follow-up / rejected.

## 1. Birdeye API audit (diagnostic)
- **Discovered:** Need per-endpoint counts, 200/400 split, CU delta per controlled cycle without changing trading logic first.
- **Changed:** Client audit hooks, `birdeye_audit` helpers, `audit-birdeye-once` CLI, summary JSON.
- **Decision:** Diagnostic-only first; then use evidence to cut waste.
- **Status:** **Frozen** as the audit pattern; extend only if needed.

## 2. Venv guard
- **Discovered:** Wrong Python = wrong deps and meaningless CU accounting; `sys.executable` resolution broke on macOS symlinks.
- **Changed:** Strict check via `sys.prefix` == project `.venv`; `ALLOW_NON_VENV`; doctor/run visibility; tests patch env.
- **Decision:** Fail fast outside `.venv`.
- **Status:** **Frozen.**

## 3. Exit-liquidity on Solana
- **Discovered:** Direct curl + audit: same mint gets 200 on `token_overview`, 400 on exit-liquidity (“Chain solana not supported”). Not bad params — unsupported endpoint for our chain.
- **Changed:** Skip HTTP entirely on Solana path; reason string; non-retryable 400 for that message class.
- **Decision:** Do not call exit-liquidity for Solana in our flow.
- **Status:** **Frozen** (revisit only if Birdeye adds Solana support).

## 4. Discovery phases and overview cap
- **Discovered:** Heavy endpoints were burning CU on every seed; `token_overview` also scaled with seed count.
- **Changed:** Phased pipeline + `DISCOVERY_OVERVIEW_LIMIT`; sort seeds by volume; heavy age enrichment only for survivors.
- **Decision:** Cheap filters before expensive Birdeye calls.
- **Status:** **Frozen.**

## 5. Conditional security / holder enrichment
- **Discovered:** Even after phases, security + holder still called when score outcome was already determined.
- **Changed:** `enrich_candidate_heavy` = creation only; `enrich_candidate_security_only` / `enrich_candidate_holders_only`; route stage score bounds vs `min_discovery_score`; flags on `TokenCandidate`.
- **Decision:** Skip security/holder when they cannot flip the score gate; always fetch security when gate still reachable (binary mint/freeze flags).
- **Status:** **Frozen.**

## 6. CU progression and audit verdict
- **Discovered:** Approximate sequence ~8581 → ~5011 → ~601 → ~131 API credit delta for comparable audit runs.
- **Decision:** Treat Birdeye usage as disciplined; monitor with `audit-birdeye-once` when tuning env.
- **Status:** **Frozen** numbers as snapshot; live CU varies with seeds and limits.

## 7. Test failures and backlog file
- **Discovered:** Full pytest still fails on Hachi + logging tests; unrelated to Birdeye merge.
- **Changed:** `docs/backlog/birdeye-and-runtime-followups.md` filed for follow-ups.
- **Decision:** Not fixed in freeze.
- **Status:** **Follow-up** (see KNOWN_ISSUES, OPEN_TASKS).

## Rejected / out of scope (today)
- Rewriting Hachi policy to satisfy old tests without a deliberate product decision.
- New dashboard features or capital-model changes as part of this freeze.
