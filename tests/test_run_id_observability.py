from __future__ import annotations

import json
import logging
from pathlib import Path

from creeper_dripper.config import load_settings
from creeper_dripper.engine.trader import CreeperDripper
from creeper_dripper.models import TradeDecision
from creeper_dripper.storage.state import load_portfolio, new_portfolio
from creeper_dripper.utils import setup_logging


def _base_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BIRDEYE_API_KEY", "x")
    monkeypatch.setenv("JUPITER_API_KEY", "x")
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("STATE_PATH", str(tmp_path / "runtime" / "state.json"))
    monkeypatch.setenv("JOURNAL_PATH", str(tmp_path / "runtime" / "journal.jsonl"))
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    monkeypatch.setenv("DISCOVERY_INTERVAL_SECONDS", "30")
    monkeypatch.setenv("MAX_ACTIVE_CANDIDATES", "7")
    monkeypatch.setenv("CANDIDATE_CACHE_TTL_SECONDS", "20")
    monkeypatch.setenv("ROUTE_CHECK_CACHE_TTL_SECONDS", "15")


def test_run_id_persisted_to_state_status_journal(monkeypatch, tmp_path):
    _base_env(monkeypatch, tmp_path)
    settings = load_settings()
    settings.run_id = "2026-03-25T05-12-33Z_ab12cd"
    settings.run_dir = settings.runtime_dir / "runs" / settings.run_id
    settings.run_dir.mkdir(parents=True, exist_ok=True)
    settings.run_log_path = settings.run_dir / "logfile.log"

    class DummyExec:
        jupiter = object()

    class DummyBirdeye:
        pass

    portfolio = new_portfolio(settings.portfolio_start_sol)
    engine = CreeperDripper(settings, DummyBirdeye(), DummyExec(), portfolio)
    cycle_summary = engine._cycle_summary("2026-03-25T05:12:34+00:00", {}, [])
    engine._persist_cycle(
        "2026-03-25T05:12:34+00:00",
        [TradeDecision(action="BUY", token_mint="mint", symbol="T", reason="r")],
        cycle_summary,
    )

    state_payload = json.loads(settings.state_path.read_text(encoding="utf-8"))
    status_payload = json.loads((settings.runtime_dir / "status.json").read_text(encoding="utf-8"))
    journal_line = json.loads(settings.journal_path.read_text(encoding="utf-8").splitlines()[0])
    assert state_payload["run_id"] == settings.run_id
    assert status_payload["run_id"] == settings.run_id
    assert status_payload["cycle_in_run"] == 0
    assert journal_line["run_id"] == settings.run_id
    assert journal_line["cycle_in_run"] == 0


def test_setup_logging_creates_per_run_logfile(tmp_path):
    runtime_dir = tmp_path / "runtime"
    run_log = runtime_dir / "runs" / "rid" / "logfile.log"
    setup_logging("INFO", runtime_dir=runtime_dir, run_log_path=run_log)
    logging.getLogger("creeper_dripper.tests").warning("event=test_per_run_logfile")
    assert run_log.exists()
    text = run_log.read_text(encoding="utf-8")
    assert "event=test_per_run_logfile" in text


def test_snapshot_readme_includes_run_id_metadata(monkeypatch, tmp_path):
    from tools import runtime_snapshot_monitor as mon

    monkeypatch.setattr(mon, "sh", lambda *args, **kwargs: "x")
    repo = tmp_path
    runtime = repo / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    run_id = "2026-03-25T05-12-33Z_ab12cd"
    run_dir = runtime / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "logfile.log").write_text("hello\n", encoding="utf-8")
    (repo / ".env").write_text("DRIP_EXIT_ENABLED=true\n", encoding="utf-8")
    (runtime / "state.json").write_text(json.dumps({"open_positions": {}}), encoding="utf-8")
    (runtime / "status.json").write_text(
        json.dumps({"run_id": run_id, "cycle_in_run": 42, "cycle_timestamp": "2026-03-25T05:12:34+00:00"}),
        encoding="utf-8",
    )
    (runtime / "scan_latest.json").write_text("{}", encoding="utf-8")
    (runtime / "entry_probe_PRl_foo.json").write_text("{}", encoding="utf-8")

    dest = repo / "review_artifacts" / "runtime_snapshots" / "t"
    mon.write_snapshot(dest, runtime, 50, repo)
    readme = (dest / "README.md").read_text(encoding="utf-8")
    assert f"| Run ID | `{run_id}` |" in readme
    assert f"| Source run folder | `runtime/runs/{run_id}` |" in readme
    assert "| Cycle in run | `42` |" in readme
