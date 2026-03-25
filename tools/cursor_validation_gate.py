#!/usr/bin/env python3
"""
Read docs/workflow/claude_task_result.json when status is ready_for_cursor_validation,
run required (and optionally optional) validations in the repo, gatekeep the working tree,
write docs/workflow/cursor_validation_result.json, and commit only if all checks pass.

Usage (from repo root):
  .venv/bin/python tools/cursor_validation_gate.py
  .venv/bin/python tools/cursor_validation_gate.py --include-optional
  .venv/bin/python tools/cursor_validation_gate.py --no-commit
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TASK_FILE = "docs/workflow/claude_task_result.json"
RESULT_FILE = "docs/workflow/cursor_validation_result.json"
MAX_LOG_CHARS = 12000


def _repo_root(start: Path) -> Path:
    cur = start.resolve()
    for _ in range(40):
        if (cur / ".git").is_dir():
            return cur
        if cur.parent == cur:
            break
        cur = cur.parent
    return start.resolve()


def _run_cmd(
    shell_cmd: str, cwd: Path
) -> tuple[int, str, str]:
    proc = subprocess.run(
        shell_cmd,
        shell=True,
        cwd=cwd,
        text=True,
        capture_output=True,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _truncate(s: str) -> str:
    if len(s) <= MAX_LOG_CHARS:
        return s
    return s[: MAX_LOG_CHARS - 40] + "\n... [truncated] ...\n"


def _git_porcelain(cwd: Path) -> list[tuple[str, str]]:
    """Return (status_letters, path) for each line of git status --porcelain."""
    code, out, _ = _run_cmd("git status --porcelain", cwd)
    if code != 0:
        return []
    rows: list[tuple[str, str]] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        status = line[:2]
        rest = line[3:]
        # Renames: "R  old -> new"
        if " -> " in rest:
            path = rest.split(" -> ", 1)[1].strip()
        else:
            path = rest.strip()
        rows.append((status, path))
    return rows


def _normalize_rel(p: str) -> str:
    return str(Path(p)).replace("\\", "/")


def _allowed_paths(payload: dict[str, Any]) -> set[str]:
    allowed: set[str] = set()
    for f in payload.get("files_changed") or []:
        allowed.add(_normalize_rel(f))
    for d in payload.get("docs_to_update") or []:
        allowed.add(_normalize_rel(d))
    allowed.add(_normalize_rel("docs/workflow"))
    return allowed


def _path_allowed(path: str, allowed: set[str]) -> bool:
    n = _normalize_rel(path).rstrip("/")
    if n in allowed:
        return True
    for a in allowed:
        if a.endswith("/") and n.startswith(a):
            return True
        if not a.endswith("/") and (n == a or n.startswith(a + "/")):
            return True
    # docs/workflow directory and anything under it
    if n == "docs/workflow" or n.startswith("docs/workflow/"):
        return True
    # Git often reports new trees as "?? .cursor/" while files_changed lists a file inside.
    for a in allowed:
        an = _normalize_rel(a).rstrip("/")
        if an == n or an.startswith(n + "/"):
            return True
    return False


def _gate_fail_reason(dirty_paths: list[str], allowed: set[str]) -> str:
    bad = [p for p in dirty_paths if not _path_allowed(p, allowed)]
    if not bad:
        return ""
    return "Working tree has changes outside allowed paths: " + ", ".join(bad)


def _commit_if_ok(
    cwd: Path, payload: dict[str, Any], dry_run: bool
) -> dict[str, Any]:
    msg = (payload.get("commit_message") or "").strip()
    if not msg:
        return {"attempted": False, "reason": "missing_commit_message", "sha": None}

    files_changed = [_normalize_rel(f) for f in (payload.get("files_changed") or [])]
    docs_updates = [_normalize_rel(d) for d in (payload.get("docs_to_update") or [])]
    to_add: list[str] = []
    for p in files_changed + docs_updates:
        if (cwd / p).exists():
            to_add.append(p)
    wf = cwd / "docs/workflow"
    if wf.is_dir():
        to_add.append("docs/workflow")

    # De-duplicate while preserving order
    seen: set[str] = set()
    add_paths = []
    for p in to_add:
        if p not in seen:
            seen.add(p)
            add_paths.append(p)

    if dry_run:
        return {
            "attempted": False,
            "reason": "dry_run",
            "would_add": add_paths,
            "sha": None,
        }

    if not add_paths:
        return {"attempted": False, "reason": "nothing_to_add", "sha": None}

    for path in add_paths:
        code, _, err = _run_cmd(f"git add -- {shlex.quote(path)}", cwd)
        if code != 0:
            return {
                "attempted": True,
                "ok": False,
                "reason": "git_add_failed",
                "detail": _truncate(err),
                "sha": None,
            }

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as tf:
        tf.write(msg)
        tf_path = tf.name
    try:
        code, out, err = _run_cmd(f"git commit -F {shlex.quote(tf_path)}", cwd)
    finally:
        Path(tf_path).unlink(missing_ok=True)

    if code != 0:
        return {
            "attempted": True,
            "ok": False,
            "reason": "git_commit_failed",
            "detail": _truncate(out + err),
            "sha": None,
        }

    sha_code, sha_out, _ = _run_cmd("git rev-parse HEAD", cwd)
    sha = sha_out.strip() if sha_code == 0 else None
    return {"attempted": True, "ok": True, "sha": sha, "added_paths": add_paths}


def main() -> int:
    ap = argparse.ArgumentParser(description="Claude task → Cursor validation gate")
    ap.add_argument(
        "--repo",
        type=Path,
        default=None,
        help="Repository root (default: auto-detect from .git)",
    )
    ap.add_argument(
        "--no-commit",
        action="store_true",
        help="Run validations and gate only; do not commit",
    )
    ap.add_argument(
        "--include-optional",
        action="store_true",
        help="Also run optional_validation commands",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Print actions only (still runs validations unless combined with check)",
    )
    args = ap.parse_args()

    cwd = args.repo or _repo_root(Path.cwd())
    env_file = cwd / ".env"
    if env_file.is_file():
        from dotenv import load_dotenv

        load_dotenv(env_file, override=False)

    task_path = cwd / TASK_FILE

    out: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "task_file": TASK_FILE,
        "repo_root": str(cwd),
    }

    if not task_path.is_file():
        out["status"] = "error"
        out["error"] = f"missing {TASK_FILE}"
        (cwd / RESULT_FILE).parent.mkdir(parents=True, exist_ok=True)
        (cwd / RESULT_FILE).write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
        return 1

    try:
        payload = json.loads(task_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        out["status"] = "error"
        out["error"] = f"invalid JSON: {e}"
        (cwd / RESULT_FILE).write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
        return 1

    out["task_id"] = payload.get("task_id")
    status = payload.get("status")
    if status != "ready_for_cursor_validation":
        out["status"] = "skipped"
        out["reason"] = f"status is {status!r}, expected ready_for_cursor_validation"
        (cwd / RESULT_FILE).write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
        return 0

    validations: list[dict[str, Any]] = []
    to_run = list(payload.get("required_validation") or [])
    if args.include_optional:
        to_run.extend(payload.get("optional_validation") or [])

    all_ok = True
    for spec in to_run:
        name = spec.get("name", "unnamed")
        cmd = spec.get("command")
        if not cmd or not isinstance(cmd, str):
            validations.append({"name": name, "ok": False, "error": "missing command"})
            all_ok = False
            continue
        code, stdout, stderr = _run_cmd(cmd, cwd)
        entry: dict[str, Any] = {
            "name": name,
            "command": cmd,
            "exit_code": code,
            "stdout": _truncate(stdout),
            "stderr": _truncate(stderr),
            "ok": code == 0,
        }
        validations.append(entry)
        if code != 0:
            all_ok = False

    out["validations"] = validations
    out["all_validations_passed"] = all_ok

    if not all_ok:
        out["status"] = "validation_failed"
        out["commit"] = {"attempted": False, "reason": "validations_failed"}
        (cwd / RESULT_FILE).write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
        print(
            f"Validation failed; see {RESULT_FILE} for details.",
            file=sys.stderr,
        )
        return 1

    allowed = _allowed_paths(payload)
    dirty = _git_porcelain(cwd)
    dirty_paths = [p for _, p in dirty]
    gate_reason = _gate_fail_reason(dirty_paths, allowed)
    out["gate"] = {
        "allowed_paths_basis": sorted(allowed),
        "dirty_paths": dirty_paths,
        "passed": gate_reason == "",
        "reason": gate_reason or None,
    }

    if gate_reason:
        out["status"] = "gate_failed"
        out["commit"] = {"attempted": False, "reason": "gatekeeper"}
        (cwd / RESULT_FILE).write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
        print(f"Gate failed: {gate_reason}", file=sys.stderr)
        return 1

    if args.no_commit:
        out["status"] = "passed_no_commit"
        out["commit"] = {"attempted": False, "reason": "no_commit_flag"}
        (cwd / RESULT_FILE).write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
        return 0

    # Write a pre-commit snapshot so docs/workflow/cursor_validation_result.json is staged with the commit.
    out["status"] = "passed_pre_commit"
    out["commit"] = {"pending": True}
    (cwd / RESULT_FILE).write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")

    commit_info = _commit_if_ok(cwd, payload, dry_run=args.dry_run)
    out["commit"] = commit_info
    if commit_info.get("attempted") and commit_info.get("ok"):
        out["status"] = "passed_committed"
    elif commit_info.get("attempted") and not commit_info.get("ok"):
        out["status"] = "gate_passed_commit_failed"
        (cwd / RESULT_FILE).write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
        print(
            "Commit failed after validation; see cursor_validation_result.json.",
            file=sys.stderr,
        )
        return 1
    else:
        out["status"] = "passed_commit_skipped"
        if commit_info.get("reason") == "nothing_to_add":
            out["note"] = "Nothing to stage; ensure files are saved and paths match task payload."

    (cwd / RESULT_FILE).write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")

    if (
        commit_info.get("attempted")
        and commit_info.get("ok")
        and not args.dry_run
    ):
        ac, _, _ = _run_cmd(f"git add -- {shlex.quote(RESULT_FILE)}", cwd)
        if ac == 0:
            _run_cmd("git commit --amend --no-edit", cwd)

    return 0 if out["status"] in ("passed_committed", "passed_no_commit", "passed_commit_skipped") else 1


if __name__ == "__main__":
    sys.exit(main())
