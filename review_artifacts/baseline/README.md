# Public baseline snapshot (no secrets)

Safe, review-only artifacts from a **local** `doctor` + `scan` run used to validate config and connectivity before a live test.

## Git context (at capture time)

| Field | Value |
|--------|--------|
| Branch | `live-jsds-test` |
| Commit (HEAD when captured) | `d05f6f25e3f4e6dddd2b34b2326feee80abbaa6a` |
| Baseline run (approx.) | `2026-03-24` — `runtime/` cleaned, then `doctor` + `scan` in sequence |

## Local runtime mode (from `doctor.json`)

- `dry_run`: **false**
- `live_trading_enabled`: **true**
- `safe_mode_active`: **false**
- Wallet path in committed `doctor.json` is **redacted**; set `SOLANA_KEYPAIR_PATH` only in your local `.env` (never commit `.env`).

## Drip exit (local `.env` for next run)

For the intended live test, the operator **local** `.env` should include (non-secret toggles only):

- `DRIP_EXIT_ENABLED=true`
- `DRIP_CHUNK_PCTS=0.10,0.25,0.50`
- `DRIP_NEAR_EQUAL_BAND=0.002`
- `DRIP_MIN_CHUNK_WAIT_SECONDS=30`

Defaults and documentation: root `.env.example` and `README.md`.

## Scan summary (from `scan_summary.json`)

- **Accepted total:** 2  
- **Accepted symbols:** PRl, EDGe  
- **Seeds total:** 20  
- **Built:** 11  
- Rejection breakdown: see `scan_summary.json` → `rejection_counts`.

## Files in this directory

| File | Purpose |
|------|---------|
| `doctor.json` | Health check JSON (wallet path redacted) |
| `scan_latest.json` | Last scan candidate payload (public mints/symbols only) |
| `scan_summary.json` | Counts and rejection histogram |
| `scan_log_excerpt.txt` | Short INFO lines (no keys); optional context |

## Not included

- `.env`, private keys, wallet JSON  
- `state.json`, full `journal.jsonl`  
- Full `runtime/logfile.log`
