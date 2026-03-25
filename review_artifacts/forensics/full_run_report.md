# Full forensic report (evidence-first)

**Generated:** see `full_run_report.json` → `generated_at_utc`  
**Scope:** Reconstruction from `runtime/logfile.log`, `runtime/journal.jsonl`, `runtime/state.json`, `runtime/status.json`, `runtime/entry_probe_*.json`, `review_artifacts/runtime_snapshots/*`, and `tools/runtime_snapshot_monitor.py`. No code was modified; runtime was not cleaned; bot was not restarted for this report.

**Log file integrity:** `runtime/logfile.log` SHA-256 is recorded in `full_run_report.json` under `sources.runtime_logfile_log.sha256`.

---

## 1. Run timeline

### Session A — partial; no `Loading Solana keypair`; no `runtime_cost:` in this prefix

| Field | Evidence |
|--------|-----------|
| Start | `runtime/logfile.log` **line 1:** `2026-03-24 23:32:51,859` — `cache_engine_init: candidate_cache_id=4415129424` |
| End (boundary) | **Line 53** last line before wallet load; **line 54** begins next session |
| `run` vs `run --once` | **Not directly logged** (argv not in log). This segment does **not** contain a `runtime_cost:` line (first `runtime_cost:` is **line 121**). |

### Session B — wallet loaded; same log file continues

| Field | Evidence |
|--------|-----------|
| Start | **Line 54:** `Loading Solana keypair from path: .../wallet_3LEZ.json` |
| Engine | **Line 55:** `cache_engine_init: candidate_cache_id=4449301712 route_cache_id=4450897104` |
| First completed cycle with CLI `runtime_cost` | **Line 121:** `runtime_cost: cache_hits=0 cache_misses=16 ...` |
| End | **Unknown / open** — log continues to last line; no graceful shutdown line cited in this file |

### Cycle count (observed in log)

| Metric | Value | Evidence |
|--------|-------|----------|
| Lines matching `runtime_cost:` from `creeper_dripper.cli.main` | **see `full_run_report.json` → `cycles.runtime_cost_lines_total`** (value captured at `generated_at_utc`) | `grep -c 'runtime_cost:' runtime/logfile.log` at same instant |
| Lines `Loading Solana keypair` | **1** | Line 54 only |

### `run` vs `run --once` (inferential)

- **Direct proof of argv:** absent from logs.
- **N** post-start `runtime_cost` lines for one wallet-loaded session implies **N** post-init cycles through the main loop (each cycle prints one `runtime_cost` after `engine.run_cycle()` returns — see §5). **N** is `cycles.runtime_cost_lines_total` in `full_run_report.json`.
- **`run --once`** sets `requested_cycles = 1` and exits after one iteration (`creeper_dripper/cli/main.py` — see §5 code reference). **Large N contradicts a single `--once` invocation** unless the process was restarted many times without appending new logs (the log is a single continuous file with one keypair load).

**Verdict (evidence-based):** The long-running segment is **consistent with `creeper-dripper run` (continuous loop)** after line 54, **not** with a single `run --once`.

---

## 2. Token timeline

Timestamps below: **log lines use local wall-clock** as printed; **journal/state use UTC ISO** where cited.

### Symbols / mints with `candidate_accepted` (first occurrence in log)

| Mint | Symbol | First `candidate_accepted` log time | Line (approx) |
|------|--------|-------------------------------------|----------------|
| `J7MzyZ4Tvwn2LBREnLU48TmKxJE28qstv35dRGBJPCpE` | PRl | 2026-03-24 23:33:35,233 | (first session; pre-keypair) |
| `5xzHELN3QZuQSm1wEejSkAha1hnxHr6KPn9uGz3dR2MA` | EDGe | 2026-03-24 23:33:35,456 | (first session) |
| `Bi1MG6rpHTA1p5UkV6qVZPZMBG4FEv7PLX6EXAm6hpht` | SPAce | 2026-03-25 01:03:39,296 | |
| `7JT1bKtTi6sSAB6bkTfFCyiPmYnZxv372EmaaFKB63A` | VDOR | 2026-03-25 02:42:39,168 | |
| `2Jno5wyrbQihZjJ4jFg7fE4pj5HsuSUujhmWYA9f7mXn` | Bp | 2026-03-25 07:03:02,025 | |

