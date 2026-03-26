from __future__ import annotations

import ast
import json
import os
import re
from collections import deque
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

# ---------------------------------------------------------------------------
# Dashboard-of-Truth: paths for supplemental runtime artifacts
# ---------------------------------------------------------------------------


def _accounting_snapshot_path() -> Path:
    return Path(
        os.environ.get("ACCOUNTING_SNAPSHOT_PATH", str(_runtime_dir() / "accounting_snapshot.json"))
    ).resolve()


def _birdeye_audit_summary_path() -> Path:
    return Path(
        os.environ.get("BIRDEYE_AUDIT_SUMMARY_PATH", str(_runtime_dir() / "birdeye_audit_summary.json"))
    ).resolve()


def _token_report_coverage_path() -> Path:
    return Path(
        os.environ.get("TOKEN_REPORT_COVERAGE_PATH", str(_runtime_dir() / "token_report_coverage.json"))
    ).resolve()


def _safe_load_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


# ---------------------------------------------------------------------------
# Birdeye budget mode derivation
# ---------------------------------------------------------------------------
# Thresholds: how many rate-limit log hits in the last LOG_SCAN_LINES lines
# constitute "constrained" vs "starved".
_BIRDEYE_RATE_STARVED_THRESHOLD = 20   # ≥20 hits in tail → starved
_BIRDEYE_RATE_CONSTRAINED_THRESHOLD = 3  # ≥3 hits in tail → constrained


def _derive_birdeye_budget_mode(
    *,
    discovery_failed: bool | None,
    discovery_error: str | None,
    birdeye_audit: dict | None,
    log_tail_lines: int = 3000,
) -> tuple[str, str]:
    """
    Return (birdeye_budget_mode, budget_reason_summary).

    Mode values:
      healthy     — no recent rate-limit signals, audit clean
      constrained — some rate-limit hits but discovery still ran
      starved     — discovery failed due to rate-limiting OR high hit count

    Sources consumed (cheapest-first):
      1. discovery_error string — direct "birdeye_rate_limited" signal
      2. logfile tail — count "birdeye_rate_limited" occurrences
      3. birdeye_audit endpoint bad-rate — from last audit run (stale but useful)
    """
    reasons: list[str] = []

    # ── 1. Direct discovery failure signal ───────────────────────────────
    disc_rate_limited = False
    if discovery_failed and discovery_error and "birdeye_rate_limited" in str(discovery_error).lower():
        disc_rate_limited = True
        reasons.append("discovery_failed_rate_limited")

    # ── 2. Log tail scan ─────────────────────────────────────────────────
    log_rate_hits = 0
    ep_hit_counts: dict[str, int] = {}
    try:
        log_lines = _tail_lines(_logfile_path(), log_tail_lines)
        for line in log_lines:
            if "birdeye_rate_limited" not in line:
                continue
            log_rate_hits += 1
            m = re.search(r"birdeye_rate_limited:(/[^\s:\'\"\\)]+)", line)
            if m:
                ep = m.group(1)
                ep_hit_counts[ep] = ep_hit_counts.get(ep, 0) + 1
    except Exception:
        pass  # log scan is best-effort — never crash the payload build

    if log_rate_hits > 0:
        ep_summary = ", ".join(
            f"{ep}×{n}" for ep, n in sorted(ep_hit_counts.items(), key=lambda x: -x[1])
        )
        reasons.append(f"log_rate_hits={log_rate_hits}({ep_summary})")

    # ── 3. Audit endpoint bad-rate (stale signal, lowest priority) ───────
    audit_bad_rate_flag = False
    if birdeye_audit:
        for ep, v in birdeye_audit.get("endpoints", {}).items():
            total = v.get("total", 0)
            bad = v.get("400", 0) + v.get("other_non200", 0)
            if total > 0 and (bad / total) >= 0.3:
                audit_bad_rate_flag = True
                reasons.append(f"audit_bad_rate:{ep}={bad}/{total}")
                break

    # ── Classify ─────────────────────────────────────────────────────────
    if disc_rate_limited or log_rate_hits >= _BIRDEYE_RATE_STARVED_THRESHOLD:
        mode = "starved"
    elif log_rate_hits >= _BIRDEYE_RATE_CONSTRAINED_THRESHOLD or audit_bad_rate_flag:
        mode = "constrained"
    else:
        mode = "healthy"

    reason_summary = "; ".join(reasons) if reasons else "ok"
    return mode, reason_summary

