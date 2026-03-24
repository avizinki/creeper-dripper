"""Jupiter sell-quote liquidity deterioration (JSDS) on exit evaluation."""
from __future__ import annotations

from creeper_dripper.config import load_settings
from creeper_dripper.engine.trader import CreeperDripper
from creeper_dripper.models import ExecutionResult, PortfolioState, PositionState, ProbeQuote, TakeProfitStep, TokenCandidate
from creeper_dripper.storage.state import new_portfolio

_NOW = "2026-06-15T12:00:00+00:00"
_MINT_A = "J7MzyZ4Tvwn2LBREnLU48TmKxJE28qstv35dRGBJPCpE"
_MINT_B = "5xzHELN3QZuQSm1wEejSkAha1hnxHr6KPn9uGz3dR2MA"
_MINT_C = "3FrUSsnVFHEvmvpJVPV4kSfG71kEbnYN64RvAkst25K7"


def _settings(monkeypatch, tmp_path):
    monkeypatch.setenv("BIRDEYE_API_KEY", "x")
    monkeypatch.setenv("JUPITER_API_KEY", "x")
    monkeypatch.setenv("BS58_PRIVATE_KEY", "x")
    monkeypatch.setenv("STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("JOURNAL_PATH", str(tmp_path / "journal.jsonl"))
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path))
    return load_settings()


def _base_position(mint: str, *, opened: str) -> PositionState:
    return PositionState(
        token_mint=mint,
        symbol="TK",
        decimals=6,
        status="OPEN",
        opened_at=opened,
        updated_at=opened,
        entry_price_usd=1.0,
        avg_entry_price_usd=1.0,
        entry_sol=1.0,
        remaining_qty_atomic=1_000_000,
        remaining_qty_ui=1.0,
        peak_price_usd=1.0,
        last_price_usd=1.0,
        entry_mark_sol_per_token=1.0,
        last_mark_sol_per_token=1.0,
        peak_mark_sol_per_token=1.0,
        position_id=f"{mint}:{opened}",
        take_profit_steps=[TakeProfitStep(trigger_pct=99.0, fraction=0.5)],
    )


class DummyBirdeye:
    def build_candidate(self, seed):
        return TokenCandidate(address=seed["address"], symbol=seed.get("symbol", "T"), decimals=6, price_usd=1.0)


class DummyExec:
    jupiter = object()

    def wallet_token_balance_atomic(self, _m):
        return 1_000_000

    def sell(self, *_a, **_k):
        return ExecutionResult(status="skipped", requested_amount=1, diagnostic_code="dry_run"), ProbeQuote(
            input_amount_atomic=1, out_amount_atomic=None, price_impact_bps=None, route_ok=False, raw={}
        )

    def buy(self, *_a, **_k):
        raise AssertionError("not used")


def test_jsds_soft_break_on_impact_and_hops(monkeypatch, tmp_path, caplog):
    now = _NOW
    settings = _settings(monkeypatch, tmp_path)
    portfolio: PortfolioState = new_portfolio(10.0)
    pos = _base_position(_MINT_A, opened=now)
    pos.entry_sell_impact_bps = 10.0
    pos.entry_sell_route_hops = 2
    pos.last_sell_impact_bps = 45.0
    pos.last_sell_route_hops = 3
    portfolio.open_positions[_MINT_A] = pos
    engine = CreeperDripper(settings, DummyBirdeye(), DummyExec(), portfolio)
    decisions: list = []
    c = TokenCandidate(address=_MINT_A, symbol="TK", decimals=6, price_usd=1.0)
    with caplog.at_level("INFO"):
        engine._evaluate_exit_rules(pos, c, decisions, now)
    assert any(d.reason == "liquidity_break_soft" for d in decisions)
    assert any("event=liquidity_break" in rec.message and "type=soft" in rec.message for rec in caplog.records)


def test_jsds_hard_break_on_severe_impact_and_hops(monkeypatch, tmp_path, caplog):
    now = _NOW
    settings = _settings(monkeypatch, tmp_path)
    portfolio: PortfolioState = new_portfolio(10.0)
    pos = _base_position(_MINT_A, opened=now)
    pos.entry_sell_impact_bps = 10.0
    pos.entry_sell_route_hops = 1
    pos.last_sell_impact_bps = 55.0
    pos.last_sell_route_hops = 3
    portfolio.open_positions[_MINT_A] = pos
    engine = CreeperDripper(settings, DummyBirdeye(), DummyExec(), portfolio)
    decisions: list = []
    c = TokenCandidate(address=_MINT_A, symbol="TK", decimals=6, price_usd=1.0)
    with caplog.at_level("INFO"):
        engine._evaluate_exit_rules(pos, c, decisions, now)
    assert any(d.reason == "liquidity_break_hard" for d in decisions)
    assert any("event=liquidity_break" in rec.message and "type=hard" in rec.message for rec in caplog.records)


def test_jsds_hard_break_on_quote_miss_streak(monkeypatch, tmp_path, caplog):
    now = _NOW
    settings = _settings(monkeypatch, tmp_path)
    portfolio: PortfolioState = new_portfolio(10.0)
    pos = _base_position(_MINT_A, opened=now)
    pos.entry_sell_impact_bps = 10.0
    pos.last_sell_impact_bps = 10.0
    pos.quote_miss_streak = 3
    portfolio.open_positions[_MINT_A] = pos
    engine = CreeperDripper(settings, DummyBirdeye(), DummyExec(), portfolio)
    decisions: list = []
    c = TokenCandidate(address=_MINT_A, symbol="TK", decimals=6, price_usd=1.0)
    with caplog.at_level("INFO"):
        engine._evaluate_exit_rules(pos, c, decisions, now)
    assert any(d.reason == "liquidity_break_hard" for d in decisions)


