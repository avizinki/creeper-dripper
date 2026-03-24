"""TradeExecutor sell settlement: Jupiter/execute primary, wallet RPC confirmation only."""

from __future__ import annotations

import pytest

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
    return TradeExecutor(StubJupiter(), FakeOwner(), settings)


@pytest.fixture(autouse=True)
def _no_settlement_sleep(monkeypatch):
    monkeypatch.setattr("creeper_dripper.execution.executor.time.sleep", lambda *_a, **_k: None)


def test_sell_primary_from_execute_when_wallet_post_unavailable(monkeypatch, tmp_path):
    ex = _executor(monkeypatch, tmp_path)
    monkeypatch.setattr(ex, "build_v2_execution_order", lambda **kw: {"transaction": "ZmFrZV90eA==", "requestId": "r1"})
    monkeypatch.setattr(
        ex,
        "sign_and_execute_v2",
        lambda _order: ("sigx", {"totalInputAmount": "100", "totalOutputAmount": "5000000"}),
    )
    monkeypatch.setattr(ex, "_retry_wallet_token_balance", lambda *a, **k: None)
    monkeypatch.setattr(ex, "wallet_token_balance_atomic", lambda _m: None)
    result, _quote = ex.sell(_VALID_MINT, 100)
    assert result.status == "success"
    sett = result.diagnostic_metadata["post_sell_settlement"]
    assert sett["settlement_confirmed"] is True
    assert sett["sold_atomic_settled"] == 100
    assert result.executed_amount == 100


def test_sell_unknown_when_wallet_contradicts_jupiter_with_pre(monkeypatch, tmp_path):
    ex = _executor(monkeypatch, tmp_path)
    monkeypatch.setattr(ex, "build_v2_execution_order", lambda **kw: {"transaction": "ZmFrZV90eA==", "requestId": "r1"})
    monkeypatch.setattr(
        ex,
        "sign_and_execute_v2",
        lambda _order: ("sigx", {"totalInputAmount": "40"}),
    )
    monkeypatch.setattr(ex, "_retry_wallet_token_balance", lambda *a, **k: 100)
    monkeypatch.setattr(ex, "wallet_token_balance_atomic", lambda _m: 100)
    result, _quote = ex.sell(_VALID_MINT, 40)
    assert result.status == "unknown"
    assert result.diagnostic_code == SETTLEMENT_UNCONFIRMED
    assert result.diagnostic_metadata["post_sell_settlement"]["wallet_confirmation"] == "no_decrease_while_jupiter_reports_sell"


def test_buy_primary_from_execute_not_wallet_only(monkeypatch, tmp_path):
    from creeper_dripper.models import TokenCandidate

    ex = _executor(monkeypatch, tmp_path)
    tok = TokenCandidate(address=_VALID_MINT, symbol="T", decimals=6, price_usd=1.0)
    monkeypatch.setattr(ex, "build_v2_execution_order", lambda **kw: {"transaction": "ZmFrZV90eA==", "requestId": "r1", "outAmount": "999"})
    monkeypatch.setattr(
        ex,
        "sign_and_execute_v2",
        lambda _order: ("sigb", {"totalOutputAmount": "12345"}),
    )
    monkeypatch.setattr(ex, "wallet_token_balance_atomic", lambda _m: None)
    result, _q = ex.buy(tok, 0.1)
    assert result.status == "success"
    assert result.executed_amount == 12345
    assert result.diagnostic_metadata["post_buy_settlement"]["primary_source"] == "jupiter_execute"