# Event types shown in the UI (journal + log-derived where needed).
FILTER_EVENT_TYPES = frozenset(
    {
        "entry_opened",
        "exit_success",
        "exit_blocked_detected",
        "zombie_recovered",
        "dripper_chunk_executed",
        "hachi_drip_stopped",
    }
)

_STATIC_DIR = Path(__file__).resolve().parent / "static"


def _runtime_dir() -> Path:
    return Path(os.environ.get("RUNTIME_DIR", "runtime")).resolve()


def _state_path() -> Path:
    return Path(os.environ.get("STATE_PATH", str(_runtime_dir() / "state.json"))).resolve()


def _journal_path() -> Path:
    return Path(os.environ.get("JOURNAL_PATH", str(_runtime_dir() / "journal.jsonl"))).resolve()


def _status_path() -> Path:
    return Path(os.environ.get("STATUS_PATH", str(_runtime_dir() / "status.json"))).resolve()


def _logfile_path() -> Path:
    return Path(os.environ.get("LOGFILE_PATH", str(_runtime_dir() / "logfile.log"))).resolve()


def _tail_lines(path: Path, max_lines: int) -> list[str]:
    if not path.is_file() or max_lines <= 0:
        return []
    dq: deque[str] = deque(maxlen=max_lines)
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            dq.append(line.rstrip("\n"))
    return list(dq)


def _journal_row_event_type(row: dict) -> str | None:
    action = row.get("action")
    reason = row.get("reason")
    if action == "BUY":
        return "entry_opened"
    if action == "SELL":
        return "exit_success"
    if action == "SELL_BLOCKED":
        return "exit_blocked_detected"
    if action == "DRIPPER_CHUNK_EXECUTED":
        return "dripper_chunk_executed"
    if action == "DRIPPER_WAIT" and reason == "max_chunks_reached":
        return "hachi_drip_stopped"
    return None


def _parse_observability_line(line: str) -> dict | None:
    if "creeper_dripper.observability]" not in line or "event=" not in line:
        return None
    m = re.search(r"event=(\w+)\s+reason=(\S+)\s+metadata=(\{.*\})\s*$", line)
    if not m:
        return None
    event_type, reason_code, meta_raw = m.group(1), m.group(2), m.group(3)
    if event_type not in FILTER_EVENT_TYPES:
        return None
    try:
        metadata = ast.literal_eval(meta_raw)
    except (SyntaxError, ValueError):
        metadata = {"raw": meta_raw}
    ts = line.split(" INFO ", 1)[0].strip() if " INFO " in line else None
    return {
        "event_type": event_type,
        "reason_code": reason_code,
        "metadata": metadata,
        "ts": ts,
        "source": "log",
    }


def _parse_trader_hachi_line(line: str) -> dict | None:
    if "event=hachi_drip_stopped" not in line or "creeper_dripper.engine.trader]" not in line:
        return None
    ts = line.split(" INFO ", 1)[0].strip() if " INFO " in line else None
    m = re.search(
        r"event=hachi_drip_stopped reason=(\S+)\s+mint=(\S+)\s+position_id=(\S+)\s+chunks_done=(\d+)",
        line,
    )
    if not m:
        return {
            "event_type": "hachi_drip_stopped",
            "reason_code": "parse_incomplete",
            "metadata": {"line_tail": line[-240:]},
            "ts": ts,
            "source": "log",
        }
    reason, mint, pos_id, chunks = m.group(1), m.group(2), m.group(3), int(m.group(4))
    return {
        "event_type": "hachi_drip_stopped",
        "reason_code": reason,
        "metadata": {"mint": mint, "position_id": pos_id, "chunks_done": chunks},
        "ts": ts,
        "source": "log",
    }


