# T-003: Wallet vs State Reconciliation Audit
**Task:** CRIT-accounting-reconciliation-audit / T-003
**Mode:** TRACE_ONLY — no code changes
**Date:** 2026-03-26
**Produced by:** Claude (ARCHITECT agent)

---

## 1. What We Are Auditing

`cash_sol` is the field in `PortfolioState` (models.py:178) that represents the system's internal belief about how much SOL is available for trading. The question is: **why does this diverge from `wallet_available_sol` (the real on-chain balance read via RPC)?**

This document maps every site where `cash_sol` is read, written, or used as a proxy; every site where the real wallet balance (`_wallet_available_sol`) is read; and identifies all the structural gaps between the two that cause drift.

---

## 2. The Two Parallel Accounting Systems

The system runs **two separate, unlinked accounting ledgers**. This is the core architectural fact.

### System A: `cash_sol` (internal ledger)

- **Lives in:** `PortfolioState.cash_sol` (models.py:178)
- **Persisted in:** `state.json` via `save_portfolio()` after every cycle
- **Initial value:** `PORTFOLIO_START_SOL` env var (default 5.0 SOL) at first boot; loaded from state.json on subsequent restarts
- **Updated by:** only the trader engine's buy and sell accounting code
- **Never reset** to the real wallet balance during a running session

### System B: `_wallet_available_sol` (visibility snapshot)

- **Lives in:** `CreeperDripper._wallet_available_sol` (trader.py:187)
- **Source:** Solana RPC `getBalance` call via `executor.native_sol_balance_lamports()`
- **Set once at startup** in `cmd_run()` (main.py:934): `engine.set_wallet_snapshot(available_sol=..., snapshot_at=now)`
- **Never refreshed** during the run loop — it is seeded once and then held in memory unchanged
- **Used for:** dynamic capacity decisions (`_effective_max_open_positions`, `_effective_max_daily_new_positions`) and cycle summary observability only
- **Comment in code:** explicitly labeled "visibility/bootstrap only; never settlement truth"

**Critical implication:** Once the run starts, `_wallet_available_sol` is a stale snapshot from startup. It is never updated again. After a few trades, it diverges from both `cash_sol` and the real wallet.

---

## 3. Complete `cash_sol` Mutation Map

### 3.1 Initialization (on startup / new portfolio)

| Site | File | Line | Operation | Value |
|------|------|-------|-----------|-------|
| `new_portfolio(initial_cash_sol)` | storage/state.py:112 | 114 | Write | `PORTFOLIO_START_SOL` env (default 5.0) |
| `load_portfolio(path, initial_cash_sol)` | storage/state.py:128 | 150 | Write | From JSON `"cash_sol"` field, or fallback to `initial_cash_sol` |

**Boot sequence in `cmd_run`:**
1. `load_portfolio(settings.state_path, settings.portfolio_start_sol)` → `cash_sol` loaded from disk or seeded from env
2. `engine.set_wallet_snapshot(available_sol=rpc_balance, snapshot_at=now)` → `_wallet_available_sol` set once
3. These two values are **never reconciled**. If the state file has a stale `cash_sol` from a previous session and the real wallet has moved (due to external transfers, slippage accumulation, or failed tx costs), they are already diverged at line 1.

### 3.2 Buy Entry — Debit (trader.py)

Two code paths both execute:
```
self.portfolio.cash_sol -= size_sol    # line 1941: settlement_unconfirmed path
self.portfolio.cash_sol -= size_sol    # line 2042: successful buy path
```

**What `size_sol` is:**
The requested entry size in SOL (e.g. `base_position_size_sol` = 0.06). This is the **intended** SOL amount, not the actual on-chain amount consumed. In practice, Jupiter may execute the swap at slightly different actual lamport cost due to:
- Slippage (up to `DEFAULT_SLIPPAGE_BPS` = 250bps)
- Network fees (transaction fees paid in SOL, not accounted in `size_sol`)
- Partial fill (if `is_partial`, fewer tokens received but same SOL spent)

