# Required topic coverage (today’s scope + honesty boundaries)

Content below is **only** what we discussed today, plus pointers to existing repo docs where we did **not** change behavior in this session.

---

## A. Runtime / trading system status
**Discussed today:** Doctor preflight integration and `run` path were part of earlier work in this repo; today focused on **venv guard** (must use project `.venv`), **Birdeye audit CLI** (`audit-birdeye-once`), and **discovery** cost.  
**Frozen assertion:** `creeper-dripper` CLI is intended to run under `.venv`; doctor/run surface interpreter context.  
**Not changed today:** Dashboard lifecycle details (auto start/stop) — **no code changes in this freeze**; treat as existing product behavior unless verified in tree.

---

## B. Capital / capacity model
**Discussed today:** Not redesigned today.  
**Repo pointer:** `HANDOFF.md` and config/env names for portfolio sizing; **dynamic capacity** / **Hachi birth** / **effective caps** exist in settings — **verify in `config.py` and runtime state** before changing.  
**Freeze:** No capacity-model edits in this handoff.

---

## C. Exit / blocked position handling
**Discussed today:** Not the focus.  
**Repo pointer:** `HANDOFF.md` safe mode, zombie / exit-blocked language may appear in engine — **unchanged** today.

---

## D. Hachi / dripper
**Discussed today:** **Test failures** in `test_hachi_brain.py` (dripper chunk vs wait). Runner-preservation / TP-level drip fields — **not modified** in this freeze; listed as follow-up alignment between tests and engine.  
**Why it matters:** Prevents false confidence in CI until tests and policy match.

---

## E. Discovery / Birdeye cost optimization (full sequence)
1. **Suspicion:** Audit showed heavy 400s and high CU on some endpoints.  
2. **Proof:** `curl` + `token_overview` 200 vs exit-liquidity 400 for Solana, same mint.  
3. **Decision:** Skip `/defi/v3/token/exit-liquidity` entirely on Solana before HTTP.  
4. **Direction:** Two-phase (light vs heavy) → three-stage (seeds → capped overview → heavy age for survivors) → **conditional** security/holder at route stage when score bounds require them.  
5. **CU progression (approximate, audit snapshots):** ~8581 → ~5011 → ~601 → **~131** (`delta_usage_api`).  
6. **Verdict:** Birdeye usage is **disciplined**; pattern is trending + new listing + **overview + creation** for candidates that survive caps + **security + holder** only when the gate can still depend on them.  
7. **Endpoint pattern (post-optimization):** See latest `runtime/birdeye_audit_summary.json` after `audit-birdeye-once` — counts are per-run.

---

## F. Dashboard / observability
**Discussed today:** Audit jsonl + summary JSON for Birdeye; **grouped events / timeline / cycle snapshot** mentioned as **future** polish, not implemented in this freeze.  
**Freeze:** No new dashboard features in this checkpoint.

---

## G. Intentionally not done (from today’s discussion)
- Fix **5 failing tests** (Hachi + logfile) — deferred.  
- **CU budget guard** and **400-rate alert** — backlog only.  
- Further **conditional enrichment** ideas — only if new evidence; current path frozen.  
- **Clean-wallet validation** — operational note in HANDOFF, not automated today.  
- Any **extra** backlog items not in `docs/backlog/birdeye-and-runtime-followups.md` — out of scope.
