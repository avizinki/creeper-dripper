from __future__ import annotations

from creeper_dripper.dashboard import app as dashboard_app


def test_birdeye_global_cooldown_never_reports_healthy(monkeypatch):
    monkeypatch.setattr(dashboard_app, "_tail_lines", lambda _path, _n: [])
    mode, reason = dashboard_app._derive_birdeye_budget_mode(
        discovery_failed=True,
        discovery_error="birdeye_global_cooldown:/defi/token_security",
        discovery_error_type=None,
        cycle_birdeye_budget_mode=None,
        birdeye_audit=None,
    )
    assert mode == "starved"
    assert "global_cooldown" in reason


def test_birdeye_failed_discovery_floor_is_constrained(monkeypatch):
    monkeypatch.setattr(dashboard_app, "_tail_lines", lambda _path, _n: [])
    mode, reason = dashboard_app._derive_birdeye_budget_mode(
        discovery_failed=True,
        discovery_error="birdeye_probe_failed",
        discovery_error_type="runtime_error",
        cycle_birdeye_budget_mode=None,
        birdeye_audit=None,
    )
    assert mode == "constrained"
    assert "discovery_failed_birdeye" in reason


def test_cycle_budget_mode_constrained_is_not_downgraded(monkeypatch):
    monkeypatch.setattr(dashboard_app, "_tail_lines", lambda _path, _n: [])
    mode, reason = dashboard_app._derive_birdeye_budget_mode(
        discovery_failed=False,
        discovery_error=None,
        discovery_error_type=None,
        cycle_birdeye_budget_mode="constrained",
        birdeye_audit=None,
    )
    assert mode == "constrained"
    assert "cycle_budget_mode=constrained" in reason