**Drift introduced by buy:**
`cash_sol` decreases by exactly `size_sol`. The real wallet decreases by `size_sol + tx_fee`. Transaction fees (typically ~5,000 lamports per tx = 0.000005 SOL) are never deducted from `cash_sol`. Over many trades, these accumulate.

Additionally, `executor.buy()` returns `ExecutionResult.output_amount = None` for buys (line 231 in executor.py — `output_amount=None` explicitly). So there is no SOL-spent reconciliation on the buy side. The system always assumes exactly `size_sol` was spent.

### 3.3 Sell Exit — Credit (trader.py:1530–1533)

```python
out_sol = None
if result.output_amount is not None:
    out_sol = max(0.0, float(result.output_amount) / 1_000_000_000.0)
    position.realized_sol += out_sol
    self.portfolio.cash_sol += out_sol
else:
    # SOL proceeds unavailable — do NOT credit cash
```

**`result.output_amount` comes from:** `_settle_sell_after_execute()` → `jup_out_lamports` from Jupiter's execute response fields `totalOutputAmount` / `outputAmount` / `outAmount`. This is the actual lamports received in the wallet.

**When `result.output_amount is None`:** The sell was executed (`settlement_confirmed=True`), tokens were burned, but Jupiter did not return a lamports figure. In this case, `cash_sol` is **not credited** — it stays lower. The real wallet received SOL, but the internal ledger doesn't know.

This is a known single-direction accumulating leak: each occurrence makes `cash_sol` pessimistically lower than reality. The code comment documents this as intentional (visibility concern) but it is a source of drift.

### 3.4 Settlement/RECONCILE_PENDING paths — No `cash_sol` change

When a sell goes to `POSITION_RECONCILE_PENDING` (lines 1496, 1561, 1661):
- `cash_sol` is **not updated**
- The sell result may have moved real SOL into the wallet
- `cash_sol` remains at pre-sell level until the position is properly settled

When `result.status == "success"` but settlement metadata is missing (lines 1488–1514):
- Position goes to `RECONCILE_PENDING`
- `cash_sol` is **not credited**
- Real wallet already has the SOL

### 3.5 Partial exits — Correct but cumulative

For partial sells (drip chunks), each successful chunk correctly credits `cash_sol` via the sell path above — but only when `output_amount is not None`. Partials with missing lamport data accumulate the same drift as full-sell missing-proceeds cases.

### 3.6 Position close — `total_realized_sol` accounting (separate ledger)

```python
self.portfolio.total_realized_sol += position.realized_sol - position.entry_sol   # line 1650
```

`total_realized_sol` is a separate tracking field and does **not affect `cash_sol`**. It is a PnL tally, not a wallet balance field.

### 3.7 `cash_sol` used as fallback for `_wallet_available_sol`

In three capacity-decision locations (trader.py:223, 259, 2224):
```python
available_sol = self._wallet_available_sol
if available_sol is None:
    available_sol = float(self.portfolio.cash_sol)
```

And in main.py (lines 402, 308) for doctor/capacity display:
```python
av = float(pf.cash_sol)
```

**This means:** when the startup RPC call fails (RPC unreachable), the system falls back to `cash_sol` for all capacity decisions. If `cash_sol` is stale or wrong, capacity limits are computed from the wrong number.

---

## 4. Lifecycle Trace: Full Sequence

