from __future__ import annotations

import json

import pytest

from creeper_dripper.config import load_settings
from creeper_dripper.engine.discovery import discover_candidates
from creeper_dripper.engine.trader import CreeperDripper
from creeper_dripper.models import PortfolioState, ProbeQuote, TokenCandidate
from creeper_dripper.storage.state import new_portfolio


class _Resp:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload or {"success": True, "data": {}}
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http_{self.status_code}")


def _settings(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BIRDEYE_API_KEY", "x")
    monkeypatch.setenv("JUPITER_API_KEY", "x")
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("STATE_PATH", str(tmp_path / "runtime" / "state.json"))
    monkeypatch.setenv("JOURNAL_PATH", str(tmp_path / "runtime" / "journal.jsonl"))
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("DISCOVERY_INTERVAL_SECONDS", "10")
    monkeypatch.setenv("DISCOVERY_SEED_LIMIT", "20")
    monkeypatch.setenv("DISCOVERY_OVERVIEW_LIMIT", "20")
    monkeypatch.setenv("DISCOVERY_MAX_CANDIDATES", "6")
    monkeypatch.setenv("MAX_ACTIVE_CANDIDATES", "6")
    return load_settings()


def test_429_burst_enters_constrained_and_disables_retries(monkeypatch):
    from creeper_dripper.clients.birdeye import BirdeyeClient

    client = BirdeyeClient(api_key="x")
    calls = {"n": 0}

    def _req(_m, _u, params=None, timeout=20):
        calls["n"] += 1
        return _Resp(429, {"success": False}, "rate limited")

    monkeypatch.setattr(client._session, "request", _req)
    client.begin_runtime_cycle()
    with pytest.raises(RuntimeError):
        client.token_overview("mint1")
    snap = client.budget_snapshot()
    assert calls["n"] == 1, "429 should disable retries in current cycle"
    assert snap["birdeye_429_count"] == 1
    assert snap["birdeye_budget_mode"] == "constrained"
    assert snap["disable_retries_in_cycle"] is True


def test_sustained_429_enters_starved_mode(monkeypatch):
    from creeper_dripper.clients.birdeye import BirdeyeClient

    client = BirdeyeClient(api_key="x")
    monkeypatch.setattr(client._session, "request", lambda _m, _u, params=None, timeout=20: _Resp(429, {"success": False}, "rate limited"))
    client.begin_runtime_cycle()
    for _ in range(3):
        with pytest.raises(RuntimeError):
            client.token_overview("mint1")
        client.begin_runtime_cycle()
    snap = client.budget_snapshot()
    assert snap["birdeye_budget_mode"] == "starved"
    assert "/defi/token_security" in snap["endpoints_disabled"]
    assert "/defi/token_creation_info" in snap["endpoints_disabled"]


def test_discovery_gates_endpoints_and_reduces_limits(monkeypatch, tmp_path):
    s = _settings(monkeypatch, tmp_path)
    s.min_discovery_score = 0
    s.min_buy_sell_ratio = 0.0
    s.block_mutable_mint = False
    s.block_freezable = False
    s.require_jup_sell_route = False

    calls = {"heavy": 0, "security": 0, "holder": 0}

    class StubBirdeye:
        def budget_snapshot(self):
            return {
                "birdeye_budget_mode": "constrained",
                "birdeye_requests_count": 12,
                "birdeye_429_count": 3,
                "birdeye_success_rate": 0.75,
                "budget_reason_summary": "429 burst",
                "endpoints_disabled": ["/defi/token_security", "/defi/token_creation_info"],
            }

        def adjusted_discovery_limits(self, seed_limit, overview_limit, max_active_candidates):
            return 8, 4, 2

        def should_skip_endpoint(self, path: str) -> bool:
            return path in {"/defi/token_security", "/defi/token_creation_info"}

        def trending_tokens(self, limit=25):
            assert limit <= 8
            return [{"address": f"m{i}", "symbol": f"T{i}", "volume24hUSD": 99_999} for i in range(6)]

        def new_listings(self, limit=10):
            return []

        def build_candidate_light(self, seed):
            return TokenCandidate(
                address=seed["address"],
                symbol=seed["symbol"],
                decimals=6,
                liquidity_usd=200_000,
                exit_liquidity_available=False,
                exit_liquidity_reason="birdeye_exit_liquidity_skipped_unsupported_chain",
                birdeye_exit_liquidity_supported=False,
                volume_24h_usd=500_000,
                buy_sell_ratio_1h=1.2,
                age_hours=2.0,
            )

        def enrich_candidate_heavy(self, c):
            calls["heavy"] += 1
            return c

        def enrich_candidate_security_only(self, c):
            calls["security"] += 1
            return c

        def enrich_candidate_holders_only(self, c):
            calls["holder"] += 1
            return c

    class StubJupiter:
        def probe_quote(self, **kwargs):
            return ProbeQuote(
                input_amount_atomic=kwargs["amount_atomic"],
                out_amount_atomic=1_000_000,
                price_impact_bps=20.0,
                route_ok=True,
                raw={},
            )

    _accepted, summary = discover_candidates(StubBirdeye(), StubJupiter(), s)
    assert summary["birdeye_budget_mode"] == "constrained"
    assert summary["effective_discovery_seed_limit"] == 8
    assert summary["effective_max_active_candidates"] == 2
    assert "/defi/token_creation_info" in summary["endpoints_disabled"]
    assert calls["heavy"] == 0
    assert calls["security"] == 0


def test_trader_persists_budget_fields_and_degrades_cadence(monkeypatch, tmp_path):
    s = _settings(monkeypatch, tmp_path)
    portfolio: PortfolioState = new_portfolio(5.0)

    class DummyBirdeye:
        def begin_runtime_cycle(self):
            return None

        def adjusted_discovery_interval_seconds(self, base_interval_seconds: int) -> int:
            return int(base_interval_seconds * 3)

    class DummyExecutor:
        jupiter = object()

        def transaction_status(self, _sig):
            return None

    engine = CreeperDripper(s, DummyBirdeye(), DummyExecutor(), portfolio)

    def fake_discover(_b, _j, _s, **_kwargs):
        return [], {
            "seeds_total": 1,
            "candidates_built": 1,
            "candidates_accepted": 0,
            "candidates_rejected_total": 1,
            "rejection_counts": {"reject_low_volume": 1},
            "birdeye_budget_mode": "starved",
            "birdeye_requests_count": 21,
            "birdeye_429_count": 9,
            "birdeye_success_rate": 0.20,
            "budget_reason_summary": "consecutive_429_burst_cycles=2",
            "endpoints_disabled": ["/defi/token_security", "/defi/token_creation_info", "/defi/v3/token/holder"],
            "effective_discovery_seed_limit": 5,
            "effective_discovery_overview_limit": 3,
            "effective_max_active_candidates": 1,
            "events": [],
            "economics_field_records": [],
        }

    monkeypatch.setattr("creeper_dripper.engine.trader.discover_candidates", fake_discover)
    out = engine.run_cycle()
    summary = out["summary"]
    assert summary["birdeye_budget_mode"] == "starved"
    assert summary["birdeye_429_count"] == 9
    assert summary["effective_discovery_seed_limit"] == 5
    assert engine._effective_discovery_interval_seconds > s.discovery_interval_seconds
    assert engine._effective_discovery_interval_seconds % 3 == 0

    status = json.loads((tmp_path / "runtime" / "status.json").read_text(encoding="utf-8"))
    ssum = status.get("summary") or {}
    assert ssum.get("birdeye_budget_mode") == "starved"
    assert "/defi/token_security" in (ssum.get("endpoints_disabled") or [])
