from __future__ import annotations

import json
from json import JSONDecoder

import creeper_dripper.cli.main as main_mod
from creeper_dripper.cli.main import main
from creeper_dripper.config import load_settings
from creeper_dripper.engine.trader import CreeperDripper
from creeper_dripper.models import PortfolioState, PositionState, TakeProfitStep
from creeper_dripper.storage.state import load_portfolio, new_portfolio, save_portfolio


def _parse_json_prefix(output: str) -> dict:
    decoder = JSONDecoder()
    obj, _idx = decoder.raw_decode(output)
    assert isinstance(obj, dict)
    return obj


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
    # Visibility-only wallet snapshot (doctor) needs a wallet address.
    monkeypatch.setenv("WALLET_ADDRESS", "Gt4RRcMg2mzEN9SDtSUjEjezC9b1nXjEGDQyEVbrc7Sk")
    monkeypatch.delenv("SOLANA_KEYPAIR_PATH", raising=False)
    monkeypatch.delenv("BS58_PRIVATE_KEY", raising=False)


def test_doctor_without_wallet_scan_safe(monkeypatch, tmp_path, capsys):
    _base_env(monkeypatch, tmp_path)
    from creeper_dripper.clients import birdeye as birdeye_mod
    from creeper_dripper.clients import jupiter as jupiter_mod
    from creeper_dripper.execution import executor as executor_mod

    monkeypatch.setattr(birdeye_mod.BirdeyeClient, "trending_tokens", lambda self, limit=1: [])
    monkeypatch.setattr(
        birdeye_mod.BirdeyeClient,
        "wallet_token_list",
        lambda self, wallet, ui_amount_mode="scaled": {"wallet": wallet, "totalUsd": 0.0, "items": []},
    )
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
    monkeypatch.setattr(executor_mod.TradeExecutor, "native_sol_balance_lamports", lambda self, _w: 1_000_000_000)

    code = main(["doctor"])
    stdout = capsys.readouterr().out
    out = _parse_json_prefix(stdout)
    assert code == 0
    assert out["ok"] is True
    assert "=== ENV SNAPSHOT (masked) ===" in stdout
    # Doctor should initialize Hachi birth baseline from first wallet snapshot (and persist it).
    settings = load_settings()
    portfolio = load_portfolio(settings.state_path, settings.portfolio_start_sol)
    assert portfolio.hachi_birth_wallet_sol is not None
    assert portfolio.hachi_birth_timestamp is not None


def test_doctor_does_not_overwrite_hachi_birth(monkeypatch, tmp_path):
    _base_env(monkeypatch, tmp_path)
    from creeper_dripper.clients import birdeye as birdeye_mod
    from creeper_dripper.clients import jupiter as jupiter_mod
    from creeper_dripper.execution import executor as executor_mod

    # Seed existing birth baseline in state.
    settings = load_settings()
    portfolio = new_portfolio(settings.portfolio_start_sol)
    portfolio.hachi_birth_wallet_sol = 9.0
    portfolio.hachi_birth_timestamp = "2026-01-01T00:00:00+00:00"
    save_portfolio(settings.state_path, portfolio)

    monkeypatch.setattr(birdeye_mod.BirdeyeClient, "trending_tokens", lambda self, limit=1: [])
    monkeypatch.setattr(
        birdeye_mod.BirdeyeClient,
        "wallet_token_list",
        lambda self, wallet, ui_amount_mode="scaled": {"wallet": wallet, "totalUsd": 0.0, "items": []},
    )
    monkeypatch.setattr(jupiter_mod.JupiterClient, "probe_quote", lambda self, **kwargs: {"ok": True})
    monkeypatch.setattr(jupiter_mod.JupiterClient, "check_swap_reachability", lambda self: None)
    monkeypatch.setattr(executor_mod.TradeExecutor, "native_sol_balance_lamports", lambda self, _w: 2_000_000_000)

    code = main(["doctor"])
    assert code == 0
    reloaded = load_portfolio(settings.state_path, settings.portfolio_start_sol)
    assert reloaded.hachi_birth_wallet_sol == 9.0
    assert reloaded.hachi_birth_timestamp == "2026-01-01T00:00:00+00:00"


def test_doctor_invalid_wallet_path(monkeypatch, tmp_path):
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("SOLANA_KEYPAIR_PATH", str(tmp_path / "wallets" / "missing.json"))
    code = main(["doctor"])
    assert code == 1


def _run_mocks(monkeypatch):
    from creeper_dripper.clients import birdeye as birdeye_mod
    from creeper_dripper.clients import jupiter as jupiter_mod
    from creeper_dripper.execution import executor as executor_mod

    monkeypatch.setattr(birdeye_mod.BirdeyeClient, "trending_tokens", lambda self, limit=1: [])
    monkeypatch.setattr(
        birdeye_mod.BirdeyeClient,
        "wallet_token_list",
        lambda self, wallet, ui_amount_mode="scaled": {"wallet": wallet, "totalUsd": 0.0, "items": []},
    )
    monkeypatch.setattr(jupiter_mod.JupiterClient, "probe_quote", lambda self, **kwargs: {"ok": True})
    monkeypatch.setattr(jupiter_mod.JupiterClient, "check_swap_reachability", lambda self: None)
    monkeypatch.setattr(executor_mod.TradeExecutor, "native_sol_balance_lamports", lambda self, _w: 1_000_000_000)