```
STARTUP
├── load_portfolio() → cash_sol = persisted value (from last session or PORTFOLIO_START_SOL)
├── RPC getBalance → _wallet_available_sol = real wallet NOW (one snapshot, never refreshed)
│   ├── If RPC fails → _wallet_available_sol = None → cash_sol used as fallback everywhere
│   └── _wallet_available_sol is stale by next cycle
│
CYCLE N (run_cycle)
├── _startup_recovery_done check → run_startup_recovery()
├── _evaluate_exit_rules() → identify exits
│   └── _attempt_exit() on triggered positions
│       ├── SUCCESS + output_amount present → cash_sol += lamports/1e9  [CORRECT]
│       ├── SUCCESS + output_amount missing → cash_sol unchanged         [DRIFT: real wallet +X]
│       ├── SETTLEMENT_UNCONFIRMED → cash_sol unchanged                  [DRIFT: real wallet +X if tx landed]
│       ├── FAILED → cash_sol unchanged, position → EXIT_BLOCKED         [CORRECT: no SOL moved]
│       └── PARTIAL chunk → same as above, per chunk
├── _maybe_open_positions() → if entry triggered
│   ├── SKIPPED (dry_run/live_disabled) → cash_sol unchanged            [CORRECT]
│   ├── FAILED (route/sanity) → cash_sol unchanged                      [CORRECT]
│   ├── SETTLEMENT_UNCONFIRMED → cash_sol -= size_sol                   [RISK: real wallet -X but maybe tx failed]
│   └── SUCCESS → cash_sol -= size_sol                                  [APPROX: real cost = size_sol + tx_fee]
├── _persist_cycle() → save_portfolio() → state.json written with current cash_sol
└── NO wallet balance re-read in cycle; _wallet_available_sol unchanged since startup

RESTART
├── load_portfolio() → cash_sol = whatever was last saved
│   ├── If it accumulated drift, that drift is now the new baseline
│   └── PORTFOLIO_START_SOL is only used if no state.json exists
└── _wallet_available_sol = new RPC snapshot (one read, immediately diverges again)
```

---

## 5. Drift Pattern Analysis

### 5.1 Sources of Drift (Ranked by Likelihood and Magnitude)

**RANK 1 — HIGHEST LIKELIHOOD: Missing sell proceeds (output_amount = None)**

- **Mechanism:** Jupiter's execute response may not return `totalOutputAmount` / `outputAmount` / `outAmount`, or those fields are zero. This triggers `out_sol = None`, `cash_sol` is not credited.
- **Direction:** `cash_sol` drifts **lower** than real wallet
- **Magnitude:** Each occurrence = 1 missed credit of ~(position entry_sol * (1 + PnL%)). With 7 trades entered today (seen in doctor output), any successful exit without lamport data loses that credit permanently.
- **Detectability:** Journal entry has `"proceeds_pending_reconcile": true`. Searchable.
- **Accumulation:** Permanent. Survives restarts. Compounds each session.

**RANK 2 — HIGH LIKELIHOOD: Transaction fees not deducted from cash_sol**

- **Mechanism:** Each Jupiter swap consumes a Solana transaction fee (~5,000–25,000 lamports per tx, typically 0.000005–0.000025 SOL). These are never deducted from `cash_sol`. Real wallet SOL decreases by `size_sol + tx_fee`; `cash_sol` decreases by only `size_sol`.
- **Direction:** `cash_sol` drifts **higher** than real wallet
- **Magnitude:** ~0.00001 SOL per trade × N trades. Low per-trade but cumulative. 100 trades = ~0.001 SOL. Not dominant but real.
- **Detectability:** No logging of fee impact.
- **Note:** Partially offsets Rank 1 drift (they go opposite directions).

**RANK 3 — MEDIUM LIKELIHOOD: Settlement_unconfirmed buy with eventual on-chain failure**

- **Mechanism:** When `execution.status == "unknown" and diagnostic_code == SETTLEMENT_UNCONFIRMED` during a buy, `cash_sol -= size_sol` is executed (line 1941) and the position is opened as `RECONCILE_PENDING`. If the transaction later fails on-chain, the position will stay in RECONCILE_PENDING and eventually exit, but `cash_sol` was already debited. The real wallet never lost those SOL.
- **Direction:** `cash_sol` drifts **lower** than real wallet
- **Magnitude:** Full `size_sol` (0.06 SOL typically) per failed unconfirmed buy. Rare but high per-occurrence.
- **Detectability:** Journal shows `classification=settlement_unconfirmed` on BUY action. But no automatic reversal of the debit.