### Per-token narrative (evidence only)

#### PRl — `J7MzyZ4Tvwn2LBREnLU48TmKxJE28qstv35dRGBJPCpE`

- **built:** many `event=candidate_built` lines; e.g. discovery in session B starting ~line 57.
- **accepted:** `candidate_accepted` (see table).
- **entry_probe artifact:** `runtime/entry_probe_PRl_20260324T213630620169_0000.json` — `timestamp` **2026-03-24T21:36:30.620169+00:00** (file on disk).
- **entry_success (log):** `event=entry_success` **2026-03-24 23:37:19,898** — `token_mint` PRl, `qty_atomic` 20998417671 (`runtime/logfile.log`).
- **position_opened:** `state.json` → was open; now in **`closed_positions`** with `status` **CLOSED**, `opened_at` **2026-03-24T21:36:30.620169+00:00**, `updated_at` **2026-03-25T01:36:30.596570+00:00**.
- **sold / exit_success:** `runtime/journal.jsonl` line with `"action": "SELL"`, `"reason": "exit_success"`, `"metadata": {"out_sol": 0.064337191, ...}`; `state.json` `last_sell_signature` **RsQ3wuQ6...**; log `event=exit_success` **2026-03-25 03:36:44,686** (local) — **line ~22444** in `logfile.log`.

#### EDGe — `5xzHELN3QZuQSm1wEejSkAha1hnxHr6KPn9uGz3dR2MA`

- **entry_success (log):** **2026-03-24 23:37:21,003** — `qty_atomic` 19680503596.
- **entry_probe:** `runtime/entry_probe_EDGe_20260324T213630620169_0000.json`.
- **position:** `state.json` → `open_positions` — `status` **OPEN**, `opened_at` **2026-03-24T21:36:30.620169+00:00** (same wall-clock bucket as PRl in UTC terms; log local differs).

#### SPAce — `Bi1MG6rpHTA1p5UkV6qVZPZMBG4FEv7PLX6EXAm6hpht`

- **accepted:** table above.
- **entry_success (log):** **2026-03-25 03:37:26,046** — `token_mint` Bi1M..., `qty_atomic` 28094457511.
- **entry_probe:** `runtime/entry_probe_SPAce_20260325T013715596108_0000.json`.
- **journal BUY:** ts **2026-03-25T01:37:15.596108+00:00** (`journal.jsonl`).
- **position:** `state.json` → `open_positions` — `status` **OPEN**.

#### VDOR — `7JT1bKtTi6sSAB6bkTfFCyiPmYnZxv372EmaaFKB63A`

- **accepted:** **2026-03-25 02:42:39,168** (log).
- **entry_success:** **no** line in `grep event=entry_success` for this mint (only three mints in `entry_success` set — see §3).
- **state:** **no** position for this mint in `state.json` (as of report generation).

#### Bp — `2Jno5wyrbQihZjJ4jFg7fE4pj5HsuSUujhmWYA9f7mXn`

- **accepted:** **2026-03-25 07:03:02,025** (log).
- **entry_success:** **no** matching line.
- **state:** **no** position for this mint.

### “Only probed” (operational definition used here)

- **`event=candidate_built` with `reason=ok`:** **390** distinct mints (first-seen index in `full_run_report.json` → `tokens.candidate_built_ok_unique_mints`).
- **Not exhaustive per-token narrative** — most mints only appear in discovery/prefilter flows and never reach `candidate_accepted` or `entry_success`.

---

## 3. Actual buys only (proof: `entry_success` + journal + state)

