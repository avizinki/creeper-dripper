# Run boundary report (isolated sessions)

**Purpose:** Separate **distinct process / wallet sessions** using hard evidence in `runtime/logfile.log`, `runtime/journal.jsonl`, and `runtime/state.json`.  
**Machine-readable:** `run_boundary_report.json` (includes `generated_at_utc`, log SHA-256, line counts).

---

## 1. Hard run boundaries (evidence)

### 1.1 `Loading Solana keypair` (process / wallet attach)

| Count in `runtime/logfile.log` | Line(s) |
|--------------------------------|---------|
| **1** | **54** |

**Snippet (line 54):**  
`2026-03-24 23:36:30,619 INFO [creeper_dripper.cli.main] Loading Solana keypair from path: .../wallet_3LEZ.json`

**Implication:** Everything **after** line 54 is one **wallet-bound** session in this log file. There is **no** second keypair load → **no** second distinct wallet session in the same logfile.

### 1.2 `cache_engine_init` (engine identity)

| Line | `candidate_cache_id` | Context |
|------|----------------------|---------|
| **1** | 4415129424 | First line of file; **before** line 54 keypair |
| **55** | 4449301712 | Immediately after keypair load |

### 1.3 `runtime_cost:` (CLI cycle completion marker)

| Segment | Line range | `runtime_cost` lines |
|---------|------------|----------------------|
| `r0_pre_wallet` | 1–53 | **0** |
| `r1_wallet_session` | 54–EOF | **645** (see JSON for value at generation) |

### 1.4 `runtime/status.json` / cycle continuity

- `status.json` only stores the **last** completed cycle (`cycle_timestamp`, `summary`). It does **not** list all historical cycles.
- **Evidence at generation:** `cycle_timestamp` = `2026-03-25T05:38:45.552740+00:00` (see `runtime/status.json`).

### 1.5 State / snapshot / journal

- **State reset:** No `state_reset` / archive marker cited in this report’s log slice.
- **Snapshots:** Folders under `review_artifacts/runtime_snapshots/` (timestamps in JSON) are **point-in-time copies**; all listed timestamps fall **after** the keypair line (`2026-03-24T21:55-47Z` UTC ≈ after local session start).
- **Journal:** `journal.jsonl` has **no** run_id field. Attribution to `r1_wallet_session` is by cross-check: **zero** `entry_success` in log **before** line 54, while journal records on-chain buys with signatures (see §4).

---

## 2. Per-run summary

### `r0_pre_wallet` (log lines 1–53)

| Field | Value |
|--------|--------|
| **Start** | Line **1** — `cache_engine_init` id=4415129424 |
| **End** | Line **53** — last line before `Loading Solana keypair` |
| **Fresh state/runtime?** | **Unknown** — no proof of empty state in this segment |
| **Accepted (unique)** | PRl (`J7M...`), EDGe (`5xz...`) |
| **Probed** (`candidate_built`/`reason=ok` unique mints) | **11** |
| **Actually bought** (`entry_success`) | **None** in this segment |
| **Actually sold** (`exit_success`) | **None** in this segment |
| **Ending open positions** | **N/A** — segment does not show wallet-bound trades |

### `r1_wallet_session` (log lines 54–EOF)

| Field | Value |
|--------|--------|
| **Start** | Lines **54–55** — `Loading Solana keypair` + `cache_engine_init` id=4449301712 |
| **End** | **EOF** at generation — no second keypair line |
| **Fresh state/runtime?** | **Not proven fresh** — normal startup loads `state.json` |
| **Accepted (unique)** | PRl, EDGe, SPAce, VDOR, Bp, BLock (6 mints — see JSON map) |
| **Probed** (`candidate_built`/`reason=ok` unique mints) | **408** |
| **Actually bought** (`entry_success` in log) | PRl, EDGe, SPAce, BLock |
| **Actually sold** (`exit_success` in log) | PRl, SPAce (lines **22444**, **45970**) |
| **Ending open positions** (from `state.json` at generation) | **EDGe**, **BLock** |

**Closed in state at generation:** PRl, SPAce (see `closed_positions`).

---

## 3. Summary table

| run_id | fresh? | accepted | bought | sold | ending open positions |
|--------|--------|----------|--------|------|-------------------------|
| `r0_pre_wallet` | unknown | **2** (PRl, EDGe) | **0** | **0** | n/a |
| `r1_wallet_session` | not proven fresh | **6** (PRl, EDGe, SPAce, VDOR, Bp, BLock) | **4** (PRl, EDGe, SPAce, BLock) | **2** (PRl, SPAce) | **EDGe, BLock** |

---

## 4. Prior-run / mixed evidence in `logfile` and `runtime`

1. **`logfile.log` lines 1–53** are **not** from the same wallet-bound process as lines 54+:
   - Different `cache_engine_init` id (4415129424 vs 4449301712).
   - **No** `Loading Solana keypair` before line 54.
   - **No** `runtime_cost` and **no** `entry_success` in 1–53.

2. **Same file** appends both segments → **one append-only log** can mix **two process starts** if the first exited and the second reused the log path.

3. **`journal.jsonl`** is a **single append-only stream** — all rows are **consistent with** `r1_wallet_session` because buys only occur after the keypair boundary in the log (no `entry_success` before line 54).

4. **`runtime/status.json`** reflects **only the latest cycle**, not historical runs — comparing it to older snapshots is **time-slice**, not “mixed run” corruption.

---

## 5. Direct answers

| Question | Answer |
|----------|--------|
| **Which run bought SPAce?** | **`r1_wallet_session`** — `entry_success` for SPAce mint appears **after** line 54 in `logfile.log`; journal BUY at `2026-03-25T01:37:15.596108+00:00`. |
| **Which run bought PRl / EDGe (currently open)?** | **PRl** was bought in **`r1_wallet_session`** — now **closed**; **EDGe** is **still open** in `state.json` and was bought in **`r1_wallet_session`** (`entry_success` at log lines **114–117**). |
| **Which run sold PRl?** | **`r1_wallet_session`** — `exit_success` in log (e.g. line **22444**); journal `SELL` at `2026-03-25T01:36:30.596570+00:00`. |
| **Are current open positions from the same run as SPAce?** | **Yes** — SPAce buy/sell and current opens (EDGe, BLock) all appear in **`journal.jsonl`** after the single **Loading Solana keypair** boundary in the log; SPAce is **closed**; EDGe and BLock are **open** from the **same `r1_wallet_session`**.

---

## Appendix: log line references (`entry_success` / `exit_success`)

| Event | Approx log line | Mint |
|--------|-----------------|------|
| `entry_success` | 114 | PRl |
| `entry_success` | 117 | EDGe |
| `entry_success` | 22525 | SPAce |
| `entry_success` | 46044 | BLock |
| `exit_success` | 22444 | PRl |
| `exit_success` | 45970 | SPAce |

*(Exact line numbers drift if `logfile.log` grows; JSON records SHA-256 and line count at generation.)*
