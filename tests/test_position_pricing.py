from __future__ import annotations

from unittest.mock import MagicMock

from creeper_dripper.engine.position_pricing import (
    SOURCE_JUPITER_SELL,
    VALUATION_STATUS_NO_ROUTE,
    VALUATION_STATUS_OK,
    resolve_position_valuation,
)
from creeper_dripper.models import PositionState, ProbeQuote


def _pos(
    mint: str = "J7MzyZ4Tvwn2LBREnLU48TmKxJE28qstv35dRGBJPCpE",
    atoms: int = 1_000_000,
    decimals: int = 6,
) -> PositionState:
    ui = atoms / (10**decimals)
    return PositionState(
        token_mint=mint,
        symbol="T",
        decimals=decimals,
        status="OPEN",
        opened_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        entry_price_usd=1.0,
        avg_entry_price_usd=1.0,
        entry_sol=0.1,
        remaining_qty_atomic=atoms,
        remaining_qty_ui=ui,
        peak_price_usd=1.0,
        last_price_usd=1.0,
        entry_mark_sol_per_token=0.05,
        last_mark_sol_per_token=0.05,
        peak_mark_sol_per_token=0.05,
    )


def test_jupiter_sell_ok_sets_value_and_mark():
    ex = MagicMock()

    def quote_sell(_mint: str, amt: int):
        assert amt == 1_000_000
        return ProbeQuote(
            input_amount_atomic=amt,
            out_amount_atomic=500_000_000,
            price_impact_bps=10.0,
            route_ok=True,
            raw={},
        )

    ex.quote_sell = quote_sell
    pos = _pos(atoms=1_000_000, decimals=6)
    r = resolve_position_valuation(mint=pos.token_mint, symbol=pos.symbol, position=pos, executor=ex)
    assert r.status == VALUATION_STATUS_OK
    assert r.source == SOURCE_JUPITER_SELL
    assert r.value_sol is not None and abs(r.value_sol - 0.5) < 1e-12
    ui = 1.0
    assert r.mark_sol_per_token is not None and abs(r.mark_sol_per_token - 0.5 / ui) < 1e-12


def test_jupiter_no_route_returns_none_values():
    ex = MagicMock()
    ex.quote_sell.return_value = ProbeQuote(
        input_amount_atomic=1,
        out_amount_atomic=None,
        price_impact_bps=None,
        route_ok=False,
        raw={},
    )
    r = resolve_position_valuation(mint="m", symbol="X", position=_pos(), executor=ex)
    assert r.status == VALUATION_STATUS_NO_ROUTE
    assert r.value_sol is None
    assert r.mark_sol_per_token is None


def test_empty_position_no_route():
    ex = MagicMock()
    pos = _pos(atoms=0, decimals=6)
    pos.remaining_qty_ui = 0.0
    r = resolve_position_valuation(mint=pos.token_mint, symbol=pos.symbol, position=pos, executor=ex)
    assert r.status == VALUATION_STATUS_NO_ROUTE
    ex.quote_sell.assert_not_called()


def test_trader_exit_rules_skipped_when_no_estimated_value(monkeypatch, tmp_path):
    from creeper_dripper.config import load_settings
    from creeper_dripper.engine.trader import CreeperDripper
    from creeper_dripper.storage.state import new_portfolio

    monkeypatch.setenv("BIRDEYE_API_KEY", "x")
    monkeypatch.setenv("JUPITER_API_KEY", "x")
    monkeypatch.setenv("STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("JOURNAL_PATH", str(tmp_path / "journal.jsonl"))
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path))
    settings = load_settings()

    class X:
        jupiter = object()

    pos = _pos()
    pos.last_estimated_exit_value_sol = None
    portfolio = new_portfolio(5.0)
    portfolio.open_positions[pos.token_mint] = pos
    engine = CreeperDripper(settings, MagicMock(), X(), portfolio)
    decisions: list = []
    from creeper_dripper.models import TokenCandidate

    c = TokenCandidate(address=pos.token_mint, symbol="T", decimals=6, price_usd=0.05)
    engine._evaluate_exit_rules(pos, c, decisions, "2026-01-01T00:00:01+00:00")
    assert decisions == []