| Mint | Symbol | Evidence |
|------|--------|----------|
| `J7MzyZ4Tvwn2LBREnLU48TmKxJE28qstv35dRGBJPCpE` | PRl | Log `event=entry_success`; `journal.jsonl` `BUY` with signature `5iPvs2rM3noB8...`; `state.json` closed position with `entry_sol` 0.06 |
| `5xzHELN3QZuQSm1wEejSkAha1hnxHr6KPn9uGz3dR2MA` | EDGe | Log `event=entry_success`; `journal.jsonl` `BUY`; `state.json` open position |
| `Bi1MG6rpHTA1p5UkV6qVZPZMBG4FEv7PLX6EXAm6hpht` | SPAce | Log `event=entry_success`; `journal.jsonl` `BUY`; `state.json` open position |

**Raw `runtime/journal.jsonl` (complete file):**

```json
{"ts": "2026-03-24T21:36:30.620169+00:00", "action": "BUY", "token_mint": "J7MzyZ4Tvwn2LBREnLU48TmKxJE28qstv35dRGBJPCpE", "symbol": "PRl", "reason": "discovery_entry", "qty_atomic": 20998417671, "qty_ui": 20998.417671, "size_sol": 0.06, "metadata": {"score": 79.36, "price_impact_bps": 0.0, "signature": "5iPvs2rM3noB8887cGkXjJhnEXcEqbjYkacXnzEUz3wFWejQp6dbtZfjUAWWFM1t1UePjvyTitZSgXxbEV5aJuut"}}
{"ts": "2026-03-24T21:36:30.620169+00:00", "action": "BUY", "token_mint": "5xzHELN3QZuQSm1wEejSkAha1hnxHr6KPn9uGz3dR2MA", "symbol": "EDGe", "reason": "discovery_entry", "qty_atomic": 19680503596, "qty_ui": 19680.503596, "size_sol": 0.06, "metadata": {"score": 79.28, "price_impact_bps": 4.081722703766769, "signature": "3GcoZm6wonYdyqp53ARnTQuJ2z5SRqVFfAoeZNgguK7UgwZhU3YzWiTHrzSrgwQeYitUamG8xp8fPPBEEq9376N4"}}
{"ts": "2026-03-25T01:36:30.596570+00:00", "action": "EXIT_PENDING", "token_mint": "J7MzyZ4Tvwn2LBREnLU48TmKxJE28qstv35dRGBJPCpE", "symbol": "PRl", "reason": "time_stop", "qty_atomic": 20998417671, "qty_ui": null, "size_sol": null, "metadata": {}}
{"ts": "2026-03-25T01:36:30.596570+00:00", "action": "SELL_ATTEMPT", "token_mint": "J7MzyZ4Tvwn2LBREnLU48TmKxJE28qstv35dRGBJPCpE", "symbol": "PRl", "reason": "time_stop", "qty_atomic": 20998417671, "qty_ui": null, "size_sol": null, "metadata": {"status": "success", "requested_amount": 20998417671, "executed_amount": 20998417671, "signature": "RsQ3wuQ6DKenHtMe93wKxYcMi9KqyuQuz33cNXRW8Bt3JrGH7NBPkHgfXMbiJ32KcBCa5UoKQe1BbtFPScaaTmA", "error": null, "price_impact_bps": 4.986239713201456, "classification": "tx_confirmed_success", "jupiter_error_code": null}}
{"ts": "2026-03-25T01:36:30.596570+00:00", "action": "SELL", "token_mint": "J7MzyZ4Tvwn2LBREnLU48TmKxJE28qstv35dRGBJPCpE", "symbol": "PRl", "reason": "exit_success", "qty_atomic": 20998417671, "qty_ui": 20998.417671, "size_sol": null, "metadata": {"out_sol": 0.064337191, "signature": "RsQ3wuQ6DKenHtMe93wKxYcMi9KqyuQuz33cNXRW8Bt3JrGH7NBPkHgfXMbiJ32KcBCa5UoKQe1BbtFPScaaTmA", "partial": false, "proceeds_pending_reconcile": false, "post_sell_settlement": {"mint": "J7MzyZ4Tvwn2LBREnLU48TmKxJE28qstv35dRGBJPCpE", "signature": "RsQ3wuQ6DKenHtMe93wKxYcMi9KqyuQuz33cNXRW8Bt3JrGH7NBPkHgfXMbiJ32KcBCa5UoKQe1BbtFPScaaTmA", "requested_sell_qty": 20998417671, "jupiter_execute_in_atomic": 20998417671, "jupiter_execute_out_lamports": 64337191, "order_in_atomic": 20998417671, "order_out_sol_hint": 64337191, "quote_in_atomic": 20998417671, "quote_out_sol_atomic": 64401592, "pre_wallet_token_atomic": 20998417671, "sold_atomic_settled": 20998417671, "sold_atomic_source": "jupiter_execute", "out_lamports": 64337191, "proceeds_source": "jupiter_execute", "wallet_post_unavailable": true, "settlement_confirmed": true}, "sold_atomic_settled": 20998417671, "remaining_after_sell_atomic": 0}}
{"ts": "2026-03-25T01:37:15.596108+00:00", "action": "BUY", "token_mint": "Bi1MG6rpHTA1p5UkV6qVZPZMBG4FEv7PLX6EXAm6hpht", "symbol": "SPAce", "reason": "discovery_entry", "qty_atomic": 28094457511, "qty_ui": 28094.457511, "size_sol": 0.06, "metadata": {"score": 79.76, "price_impact_bps": 2.0599927959306563, "signature": "3Zm6HcnoRzLSm8GwgnDojv8JdYxyf8fDgzVs9zoAWcMHJKhcnHJjdwTfQgHNYo2Zd4JqqiBbesnR88ioqCwPcXeQ"}}
```