def _collect_filtered_events(journal_lines: int, log_lines: int) -> list[dict]:
    out: list[dict] = []
    jp = _journal_path()
    for line in _tail_lines(jp, journal_lines):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        et = _journal_row_event_type(row)
        if et is None or et not in FILTER_EVENT_TYPES:
            continue
        out.append(
            {
                "event_type": et,
                "reason_code": row.get("reason"),
                "metadata": {
                    "action": row.get("action"),
                    "symbol": row.get("symbol"),
                    "token_mint": row.get("token_mint"),
                    "cycle_in_run": row.get("cycle_in_run"),
                    "run_id": row.get("run_id"),
                    "extra": {k: v for k, v in row.items() if k not in {"ts", "action", "reason", "symbol", "token_mint"}},
                },
                "ts": row.get("ts"),
                "source": "journal",
            }
        )

    lp = _logfile_path()
    for line in _tail_lines(lp, log_lines):
        p = _parse_observability_line(line)
        if p:
            out.append(p)
        h = _parse_trader_hachi_line(line)
        if h:
            out.append(h)

    def _sort_key(e: dict) -> str:
        t = e.get("ts") or ""
        return t

    out.sort(key=_sort_key)
    return out[-50:]


def _load_cycle_summaries(limit: int = 10) -> list[dict]:
    sp = _status_path()
    if not sp.is_file():
        return []
    try:
        status = json.loads(sp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    run_dir = status.get("run_dir")
    if not run_dir:
        summ = status.get("summary")
        return [summ] if isinstance(summ, dict) else []
    cs_path = Path(run_dir) / "cycle_summaries.jsonl"
    if not cs_path.is_file():
        summ = status.get("summary")
        return [summ] if isinstance(summ, dict) else []
    lines = _tail_lines(cs_path, 5000)
    parsed: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row.get("summary"), dict):
            parsed.append(row)
    if not parsed:
        summ = status.get("summary")
        return [summ] if isinstance(summ, dict) else []
    return parsed[-limit:]


