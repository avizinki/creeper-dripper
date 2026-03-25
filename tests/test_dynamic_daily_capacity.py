"""Tests for effective_max_daily_new_positions (birth-baseline scaling)."""

from __future__ import annotations

import pytest

from creeper_dripper.config import load_settings
from creeper_dripper.engine.trader import CreeperDripper
from creeper_dripper.models import PortfolioState
from creeper_dripper.storage.state import new_portfolio
from creeper_dripper.utils import utc_now_iso


def _base_settings(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BIRDEYE_API_KEY", "x")
    monkeypatch.setenv("JUPITER_API_KEY", "x")
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("JOURNAL_PATH", str(tmp_path / "journal.jsonl"))
    return load_settings()


class _DummyBirdeye:
    pass


class _DummyExecutor:
    jupiter = object()


def _engine(settings: object, portfolio: PortfolioState) -> CreeperDripper:
    return CreeperDripper(settings, _DummyBirdeye(), _DummyExecutor(), portfolio)


def test_daily_cap_near_baseline_at_1x_birth(monkeypatch, tmp_path):
    settings = _base_settings(monkeypatch, tmp_path)
    monkeypatch.setenv("MAX_DAILY_NEW_POSITIONS", "6")
    monkeypatch.setenv("HARD_MAX_DAILY_NEW_POSITIONS", "30")
    monkeypatch.setenv("MAX_OPEN_POSITIONS", "20")
    monkeypatch.setenv("HARD_MAX_OPEN_POSITIONS", "50")
    monkeypatch.setenv("BASE_POSITION_SIZE_SOL", "0.2")
    monkeypatch.setenv("MIN_ORDER_SIZE_SOL", "0.03")
    monkeypatch.setenv("CASH_RESERVE_SOL", "0.25")
    monkeypatch.setenv("DYNAMIC_CAPACITY_ENABLED", "true")
    settings = load_settings()
    portfolio: PortfolioState = new_portfolio(10.0)
    birth = 10.0
    portfolio.hachi_birth_wallet_sol = birth
    engine = _engine(settings, portfolio)
    engine.set_wallet_snapshot(available_sol=birth, snapshot_at=utc_now_iso())
    eff = engine._effective_max_daily_new_positions()
    assert eff == pytest.approx(6, abs=1)


def test_daily_cap_increases_when_wallet_grows(monkeypatch, tmp_path):
    settings = _base_settings(monkeypatch, tmp_path)
    monkeypatch.setenv("MAX_DAILY_NEW_POSITIONS", "5")
    monkeypatch.setenv("HARD_MAX_DAILY_NEW_POSITIONS", "100")
    monkeypatch.setenv("MAX_OPEN_POSITIONS", "40")
    monkeypatch.setenv("HARD_MAX_OPEN_POSITIONS", "100")
    monkeypatch.setenv("BASE_POSITION_SIZE_SOL", "0.1")
    monkeypatch.setenv("MIN_ORDER_SIZE_SOL", "0.03")
    monkeypatch.setenv("CASH_RESERVE_SOL", "0.1")
    monkeypatch.setenv("DYNAMIC_CAPACITY_ENABLED", "true")
    settings = load_settings()
    portfolio: PortfolioState = new_portfolio(50.0)
    birth = 10.0
    portfolio.hachi_birth_wallet_sol = birth
    engine = _engine(settings, portfolio)
    engine.set_wallet_snapshot(available_sol=20.0, snapshot_at=utc_now_iso())
    at_2x = engine._effective_max_daily_new_positions()
    engine.set_wallet_snapshot(available_sol=40.0, snapshot_at=utc_now_iso())
    at_4x = engine._effective_max_daily_new_positions()
    assert at_4x >= at_2x
    assert at_2x >= 5


def test_hard_max_daily_is_never_exceeded(monkeypatch, tmp_path):
    settings = _base_settings(monkeypatch, tmp_path)
    monkeypatch.setenv("MAX_DAILY_NEW_POSITIONS", "4")
    monkeypatch.setenv("HARD_MAX_DAILY_NEW_POSITIONS", "7")
    monkeypatch.setenv("MAX_OPEN_POSITIONS", "50")
    monkeypatch.setenv("HARD_MAX_OPEN_POSITIONS", "80")
    monkeypatch.setenv("BASE_POSITION_SIZE_SOL", "0.05")
    monkeypatch.setenv("MIN_ORDER_SIZE_SOL", "0.03")
    monkeypatch.setenv("CASH_RESERVE_SOL", "0.01")
    monkeypatch.setenv("DYNAMIC_CAPACITY_ENABLED", "true")
    settings = load_settings()
    portfolio: PortfolioState = new_portfolio(1000.0)
    portfolio.hachi_birth_wallet_sol = 1.0
    engine = _engine(settings, portfolio)
    engine.set_wallet_snapshot(available_sol=500.0, snapshot_at=utc_now_iso())
    assert engine._effective_max_daily_new_positions() <= 7


def test_fallback_to_static_when_dynamic_disabled(monkeypatch, tmp_path):
    settings = _base_settings(monkeypatch, tmp_path)
    monkeypatch.setenv("MAX_DAILY_NEW_POSITIONS", "4")
    monkeypatch.setenv("HARD_MAX_DAILY_NEW_POSITIONS", "9")
    monkeypatch.setenv("DYNAMIC_CAPACITY_ENABLED", "false")
    settings = load_settings()
    portfolio: PortfolioState = new_portfolio(999.0)
    portfolio.hachi_birth_wallet_sol = 1.0
    engine = _engine(settings, portfolio)
    engine.set_wallet_snapshot(available_sol=500.0, snapshot_at=utc_now_iso())
    assert engine._effective_max_daily_new_positions() == 4
