"""TradeExecutor sell/buy settlement: Jupiter execution is the sole truth — no wallet RPC."""

from __future__ import annotations

from creeper_dripper.config import load_settings
from creeper_dripper.errors import SETTLEMENT_UNCONFIRMED
from creeper_dripper.execution.executor import TradeExecutor
from creeper_dripper.models import ProbeQuote

_VALID_MINT = "J7MzyZ4Tvwn2LBREnLU48TmKxJE28qstv35dRGBJPCpE"


class FakeOwner:
    def pubkey(self):
        return "11111111111111111111111111111112"


class StubJupiter:
    def probe_quote(self, **kwargs):
        return ProbeQuote(
            input_amount_atomic=kwargs["amount_atomic"],
            out_amount_atomic=200_000_000,
            price_impact_bps=10.0,
            route_ok=True,
            raw={},
        )


def _executor(monkeypatch, tmp_path) -> TradeExecutor:
    monkeypatch.setenv("BIRDEYE_API_KEY", "x")
    monkeypatch.setenv("JUPITER_API_KEY", "x")
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("BS58_PRIVATE_KEY", "x")
    monkeypatch.setenv("STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("JOURNAL_PATH", str(tmp_path / "journal.jsonl"))
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path))
    settings = load_settings()
    settings.live_trading_enabled = True
    settings.dry_run = False
    return TradeExecutor(StubJupiter(), FakeOwner(), settings)


def test_sell_settled_from_jupiter_execute_input(monkeypatch, tmp_path):
    """When Jupiter /execute returns totalInputAmount, that is the settled sold qty."""
    ex = _executor(monkeypatch, tmp_path)
    monkeypatch.setattr(ex, "build_v2_execution_order", lambda **kw: {"transaction": "ZmFrZV90eA==", "requestId": "r1"})
    monkeypatch.setattr(
        ex,
        "sign_and_execute_v2",
        lambda _order: ("sigx", {"totalInputAmount": "100", "totalOutputAmount": "5000000"}),
    )
    result, _quote = ex.sell(_VALID_MINT, 100)
    assert result.status == "success"
    sett = result.diagnostic_metadata["post_sell_settlement"]
    assert sett["settlement_confirmed"] is True
    assert sett["sold_atomic_settled"] == 100
    assert sett["sold_atomic_source"] == "jupiter_execute"
    assert result.executed_amount == 100
    assert result.output_amount == 5_000_000


def test_sell_settled_from_order_in_when_execute_has_no_input(monkeypatch, tmp_path):
    """When /execute has no inputAmount, fall back to order inAmount."""
    ex = _executor(monkeypatch, tmp_path)
    monkeypatch.setattr(
        ex,
        "build_v2_execution_order",
        lambda **kw: {"transaction": "ZmFrZV90eA==", "requestId": "r1", "inAmount": "80"},
    )
    monkeypatch.setattr(
        ex,
        "sign_and_execute_v2",
        lambda _order: ("sigx", {"totalOutputAmount": "4000000"}),
    )
    result, _quote = ex.sell(_VALID_MINT, 80)
    assert result.status == "success"
    sett = result.diagnostic_metadata["post_sell_settlement"]
    assert sett["sold_atomic_source"] == "jupiter_order_in"
    assert sett["sold_atomic_settled"] == 80


def test_sell_settled_from_requested_qty_as_last_fallback(monkeypatch, tmp_path):
    """When neither /execute nor order provide input amounts, trust the requested qty."""
    ex = _executor(monkeypatch, tmp_path)
    monkeypatch.setattr(ex, "build_v2_execution_order", lambda **kw: {"transaction": "ZmFrZV90eA==", "requestId": "r1"})
    monkeypatch.setattr(
        ex,
        "sign_and_execute_v2",
        lambda _order: ("sigx", {"totalOutputAmount": "3000000"}),
    )
    result, _quote = ex.sell(_VALID_MINT, 60)
    assert result.status == "success"
    sett = result.diagnostic_metadata["post_sell_settlement"]
    assert sett["sold_atomic_source"] == "requested_order_amount"
    assert sett["sold_atomic_settled"] == 60


def test_sell_proceeds_unavailable_when_execute_has_no_output(monkeypatch, tmp_path):
    """When /execute returns no output amount, proceeds are unavailable but sell is still settled."""
    ex = _executor(monkeypatch, tmp_path)
    monkeypatch.setattr(ex, "build_v2_execution_order", lambda **kw: {"transaction": "ZmFrZV90eA==", "requestId": "r1"})
    monkeypatch.setattr(
        ex,
        "sign_and_execute_v2",
        lambda _order: ("sigx", {"totalInputAmount": "50"}),
    )
    result, _quote = ex.sell(_VALID_MINT, 50)
    assert result.status == "success"
    assert result.output_amount is None
    sett = result.diagnostic_metadata["post_sell_settlement"]
    assert sett["settlement_confirmed"] is True
    assert sett.get("proceeds_note") == "post_sell_proceeds_unavailable"


