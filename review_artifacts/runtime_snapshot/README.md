# Runtime snapshot (external review)

**Runtime snapshot for external review** — sanitized copies of selected `runtime/` files and a short log excerpt. No secrets, no `.env`, no wallet key material.

| Field | Value |
|--------|--------|
| Branch | `live-jsds-test` |
| HEAD (at snapshot packaging) | `b97a0376cae0aa43afe908b1ee095fcecdb5c311` |
| Snapshot timestamp (UTC) | `2026-03-24T21:44:12Z` |
| Drip exit enabled (operator local `.env`) | yes — `DRIP_EXIT_ENABLED=true` with standard chunk settings (see root `.env.example`) |
| Open positions (from `state.json`) | **2** (symbols PRl, EDGe) |

## Contents

- `state.json`, `status.json`, `scan_latest.json`, `scan_summary.json` — absolute filesystem paths redacted to `<local-path-redacted>` where present.
- `entry_probe_PRl_*.json`, `entry_probe_EDGe_*.json` — latest matching probes from the live session.
- `log_excerpt.txt` — startup, `entry_success`, `position_valuation_sol`, sample `liquidity_deterioration_watch` (JSDS); wallet path line redacted.

## Not included

Full `logfile.log`, `journal.jsonl`, private keys, or raw `.env`.
