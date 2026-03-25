# Handoff pack: 2026-03-25 freeze

One-page index for continuation work after the **trading checkpoint freeze**.

| File | Purpose |
|------|---------|
| [TODAY_SUMMARY.md](./TODAY_SUMMARY.md) | What happened today (topics, outcomes, final state) |
| [CURRENT_SYSTEM_STATE.md](./CURRENT_SYSTEM_STATE.md) | Frozen system description: working, frozen, deferred |
| [OPEN_TASKS.md](./OPEN_TASKS.md) | Actionable todos for a coworker (priorities + status) |
| [TODAY_CHAT_CONTEXT.md](./TODAY_CHAT_CONTEXT.md) | Per-topic summary: discovery, decision, frozen vs follow-up |
| [KNOWN_ISSUES.md](./KNOWN_ISSUES.md) | Pre-existing test failures and known gaps (not fixed in freeze) |
| [DISCUSSED_TOPICS.md](./DISCUSSED_TOPICS.md) | Required A–G coverage (today vs repo-only, discovery/CU sequence) |

**Scope:** Today’s work only (Birdeye cost path, venv guard, audit CLI, discovery pipeline, conditional enrichment, backlog notes). No new architecture beyond what is already merged.

**Do not commit:** `runtime/`, logs, `.env`, caches, or local junk.