**RANK 4 — MEDIUM LIKELIHOOD: Stale `_wallet_available_sol` used for capacity**

- **Mechanism:** `_wallet_available_sol` is seeded once at startup via RPC. It is never refreshed. After buys and sells, the real wallet balance changes but `_wallet_available_sol` remains at the startup value. Capacity decisions (`_effective_max_open_positions`) and the `deployable_sol` metric in cycle summaries use this stale value.
- **Direction:** `deployable_sol` drifts from reality (could be higher or lower)
- **Magnitude:** After N cycles with entries, the stale snapshot could be 0.3–0.5 SOL off for an active session.
- **Detectability:** `wallet_snapshot_at` field in `entry_capacity_mode_summary` event shows the timestamp of the last (only) snapshot. If this is hours old, the capacity number is computed from stale data.
- **Note:** This does NOT affect `cash_sol` directly, but does affect how `cash_sol` is used as a fallback (if RPC failed at startup), making capacity decisions wrong.

**RANK 5 — LOWER LIKELIHOOD: Initial cash_sol misconfiguration on restart**

- **Mechanism:** If `state.json` is missing or corrupted, `new_portfolio(initial_cash_sol)` is called with `PORTFOLIO_START_SOL` (default 5.0). If the real wallet has less than 5.0 SOL, `cash_sol` starts higher than reality. All subsequent trades proceed under a wrong assumption about available capital.
- **Direction:** `cash_sol` drifts **higher** than real wallet
- **Magnitude:** Up to the full `PORTFOLIO_START_SOL` value (5.0 SOL by default)
- **Detectability:** Operator must notice the mismatch manually. No alert on startup.
- **Note:** Doctor (`cmd_doctor`) calls `load_portfolio()` and prints `cash_sol` but does not compare it to the RPC balance.

**RANK 6 — LOWER LIKELIHOOD: Slippage mismatch on buy**

- **Mechanism:** Buy probe gets `out_amount_atomic` at 0bps slippage; actual execution uses `DEFAULT_SLIPPAGE_BPS = 250bps`. The actual SOL spent could be slightly different from `size_sol` if Jupiter's managed execution adjusts amounts. Since `output_amount=None` on buys, the actual lamports consumed are never read back.
- **Direction:** `cash_sol` could drift slightly higher or lower
- **Magnitude:** Up to slippage_bps/10000 × size_sol ≈ 0.0015 SOL per trade. Small but real.

---

## 6. The Core Structural Gap

There is **no reconciliation loop** that ever synchronizes `cash_sol` with the real wallet balance. Specifically:

1. **No per-cycle wallet re-read.** `_wallet_available_sol` is set once at startup and never updated inside the run loop.
2. **No startup alignment.** At boot, `cash_sol` from state.json is loaded and `_wallet_available_sol` from RPC is loaded, but they are never compared, logged side-by-side, or reconciled.
3. **Asymmetric credit on sells.** Sells only credit `cash_sol` when `output_amount is not None`. There is no compensating read from the wallet when lamport data is unavailable.
4. **No debit reversal on failed-unconfirmed buys.** When a buy reports `SETTLEMENT_UNCONFIRMED` and the tx later fails, `cash_sol` was already decremented. No path exists to reverse that debit.
5. **`cash_sol` as a guard.** The `cash_sol` check at line 1801 (`if self.portfolio.cash_sol - size_sol < self.settings.cash_reserve_sol`) is the entry guard. If `cash_sol` has drifted low (Rank 1 scenario), legitimate entries get blocked. If it drifts high (Rank 5 scenario), entries proceed with overstated capital.

---

## 7. Observability Gap

The current system logs and events do **not** provide:
- A side-by-side of `cash_sol` vs `wallet_available_sol` per cycle
- A drift metric (`wallet_available_sol - cash_sol`)
- An alert when `output_amount is None` on an otherwise-successful sell
- A log of the initial `cash_sol` vs `_wallet_available_sol` comparison at startup

