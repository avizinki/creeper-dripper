from __future__ import annotations

from creeper_dripper.config import load_settings
from creeper_dripper.engine.trader import CreeperDripper
from creeper_dripper.errors import POSITION_RECONCILE_PENDING, SETTLEMENT_UNCONFIRMED
from creeper_dripper.models import ExecutionResult, PortfolioState, PositionState, ProbeQuote, TakeProfitStep, TokenCandidate
from creeper_dripper.storage.state import new_portfolio
from creeper_dripper.utils import utc_now_iso

_VALID_TEST_MINT = "J7MzyZ4Tvwn2LBREnLU48TmKxJE28qstv35dRGBJPCpE"


def _sell_success_result(
    *,
    requested_amount: int,
    sold_atomic: int,
    signature: str = "sig1",
    is_partial: bool = False,
    output_amount: int | None = 1_000_000_000,
    settlement_confirmed: bool = True,
) -> ExecutionResult:
    return ExecutionResult(
        status="success",
        requested_amount=requested_amount,
        executed_amount=sold_atomic,
        output_amount=output_amount,
        signature=signature,
        is_partial=is_partial,
        diagnostic_metadata={
            "post_sell_settlement": {
                "settlement_confirmed": settlement_confirmed,
                "sold_atomic_settled": sold_atomic,
                "sold_atomic_source": "jupiter_execute",
            },
        },
    )


class DummyBirdeye:
    def build_candidate(self, seed):
        return TokenCandidate(address=seed["address"], symbol=seed.get("symbol", "T"), decimals=6, price_usd=1.0)


class DummyExecutor:
    def __init__(self, sell_results: list[ExecutionResult], wallet_balance: int = 1_000_000) -> None:
        self.sell_results = sell_results
        self.wallet_balance = wallet_balance
        self.jupiter = object()
        self._idx = 0

    def wallet_token_balance_atomic(self, _token_mint: str) -> int:
        return self.wallet_balance

    def sell(self, _token_mint: str, _amount_atomic: int):
        result = self.sell_results[min(self._idx, len(self.sell_results) - 1)]
        self._idx += 1
        return result, ProbeQuote(input_amount_atomic=1, out_amount_atomic=1_000_000_000, price_impact_bps=100.0, route_ok=True, raw={})

    def buy(self, _token: TokenCandidate, _size_sol: float):
        return ExecutionResult(status="failed", requested_amount=1, error="buy not used"), ProbeQuote(
            input_amount_atomic=1,
            out_amount_atomic=None,
            price_impact_bps=None,
            route_ok=False,
            raw={},
        )


def _settings(monkeypatch, tmp_path):
    monkeypatch.setenv("BIRDEYE_API_KEY", "x")
    monkeypatch.setenv("JUPITER_API_KEY", "x")
    monkeypatch.setenv("BS58_PRIVATE_KEY", "x")
    monkeypatch.setenv("STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("JOURNAL_PATH", str(tmp_path / "journal.jsonl"))
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path))
    return load_settings()


def _position(now: str) -> PositionState:
    return PositionState(
        token_mint=_VALID_TEST_MINT,
        symbol="TKN",
        decimals=0,
        status="OPEN",
        opened_at=now,
        updated_at=now,
        entry_price_usd=1.0,
        avg_entry_price_usd=1.0,
        entry_sol=1.0,
        remaining_qty_atomic=100,
        remaining_qty_ui=100.0,
        peak_price_usd=1.0,
        last_price_usd=1.0,
        entry_mark_sol_per_token=0.01,
        last_mark_sol_per_token=0.01,
        peak_mark_sol_per_token=0.01,
        position_id=f"{_VALID_TEST_MINT}:{now}",
        take_profit_steps=[TakeProfitStep(trigger_pct=25.0, fraction=0.5)],
    )


def test_partial_sell(monkeypatch, tmp_path):
    now = utc_now_iso()
    settings = _settings(monkeypatch, tmp_path)
    portfolio: PortfolioState = new_portfolio(5.0)
    position = _position(now)
    portfolio.open_positions[position.token_mint] = position
    executor = DummyExecutor([_sell_success_result(requested_amount=40, sold_atomic=40, is_partial=True)])
    engine = CreeperDripper(settings, DummyBirdeye(), executor, portfolio)
    decisions = []
    engine._start_exit(position, 40, "take_profit_25", decisions, now)
    assert position.status == "PARTIAL"
    assert position.remaining_qty_atomic == 60
    assert position.token_mint in portfolio.open_positions