def _build_truth_payload() -> dict:
    """
    Pre-aggregate all runtime artifacts into a single structured payload
    for the Dashboard of Truth.  Never raises — missing data sections are
    returned as None so the UI can show explicit gaps.
    """
    state = _safe_load_json(_state_path())
    status = _safe_load_json(_status_path())
    acct = _safe_load_json(_accounting_snapshot_path())
    birdeye = _safe_load_json(_birdeye_audit_summary_path())
    coverage = _safe_load_json(_token_report_coverage_path())

    # ── latest cycle summary ─────────────────────────────────────────────
    latest_summary: dict = {}
    derived_policy: dict = {}
    if status:
        raw_summ = status.get("summary")
        if isinstance(raw_summ, dict):
            latest_summary = raw_summ
        raw_dp = status.get("derived_policy")
        if isinstance(raw_dp, dict):
            derived_policy = raw_dp
        # derived_policy is also nested inside summary — prefer top-level
        if not derived_policy and isinstance(latest_summary.get("derived_policy"), dict):
            derived_policy = latest_summary["derived_policy"]

    # ── capital truth ────────────────────────────────────────────────────
    cash_sol = None
    wallet_available_sol = None
    deployable_sol = None
    accounting_drift_sol = None
    reconciled_cash_sol = None
    reconciliation_applied = None
    reconciliation_delta_sol = None
    reserved_sol = None
    total_realized_sol = None
    hachi_birth_wallet_sol = None
    safe_mode_active = False
    safety_stop_reason = None

    if state:
        cash_sol = state.get("cash_sol")
        reserved_sol = state.get("reserved_sol")
        total_realized_sol = state.get("total_realized_sol")
        hachi_birth_wallet_sol = state.get("hachi_birth_wallet_sol")
        safe_mode_active = bool(state.get("safe_mode_active", False))
        safety_stop_reason = state.get("safety_stop_reason")

    if latest_summary:
        wallet_available_sol = latest_summary.get("wallet_available_sol")
        deployable_sol = latest_summary.get("deployable_sol")
        accounting_drift_sol = latest_summary.get("accounting_drift_sol")
        reconciled_cash_sol = latest_summary.get("reconciled_cash_sol")
        reconciliation_applied = latest_summary.get("reconciliation_applied")
        reconciliation_delta_sol = latest_summary.get("reconciliation_delta_sol")
        # Fall back to state if summary doesn't have these
        if cash_sol is None:
            cash_sol = latest_summary.get("cash_sol")

    # accounting_snapshot gives us the definitive reconciliation snapshot
    acct_snapshot: dict | None = None
    if acct:
        acct_snapshot = {
            "generated_at": acct.get("generated_at"),
            "wallet_available_sol": acct.get("wallet_available_sol"),
            "cash_sol_before": acct.get("cash_sol_before"),
            "reconciled_cash_sol": acct.get("reconciled_cash_sol"),
            "reconciliation_delta_sol": acct.get("reconciliation_delta_sol"),
        }

    # accounting drift check: if delta is large, flag it
    acct_drift_warning = False
    if acct_snapshot and acct_snapshot.get("reconciliation_delta_sol") is not None:
        delta = acct_snapshot["reconciliation_delta_sol"]
        if abs(delta) > 0.01:
            acct_drift_warning = True

    # ── positions ────────────────────────────────────────────────────────
    positions_raw = state.get("open_positions", {}) if state else {}
    positions_list: list[dict] = []
    for mint, p in positions_raw.items():
        status_str = p.get("status", "UNKNOWN")
        entry_sol = p.get("entry_sol") or 0.0
        last_exit_val = p.get("last_estimated_exit_value_sol")
        unrealized_sol = p.get("unrealized_pnl_sol")
        val_status = p.get("valuation_status", "unknown")
        pnl_pct = None
        if unrealized_sol is not None and entry_sol and entry_sol > 0:
            pnl_pct = (unrealized_sol / entry_sol) * 100.0

        positions_list.append({
            "mint": mint,
            "symbol": p.get("symbol", "?"),
            "status": status_str,
            "entry_sol": entry_sol,
            "last_estimated_exit_value_sol": last_exit_val,
            "unrealized_pnl_sol": unrealized_sol,
            "unrealized_pnl_pct": pnl_pct,
            "valuation_status": val_status,
            "valuation_source": p.get("valuation_source"),
            "zombie_reason": p.get("zombie_reason"),
            "zombie_class": p.get("zombie_class"),
            "zombie_since": p.get("zombie_since"),
            "zombie_age_cycles": p.get("zombie_age_cycles"),
            "recovery_attempts": p.get("recovery_attempts"),
            "last_recovery_attempt_at": p.get("last_recovery_attempt_at"),
            "final_zombie_at": p.get("final_zombie_at"),
            "entry_sell_impact_bps": p.get("entry_sell_impact_bps"),
            "last_sell_impact_bps": p.get("last_sell_impact_bps"),
            "last_sell_route_label": p.get("last_sell_route_label"),
            "drip_exit_active": p.get("drip_exit_active"),
            "drip_chunks_done": p.get("drip_chunks_done"),
            "hachi_drip_completed": p.get("hachi_drip_completed"),
            "last_hachi_pnl_pct": p.get("last_hachi_pnl_pct"),
            "hachi_last_tp_level": p.get("hachi_last_tp_level"),
            "exit_retry_count": p.get("exit_retry_count"),
            "exit_blocked_cycles": p.get("exit_blocked_cycles"),
            "opened_at": p.get("opened_at"),
            "pending_exit_reason": p.get("pending_exit_reason"),
            "quote_miss_streak": p.get("quote_miss_streak"),
        })

    # sort: OPEN first, ZOMBIE by locked SOL desc, FINAL_ZOMBIE last
    def _pos_sort_key(p: dict) -> tuple:
        s = p["status"]
        order = {"OPEN": 0, "PARTIAL": 1, "EXIT_PENDING": 2, "EXIT_BLOCKED": 3, "ZOMBIE": 4, "FINAL_ZOMBIE": 5}
        return (order.get(s, 9), -(p["entry_sol"] or 0))

    positions_list.sort(key=_pos_sort_key)

    # ── portfolio aggregates ──────────────────────────────────────────────
    open_count = sum(1 for p in positions_list if p["status"] == "OPEN")
    partial_count = sum(1 for p in positions_list if p["status"] == "PARTIAL")
    zombie_count = sum(1 for p in positions_list if p["status"] == "ZOMBIE")
    final_zombie_count = sum(1 for p in positions_list if p["status"] == "FINAL_ZOMBIE")
    exit_blocked_count = sum(1 for p in positions_list if p["status"] == "EXIT_BLOCKED")
    exit_stuck_total = zombie_count + final_zombie_count + exit_blocked_count

    zombie_locked_sol = sum(p["entry_sol"] or 0 for p in positions_list if p["status"] in {"ZOMBIE", "FINAL_ZOMBIE"})
    recoverable_sol = sum(p["last_estimated_exit_value_sol"] or 0 for p in positions_list if p["status"] in {"ZOMBIE", "FINAL_ZOMBIE"} and p["last_estimated_exit_value_sol"] is not None)
    dead_sol = zombie_locked_sol - recoverable_sol

    # Use summary values if they exist (more authoritative)
    if latest_summary:
        zombie_locked_sol = latest_summary.get("zombie_locked_sol_estimate", zombie_locked_sol)
        recoverable_sol = latest_summary.get("recoverable_sol_estimate", recoverable_sol)
        dead_sol = latest_summary.get("dead_sol_estimate", dead_sol)

    total_open_unrealized = sum(
        p["unrealized_pnl_sol"] or 0 for p in positions_list if p["status"] == "OPEN" and p["unrealized_pnl_sol"] is not None
    )
    total_realized = total_realized_sol

    # ── discovery health ─────────────────────────────────────────────────
    discovery_failed = latest_summary.get("discovery_failed", False) if latest_summary else None
    discovery_error = latest_summary.get("discovery_error") if latest_summary else None
    discovery_error_type = latest_summary.get("discovery_error_type") if latest_summary else None
    seeds_total = latest_summary.get("seeds_total", 0) if latest_summary else 0
    candidates_accepted = latest_summary.get("candidates_accepted", 0) if latest_summary else 0
    candidates_rejected_total = latest_summary.get("candidates_rejected_total", 0) if latest_summary else 0
    rejection_counts = latest_summary.get("rejection_counts", {}) if latest_summary else {}
    route_checked_candidates = latest_summary.get("route_checked_candidates", 0) if latest_summary else 0
    birdeye_candidate_build_calls = latest_summary.get("birdeye_candidate_build_calls", 0) if latest_summary else 0
    jupiter_buy_probe_calls = latest_summary.get("jupiter_buy_probe_calls", 0) if latest_summary else 0
    cache_hits = latest_summary.get("cache_hits", 0) if latest_summary else 0
    cache_misses = latest_summary.get("cache_misses", 0) if latest_summary else 0
    candidate_cache_hits = latest_summary.get("candidate_cache_hits", 0) if latest_summary else 0
    candidate_cache_misses = latest_summary.get("candidate_cache_misses", 0) if latest_summary else 0
    # JUP-FIRST mode fields — sourced from cycle summary (set by trader._cycle_summary).
    data_source_mode = latest_summary.get("data_source_mode", "enrichment_enabled") if latest_summary else "enrichment_enabled"
    discovery_mode = latest_summary.get("discovery_mode", "active") if latest_summary else "active"

    # ── Birdeye budget mode (derived from log + discovery signals) ────────
    birdeye_budget_mode, birdeye_budget_reason = _derive_birdeye_budget_mode(
        discovery_failed=discovery_failed,
        discovery_error=discovery_error,
        birdeye_audit=birdeye,
    )

    # ── Birdeye budget section ────────────────────────────────────────────
    birdeye_section: dict | None = None
    if birdeye:
        credits = birdeye.get("credits", {})
        endpoints = birdeye.get("endpoints", {})
        birdeye_section = {
            "birdeye_budget_mode": birdeye_budget_mode,
            "budget_reason_summary": birdeye_budget_reason,
            "data_source_mode": data_source_mode,
            "discovery_mode": discovery_mode,
            "delta_usage_api": credits.get("delta_usage_api"),
            "usage_api_after": credits.get("usage_api_after"),
            "estimated_cu_api_per_seed": birdeye.get("estimates_per_discovery_cycle", {}).get("estimated_cu_api_per_seed"),
            "estimated_cu_api_per_accepted_token": birdeye.get("estimates_per_discovery_cycle", {}).get("estimated_cu_api_per_accepted_token"),
            "seeds_total": birdeye.get("estimates_per_discovery_cycle", {}).get("seeds_total"),
            "candidates_accepted": birdeye.get("estimates_per_discovery_cycle", {}).get("candidates_accepted"),
            "doctor_ok": birdeye.get("doctor_ok"),
            "endpoints": {
                ep: {
                    "total": v.get("total", 0),
                    "ok": v.get("200", 0),
                    "bad": (v.get("400", 0) + v.get("other_non200", 0)),
                    "rate": round(v.get("400", 0) / v["total"], 3) if v.get("total", 0) > 0 else 0.0,
                }
                for ep, v in endpoints.items()
            },
            "top_10_mints_causing_400": birdeye.get("top_10_mints_causing_400", []),
            "disable_or_fix_first": birdeye.get("disable_or_fix_first", []),
        }
    else:
        # No audit file — still derive budget mode from log signals alone
        birdeye_section = {
            "birdeye_budget_mode": birdeye_budget_mode,
            "budget_reason_summary": birdeye_budget_reason,
            "data_source_mode": data_source_mode,
            "discovery_mode": discovery_mode,
        }

    # ── data quality / coverage ───────────────────────────────────────────
    # Confidence thresholds for grouping fields:
    #   high:   ≥ 70% coverage  — data is reliably present
    #   medium: ≥ 20% coverage  — present for meaningful subset, use cautiously
    #   low:    < 20% coverage  — sparse or inferred; treat as signal-weak
    _DQ_HIGH_THRESHOLD = 70.0
    _DQ_MEDIUM_THRESHOLD = 20.0

    coverage_section: dict | None = None
    if coverage:
        fields = coverage.get("fields", {})
        enriched_fields: dict[str, dict] = {}
        for k, v in fields.items():
            pct = round(v.get("pct_present", 0), 1)
            if pct >= _DQ_HIGH_THRESHOLD:
                confidence = "high"
            elif pct >= _DQ_MEDIUM_THRESHOLD:
                confidence = "medium"
            else:
                confidence = "low"
            enriched_fields[k] = {
                "pct": pct,
                "present": v.get("present", 0),
                "confidence": confidence,
            }
        coverage_section = {
            "generated_at": coverage.get("generated_at"),
            "total_tokens": coverage.get("total_tokens"),
            "fields": enriched_fields,
        }

    # ── run / cycle context ───────────────────────────────────────────────
    run_context: dict = {}
    if status:
        run_context = {
            "run_id": status.get("run_id"),
            "cycle_in_run": status.get("cycle_in_run"),
            "cycle_timestamp": status.get("cycle_timestamp"),
            "safe_mode_active": bool(status.get("safe_mode_active", False)),
            "safety_stop_reason": status.get("safety_stop_reason"),
        }
    if latest_summary and not run_context.get("run_id"):
        run_context = {
            "run_id": latest_summary.get("run_id"),
            "cycle_in_run": latest_summary.get("cycle_in_run"),
            "cycle_timestamp": latest_summary.get("timestamp"),
            "safe_mode_active": bool(latest_summary.get("safe_mode_active", False)),
            "safety_stop_reason": None,
        }

    # ── entry gate answer ─────────────────────────────────────────────────
    entry_enabled = derived_policy.get("entry_enabled", True)
    entries_blocked_reason = derived_policy.get("entries_blocked_reason") or latest_summary.get("entries_blocked_reason")
    opened_today_count = latest_summary.get("opened_today_count") if latest_summary else (state.get("opened_today_count") if state else None)
    effective_max_daily_new_positions = derived_policy.get("effective_max_daily_new_positions")
    effective_max_open_positions = derived_policy.get("effective_max_open_positions")
    daily_cap_hit = (
        isinstance(opened_today_count, int)
        and isinstance(effective_max_daily_new_positions, int)
        and opened_today_count >= effective_max_daily_new_positions
    )
    open_cap_hit = (
        isinstance(open_count, int)
        and isinstance(effective_max_open_positions, int)
        and open_count >= effective_max_open_positions
    )

    # ── posture / risk level ──────────────────────────────────────────────
    posture = derived_policy.get("policy_posture", "unknown")
    posture_severity = {"balanced": 0, "aggressive": 0, "constrained": 1, "recovery_only": 2}.get(posture, 0)

    # ── entry_allowed: explicit single bool (true only when truly open) ──
    # entry_enabled from derived_policy is the policy gate.
    # entry_allowed also gates out daily/open caps so the UI has one field.
    entry_allowed = bool(entry_enabled) and not daily_cap_hit and not open_cap_hit

    # ── zombie rank: mark top-3 worst by locked SOL ───────────────────────
    zombie_entries = [
        (i, p) for i, p in enumerate(positions_list)
        if p["status"] in {"ZOMBIE", "FINAL_ZOMBIE"}
    ]
    zombie_entries.sort(key=lambda x: -(x[1]["entry_sol"] or 0))
    top3_zombie_indices = {idx for rank, (idx, _) in enumerate(zombie_entries) if rank < 3}
    for i, p in enumerate(positions_list):
        p["zombie_rank"] = None
        if i in top3_zombie_indices:
            rank = next(r for r, (idx, _) in enumerate(zombie_entries) if idx == i)
            p["zombie_rank"] = rank + 1  # 1-based

    return {
        "run": run_context,
        "capital": {
            "wallet_available_sol": wallet_available_sol,
            "cash_sol": cash_sol,
            "reserved_sol": reserved_sol,
            "deployable_sol": deployable_sol,
            "accounting_drift_sol": accounting_drift_sol,
            "reconciled_cash_sol": reconciled_cash_sol,
            "reconciliation_applied": reconciliation_applied,
            "reconciliation_delta_sol": reconciliation_delta_sol,
            "total_realized_sol": total_realized_sol,
            "hachi_birth_wallet_sol": hachi_birth_wallet_sol,
            "safe_mode_active": safe_mode_active,
            "safety_stop_reason": safety_stop_reason,
            "accounting_snapshot": acct_snapshot,
            "accounting_drift_warning": acct_drift_warning,
        },
        "portfolio": {
            "open_count": open_count,
            "partial_count": partial_count,
            "zombie_count": zombie_count,
            "final_zombie_count": final_zombie_count,
            "exit_blocked_count": exit_blocked_count,
            "exit_stuck_total": exit_stuck_total,
            "zombie_locked_sol": zombie_locked_sol,
            "recoverable_sol": recoverable_sol,
            "dead_sol": dead_sol,
            "total_open_unrealized_sol": total_open_unrealized,
            "total_realized_sol": total_realized_sol,
            "opened_today_count": opened_today_count,
        },
        "policy": derived_policy,
        "entry_gate": {
            "entry_allowed": entry_allowed,
            "entry_enabled": entry_enabled,
            "entries_blocked_reason": entries_blocked_reason,
            "daily_cap_hit": daily_cap_hit,
            "open_cap_hit": open_cap_hit,
            "posture": posture,
            "posture_severity": posture_severity,
            "policy_adjustments_applied": derived_policy.get("policy_adjustments_applied", []),
            "policy_reason_summary": derived_policy.get("policy_reason_summary"),
        },
        "discovery": {
            "failed": discovery_failed,
            "error": discovery_error,
            "error_type": discovery_error_type,
            "seeds_total": seeds_total,
            "candidates_accepted": candidates_accepted,
            "candidates_rejected_total": candidates_rejected_total,
            "rejection_counts": rejection_counts,
            "route_checked_candidates": route_checked_candidates,
            "birdeye_candidate_build_calls": birdeye_candidate_build_calls,
            "jupiter_buy_probe_calls": jupiter_buy_probe_calls,
            "cache_hits": cache_hits,
            "cache_misses": cache_misses,
            "candidate_cache_hits": candidate_cache_hits,
            "candidate_cache_misses": candidate_cache_misses,
            # JUP-FIRST: data and discovery mode visibility.
            "data_source_mode": data_source_mode,
            "discovery_mode": discovery_mode,
        },
        "birdeye": birdeye_section,
        "positions": positions_list,
        "data_quality": coverage_section,
    }