def test_jsds_suppression_blocks_soft_when_miss_ratio_high(monkeypatch, tmp_path, caplog):
    now = _NOW
    settings = _settings(monkeypatch, tmp_path)
    portfolio: PortfolioState = new_portfolio(10.0)
    p_a = _base_position(_MINT_A, opened=now)
    p_a.entry_sell_impact_bps = 10.0
    p_a.entry_sell_route_hops = 2
    p_a.last_sell_impact_bps = 45.0
    p_a.last_sell_route_hops = 3
    p_a.quote_miss_streak = 0
    p_b = _base_position(_MINT_B, opened=now)
    p_b.quote_miss_streak = 1
    p_c = _base_position(_MINT_C, opened=now)
    p_c.quote_miss_streak = 1
    portfolio.open_positions[_MINT_A] = p_a
    portfolio.open_positions[_MINT_B] = p_b
    portfolio.open_positions[_MINT_C] = p_c
    engine = CreeperDripper(settings, DummyBirdeye(), DummyExec(), portfolio)
    decisions: list = []
    c = TokenCandidate(address=_MINT_A, symbol="TK", decimals=6, price_usd=1.0)
    with caplog.at_level("INFO"):
        engine._evaluate_exit_rules(p_a, c, decisions, now)
    assert not any("liquidity_break_soft" in d.reason for d in decisions)
    assert any("event=liquidity_signal_suppressed_platform_issue" in rec.message for rec in caplog.records)


def test_jsds_suppression_allows_hard_at_streak_five(monkeypatch, tmp_path, caplog):
    now = _NOW
    settings = _settings(monkeypatch, tmp_path)
    portfolio: PortfolioState = new_portfolio(10.0)
    p_a = _base_position(_MINT_A, opened=now)
    p_a.entry_sell_impact_bps = 10.0
    p_a.last_sell_impact_bps = 10.0
    p_a.quote_miss_streak = 5
    p_b = _base_position(_MINT_B, opened=now)
    p_b.quote_miss_streak = 1
    p_c = _base_position(_MINT_C, opened=now)
    p_c.quote_miss_streak = 1
    portfolio.open_positions[_MINT_A] = p_a
    portfolio.open_positions[_MINT_B] = p_b
    portfolio.open_positions[_MINT_C] = p_c
    engine = CreeperDripper(settings, DummyBirdeye(), DummyExec(), portfolio)
    decisions: list = []
    c = TokenCandidate(address=_MINT_A, symbol="TK", decimals=6, price_usd=1.0)
    with caplog.at_level("INFO"):
        engine._evaluate_exit_rules(p_a, c, decisions, now)
    assert any(d.reason == "liquidity_break_hard" for d in decisions)
    assert not any("event=liquidity_signal_suppressed_platform_issue" in rec.message for rec in caplog.records)


def test_jsds_missing_entry_baseline_only_streak_hard(monkeypatch, tmp_path):
    now = _NOW
    settings = _settings(monkeypatch, tmp_path)
    portfolio: PortfolioState = new_portfolio(10.0)
    pos = _base_position(_MINT_A, opened=now)
    pos.entry_sell_impact_bps = None
    pos.last_sell_impact_bps = 500.0
    pos.entry_sell_route_hops = 1
    pos.last_sell_route_hops = 5
    pos.quote_miss_streak = 2
    portfolio.open_positions[_MINT_A] = pos
    engine = CreeperDripper(settings, DummyBirdeye(), DummyExec(), portfolio)
    decisions: list = []
    c = TokenCandidate(address=_MINT_A, symbol="TK", decimals=6, price_usd=1.0)
    engine._evaluate_exit_rules(pos, c, decisions, now)
    assert not decisions

    pos.quote_miss_streak = 3
    decisions.clear()
    engine._evaluate_exit_rules(pos, c, decisions, now)
    assert any(d.reason == "liquidity_break_hard" for d in decisions)


def test_jsds_deterioration_watch_logs_only(monkeypatch, tmp_path, caplog):
    now = _NOW
    settings = _settings(monkeypatch, tmp_path)
    portfolio: PortfolioState = new_portfolio(10.0)
    pos = _base_position(_MINT_A, opened=now)
    pos.entry_sell_impact_bps = 10.0
    pos.entry_sell_route_hops = 2
    pos.last_sell_impact_bps = 26.0
    pos.last_sell_route_hops = 2
    portfolio.open_positions[_MINT_A] = pos
    engine = CreeperDripper(settings, DummyBirdeye(), DummyExec(), portfolio)
    decisions: list = []
    c = TokenCandidate(address=_MINT_A, symbol="TK", decimals=6, price_usd=1.0)
    with caplog.at_level("INFO"):
        engine._evaluate_exit_rules(pos, c, decisions, now)
    assert not decisions
    assert any("event=liquidity_deterioration_watch" in rec.message for rec in caplog.records)


def test_extract_sell_quote_liquidity_route_plan():
    from creeper_dripper.engine.position_pricing import extract_sell_quote_liquidity

    raw = {
        "routePlan": [
            {"swapInfo": {"label": "Raydium", "ammKey": "abc"}},
            {"swapInfo": {"label": "Orca", "ammKey": "def"}},
        ]
    }
    q = ProbeQuote(
        input_amount_atomic=100,
        out_amount_atomic=50,
        price_impact_bps=12.5,
        route_ok=True,
        raw=raw,
    )
    impact, hops, label = extract_sell_quote_liquidity(q)
    assert impact == 12.5
    assert hops == 2
    assert label == "Raydium"
