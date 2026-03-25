from __future__ import annotations

import json

import pytest

from creeper_dripper.cli import main as cli_mod
from creeper_dripper.config import load_settings
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
    monkeypatch.setenv("WALLET_ADDRESS", "Gt4RRcMg2mzEN9SDtSUjEjezC9b1nXjEGDQyEVbrc7Sk")
    # Ensure no ambient dev environment leaks into this test.
    monkeypatch.delenv("SOLANA_KEYPAIR_PATH", raising=False)
    monkeypatch.delenv("BS58_PRIVATE_KEY", raising=False)


def test_cleanup_dust_classifies_and_summarizes(monkeypatch, tmp_path, capsys):
    _base_env(monkeypatch, tmp_path)
    settings = load_settings()
    portfolio: PortfolioState = new_portfolio(settings.portfolio_start_sol)

    class DummyEngine:
        def __init__(self, p):
            self.portfolio = p

    class DummyQuote:
        def __init__(self, route_ok: bool):
            self.route_ok = route_ok
            self.out_amount_atomic = 1 if route_ok else None

    class DummyExec:
        def quote_sell(self, mint, amount_atomic):
            if mint == "NOROUTE":
                return DummyQuote(False)
            return DummyQuote(True)

        def sell(self, mint, amount_atomic):
            # In DRY_RUN, real executor would return "skipped"; we accept "skipped" as "sold" bucket.
            return (type("R", (), {"status": "skipped", "diagnostic_code": "dry_run", "signature": None, "error": None})(), DummyQuote(True))

    class DummyBirdeye:
        def wallet_token_list(self, wallet):
            return {
                "wallet": wallet,
                "totalUsd": 10.0,
                "items": [
                    {"address": "DUST", "symbol": "DST", "balance": 10, "uiAmount": 0.1, "valueUsd": 0.5},
                    {"address": "NOROUTE", "symbol": "NR", "balance": 10, "uiAmount": 0.1, "valueUsd": 5.0},
                    {"address": "TRADABLE", "symbol": "TR", "balance": 10, "uiAmount": 0.1, "valueUsd": 5.0},
                ],
            }

    def _fake_build_runtime(*, require_owner, load_owner, settings=None, configure_logging=True):
        return settings or load_settings(), DummyEngine(portfolio), DummyExec(), DummyBirdeye(), None

    monkeypatch.setattr(cli_mod, "build_runtime", _fake_build_runtime)

    code = cli_mod.main(["cleanup-dust"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["leftovers_seen"] == 3
    assert out["sold"] == 1
    assert out["archived"] == 2
    assert out["still_blocked"] == 0
    archived_reasons = {it["reason"] for it in out["archived_items"]}
    assert "dust" in archived_reasons
    assert "no_route" in archived_reasons


def test_cleanup_dust_requires_wallet(monkeypatch, tmp_path):
    _base_env(monkeypatch, tmp_path)
    monkeypatch.delenv("WALLET_ADDRESS", raising=False)

    def _fake_build_runtime(*, require_owner, load_owner, settings=None, configure_logging=True):
        settings = settings or load_settings()
        return settings, type("E", (), {"portfolio": new_portfolio(settings.portfolio_start_sol)})(), object(), object(), None

    monkeypatch.setattr(cli_mod, "build_runtime", _fake_build_runtime)

    with pytest.raises(RuntimeError):
        cli_mod.main(["cleanup-dust"])