---

## 4. Actual sells only (proof: journal + log + state)

| Mint | Symbol | Proceeds (known) | Evidence |
|------|--------|-------------------|----------|
| `J7MzyZ4Tvwn2LBREnLU48TmKxJE28qstv35dRGBJPCpE` | PRl | **0.064337191 SOL** (`metadata.out_sol`) | `journal.jsonl` `SELL` `exit_success`; `state.json` `closed_positions[0].realized_sol` **0.064337191**; log `event=exit_success` |

**No other `SELL` / `exit_success` records** in `journal.jsonl` (6 lines total).

---

## 5. Cycle semantics (code)

### What a “cycle” is

- **`Trader.run_cycle()`** sets `now = utc_now_iso()`, runs discovery, safety, opens, marks, builds `cycle_summary`, then **`_persist_cycle(now, ...)`** which writes `state.json`, appends `journal.jsonl` for decisions, and writes **`status.json`** with **`cycle_timestamp`: `now`** (`creeper_dripper/engine/trader.py`).

```135:174:creeper_dripper/engine/trader.py
    def run_cycle(self) -> dict:
        now = utc_now_iso()
        self._reset_daily_counters(now)
        decisions: list[TradeDecision] = []
        ...
        self.portfolio.last_cycle_at = now
        cycle_summary = self._cycle_summary(now, discovery_summary, decisions)
        self._persist_cycle(now, decisions, cycle_summary)
```

```1211:1240:creeper_dripper/engine/trader.py
    def _persist_cycle(self, now: str, decisions: list[TradeDecision], cycle_summary: dict) -> None:
        ...
        status_path = self.settings.runtime_dir / "status.json"
        try:
            ...
            save_status_snapshot(
                status_path,
                {
                    "cycle_timestamp": now,
                    "safe_mode_active": self.portfolio.safe_mode_active,
                    ...
                },
            )
```

- **`save_status_snapshot`** writes JSON atomically (`creeper_dripper/storage/state.py`).