def _minimal_run_cycle_output():
    return {
        "timestamp": "2026-01-01T00:00:00+00:00",
        "cash_sol": 5.0,
        "open_positions": 0,
        "candidate_symbols": [],
        "decisions": [],
        "summary": {
            "cache_hits": 0,
            "cache_misses": 0,
            "candidate_cache_hits": 0,
            "candidate_cache_misses": 0,
            "route_cache_hits": 0,
            "route_cache_misses": 0,
            "discovered_candidates": 0,
            "prefiltered_candidates": 0,
            "candidates_built": 0,
            "topn_candidates": 0,
            "route_checked_candidates": 0,
            "candidates_accepted": 0,
            "discovery_cached": False,
            "cache_debug_first_keys": [],
            "cache_debug_identity": {},
            "cache_engine_identity": {},
            "cache_debug_trace": {},
            "entries_skipped_dry_run": 0,
            "entries_skipped_live_disabled": 0,
            "entries_attempted": 0,
            "entries_succeeded": 0,
            "exits_attempted": 0,
            "exits_succeeded": 0,
            "exit_blocked_positions": 0,
            "execution_failures": 0,
        },
        "events": [],
    }


def test_run_preflight_fails_when_birdeye_unreachable(monkeypatch, tmp_path, capsys):
    _base_env(monkeypatch, tmp_path)
    from creeper_dripper.clients import birdeye as birdeye_mod

    def _boom(self, limit=1):
        raise RuntimeError("birdeye down")

    monkeypatch.setattr(birdeye_mod.BirdeyeClient, "trending_tokens", _boom)
    code = main(["run", "--once"])
    out = capsys.readouterr().out
    assert code == 1
    assert "Preflight doctor: FAILED" in out
    assert "birdeye_auth" in out
    assert "run_started" not in out


def test_run_preflight_ok_runs_once(monkeypatch, tmp_path, capsys):
    _base_env(monkeypatch, tmp_path)
    _run_mocks(monkeypatch)
    monkeypatch.setattr(main_mod, "_start_dashboard_process", lambda _settings: None)
    monkeypatch.setattr(CreeperDripper, "run_startup_recovery", lambda self: [])
    monkeypatch.setattr(CreeperDripper, "run_cycle", lambda self: _minimal_run_cycle_output())
    code = main(["run", "--once"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Preflight doctor: ok" in out
    assert "Preflight summary:" in out
    assert "[preflight] capacity (config):" in out
    assert "run_started" in out
    assert "STARTUP DYNAMIC CAPACITY" in out


def test_status_command_empty_state(monkeypatch, tmp_path, capsys):
    _base_env(monkeypatch, tmp_path)
    code = main(["status"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["open_positions"] == 0
    assert out["closed_positions"] == 0
    assert out["exit_blocked_positions"] == 0
    assert out["zombie_positions"] == 0
    assert out["blocked_or_zombie_symbols"] == []
    assert out["blocked_or_zombie_positions"] == []


def test_status_command_populated_state(monkeypatch, tmp_path, capsys):
    _base_env(monkeypatch, tmp_path)
    settings = load_settings()
    portfolio: PortfolioState = new_portfolio(settings.portfolio_start_sol)
    portfolio.safe_mode_active = True
    portfolio.safety_stop_reason = "safety_daily_loss_cap"
    portfolio.opened_today_count = 2
    now = "2026-01-01T00:00:00+00:00"
    # Valid Solana pubkeys for persistence tests (save_portfolio drops non-pubkey mints).
    mint_blocked = "J7MzyZ4Tvwn2LBREnLU48TmKxJE28qstv35dRGBJPCpE"
    mint_zombie = "Fh2PG8Cnp9cJ3QhL3Zf3V7i8k5hQwJm6n8Yd7s9aQx1B"
    # Seed one EXIT_BLOCKED and one ZOMBIE position to validate operator visibility.
    portfolio.open_positions[mint_blocked] = PositionState(
        token_mint=mint_blocked,
        symbol="BLK",
        decimals=6,
        status="EXIT_BLOCKED",
        opened_at=now,
        updated_at=now,
        entry_price_usd=0.0,
        avg_entry_price_usd=0.0,
        entry_sol=0.1,
        remaining_qty_atomic=1_000_000,
        remaining_qty_ui=1.0,
        peak_price_usd=0.0,
        last_price_usd=0.0,
        take_profit_steps=[TakeProfitStep(trigger_pct=25.0, fraction=0.1)],
        valuation_status="no_route",
        exit_blocked_cycles=7,
    )
    portfolio.open_positions[mint_zombie] = PositionState(
        token_mint=mint_zombie,
        symbol="ZMB",
        decimals=6,
        status="ZOMBIE",
        opened_at=now,
        updated_at=now,
        entry_price_usd=0.0,
        avg_entry_price_usd=0.0,
        entry_sol=0.1,
        remaining_qty_atomic=1_000_000,
        remaining_qty_ui=1.0,
        peak_price_usd=0.0,
        last_price_usd=0.0,
        take_profit_steps=[TakeProfitStep(trigger_pct=25.0, fraction=0.1)],
        valuation_status="no_route",
        exit_blocked_cycles=12,
        zombie_reason="no_route_persistent",
        zombie_since=now,
    )
    from creeper_dripper.storage.state import save_portfolio

    save_portfolio(settings.state_path, portfolio)
    code = main(["status"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["safe_mode_active"] is True
    assert out["opened_today_count"] == 2
    assert out["exit_blocked_positions"] == 1
    assert out["zombie_positions"] == 1
    assert set(out["blocked_or_zombie_symbols"]) == {"BLK", "ZMB"}
    detailed = {(p["symbol"], p["status"]) for p in out["blocked_or_zombie_positions"]}
    assert ("BLK", "EXIT_BLOCKED") in detailed
    assert ("ZMB", "ZOMBIE") in detailed


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