app = FastAPI(title="creeper-dripper dashboard", version="0.1.0")


@app.get("/state")
def get_state() -> JSONResponse:
    p = _state_path()
    if not p.is_file():
        return JSONResponse({"error": "state file not found", "path": str(p)}, status_code=404)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return JSONResponse({"error": "failed to read state", "detail": str(exc)}, status_code=500)
    return JSONResponse(data)


@app.get("/events")
def get_events(
    journal_lines: int = Query(8000, ge=100, le=500_000, description="Last N lines of journal.jsonl to scan"),
    log_lines: int = Query(12_000, ge=0, le=500_000, description="Last N lines of logfile.log for observability events"),
) -> JSONResponse:
    events = _collect_filtered_events(journal_lines, log_lines)
    return JSONResponse({"events": events, "count": len(events)})


@app.get("/summary")
def get_summary() -> JSONResponse:
    sp = _status_path()
    if not sp.is_file():
        return JSONResponse({"error": "status file not found", "path": str(sp)}, status_code=404)
    try:
        status = json.loads(sp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return JSONResponse({"error": "failed to read status", "detail": str(exc)}, status_code=500)
    summ = status.get("summary")
    recent = _load_cycle_summaries(10)
    if not isinstance(summ, dict):
        if recent:
            last = recent[-1]
            summ = last.get("summary") if isinstance(last.get("summary"), dict) else last
        if not isinstance(summ, dict):
            return JSONResponse({"error": "no cycle summary available", "status_keys": list(status.keys())}, status_code=404)
    return JSONResponse({"latest": summ, "recent": recent})


@app.get("/truth")
def get_truth() -> JSONResponse:
    """Dashboard-of-Truth: pre-aggregated operator payload."""
    try:
        payload = _build_truth_payload()
    except Exception as exc:
        return JSONResponse({"error": "truth build failed", "detail": str(exc)}, status_code=500)
    return JSONResponse(payload)


if _STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


@app.get("/", response_model=None)
def index() -> Response:
    index_path = _STATIC_DIR / "index.html"
    if not index_path.is_file():
        return JSONResponse({"error": "dashboard static files missing", "path": str(index_path)}, status_code=404)
    return FileResponse(index_path)
