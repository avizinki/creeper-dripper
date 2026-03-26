from __future__ import annotations

from creeper_dripper.config import load_settings
from creeper_dripper.engine.trader import CreeperDripper
from creeper_dripper.models import ExecutionResult, ProbeQuote, TokenCandidate
from creeper_dripper.storage.state import new_portfolio
from creeper_dripper.utils import utc_now_iso


def _settings(monkeypatch, tmp_path, *, extra_env: dict | None = None):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BIRDEYE_API_KEY", "x")
    monkeypatch.setenv("JUPITER_API_KEY", "x")
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("JOURNAL_PATH", str(tmp_path / "journal.jsonl"))
    if extra_env:
        for k, v in extra_env.items():
            monkeypatch.setenv(k, str(v))
    return load_settings()


class _DummyBirdeye:
    pass


class _Exec:
    def __init__(self, *, sell_ok_full: bool, sell_ok_small: bool):
        self.jupiter = object()
        self._sell_ok_full = sell_ok_full
        self._sell_ok_small = sell_ok_small
        self.sell_calls: list[int] = []

    def transaction_status(self, _sig):
        return None

    def quote_buy(self, _candidate, _size_sol):
        return ProbeQuote(input_amount_atomic=10_000_000, out_amount_atomic=1_000_000, price_impact_bps=100.0, route_ok=True, raw={})

    def quote_sell(self, _mint, amount_atomic):
        self.sell_calls.append(int(amount_atomic))
        ok = self._sell_ok_small if int(amount_atomic) < 1_000_000 else self._sell_ok_full
        return ProbeQuote(input_amount_atomic=int(amount_atomic), out_amount_atomic=(123 if ok else None), price_impact_bps=100.0, route_ok=bool(ok), raw={})

    def buy(self, *_a, **_kw):
        return ExecutionResult(status="failed", requested_amount=1, diagnostic_code="order_failed", error="x"), ProbeQuote(
            input_amount_atomic=1, out_amount_atomic=None, price_impact_bps=None, route_ok=False, raw={}
        )


class _Capture:
    def __init__(self):
        self._events = []

    def emit(self, event_type: str, label: str, **metadata):
        self._events.append({"event_type": event_type, "reason": label, "metadata": metadata})

    def find(self, name: str):
        return [e for e in self._events if e.get("event_type") == name]


def test_young_token_requires_two_bucket_survivability(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    engine = CreeperDripper(settings, _DummyBirdeye(), _Exec(sell_ok_full=True, sell_ok_small=False), new_portfolio(5.0))
    c = TokenCandidate(
        address="mintY",
        symbol="YNG",
        decimals=6,
        liquidity_usd=20000,
        buy_sell_ratio_1h=2.0,
        age_hours=0.5,
        discovery_score=100.0,
    )
    decisions = []
    engine._maybe_open_positions([c], decisions, utc_now_iso())
    assert any(d.reason == "reject_route_survivability" for d in decisions)


def test_old_token_requires_higher_liquidity_floor(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    ex = _Exec(sell_ok_full=True, sell_ok_small=True)
    engine = CreeperDripper(settings, _DummyBirdeye(), ex, new_portfolio(5.0))
    cap = _Capture()
    engine.events = cap
    c = TokenCandidate(
        address="mintO",
        symbol="OLD",
        decimals=6,
        liquidity_usd=50000,  # below gt_24h floor
        buy_sell_ratio_1h=2.0,
        age_hours=48.0,
        discovery_score=100.0,
    )
    decisions = []
    engine._maybe_open_positions([c], decisions, utc_now_iso())
    assert ex.sell_calls == []
    gates = cap.find("entry_liquidity_gate")
    assert gates and gates[0].get("reason") == "blocked"
