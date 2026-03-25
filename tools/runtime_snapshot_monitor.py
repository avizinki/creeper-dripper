#!/usr/bin/env python3
"""
Passive runtime snapshot monitor for live runs.

Follows docs/runtime_snapshot_playbook.md — does not modify trading logic.
Polls runtime/status.json for cycle completion; every N cycles copies sanitized
artifacts into review_artifacts/runtime_snapshots/<UTC>/ and commits + pushes.

Critical conditions: snapshot + optional bot stop (SIGTERM to `creeper-dripper run` only).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]


def redact(s: str) -> str:
    return re.sub(r"/Users/[^\s\"']+", "<local-path-redacted>", s)


def sh(*args: str, cwd: Optional[Path] = None, check: bool = True) -> str:
    r = subprocess.run(list(args), cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        sys.stderr.write(r.stderr or "")
        raise RuntimeError(f"command failed: {args!r} rc={r.returncode}")
    return (r.stdout or "").strip()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def exec_failures_threshold(repo: Path) -> int:
    try:
        for line in (repo / ".env").read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("MAX_CONSECUTIVE_EXECUTION_FAILURES="):
                return max(1, int(line.split("=", 1)[1].strip()))
    except OSError:
        pass
    return int(os.environ.get("MAX_CONSECUTIVE_EXECUTION_FAILURES", "6"))


def check_critical(state: dict, status: dict, log_tail: str, repo: Path) -> Optional[str]:
    if status.get("safe_mode_active") is True:
        return "status.safe_mode_active is true"
    summ = status.get("summary") or {}
    if isinstance(summ, dict) and summ.get("safe_mode_active") is True:
        return "status.summary.safe_mode_active is true"
    if state.get("safe_mode_active") is True:
        return "state.safe_mode_active is true"

    if isinstance(summ, dict) and int(summ.get("exit_blocked_positions") or 0) > 0:
        return f"status.summary.exit_blocked_positions={summ.get('exit_blocked_positions')}"

    thr = exec_failures_threshold(repo)
    fails = int(state.get("consecutive_execution_failures") or 0)
    if fails >= thr:
        return f"consecutive_execution_failures={fails} >= threshold {thr}"

    ver = state.get("version")
    if ver is not None and ver != 2:
        return f"unexpected state version: {ver}"

    try:
        cash = float(state.get("cash_sol"))
        if cash < -1e6 or cash != cash:  # NaN
            return f"suspicious cash_sol: {state.get('cash_sol')}"
    except (TypeError, ValueError):
        pass

    for mint, pos in (state.get("open_positions") or {}).items():
        if not isinstance(pos, dict):
            return f"corrupt open_positions entry: {mint!r}"
        st = pos.get("status")
        if st == "EXIT_BLOCKED":
            return f"EXIT_BLOCKED position: {pos.get('symbol') or mint}"
        if st == "RECONCILE_PENDING" and pos.get("reconcile_context") == "exit":
            return f"RECONCILE_PENDING (exit) position: {pos.get('symbol') or mint}"

    lt = log_tail.lower()
    if "sell_proceeds_unknown" in lt or "exit_unknown_pending_reconcile" in lt:
        return "log indicates sell_proceeds_unknown or exit unknown reconcile"
    if "state_file_corrupted" in lt or "state corrupted" in lt:
        return "log indicates state corruption"
    if "safety_max_consecutive_execution_failures" in lt or "SAFETY_MAX_CONSEC_EXEC_FAILURES" in log_tail:
        return "log indicates safety stop from consecutive execution failures"

    return None


def latest_probe(base: Optional[Path], symbol_prefix: str) -> Optional[Path]:
    if base is None:
        return None
    art = base / "artifacts"
    for d in (art, base):
        if not d.is_dir():
            continue
        files = sorted(d.glob(f"entry_probe_{symbol_prefix}_*.json"))
        if files:
            return files[-1]
    return None


def write_snapshot(dest: Path, runtime: Path, log_lines: int, repo_root: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    status = load_json(runtime / "status.json") if (runtime / "status.json").exists() else {}
    run_id = status.get("run_id")
    cycle_in_run = status.get("cycle_in_run")
    run_folder = runtime / "runs" / str(run_id) if run_id else None
    run_folder_display = f"runtime/runs/{run_id}" if run_id else "N/A"
    for name in ("state.json", "status.json", "scan_latest.json", "scan_summary.json"):
        p = runtime / name
        if p.exists():
            (dest / name).write_text(redact(p.read_text(encoding="utf-8")), encoding="utf-8")

    for sym in ("PRl", "EDGe"):
        src = latest_probe(run_folder, sym) if run_folder else None
        if src is None:
            src = latest_probe(runtime, sym)
        if src:
            (dest / src.name).write_text(redact(src.read_text(encoding="utf-8")), encoding="utf-8")

    log_path = (run_folder / "logfile.log") if run_folder and (run_folder / "logfile.log").exists() else (runtime / "logfile.log")
    if log_path.exists():
        lines = log_path.read_text(encoding="utf-8").splitlines()
        tail = lines[-log_lines:] if len(lines) > log_lines else lines
        (dest / "log_excerpt.txt").write_text(
            "\n".join(redact(line) for line in tail) + "\n", encoding="utf-8"
        )

    state = load_json(runtime / "state.json")
    open_n = len(state.get("open_positions") or {})
    drip_enabled = False
    envp = repo_root / ".env"
    if envp.exists():
        for line in envp.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("DRIP_EXIT_ENABLED="):
                drip_enabled = line.split("=", 1)[-1].strip().lower() in ("true", "1", "yes")
                break

    ts_folder = dest.name
    head = sh("git", "rev-parse", "HEAD", cwd=repo_root)
    branch = sh("git", "branch", "--show-current", cwd=repo_root)
    tail_text = (dest / "log_excerpt.txt").read_text(encoding="utf-8") if (dest / "log_excerpt.txt").exists() else ""
    drip_ev = bool(re.search(r"DRIP_CHUNK|DRIP_", tail_text))

    readme = f"""# Runtime snapshot (automated monitor)