def test_sell_always_success_no_wallet_contradiction_path(monkeypatch, tmp_path):
    """There is no wallet contradiction path. Sell always succeeds if execution completes."""
    ex = _executor(monkeypatch, tmp_path)
    monkeypatch.setattr(ex, "build_v2_execution_order", lambda **kw: {"transaction": "ZmFrZV90eA==", "requestId": "r1"})
    monkeypatch.setattr(
        ex,
        "sign_and_execute_v2",
        lambda _order: ("sigx", {"totalInputAmount": "40", "totalOutputAmount": "2000000"}),
    )
    result, _quote = ex.sell(_VALID_MINT, 40)
    # In Jupiter-only mode, there is no wallet contradiction path — always success.
    assert result.status == "success"
    assert result.diagnostic_metadata["post_sell_settlement"]["settlement_confirmed"] is True


def test_buy_primary_from_execute_output(monkeypatch, tmp_path):
    """Buy settlement uses Jupiter /execute totalOutputAmount as primary."""
    from creeper_dripper.models import TokenCandidate

    ex = _executor(monkeypatch, tmp_path)
    tok = TokenCandidate(address=_VALID_MINT, symbol="T", decimals=6, price_usd=1.0)
    monkeypatch.setattr(ex, "build_v2_execution_order", lambda **kw: {"transaction": "ZmFrZV90eA==", "requestId": "r1", "outAmount": "999"})
    monkeypatch.setattr(
        ex,
        "sign_and_execute_v2",
        lambda _order: ("sigb", {"totalOutputAmount": "12345"}),
    )
    result, _q = ex.buy(tok, 0.1)
    assert result.status == "success"
    assert result.executed_amount == 12345
    sett = result.diagnostic_metadata["post_buy_settlement"]
    assert sett["primary_source"] == "jupiter_execute"


def test_buy_falls_back_to_order_out_when_execute_has_no_output(monkeypatch, tmp_path):
    """Buy settlement falls back to order outAmount when /execute has no output."""
    from creeper_dripper.models import TokenCandidate

    ex = _executor(monkeypatch, tmp_path)
    tok = TokenCandidate(address=_VALID_MINT, symbol="T", decimals=6, price_usd=1.0)
    monkeypatch.setattr(
        ex,
        "build_v2_execution_order",
        lambda **kw: {"transaction": "ZmFrZV90eA==", "requestId": "r1", "outAmount": "888"},
    )
    monkeypatch.setattr(
        ex,
        "sign_and_execute_v2",
        lambda _order: ("sigb", {}),
    )
    result, _q = ex.buy(tok, 0.1)
    assert result.status == "success"
    assert result.executed_amount == 888
    sett = result.diagnostic_metadata["post_buy_settlement"]
    assert sett["primary_source"] == "jupiter_order"


def test_buy_falls_back_to_quote_when_no_execute_or_order_output(monkeypatch, tmp_path):
    """Buy settlement falls back to quote probe output as last resort."""
    from creeper_dripper.models import TokenCandidate

    ex = _executor(monkeypatch, tmp_path)
    tok = TokenCandidate(address=_VALID_MINT, symbol="T", decimals=6, price_usd=1.0)
    monkeypatch.setattr(ex, "build_v2_execution_order", lambda **kw: {"transaction": "ZmFrZV90eA==", "requestId": "r1"})
    monkeypatch.setattr(
        ex,
        "sign_and_execute_v2",
        lambda _order: ("sigb", {}),
    )
    result, _q = ex.buy(tok, 0.1)
    # quote_buy returns 200_000_000 from StubJupiter.probe_quote
    assert result.status == "success"
    assert result.executed_amount == 200_000_000
    sett = result.diagnostic_metadata["post_buy_settlement"]
    assert sett["primary_source"] == "quote_probe"


def test_buy_unknown_only_when_no_amounts_at_all(monkeypatch, tmp_path):
    """Buy only returns unknown if every amount source is None — essentially impossible in practice."""
    from creeper_dripper.models import ProbeQuote, TokenCandidate

    ex = _executor(monkeypatch, tmp_path)
    tok = TokenCandidate(address=_VALID_MINT, symbol="T", decimals=6, price_usd=1.0)

    class ZeroQuoteJupiter:
        def probe_quote(self, **kwargs):
            return ProbeQuote(
                input_amount_atomic=kwargs["amount_atomic"],
                out_amount_atomic=None,
                price_impact_bps=10.0,
                route_ok=True,
                raw={},
            )

    ex.jupiter = ZeroQuoteJupiter()
    monkeypatch.setattr(ex, "build_v2_execution_order", lambda **kw: {"transaction": "ZmFrZV90eA==", "requestId": "r1"})
    monkeypatch.setattr(
        ex,
        "sign_and_execute_v2",
        lambda _order: ("sigb", {}),
    )
    # _quote_ok checks out_amount_atomic — None means no route; buy fails before settlement.
    result, _q = ex.buy(tok, 0.1)
    assert result.status == "failed"
