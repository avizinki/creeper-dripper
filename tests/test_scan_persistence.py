from __future__ import annotations

import json
from dataclasses import dataclass

from creeper_dripper.cli.main import main
from creeper_dripper.engine.discovery import serialize_candidate


def _base_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BIRDEYE_API_KEY", "x")
    monkeypatch.setenv("JUPITER_API_KEY", "x")
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("STATE_PATH", str(tmp_path / "runtime" / "state.json"))
    monkeypatch.setenv("JOURNAL_PATH", str(tmp_path / "runtime" / "journal.jsonl"))
    monkeypatch.delenv("SOLANA_KEYPAIR_PATH", raising=False)
    monkeypatch.delenv("BS58_PRIVATE_KEY", raising=False)


@dataclass(slots=True)
class _Candidate:
    symbol: str
    address: str
    discovery_score: float
    liquidity_usd: float
    volume_24h_usd: float
    buy_sell_ratio: float
    exit_liquidity_available: bool
    exit_liquidity_reason: str | None = None
    rejection_reasons: list[str] | None = None


def test_accepted_token_candidate_serializes_correctly():
    candidate = _Candidate(
        symbol="ABC",
        address="mint1",
        discovery_score=71.2,
        liquidity_usd=12345.0,
        volume_24h_usd=54321.0,
        buy_sell_ratio=1.2,
        exit_liquidity_available=False,
        exit_liquidity_reason="birdeye_chain_not_supported",
        rejection_reasons=[],
    )
    payload = serialize_candidate(candidate)
    assert payload["symbol"] == "ABC"
    assert payload["address"] == "mint1"
    assert payload["mint"] == "mint1"
    assert payload["discovery_score"] == 71.2
    assert payload["rejection_reasons"] == []


def test_top_candidates_seen_serializes_correctly(monkeypatch, tmp_path):
    _base_env(monkeypatch, tmp_path)
    import creeper_dripper.cli.main as cli_main

    def fake_discover(_b, _j, _s, progress_callback=None):
        accepted = [
            {
                "symbol": "TOP",
                "address": "mint-top",
                "discovery_score": 99,
                "liquidity_usd": 100000,
                "volume_24h_usd": 200000,
                "buy_sell_ratio": 1.5,
                "exit_liquidity_available": False,
                "exit_liquidity_reason": "birdeye_chain_not_supported",
                "rejection_reasons": [],
            }
        ]
        if progress_callback:
            progress_callback(
                {
                    "seeds_total": 1,
                    "processed_total": 1,
                    "built_total": 1,
                    "accepted_total": 1,
                    "rejection_counts": {},
                    "last_processed_symbol": "TOP",
                    "last_processed_mint": "mint-top",
                    "top_candidates_seen": accepted,
                },
                accepted,
            )
        return accepted, {"seeds_total": 1, "candidates_built": 1, "candidates_accepted": 1, "candidates_rejected_total": 0, "rejection_counts": {}}

    monkeypatch.setattr(cli_main, "discover_candidates", fake_discover)
    assert main(["scan"]) == 0
    summary = json.loads((tmp_path / "runtime" / "scan_summary.json").read_text(encoding="utf-8"))
    assert len(summary["top_candidates_seen"]) == 1
    assert summary["top_candidates_seen"][0]["symbol"] == "TOP"


def test_zero_accepted_writes_empty_scan_latest(monkeypatch, tmp_path):
    _base_env(monkeypatch, tmp_path)
    import creeper_dripper.cli.main as cli_main

    monkeypatch.setattr(cli_main, "discover_candidates", lambda *args, **kwargs: ([], {"seeds_total": 1, "candidates_built": 1, "candidates_accepted": 0, "candidates_rejected_total": 1, "rejection_counts": {"reject_low_score": 1}}))
    code = main(["scan"])
    assert code == 0
    latest = json.loads((tmp_path / "runtime" / "scan_latest.json").read_text(encoding="utf-8"))
    assert latest == []