**Ongoing live run snapshot** — generated by `tools/runtime_snapshot_monitor.py`.

| Field | Value |
|--------|--------|
| Branch | `{branch}` |
| HEAD | `{head}` |
| Snapshot folder (UTC) | `{ts_folder}` |
| Run ID | `{run_id or 'N/A'}` |
| Source run folder | `{run_folder_display}` |
| Cycle in run | `{cycle_in_run if cycle_in_run is not None else 'N/A'}` |
| Drip exit enabled (local `.env`, not committed) | **{'true' if drip_enabled else 'false'}** |
| Open positions | **{open_n}** |
| DRIP_* in log excerpt | **{'yes' if drip_ev else 'no'}** |

## Files

Sanitized per `docs/runtime_snapshot_playbook.md`.
"""
    (dest / "README.md").write_text(readme, encoding="utf-8")

    # verify no raw paths
    for f in dest.rglob("*"):
        if f.is_file() and "/Users/" in f.read_text(encoding="utf-8", errors="replace"):
            raise RuntimeError(f"sanitization failed: {f}")


def verify_no_users(dest: Path) -> None:
    for f in dest.rglob("*"):
        if f.is_file() and "/Users/" in f.read_text(encoding="utf-8", errors="replace"):
            raise RuntimeError(f"/Users/ still present in {f}")


def git_commit_push(dest: Path, branch: str, repo_root: Path) -> str:
    rel = dest.relative_to(repo_root)
    subprocess.run(["git", "add", str(rel)], cwd=repo_root, check=True)
    r = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=repo_root)
    if r.returncode == 0:
        return ""
    msg = f"chore: add runtime snapshot {dest.name} for live monitoring"
    cr = subprocess.run(
        ["git", "commit", "-m", msg],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if cr.returncode != 0:
        sys.stderr.write(cr.stderr or "")
        raise RuntimeError(f"git commit failed: {cr.stdout!r}")
    subprocess.run(["git", "push", "origin", branch], cwd=repo_root, check=True)
    return sh("git", "rev-parse", "HEAD", cwd=repo_root)


def unique_snapshot_dir(parent: Path, name: str) -> Path:
    d = parent / name
    if not d.exists():
        return d
    i = 2
    while (parent / f"{name}_{i}").exists():
        i += 1
    return parent / f"{name}_{i}"


def stop_bot() -> None:
    subprocess.run(["pkill", "-TERM", "-f", r"creeper-dripper\s+run"], check=False)


def main() -> int:
    ap = argparse.ArgumentParser(description="Passive runtime snapshot monitor")
    ap.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    ap.add_argument("--branch", default="live-jsds-test")
    ap.add_argument("--poll-seconds", type=float, default=5.0)
    ap.add_argument("--cycles-between-snapshots", type=int, default=10)
    ap.add_argument("--max-snapshots", type=int, default=10)
    ap.add_argument("--log-excerpt-lines", type=int, default=280)
    ap.add_argument("--no-stop-on-critical", action="store_true")
    args = ap.parse_args()

    repo: Path = args.repo_root.resolve()
    runtime = repo / "runtime"
    status_path = runtime / "status.json"
    state_path = runtime / "state.json"
    log_path = runtime / "logfile.log"

    last_cycle_ts: Optional[str] = None
    cycle_count = 0
    snapshots_done = 0

    print(f"monitor: repo={repo} branch={args.branch}", flush=True)
    print(
        f"monitor: every {args.cycles_between_snapshots} cycles -> snapshot, max {args.max_snapshots}",
        flush=True,
    )

    while snapshots_done < args.max_snapshots:
        time.sleep(args.poll_seconds)
        if not status_path.exists():
            print("monitor: waiting for status.json", flush=True)
            continue

        st = load_json(status_path)
        cts = str(st.get("cycle_timestamp") or "")
        if not cts:
            continue

        if last_cycle_ts is None:
            last_cycle_ts = cts
            print(f"monitor: baseline cycle_timestamp={cts}", flush=True)
            continue

        if cts == last_cycle_ts:
            continue

        last_cycle_ts = cts
        cycle_count += 1
        print(f"monitor: cycle {cycle_count} completed at {cts}", flush=True)

        run_id = st.get("run_id")
        run_log_path = runtime / "runs" / str(run_id) / "logfile.log" if run_id else log_path
        if not run_log_path.exists():
            run_log_path = log_path
        log_tail = ""
        if run_log_path.exists():
            lines = run_log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            log_tail = "\n".join(lines[-400:])

        state = load_json(state_path) if state_path.exists() else {}
        crit = check_critical(state, st, log_tail, repo)
        if crit:
            print(f"monitor: CRITICAL: {crit}", flush=True)
            ts = subprocess.check_output(["date", "-u", "+%Y-%m-%dT%H-%M-%SZ"], text=True).strip()
            dest = unique_snapshot_dir(
                repo / "review_artifacts" / "runtime_snapshots", f"{ts}-CRITICAL"
            )
            write_snapshot(dest, runtime, args.log_excerpt_lines, repo)
            verify_no_users(dest)
            h = git_commit_push(dest, args.branch, repo)
            print(f"monitor: critical snapshot commit {h}", flush=True)
            if not args.no_stop_on_critical:
                stop_bot()
                print("monitor: sent SIGTERM to creeper-dripper run", flush=True)
            return 2

        if cycle_count % args.cycles_between_snapshots != 0:
            continue

        ts = subprocess.check_output(["date", "-u", "+%Y-%m-%dT%H-%M-%SZ"], text=True).strip()
        dest = unique_snapshot_dir(repo / "review_artifacts" / "runtime_snapshots", ts)
        write_snapshot(dest, runtime, args.log_excerpt_lines, repo)
        verify_no_users(dest)
        h = git_commit_push(dest, args.branch, repo)
        snapshots_done += 1
        print(f"monitor: snapshot {snapshots_done}/{args.max_snapshots} commit {h}", flush=True)

    print("monitor: completed max snapshots; bot not stopped.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