def test_partial_sell_remaining_qty_ui_from_state_not_wallet(monkeypatch, tmp_path):
    now = utc_now_iso()
    settings = _settings(monkeypatch, tmp_path)
    portfolio: PortfolioState = new_portfolio(5.0)
    position = _position(now)
    position.decimals = 6
    position.remaining_qty_atomic = 1_000_000
    position.remaining_qty_ui = 1.0
    portfolio.open_positions[position.token_mint] = position
    executor = DummyExecutor([_sell_success_result(requested_amount=500_000, sold_atomic=500_000, is_partial=True)])
    engine = CreeperDripper(settings, DummyBirdeye(), executor, portfolio)
    decisions = []
    engine._start_exit(position, 500_000, "take_profit_25", decisions, now)
    assert position.status == "PARTIAL"
    assert position.remaining_qty_atomic == 500_000
    assert abs(position.remaining_qty_ui - 0.5) < 1e-12


def test_failed_sell(monkeypatch, tmp_path):
    now = utc_now_iso()
    settings = _settings(monkeypatch, tmp_path)
    portfolio: PortfolioState = new_portfolio(5.0)
    position = _position(now)
    portfolio.open_positions[position.token_mint] = position
    executor = DummyExecutor([ExecutionResult(status="failed", requested_amount=40, error="reverted")])
    engine = CreeperDripper(settings, DummyBirdeye(), executor, portfolio)
    decisions = []
    engine._start_exit(position, 40, "stop_loss", decisions, now)
    assert position.status == "EXIT_BLOCKED"
    assert position.remaining_qty_atomic == 100


def test_unknown_sell(monkeypatch, tmp_path):
    now = utc_now_iso()
    settings = _settings(monkeypatch, tmp_path)
    portfolio: PortfolioState = new_portfolio(5.0)
    position = _position(now)
    portfolio.open_positions[position.token_mint] = position
    executor = DummyExecutor([ExecutionResult(status="unknown", requested_amount=40, error="timeout")])
    engine = CreeperDripper(settings, DummyBirdeye(), executor, portfolio)
    decisions = []
    engine._start_exit(position, 40, "liquidity_break", decisions, now)
    assert position.status == "EXIT_PENDING"
    assert position.remaining_qty_atomic == 100
    assert position.exit_retry_count == 0


def test_retry_logic(monkeypatch, tmp_path):
    now = utc_now_iso()
    settings = _settings(monkeypatch, tmp_path)
    portfolio: PortfolioState = new_portfolio(5.0)
    position = _position(now)
    position.status = "EXIT_PENDING"
    position.pending_exit_reason = "take_profit_25"
    position.pending_exit_qty_atomic = 50
    position.next_exit_retry_at = now
    portfolio.open_positions[position.token_mint] = position
    executor = DummyExecutor(
        [
            ExecutionResult(status="unknown", requested_amount=50, error="network"),
            _sell_success_result(requested_amount=50, sold_atomic=50, signature="sig2"),
        ]
    )
    engine = CreeperDripper(settings, DummyBirdeye(), executor, portfolio)
    decisions = []
    engine._retry_pending_exit(position, decisions, now)
    assert position.status == "EXIT_PENDING"
    position.next_exit_retry_at = now
    engine._retry_pending_exit(position, decisions, now)
    assert position.status == "PARTIAL"
    assert position.remaining_qty_atomic == 50


def test_full_sell_closes_without_exit_blocked(monkeypatch, tmp_path):
    now = utc_now_iso()
    settings = _settings(monkeypatch, tmp_path)
    portfolio: PortfolioState = new_portfolio(5.0)
    position = _position(now)
    portfolio.open_positions[position.token_mint] = position
    executor = DummyExecutor([_sell_success_result(requested_amount=100, sold_atomic=100, is_partial=False)])
    engine = CreeperDripper(settings, DummyBirdeye(), executor, portfolio)
    decisions = []
    engine._start_exit(position, 100, "stop_loss", decisions, now)
    assert position.status == "CLOSED"
    assert position.remaining_qty_atomic == 0
    assert position.token_mint not in portfolio.open_positions
    assert not any(d.action == "SELL_BLOCKED" for d in decisions)