def test_scan_summary_updated_incrementally(monkeypatch, tmp_path):
    _base_env(monkeypatch, tmp_path)
    import creeper_dripper.cli.main as cli_main

    def fake_discover(_b, _j, _s, progress_callback=None):
        if progress_callback:
            progress_callback(
                {
                    "seeds_total": 5,
                    "processed_total": 2,
                    "built_total": 2,
                    "accepted_total": 1,
                    "rejection_counts": {"reject_low_score": 1},
                    "last_processed_symbol": "ABC",
                    "last_processed_mint": "mint1",
                    "top_candidates_seen": [{"symbol": "ABC", "address": "mint1", "discovery_score": 70}],
                },
                [{"symbol": "ABC", "address": "mint1", "discovery_score": 70}],
            )
        return [], {"seeds_total": 5, "candidates_built": 2, "candidates_accepted": 0, "candidates_rejected_total": 2, "rejection_counts": {"reject_low_score": 2}}

    monkeypatch.setattr(cli_main, "discover_candidates", fake_discover)
    main(["scan"])
    summary = json.loads((tmp_path / "runtime" / "scan_summary.json").read_text(encoding="utf-8"))
    assert summary["seeds_total"] == 5
    assert "rejection_counts" in summary


def test_keyboard_interrupt_writes_valid_partial_files(monkeypatch, tmp_path, capsys):
    _base_env(monkeypatch, tmp_path)
    import creeper_dripper.cli.main as cli_main

    def fake_discover(_b, _j, _s, progress_callback=None):
        if progress_callback:
            progress_callback(
                {
                    "seeds_total": 7,
                    "processed_total": 3,
                    "built_total": 2,
                    "accepted_total": 1,
                    "rejection_counts": {"reject_low_score": 2},
                    "last_processed_symbol": "ZZZ",
                    "last_processed_mint": "mintz",
                    "top_candidates_seen": [{"symbol": "ZZZ", "address": "mintz", "discovery_score": 60}],
                },
                [{"symbol": "ZZZ", "address": "mintz", "discovery_score": 60}],
            )
        raise KeyboardInterrupt()

    monkeypatch.setattr(cli_main, "discover_candidates", fake_discover)
    code = main(["scan"])
    out = capsys.readouterr().out
    assert code == 130
    assert "scan interrupted; partial results written" in out
    latest = json.loads((tmp_path / "runtime" / "scan_latest.json").read_text(encoding="utf-8"))
    summary = json.loads((tmp_path / "runtime" / "scan_summary.json").read_text(encoding="utf-8"))
    assert isinstance(latest, list)
    assert summary["interrupted"] is True


def test_accepted_candidates_persist_before_completion(monkeypatch, tmp_path):
    _base_env(monkeypatch, tmp_path)
    import creeper_dripper.cli.main as cli_main

    def fake_discover(_b, _j, _s, progress_callback=None):
        accepted = [{"symbol": "T1", "address": "m1", "discovery_score": 80}]
        if progress_callback:
            progress_callback(
                {
                    "seeds_total": 4,
                    "processed_total": 1,
                    "built_total": 1,
                    "accepted_total": 1,
                    "rejection_counts": {},
                    "last_processed_symbol": "T1",
                    "last_processed_mint": "m1",
                    "top_candidates_seen": accepted,
                },
                accepted,
            )
        raise KeyboardInterrupt()

    monkeypatch.setattr(cli_main, "discover_candidates", fake_discover)
    main(["scan"])
    latest = json.loads((tmp_path / "runtime" / "scan_latest.json").read_text(encoding="utf-8"))
    assert len(latest) == 1
    assert latest[0]["symbol"] == "T1"


def test_summary_serialization_failure_does_not_erase_accepted_candidates(monkeypatch, tmp_path):
    _base_env(monkeypatch, tmp_path)
    import creeper_dripper.cli.main as cli_main

    accepted = [{"symbol": "KEEP", "address": "mint-keep", "discovery_score": 88}]

    def fake_discover(_b, _j, _s, progress_callback=None):
        return accepted, {"seeds_total": 1, "candidates_built": 1, "candidates_accepted": 1, "candidates_rejected_total": 0, "rejection_counts": {}}

    call_count = {"n": 0}

    def flaky_serialize(_candidates):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return accepted
        raise RuntimeError("boom")

    monkeypatch.setattr(cli_main, "discover_candidates", fake_discover)
    monkeypatch.setattr(cli_main, "serialize_candidates", flaky_serialize)
    assert main(["scan"]) == 0
    latest = json.loads((tmp_path / "runtime" / "scan_latest.json").read_text(encoding="utf-8"))
    summary = json.loads((tmp_path / "runtime" / "scan_summary.json").read_text(encoding="utf-8"))
    assert latest == accepted
    assert summary["accepted_total"] == 1
    assert summary["top_candidates_seen"] == []
    assert "summary_serialization_error" in summary
