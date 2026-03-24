from __future__ import annotations

import json

from creeper_dripper.cli.main import main
from creeper_dripper.config import load_settings
from creeper_dripper.engine.trader import CreeperDripper
from creeper_dripper.models import PortfolioState
from creeper_dripper.storage.state import new_portfolio


def _base_env(monkeypatch, tmp_path):
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
    monkeypatch.delenv("SOLANA_KEYPAIR_PATH", raising=False)
    monkeypatch.delenv("BS58_PRIVATE_KEY", raising=False)


def test_doctor_without_wallet_scan_safe(monkeypatch, tmp_path, capsys):
    _base_env(monkeypatch, tmp_path)
    from creeper_dripper.clients import birdeye as birdeye_mod
    from creeper_dripper.clients import jupiter as jupiter_mod

    monkeypatch.setattr(birdeye_mod.BirdeyeClient, "trending_tokens", lambda self, limit=1: [])
    monkeypatch.setattr(
        jupiter_mod.JupiterClient,
        "probe_quote",
        lambda self, **kwargs: {"ok": True},
    )
    monkeypatch.setattr(
        jupiter_mod.JupiterClient,
        "check_swap_reachability",
        lambda self: None,
    )

    code = main(["doctor"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["ok"] is True


def test_doctor_invalid_wallet_path(monkeypatch, tmp_path):
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("SOLANA_KEYPAIR_PATH", str(tmp_path / "wallets" / "missing.json"))
    code = main(["doctor"])
    assert code == 1


def test_status_command_empty_state(monkeypatch, tmp_path, capsys):
    _base_env(monkeypatch, tmp_path)
    code = main(["status"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["open_positions"] == 0
    assert out["closed_positions"] == 0


def test_status_command_populated_state(monkeypatch, tmp_path, capsys):
    _base_env(monkeypatch, tmp_path)
    settings = load_settings()
    portfolio: PortfolioState = new_portfolio(settings.portfolio_start_sol)
    portfolio.safe_mode_active = True
    portfolio.safety_stop_reason = "safety_daily_loss_cap"
    portfolio.opened_today_count = 2
    from creeper_dripper.storage.state import save_portfolio

    save_portfolio(settings.state_path, portfolio)
    code = main(["status"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["safe_mode_active"] is True
    assert out["opened_today_count"] == 2


def test_runtime_status_snapshot_creation_after_cycle(monkeypatch, tmp_path):
    _base_env(monkeypatch, tmp_path)
    settings = load_settings()
    portfolio: PortfolioState = new_portfolio(settings.portfolio_start_sol)

    class DummyExec:
        jupiter = object()

    class DummyBirdeye:
        pass

    engine = CreeperDripper(settings, DummyBirdeye(), DummyExec(), portfolio)
    from creeper_dripper.engine import trader as trader_mod

    monkeypatch.setattr(trader_mod, "discover_candidates", lambda *args, **kwargs: ([], {"seeds_total": 0, "candidates_built": 0, "candidates_accepted": 0, "candidates_rejected_total": 0, "rejection_counts": {}}))
    monkeypatch.setattr(CreeperDripper, "_mark_positions", lambda self, c, d, n: None)
    monkeypatch.setattr(CreeperDripper, "_maybe_open_positions", lambda self, c, d, n: None)

    engine.run_cycle()
    status_path = settings.runtime_dir / "status.json"
    assert status_path.exists()
    payload = json.loads(status_path.read_text(encoding="utf-8"))
    assert "summary" in payload