def test_success_without_settlement_metadata_is_reconcile_pending_not_exit_blocked(monkeypatch, tmp_path):
    now = utc_now_iso()
    settings = _settings(monkeypatch, tmp_path)
    portfolio: PortfolioState = new_portfolio(5.0)
    position = _position(now)
    portfolio.open_positions[position.token_mint] = position
    bad = ExecutionResult(
        status="success",
        requested_amount=40,
        executed_amount=40,
        signature="sig-bad",
        diagnostic_metadata={},
    )
    executor = DummyExecutor([bad])
    engine = CreeperDripper(settings, DummyBirdeye(), executor, portfolio)
    decisions = []
    engine._start_exit(position, 40, "take_profit_25", decisions, now)
    assert position.status == POSITION_RECONCILE_PENDING
    assert position.reconcile_context == "exit"
    assert position.remaining_qty_atomic == 100
    assert any(d.reason == "sell_settlement_metadata_missing" for d in decisions)


def test_hard_execution_failed_still_exit_blocked(monkeypatch, tmp_path):
    now = utc_now_iso()
    settings = _settings(monkeypatch, tmp_path)
    portfolio: PortfolioState = new_portfolio(5.0)
    position = _position(now)
    portfolio.open_positions[position.token_mint] = position
    executor = DummyExecutor(
        [
            ExecutionResult(
                status="failed",
                requested_amount=40,
                diagnostic_code="execute_failed",
                error="reverted",
                diagnostic_metadata={"classification": "execute_failed", "phase": "execute", "side": "sell"},
            ),
        ]
    )
    engine = CreeperDripper(settings, DummyBirdeye(), executor, portfolio)
    decisions = []
    engine._start_exit(position, 40, "take_profit_25", decisions, now)
    assert position.status == "EXIT_BLOCKED"
    assert position.remaining_qty_atomic == 100


def test_settlement_confirmed_false_is_reconcile_pending(monkeypatch, tmp_path):
    now = utc_now_iso()
    settings = _settings(monkeypatch, tmp_path)
    portfolio: PortfolioState = new_portfolio(5.0)
    position = _position(now)
    portfolio.open_positions[position.token_mint] = position
    executor = DummyExecutor([_sell_success_result(requested_amount=40, sold_atomic=40, settlement_confirmed=False)])
    engine = CreeperDripper(settings, DummyBirdeye(), executor, portfolio)
    decisions = []
    engine._start_exit(position, 40, "take_profit_25", decisions, now)
    assert position.status == POSITION_RECONCILE_PENDING
    assert position.remaining_qty_atomic == 100


def test_settlement_unconfirmed_unknown_not_exit_blocked(monkeypatch, tmp_path):
    now = utc_now_iso()
    settings = _settings(monkeypatch, tmp_path)
    portfolio: PortfolioState = new_portfolio(5.0)
    position = _position(now)
    portfolio.open_positions[position.token_mint] = position
    executor = DummyExecutor(
        [
            ExecutionResult(
                status="unknown",
                requested_amount=40,
                diagnostic_code=SETTLEMENT_UNCONFIRMED,
                error="sell_settlement_unconfirmed",
                signature="sig-u",
                executed_amount=40,
                diagnostic_metadata={"post_sell_settlement": {"wallet_confirmation": "sold_mismatch_vs_jupiter"}},
            ),
        ]
    )
    engine = CreeperDripper(settings, DummyBirdeye(), executor, portfolio)
    decisions = []
    engine._start_exit(position, 40, "take_profit_25", decisions, now)
    assert position.status == POSITION_RECONCILE_PENDING
    assert position.reconcile_context == "exit"
    assert position.remaining_qty_atomic == 100
    assert position.pending_exit_signature == "sig-u"
    assert any(d.reason == "sell_settlement_unconfirmed" for d in decisions)


def test_wallet_zero_does_not_force_exit_blocked_with_positive_state(monkeypatch, tmp_path):
    now = utc_now_iso()
    settings = _settings(monkeypatch, tmp_path)
    portfolio: PortfolioState = new_portfolio(5.0)
    position = _position(now)
    portfolio.open_positions[position.token_mint] = position
    executor = DummyExecutor([_sell_success_result(requested_amount=40, sold_atomic=40)], wallet_balance=0)
    engine = CreeperDripper(settings, DummyBirdeye(), executor, portfolio)
    decisions = []
    engine._start_exit(position, 40, "take_profit_25", decisions, now)
    assert position.status == POSITION_RECONCILE_PENDING
    assert position.remaining_qty_atomic == 100
    assert any("wallet_balance_zero" in d.reason for d in decisions)