The `entry_capacity_mode_summary` event (emitted each cycle) contains both `cash_sol` and `wallet_available_sol` — but `wallet_available_sol` is the stale startup snapshot, not a fresh read. Parsing the journal for this event gives the appearance of per-cycle observability, but `wallet_available_sol` is constant throughout a session.

---

## 8. Simulation of Known Cases

### Case A: Normal buy + sell with lamport data
```
Before: cash_sol=1.000, real_wallet=1.000
Buy 0.06 SOL:
  cash_sol=0.940, real_wallet≈0.93999 (0.06 + tx_fee)
  [drift: cash_sol HIGH by ~0.00001]
Sell proceeds = 0.065 SOL, lamports received and reported:
  cash_sol=1.005, realized_sol += 0.065
  real_wallet≈1.00499
  [drift: cash_sol HIGH by ~0.00001 (fee)]
```

### Case B: Sell with missing lamport data (Rank 1)
```
Before: cash_sol=0.940, real_wallet≈0.93999
Sell executes on chain. Jupiter reports no outAmount.
  cash_sol=0.940 (unchanged)
  real_wallet≈0.940 + 0.065 = 1.00499
  [drift: cash_sol LOW by 0.065]
Survivor across restart: drift permanently encoded in state.json
```

### Case C: Unconfirmed buy that fails on chain (Rank 3)
```
Before: cash_sol=1.000, real_wallet=1.000
Buy submitted, SETTLEMENT_UNCONFIRMED:
  cash_sol=0.940 (debited)
  real_wallet=1.000 (tx failed, nothing moved)
  [drift: cash_sol LOW by 0.060]
Position stays RECONCILE_PENDING forever unless operator manually resolves.
```

### Case D: Zombie position with multiple sell retries
```
Position ZOMBIE, _attempt_exit called repeatedly.
Each sell attempt: status=failed → no cash_sol change.
[No drift introduced by zombie retries themselves — but accumulated drift from entry buy debit remains]
```

---

## 9. Root Cause Summary (Priority Order)

| Rank | Root Cause | Direction | Per-Occurrence | Cumulative | Survivable Across Restart |
|------|-----------|-----------|----------------|------------|--------------------------|
| 1 | `output_amount is None` on sell → no `cash_sol` credit | LOW | Full position exit value (0.04–0.15 SOL) | HIGH | YES |
| 2 | Tx fees not modeled in `cash_sol` debit | HIGH | ~0.00001 SOL | LOW | YES |
| 3 | Unconfirmed buy debit not reversed if tx fails | LOW | Full `size_sol` (0.06 SOL) | MEDIUM | YES |
| 4 | `_wallet_available_sol` never refreshed → stale capacity | Varies | Capacity metric only | MEDIUM | NO (reset on restart) |
| 5 | Wrong initial `cash_sol` if state.json missing | HIGH | Up to 5.0 SOL | HIGH (one-time) | YES (until fixed manually) |
| 6 | Slippage on buy not reconciled | Varies | <0.002 SOL | LOW | YES |

**Most likely current drift pattern:** Based on doctor output showing 7 entries today with 23 token positions visible in wallet, the dominant drift source is likely **Rank 1** (missing lamport data on some sells), compounded by **Rank 3** if any entries had unconfirmed-then-failed settlements. The `cash_sol` in the current session is likely **lower than the real wallet balance** by a few hundred mSOL.

---

## 10. Suggested Investigation Steps (for T-004)

1. At next doctor run: log `cash_sol`, `_wallet_available_sol` (re-read, not cached), and their difference.
2. Search journal for `"proceeds_pending_reconcile": true` — each occurrence is a Rank 1 drift event.
3. Search journal for BUY entries with `classification=settlement_unconfirmed` — each is a potential Rank 3 drift event.
4. Compare `PORTFOLIO_START_SOL` (1.0 SOL per env snapshot) against the sum of `entry_sol` across all closed+open positions + current `cash_sol` — the difference is total accumulated drift since the state was last fresh.
