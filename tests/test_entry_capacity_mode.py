from __future__ import annotations

from creeper_dripper.config import load_settings
from creeper_dripper.engine.trader import CreeperDripper
from creeper_dripper.models import ExecutionResult, PortfolioState, PositionState, ProbeQuote, TakeProfitStep, TokenCandidate
from creeper_dripper.storage.state import new_portfolio
from creeper_dripper.utils import utc_now_iso


class DummyBirdeye:
    pass


class DummyExecutor:
    def __init__(self):
        self.jupiter = object()

    def transaction_status(self, _sig):
        return None

    def quote_buy(self, _candidate, _size_sol):
        # viable route
        return ProbeQuote(input_amount_atomic=10_000_000, out_amount_atomic=50_000, price_impact_bps=50.0, route_ok=True, raw={})

    def quote_sell(self, _token_mint, _amount_atomic):
        # viable route
        return ProbeQuote(input_amount_atomic=50_000, out_amount_atomic=9_000_000, price_impact_bps=50.0, route_ok=True, raw={})

    def buy(self, candidate, size_sol):
        # success buy
        return ExecutionResult(status="success", requested_amount=int(size_sol * 1_000_000_000), executed_amount=100_000), ProbeQuote(
            input_amount_atomic=10_000_000, out_amount_atomic=100_000, price_impact_bps=50.0, route_ok=True, raw={}
        )

    def sell(self, *_args, **_kwargs):
        raise AssertionError("exit logic not part of these tests")


def _settings(monkeypatch, tmp_path, *, mode: str):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BIRDEYE_API_KEY", "x")
    monkeypatch.setenv("JUPITER_API_KEY", "x")
    monkeypatch.setenv("BS58_PRIVATE_KEY", "x")
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("JOURNAL_PATH", str(tmp_path / "journal.jsonl"))
    monkeypatch.setenv("DISCOVERY_INTERVAL_SECONDS", "30")
    monkeypatch.setenv("MAX_ACTIVE_CANDIDATES", "5")
    monkeypatch.setenv("ENTRY_CAPACITY_MODE", mode)
    monkeypatch.setenv("EARLY_RISK_BUCKET_ENABLED", "true")
    monkeypatch.setenv("EARLY_RISK_POSITION_SIZE_SOL", "0.03")
    monkeypatch.setenv("MIN_ORDER_SIZE_SOL", "0.03")
    monkeypatch.setenv("BASE_POSITION_SIZE_SOL", "0.06")
    monkeypatch.setenv("MAX_POSITION_SIZE_SOL", "0.10")
    monkeypatch.setenv("MAX_OPEN_POSITIONS", "3")
    monkeypatch.setenv("MAX_DAILY_NEW_POSITIONS", "10")
    monkeypatch.setenv("CASH_RESERVE_SOL", "0.20")
    return load_settings()


def _candidate(mint: str, symbol: str, *, early_risk: bool) -> TokenCandidate:
    raw = {"early_risk_bucket": True} if early_risk else {}
    return TokenCandidate(
        address=mint,
        symbol=symbol,
        decimals=6,
        liquidity_usd=200_000,
        exit_liquidity_usd=150_000,
        volume_24h_usd=500_000,
        buy_sell_ratio_1h=1.5,
        age_hours=12.0,
        discovery_score=80.0,
        sell_route_available=True,
        jupiter_buy_price_impact_bps=50.0,
        jupiter_sell_price_impact_bps=50.0,
        raw=raw,
    )


def _seed_open_position(portfolio: PortfolioState, mint: str) -> None:
    now = utc_now_iso()
    portfolio.open_positions[mint] = PositionState(
        token_mint=mint,
        symbol="X",
        decimals=6,
        status="OPEN",
        opened_at=now,
        updated_at=now,
        entry_price_usd=0.0,
        avg_entry_price_usd=0.0,
        entry_sol=0.01,
        remaining_qty_atomic=1,
        remaining_qty_ui=0.000001,
        peak_price_usd=0.0,
        last_price_usd=0.0,
        take_profit_steps=[TakeProfitStep(trigger_pct=25.0, fraction=0.1)],
    )


def test_strict_blocks_early_risk_when_open_positions_gt_1(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path, mode="strict")
    portfolio = new_portfolio(1.0)
    _seed_open_position(portfolio, "mintA")
    _seed_open_position(portfolio, "mintB")
    engine = CreeperDripper(settings, DummyBirdeye(), DummyExecutor(), portfolio)
    decisions = []
    engine._maybe_open_positions([_candidate("mintE", "EARLY", early_risk=True)], decisions, utc_now_iso())
    assert "mintE" not in portfolio.open_positions
    events = engine.events.to_dicts()
    assert any(e["event_type"] == "entry_capacity_decision" and e["metadata"].get("allowed") is False for e in events)


def test_balanced_orders_standard_before_early(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path, mode="balanced")
    portfolio = new_portfolio(1.0)
    engine = CreeperDripper(settings, DummyBirdeye(), DummyExecutor(), portfolio)
    decisions = []
    early = _candidate("mintE", "EARLY", early_risk=True)
    standard = _candidate("mintS", "STD", early_risk=False)
    # Provide reversed; allocator must reorder standard first.
    engine._maybe_open_positions([early, standard], decisions, utc_now_iso())
    buys = [d for d in decisions if getattr(d, "action", None) == "BUY"]
    assert buys, "expected at least one entry"
    assert buys[0].token_mint == "mintS"


def test_fill_slots_enters_early_with_capped_size(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path, mode="fill_slots")
    portfolio = new_portfolio(1.0)
    engine = CreeperDripper(settings, DummyBirdeye(), DummyExecutor(), portfolio)
    decisions = []
    engine._maybe_open_positions(
        [_candidate("mintS", "STD", early_risk=False), _candidate("mintE", "EARLY", early_risk=True)],
        decisions,
        utc_now_iso(),
    )
    buys = [d for d in decisions if getattr(d, "action", None) == "BUY"]
    assert len(buys) >= 2
    early_buy = next(d for d in buys if d.token_mint == "mintE")
    assert early_buy.size_sol == 0.03


def test_entry_capacity_mode_validation(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BIRDEYE_API_KEY", "x")
    monkeypatch.setenv("JUPITER_API_KEY", "x")
    monkeypatch.setenv("BS58_PRIVATE_KEY", "x")
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("JOURNAL_PATH", str(tmp_path / "journal.jsonl"))
    monkeypatch.setenv("DISCOVERY_INTERVAL_SECONDS", "30")
    monkeypatch.setenv("MAX_ACTIVE_CANDIDATES", "5")

    # valid values
    for val in ("strict", "balanced", "fill_slots"):
        monkeypatch.setenv("ENTRY_CAPACITY_MODE", val)
        load_settings()

    # invalid value
    monkeypatch.setenv("ENTRY_CAPACITY_MODE", "nope")
    try:
        load_settings()
    except ValueError as exc:
        assert "Invalid ENTRY_CAPACITY_MODE: nope" in str(exc)
    else:
        raise AssertionError("expected ValueError for invalid ENTRY_CAPACITY_MODE")

