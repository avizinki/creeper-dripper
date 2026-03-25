from __future__ import annotations

from creeper_dripper.clients.birdeye import BirdeyeClient
from creeper_dripper.config import load_settings
from creeper_dripper.engine.discovery import discover_candidates
from creeper_dripper.errors import BIRDEYE_EXIT_LIQUIDITY_UNSUPPORTED_CHAIN
from creeper_dripper.models import ProbeQuote


def _settings(monkeypatch, require_exit: bool):
    monkeypatch.setenv("BIRDEYE_API_KEY", "x")
    monkeypatch.setenv("JUPITER_API_KEY", "x")
    monkeypatch.setenv("REQUIRE_BIRDEYE_EXIT_LIQUIDITY", "true" if require_exit else "false")
    settings = load_settings()
    # Force-assign so load_dotenv(override=True) from .env doesn't stomp the test value.
    settings.require_birdeye_exit_liquidity = require_exit
    return settings


def test_birdeye_unsupported_exit_liquidity_does_not_fail_build(monkeypatch):
    client = BirdeyeClient("x", min_interval_s=0.0)
    monkeypatch.setattr(client, "token_overview", lambda address: {"decimals": 6, "price": 1.0, "liquidity": 200000, "v24hUSD": 300000, "buy1h": 10, "sell1h": 5})
    monkeypatch.setattr(client, "token_security", lambda address: {})
    monkeypatch.setattr(client, "token_holders", lambda address: {})
    monkeypatch.setattr(client, "token_creation_info", lambda address: {})
    c = client.build_candidate({"address": "mint1", "symbol": "TOK"})
    assert c.address == "mint1"
    assert c.exit_liquidity_available is False
    assert c.exit_liquidity_reason == BIRDEYE_EXIT_LIQUIDITY_UNSUPPORTED_CHAIN


def test_discovery_fallback_allows_candidate_when_exit_liquidity_unavailable(monkeypatch):
    settings = _settings(monkeypatch, require_exit=False)

    class StubBirdeye:
        def trending_tokens(self, limit=25):
            return [{"address": "mint1", "symbol": "TOK"}]

        def new_listings(self, limit=10):
            return []

        def build_candidate(self, seed):
            raise RuntimeError("patched")

        def build_candidate(self, seed):
            return BirdeyeClient("x").build_candidate(seed)

    # Build candidate via real builder but patched to unsupported exit-liquidity.
    real = BirdeyeClient("x", min_interval_s=0.0)
    monkeypatch.setattr(real, "token_overview", lambda address: {"decimals": 6, "price": 1.0, "liquidity": 250000, "v24hUSD": 500000, "buy1h": 15, "sell1h": 5})
    monkeypatch.setattr(real, "token_security", lambda address: {})
    monkeypatch.setattr(real, "token_holders", lambda address: {})
    monkeypatch.setattr(real, "token_creation_info", lambda address: {})
    monkeypatch.setattr(real, "token_exit_liquidity", lambda address: (_ for _ in ()).throw(RuntimeError('{"success":false,"message":"Chain solana not supported"}')))

    class StubJupiter:
        def probe_quote(self, **kwargs):
            return ProbeQuote(input_amount_atomic=kwargs["amount_atomic"], out_amount_atomic=1000000, price_impact_bps=100.0, route_ok=True, raw={})

    # Inject patched builder instance.
    birdeye = StubBirdeye()
    monkeypatch.setattr(birdeye, "build_candidate", real.build_candidate, raising=False)

    candidates, _summary = discover_candidates(birdeye, StubJupiter(), settings)
    assert len(candidates) == 1
    assert candidates[0].exit_liquidity_available is False


def test_discovery_strict_mode_rejects_when_exit_liquidity_unavailable(monkeypatch):
    settings = _settings(monkeypatch, require_exit=True)

    class StubBirdeye:
        def trending_tokens(self, limit=25):
            return [{"address": "mint1", "symbol": "TOK"}]

        def new_listings(self, limit=10):
            return []

    real = BirdeyeClient("x", min_interval_s=0.0)
    monkeypatch.setattr(real, "token_overview", lambda address: {"decimals": 6, "price": 1.0, "liquidity": 250000, "v24hUSD": 500000, "buy1h": 15, "sell1h": 5})
    monkeypatch.setattr(real, "token_security", lambda address: {})
    monkeypatch.setattr(real, "token_holders", lambda address: {})
    monkeypatch.setattr(real, "token_creation_info", lambda address: {})
    monkeypatch.setattr(real, "token_exit_liquidity", lambda address: (_ for _ in ()).throw(RuntimeError('{"success":false,"message":"Chain solana not supported"}')))

    birdeye = StubBirdeye()
    monkeypatch.setattr(birdeye, "build_candidate", real.build_candidate, raising=False)

    class StubJupiter:
        def probe_quote(self, **kwargs):
            return ProbeQuote(input_amount_atomic=kwargs["amount_atomic"], out_amount_atomic=1000000, price_impact_bps=100.0, route_ok=True, raw={})

    candidates, summary = discover_candidates(birdeye, StubJupiter(), settings)
    assert candidates == []
    assert summary["rejection_counts"].get(BIRDEYE_EXIT_LIQUIDITY_UNSUPPORTED_CHAIN, 0) >= 1
