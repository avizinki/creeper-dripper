from __future__ import annotations

from creeper_dripper.clients.birdeye_audit import (
    BirdeyeAuditSession,
    build_birdeye_audit_summary_dict,
    classify_waste_hint,
    extract_credits_api_usage,
    sanitize_birdeye_params,
)


def test_sanitize_birdeye_params_truncates_wallet():
    p = sanitize_birdeye_params({"wallet": "12345678901234567890123456789012", "address": "So11111111111111111111111111111111111111112"})
    assert p["wallet"].startswith("12345678")
    assert "…" in p["wallet"]
    assert p["address"] == "So11111111111111111111111111111111111111112"


def test_classify_waste_hint():
    assert classify_waste_hint('{"message":"Chain solana not supported"}') == "unsupported_chain"
    assert classify_waste_hint("missing required param address") == "missing_param"


def test_extract_credits_api_usage():
    payload = {"success": True, "data": {"usage": {"api": 100, "ws": 0, "total": 100}}}
    assert extract_credits_api_usage(payload) == 100
    assert extract_credits_api_usage(None) is None


def test_audit_session_top_mints_400():
    s = BirdeyeAuditSession()
    s.record("/defi/x", 400, phase="discovery", body_snippet="{}", mint="mintA")
    s.record("/defi/x", 400, phase="discovery", body_snippet="{}", mint="mintA")
    s.record("/defi/x", 400, phase="discovery", body_snippet="{}", mint="mintB")
    top = s.top_mints_400(10)
    assert top[0]["mint"] == "mintA"
    assert top[0]["count_400"] == 2


def test_build_birdeye_audit_summary_dict_smoke():
    s = BirdeyeAuditSession()
    s.record("/defi/token_overview", 200, phase="discovery", body_snippet="", mint="m1")
    s.record("/defi/token_overview", 400, phase="discovery", body_snippet="bad", mint="m2")
    out = build_birdeye_audit_summary_dict(
        s,
        credits_before={"success": True, "data": {"usage": {"api": 1000, "ws": 0, "total": 1000}}},
        credits_after={"success": True, "data": {"usage": {"api": 1100, "ws": 0, "total": 1100}}},
        discovery_summary={
            "seeds_total": 10,
            "candidates_accepted": 2,
            "birdeye_candidate_build_calls": 5,
        },
        doctor_ok=True,
    )
    assert out["credits"]["delta_usage_api"] == 100
    assert out["estimates_per_discovery_cycle"]["estimated_cu_api_per_seed"] == 10.0
    assert "/defi/token_overview" in out["endpoints"]
