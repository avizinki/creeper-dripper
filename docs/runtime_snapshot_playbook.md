# Runtime snapshot playbook

## Purpose

Capture **safe** runtime state for **external review** (GitHub, auditors, teammates) without exposing secrets or disrupting a live process.

## Include (copy into the snapshot folder)

- `state.json` — portfolio / positions (sanitized)
- `status.json` — last cycle summary snapshot
- `scan_latest.json`, `scan_summary.json` — last discovery scan outputs
- **Latest** `entry_probe_PRl_*.json` and `entry_probe_EDGe_*.json` (or your tracked symbols), from:
  - `runtime/artifacts/` **if present**, else
  - `runtime/` (project layout may vary)
- `log_excerpt.txt` — **short** tail from `runtime/logfile.log` (see below)

## Never include

- `.env` or any env file with real keys
- Wallet JSON / private keys / mnemonics
- Full `logfile.log` or entire `journal.jsonl` (too noisy; may leak paths or tx detail you did not intend)

## Sanitization

Before commit, replace any absolute local path, e.g. `/Users/...`, with:

`<local-path-redacted>`

Re-scan the snapshot folder: `grep -R '/Users/' review_artifacts/runtime_snapshots/<ts>/` must return nothing.

## Log excerpt

- Take roughly **150–300 lines** from the **end** of `runtime/logfile.log`
- Prefer lines showing: valuation (`position_valuation_sol`), drip-related actions if any (`DRIP_*` trade decisions), liquidity/JSDS watch lines if any
- Apply the same path redaction to the excerpt

## Steps (repeatable)

1. Choose UTC folder name: `YYYY-MM-DDTHH-MM-SSZ` (filesystem-safe; hyphens in time).
2. Create: `review_artifacts/runtime_snapshots/<timestamp>/`
3. Copy the JSON files listed above from `runtime/` (and probes from `runtime/artifacts/` or `runtime/`).
4. Build `log_excerpt.txt` from the tail of `runtime/logfile.log`; sanitize paths.
5. Add `README.md` in that folder: branch, `git rev-parse HEAD`, snapshot time, `DRIP_EXIT_ENABLED` from **local** `.env` (do not commit `.env`), open position count from `state.json`, one-line run summary.
6. Sanitize all files; verify no `/Users/` left.
7. Commit: `chore: add runtime snapshot <timestamp> for live drip monitoring`
8. Push branch (e.g. `live-jsds-test`).

## Naming

```
review_artifacts/runtime_snapshots/<ISO_UTC_TIMESTAMP>/
```

Example: `review_artifacts/runtime_snapshots/2026-03-24T22-05-00Z/`

## Notes

- Does **not** require stopping the bot or cleaning `runtime/`.
- Does **not** change trading logic; documentation + artifacts only.