```173:174:creeper_dripper/storage/state.py
def save_status_snapshot(path: Path, payload: dict) -> None:
    atomic_write_json(path, payload)
```

### CLI loop (main process)

- **`cmd_run`** increments `cycles`, calls `engine.run_cycle()`, logs `runtime_cost`, sleeps until `poll_interval` unless stop (`creeper_dripper/cli/main.py`).

```237:261:creeper_dripper/cli/main.py
    requested_cycles = 1 if args.once else (max(1, int(args.cycles)) if args.cycles is not None else None)
    ...
    while not STOP:
        cycles += 1
        ...
        summary = engine.run_cycle()
```

```407:410:creeper_dripper/cli/main.py
        if requested_cycles is not None and cycles >= requested_cycles:
            break
        next_run += settings.poll_interval_seconds
        monotonic_sleep_until(next_run)
```

### Monitor script

- **`tools/runtime_snapshot_monitor.py`** — **polls** `runtime/status.json` field **`cycle_timestamp`**; does **not** import the trading engine or call `run_cycle`. It only reads files and optionally runs git commands.

**Answer — “Is the monitor doing run --once in a loop?”**  
**No.** Evidence: the monitor script contains **no** `run_cycle`, **no** subprocess invoking `creeper-dripper`, and **no** `--once` string (`grep` over `tools/runtime_snapshot_monitor.py`). It **observes** `status.json` only.

---

## 6. Inconsistencies

| # | Where | What | Notes |
|---|--------|------|------|
| 1 | `runtime/status.json` `summary.entries_succeeded` vs `state.json` historical positions | Last-cycle summary shows `entries_succeeded: 0` while portfolio holds positions opened earlier | **Expected:** summary is **per last cycle**, not lifetime (see `trader._cycle_summary` / CLI aggregation). |
| 2 | `runtime/logfile.log` timestamps vs `journal.jsonl` `ts` | Log prefix is **local**; journal is **UTC ISO** | Not a contradiction once timezone accounted for. |
| 3 | First log segment (lines 1–53) vs line 121 first `runtime_cost` | Discovery and `candidate_accepted` appear before keypair load; no `runtime_cost` until after restart | Indicates **process boundary** / incomplete first segment logging, not necessarily “mixed state” in one running engine. |
| 4 | `review_artifacts/runtime_snapshots/*` vs live `runtime/` | Snapshots are **point-in-time copies** committed to git; older folders can differ from current `state.json` | **Expected** for time-series artifacts. |

**No evidence in cited files** of `state.json` parse errors, `STATE_SAVE_FAILED` in this log slice, or journal lines contradicting `entry_success` for the three BUY mints.

---

## 7. Final verdict (answers)

| Question | Answer |
|----------|--------|
| Accidental **`run --once` in a loop** by the **monitor**? | **No** — monitor only reads `status.json` (see §5). |
| Accidental **`run --once` loop** by the **bot process**? | **No evidence** of an external script re-invoking `creeper-dripper run --once` in a loop; log shows **one** keypair load and **599** `runtime_cost` lines → **consistent with continuous `run`**. |
| State corruption? | **No positive evidence** in `journal.jsonl` vs `state.json` for the three buys and one sell; `version` 2 in state. |
| Mixed artifacts from different sessions? | **Yes, in one sense:** `logfile.log` **appends** a **partial first process** (no keypair line) and a **second process** (keypair + main loop). **Snapshot folders** are **different commit times**, not one mixed JSON file. |
| What is wrong, if anything? | **Not stated as a defect:** Session A did not produce `runtime_cost` before Session B started — **consistent with restart** after discovery-only / incomplete first run. |

---

## Appendix A — `review_artifacts/runtime_snapshots/` (count)

**12** timestamped folders under `review_artifacts/runtime_snapshots/` (exact names in `full_run_report.json`).

## Appendix B — Git (recent)

See `full_run_report.json` → `git.recent_commits` for hashes and messages (snapshot automation on `live-jsds-test`).
