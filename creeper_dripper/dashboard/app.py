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


if _STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


@app.get("/", response_model=None)
def index() -> Response:
    index_path = _STATIC_DIR / "index.html"
    if not index_path.is_file():
        return JSONResponse({"error": "dashboard static files missing", "path": str(index_path)}, status_code=404)
    return FileResponse(index_path)
