from __future__ import annotations

import base64
import json
import logging
import os
import tempfile
import time
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

LOGGER = logging.getLogger("creeper_dripper")

_LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
_RUNTIME_FILE_HANDLER_MARKER = "_creeper_dripper_runtime_logfile"
_RUN_FILE_HANDLER_MARKER = "_creeper_dripper_run_logfile"
_LOGFILE_MAX_BYTES = 10 * 1024 * 1024


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    val = raw.strip().lower()
    if val in {"1", "true", "yes", "y", "on"}:
        return True
    if val in {"0", "false", "no", "n", "off"}:
        return False
    return default


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(float(raw.strip()))
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default


def env_csv_floats(name: str, default: list[float]) -> list[float]:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    vals: list[float] = []
    for piece in raw.split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            vals.append(float(piece))
        except ValueError:
            continue
    return vals or default


def env_str(name: str, default: str) -> str:
    raw = os.getenv(name)
    return raw.strip() if raw is not None and raw.strip() else default


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def json_default(obj: Any) -> Any:
    if is_dataclass(obj):
        return asdict(obj)
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def atomic_write_json(path: Path, payload: Any) -> None:
    ensure_parent(path)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False, default=json_default)
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def append_jsonl(path: Path, obj: Any) -> None:
    ensure_parent(path)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(obj, ensure_ascii=False, default=json_default) + "\n")


def pct_change(new: float | None, old: float | None) -> float | None:
    if new is None or old is None or old == 0:
        return None
    return ((new - old) / old) * 100.0


def clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def chunks(values: Iterable[Any], size: int) -> list[list[Any]]:
    bucket: list[Any] = []
    out: list[list[Any]] = []
    for value in values:
        bucket.append(value)
        if len(bucket) >= size:
            out.append(bucket)
            bucket = []
    if bucket:
        out.append(bucket)
    return out


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("utf-8")


def b64decode(data: str) -> bytes:
    return base64.b64decode(data.encode("utf-8"))


def _maybe_rotate_logfile(path: Path, *, max_bytes: int) -> None:
    if not path.exists():
        return
    try:
        if path.stat().st_size <= max_bytes:
            return
    except OSError:
        return
    rotated = path.with_name(f"{path.name}.1")
    if rotated.exists():
        rotated.unlink()
    path.rename(rotated)


def setup_logging(level: str, *, runtime_dir: Path | None = None, run_log_path: Path | None = None) -> None:
    level_no = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(level=level_no, format=_LOG_FORMAT)
    if runtime_dir is None:
        return
    root = logging.getLogger()
    if not any(getattr(h, _RUNTIME_FILE_HANDLER_MARKER, False) for h in root.handlers):
        runtime_dir.mkdir(parents=True, exist_ok=True)
        log_path = runtime_dir / "logfile.log"
        _maybe_rotate_logfile(log_path, max_bytes=_LOGFILE_MAX_BYTES)
        file_handler = logging.FileHandler(log_path, mode="a", encoding="utf-8")
        setattr(file_handler, _RUNTIME_FILE_HANDLER_MARKER, True)
        file_handler.setLevel(logging.NOTSET)
        file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        root.addHandler(file_handler)
    if run_log_path is None:
        return
    ensure_parent(run_log_path)
    run_log_path.touch(exist_ok=True)
    resolved = str(run_log_path.resolve())
    if any(
        getattr(h, _RUN_FILE_HANDLER_MARKER, False)
        and str(getattr(h, "baseFilename", None)) == resolved
        for h in root.handlers
    ):
        return
    _maybe_rotate_logfile(run_log_path, max_bytes=_LOGFILE_MAX_BYTES)
    run_handler = logging.FileHandler(run_log_path, mode="a", encoding="utf-8")
    setattr(run_handler, _RUN_FILE_HANDLER_MARKER, True)
    run_handler.setLevel(logging.NOTSET)
    run_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    root.addHandler(run_handler)


def mask_secret(secret: str) -> str:
    s = secret.strip()
    if len(s) <= 8:
        return "*" * len(s)
    return f"{s[:4]}…{s[-4:]}"


def monotonic_sleep_until(next_run: float) -> None:
    now = time.monotonic()
    if next_run > now:
        time.sleep(next_run - now)
