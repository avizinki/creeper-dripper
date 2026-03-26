from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _load_worker_module():
    root = Path(__file__).resolve().parents[1]
    worker_path = root / "tools" / "token_enrichment_worker.py"
    spec = importlib.util.spec_from_file_location("token_enrichment_worker", worker_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_direction_selection_prefers_sell_for_zombie_traded_or_history():
    mod = _load_worker_module()

    assert mod._pick_quote_direction({"ever_opened": True, "latest_status": "active"}) == "sell"
    assert mod._pick_quote_direction({"ever_zombie": True, "latest_status": "unknown"}) == "sell"
    assert mod._pick_quote_direction({"blocked_events_count": 1, "latest_status": "unknown"}) == "sell"
    assert mod._pick_quote_direction({"ever_opened": False, "ever_sold": False, "latest_status": "unknown"}) == "buy"


def test_direction_aware_scoring_and_route_quality_vary():
    mod = _load_worker_module()

    strong_buy = mod._route_quality(direction="buy", route_exists=True, impact_bps=40.0)
    weak_sell = mod._route_quality(direction="sell", route_exists=True, impact_bps=500.0)
    none_route = mod._route_quality(direction="sell", route_exists=False, impact_bps=None)
    assert strong_buy == "strong"
    assert weak_sell == "weak"
    assert none_route == "none"

    buy_score = mod._inferred_liquidity_score_with_direction(direction="buy", route_exists=True, impact_bps=80.0)
    sell_score = mod._inferred_liquidity_score_with_direction(direction="sell", route_exists=True, impact_bps=300.0)
    none_score = mod._inferred_liquidity_score_with_direction(direction="sell", route_exists=False, impact_bps=None)
    assert buy_score != sell_score
    assert none_score == 0.0


def test_run_worker_uses_mixed_directions_and_non_flat_outputs(tmp_path, monkeypatch):
    mod = _load_worker_module()
    monkeypatch.setenv("JUPITER_API_KEY", "x")

    report_path = tmp_path / "token_report.json"
    progress_path = tmp_path / "enrichment_progress.json"

    report_payload = {
        "tokens": [
            {"mint": "mint-traded", "ever_opened": True, "highest_score_seen": 10.0, "latest_status": "active"},
            {"mint": "mint-zombie", "ever_zombie": True, "highest_score_seen": 10.0, "latest_status": "zombie"},
            {"mint": "mint-history", "blocked_events_count": 1, "highest_score_seen": 90.0, "latest_status": "unknown"},
            {"mint": "mint-buy-1", "highest_score_seen": 80.0, "latest_status": "unknown"},
            {"mint": "mint-buy-2", "highest_score_seen": 70.0, "latest_status": "unknown"},
        ]
    }
    report_path.write_text(json.dumps(report_payload), encoding="utf-8")

    calls: list[tuple[str, str]] = []

    def fake_quote_once(_session, *, api_key, input_mint, output_mint, amount_atomic):
        assert api_key == "x"
        assert amount_atomic > 0
        calls.append((input_mint, output_mint))
        if input_mint.startswith("mint-") and output_mint == mod.SOL_MINT:
            if input_mint == "mint-history":
                return {"outAmount": "0", "priceImpactPct": "0.20"}, None
            return {"outAmount": "1000", "priceImpactPct": "0.03"}, None
        if input_mint == mod.SOL_MINT and output_mint.startswith("mint-"):
            return {"outAmount": "5000", "priceImpactPct": "0.005"}, None
        return None, "unexpected"

    monkeypatch.setattr(mod, "_jupiter_quote_once", fake_quote_once)

    rc = mod.run_worker(
        report_path=report_path,
        progress_path=progress_path,
        max_tokens=5,
        priority="high",
        resume=False,
        cooldown_seconds=0.0,
        stop_after_failures=5,
        score_threshold=55.0,
        recent_seen_days=7,
        sol_input_lamports=1_000_000,
        token_input_atomic=1_000_000,
    )
    assert rc == 0
    assert len(calls) == 5

    updated = json.loads(report_path.read_text(encoding="utf-8"))["tokens"]
    directions = {t.get("quote_direction") for t in updated}
    route_states = {t.get("last_known_route_state") for t in updated}
    liq_scores = {t.get("inferred_liquidity_score") for t in updated}

    assert directions == {"buy", "sell"}
    assert route_states == {"route_exists", "no_route"}
    assert len(liq_scores) > 1
