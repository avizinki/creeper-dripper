from __future__ import annotations

import time

from creeper_dripper.config import load_settings
from creeper_dripper.engine.discovery import discover_candidates
from creeper_dripper.models import ProbeQuote, TokenCandidate


class DummyBirdeye:
    def trending_tokens(self, *, limit: int):
        now = int(time.time())
        return [
            {
                "address": "mint1",
                "symbol": "CARDS",
                "liquidityUsd": 200_000,
                "volume24hUSD": 500_000,
                "blockUnixTime": now,  # seed prefilter uses this for age gating
            }
        ][:limit]

    def new_listings(self, *, limit: int):
        return []

    def build_candidate(self, seed: dict):
        # Weak signals: bad ratio + low score; hard safety still OK.
        c = TokenCandidate(
            address=seed["address"],
            symbol=seed["symbol"],
            decimals=6,
            liquidity_usd=200_000,
            exit_liquidity_usd=150_000,
            volume_24h_usd=500_000,
            buy_sell_ratio_1h=0.4,
            age_hours=12.0,
            security_mint_mutable=False,
            security_freezable=False,
        )
        # Make sure it's "below score" even after score_candidate runs.
        c.discovery_score = 0.0
        return c


class DummyJupiter:
    def probe_quote(self, *, input_mint: str, output_mint: str, amount_atomic: int, slippage_bps: int):
        # Always return a viable route with low impact.
        return ProbeQuote(
            input_amount_atomic=amount_atomic,
            out_amount_atomic=max(1, amount_atomic // 10),
            price_impact_bps=50.0,
            route_ok=True,
            raw={},
        )


def _base_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BIRDEYE_API_KEY", "x")
    monkeypatch.setenv("JUPITER_API_KEY", "x")
    monkeypatch.setenv("BS58_PRIVATE_KEY", "x")
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("JOURNAL_PATH", str(tmp_path / "journal.jsonl"))
    monkeypatch.setenv("DISCOVERY_INTERVAL_SECONDS", "30")
    monkeypatch.setenv("MAX_ACTIVE_CANDIDATES", "5")


def test_early_risk_bucket_accepts_soft_rejects(monkeypatch, tmp_path):
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("EARLY_RISK_BUCKET_ENABLED", "true")
    monkeypatch.setenv("EARLY_RISK_MIN_SCORE_FLOOR", "0")
    settings = load_settings()

    accepted, summary = discover_candidates(DummyBirdeye(), DummyJupiter(), settings)
    assert summary["jupiter_buy_probe_calls"] >= 1
    assert len(accepted) >= 1
    assert accepted[0].raw.get("early_risk_bucket") is True

