#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

SOL_MINT = "So11111111111111111111111111111111111111112"
JUPITER_QUOTE_URL = "https://api.jup.ag/swap/v1/quote"

DEFAULT_MAX_TOKENS = 35
DEFAULT_HIGH_SCORE_THRESHOLD = 55.0
DEFAULT_RECENT_SEEN_DAYS = 7
DEFAULT_COOLDOWN_SECONDS = 0.5
DEFAULT_STOP_AFTER_FAILURES = 5
DEFAULT_SOL_INPUT_LAMPORTS = 1_000_000  # 0.001 SOL
DEFAULT_TOKEN_INPUT_ATOMIC = 1_000_000


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return fallback


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _token_needs_enrichment(token: dict[str, Any]) -> bool:
    return (
        token.get("last_known_price_impact_bps") is None
        or token.get("last_known_route_state") is None
        or token.get("inferred_liquidity_score") is None
        or token.get("last_enriched_at") is None
    )


def _assign_priority(token: dict[str, Any], *, score_threshold: float, recent_cutoff: datetime) -> str:
    latest_status = str(token.get("latest_status") or "").lower()
    rejection_reason = str(token.get("rejection_reason") or "").lower()
    highest_score_seen = token.get("highest_score_seen")

    traded = bool(token.get("ever_opened")) or bool(token.get("ever_sold"))
    zombie = bool(token.get("ever_zombie")) or bool(token.get("zombie_class")) or ("zombie" in latest_status)
    high_score = isinstance(highest_score_seen, (int, float)) and float(highest_score_seen) >= score_threshold

    if traded or zombie or high_score:
        return "high"

    last_seen = _parse_ts(token.get("last_seen_at"))
    if last_seen is not None and last_seen >= recent_cutoff:
        return "medium"

    # candidate-only/noise, unknown, or old tokens default to low.
    if rejection_reason or latest_status in {"unknown", "rejected", "candidate_only"}:
        return "low"
    return "low"


def _has_position_history(token: dict[str, Any]) -> bool:
    counters = (
        "blocked_events_count",
        "no_route_events_count",
        "route_found_events_count",
        "successful_exit_after_block_count",
    )
    if any((token.get(name) or 0) > 0 for name in counters):
        return True
    if token.get("last_blocked_at") or token.get("last_successful_exit_at"):
        return True
    return False


def _pick_quote_direction(token: dict[str, Any]) -> str:
    latest_status = str(token.get("latest_status") or "").lower()
    is_traded = bool(token.get("ever_opened")) or bool(token.get("ever_sold"))
    is_zombie = bool(token.get("ever_zombie")) or bool(token.get("zombie_class")) or ("zombie" in latest_status)
    if is_traded or is_zombie or _has_position_history(token):
        return "sell"
    return "buy"


def _extract_price_impact_bps(raw: dict[str, Any]) -> float | None:
    for candidate in (raw.get("priceImpactPct"), raw.get("priceImpact"), raw.get("slippageBps")):
        if candidate in (None, ""):
            continue
        try:
            value = float(candidate)
        except (TypeError, ValueError):
            continue
        if value <= 1.0:
            return value * 10_000.0
        return value
    return None


def _inferred_liquidity_score(route_exists: bool, impact_bps: float | None) -> float:
    return _inferred_liquidity_score_with_direction(
        route_exists=route_exists,
        impact_bps=impact_bps,
        direction="buy",
    )


def _route_quality(*, direction: str, route_exists: bool, impact_bps: float | None) -> str:
    if not route_exists:
        return "none"
    if impact_bps is None:
        return "weak"
    strong_cutoff = 120.0 if direction == "buy" else 220.0
    weak_cutoff = 850.0 if direction == "buy" else 950.0
    if impact_bps <= strong_cutoff:
        return "strong"
    if impact_bps <= weak_cutoff:
        return "weak"
    return "none"


def _inferred_liquidity_score_with_direction(*, route_exists: bool, impact_bps: float | None, direction: str) -> float:
    if not route_exists:
        return 0.0
    if impact_bps is None:
        return 25.0 if direction == "sell" else 35.0

    # Direction-aware score that degrades with impact and applies a sell-route penalty.
    base_score = 100.0 - min(max(float(impact_bps), 0.0), 1_000.0) / 10.0
    if direction == "sell":
        base_score -= 8.0
    return max(0.0, min(100.0, round(base_score, 2)))


def _build_queue(
    tokens: list[dict[str, Any]],
    *,
    priority_filter: str,
    score_threshold: float,
    recent_seen_days: int,
) -> list[int]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=recent_seen_days)
    buckets: dict[str, list[int]] = {"high": [], "medium": [], "low": []}

    for index, token in enumerate(tokens):
        if not _token_needs_enrichment(token):
            continue
        bucket = _assign_priority(token, score_threshold=score_threshold, recent_cutoff=cutoff)
        buckets[bucket].append(index)

    if priority_filter == "high":
        return buckets["high"]
    if priority_filter == "medium":
        return buckets["medium"]
    return buckets["high"] + buckets["medium"] + buckets["low"]


def _jupiter_quote_once(
    session: requests.Session,
    *,
    api_key: str,
    input_mint: str,
    output_mint: str,
    amount_atomic: int,
) -> tuple[dict[str, Any] | None, str | None]:
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount_atomic),
    }
    try:
        response = session.get(
            JUPITER_QUOTE_URL,
            params=params,
            headers={"Accept": "application/json", "x-api-key": api_key},
            timeout=20,
        )
    except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError) as exc:
        return None, f"network_error:{type(exc).__name__}"

    if response.status_code >= 400:
        return None, f"http_{response.status_code}"

    try:
        return response.json(), None
    except ValueError:
        return None, "invalid_json"


def run_worker(
    *,
    report_path: Path,
    progress_path: Path,
    max_tokens: int,
    priority: str,
    resume: bool,
    cooldown_seconds: float,
    stop_after_failures: int,
    score_threshold: float,
    recent_seen_days: int,
    sol_input_lamports: int,
    token_input_atomic: int,
) -> int:
    load_dotenv(Path.cwd() / ".env", override=False)
    api_key = (os.getenv("JUPITER_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("JUPITER_API_KEY is required for token enrichment worker")

    report = _read_json(report_path, fallback={})
    tokens = report.get("tokens")
    if not isinstance(tokens, list):
        raise RuntimeError(f"Invalid token report format: expected tokens list at {report_path}")

    queue = _build_queue(
        tokens,
        priority_filter=priority,
        score_threshold=score_threshold,
        recent_seen_days=recent_seen_days,
    )
    if not queue:
        print("processed tokens: 0")
        print("skipped tokens: 0")
        print("updated fields count: 0")
        print("notes: no eligible tokens found for selected priority")
        return 0

    progress = _read_json(progress_path, fallback={})
    queue_cursor = 0
    if resume:
        progress_priority = progress.get("priority")
        if progress_priority == priority:
            queue_cursor = int(progress.get("cursor") or 0)

    queue_cursor = min(max(queue_cursor, 0), len(queue))
    selected = queue[queue_cursor : queue_cursor + max_tokens]

    session = requests.Session()
    now_iso = _utc_now_iso()
    processed_tokens = 0
    skipped_tokens = 0
    updated_fields_count = 0
    consecutive_failures = 0

    for position_in_batch, token_index in enumerate(selected):
        token = tokens[token_index]
        mint = str(token.get("mint") or "").strip()
        if not mint:
            skipped_tokens += 1
            continue

        quote_direction = _pick_quote_direction(token)
        if quote_direction == "sell":
            input_mint = mint
            output_mint = SOL_MINT
            amount_atomic = token_input_atomic
        else:
            input_mint = SOL_MINT
            output_mint = mint
            amount_atomic = sol_input_lamports

        raw, error = _jupiter_quote_once(
            session,
            api_key=api_key,
            input_mint=input_mint,
            output_mint=output_mint,
            amount_atomic=amount_atomic,
        )

        if error is not None:
            consecutive_failures += 1
            previous_state = token.get("last_known_route_state")
            token["last_known_route_state"] = f"quote_error:{error}"
            token["quote_direction"] = quote_direction
            token["route_quality"] = "none"
            token["inferred_liquidity_score"] = 0.0
            token["last_enriched_at"] = now_iso
            if previous_state != token["last_known_route_state"]:
                updated_fields_count += 1
            updated_fields_count += 4
        else:
            consecutive_failures = 0
            out_amount = raw.get("outAmount") if isinstance(raw, dict) else None
            try:
                out_atomic = int(str(out_amount))
            except (TypeError, ValueError):
                out_atomic = 0
            route_exists = out_atomic > 0
            impact_bps = _extract_price_impact_bps(raw if isinstance(raw, dict) else {})
            route_state = "route_exists" if route_exists else "no_route"
            route_quality = _route_quality(
                direction=quote_direction,
                route_exists=route_exists,
                impact_bps=impact_bps,
            )
            liquidity_score = _inferred_liquidity_score_with_direction(
                direction=quote_direction,
                route_exists=route_exists,
                impact_bps=impact_bps,
            )

            before = (
                token.get("last_known_price_impact_bps"),
                token.get("last_known_route_state"),
                token.get("inferred_liquidity_score"),
                token.get("quote_direction"),
                token.get("route_quality"),
                token.get("last_enriched_at"),
            )
            token["last_known_price_impact_bps"] = impact_bps
            token["last_known_route_state"] = route_state
            token["inferred_liquidity_score"] = liquidity_score
            token["quote_direction"] = quote_direction
            token["route_quality"] = route_quality
            token["last_enriched_at"] = now_iso
            after = (
                token.get("last_known_price_impact_bps"),
                token.get("last_known_route_state"),
                token.get("inferred_liquidity_score"),
                token.get("quote_direction"),
                token.get("route_quality"),
                token.get("last_enriched_at"),
            )
            updated_fields_count += sum(1 for old, new in zip(before, after) if old != new)

        processed_tokens += 1

        if consecutive_failures >= stop_after_failures:
            print(f"notes: stopping early after repeated failures ({consecutive_failures})")
            break

        # Cooldown between calls to avoid request spikes.
        if position_in_batch < len(selected) - 1:
            time.sleep(cooldown_seconds)

    # Persist changed token report and cursor.
    report["tokens"] = tokens
    report["enrichment_last_run_at"] = _utc_now_iso()
    _write_json(report_path, report)

    new_cursor = queue_cursor + processed_tokens
    if new_cursor >= len(queue):
        new_cursor = 0
    _write_json(
        progress_path,
        {
            "updated_at": _utc_now_iso(),
            "priority": priority,
            "cursor": new_cursor,
            "queue_size": len(queue),
            "processed_in_last_run": processed_tokens,
            "last_run_max_tokens": max_tokens,
        },
    )

    print(f"processed tokens: {processed_tokens}")
    print(f"skipped tokens: {skipped_tokens}")
    print(f"updated fields count: {updated_fields_count}")
    print(f"remaining in queue (current priority set): {max(0, len(queue) - new_cursor)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Controlled token enrichment worker (Jupiter-only).")
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS, help="Max tokens to process in this run.")
    parser.add_argument(
        "--priority",
        choices=("high", "medium", "all"),
        default="all",
        help="Priority bucket selector.",
    )
    parser.add_argument("--resume", action="store_true", help="Resume from runtime/enrichment_progress.json cursor.")
    parser.add_argument("--report-path", type=Path, default=Path("runtime/token_report.json"))
    parser.add_argument("--progress-path", type=Path, default=Path("runtime/enrichment_progress.json"))
    parser.add_argument("--cooldown-seconds", type=float, default=DEFAULT_COOLDOWN_SECONDS)
    parser.add_argument("--stop-after-failures", type=int, default=DEFAULT_STOP_AFTER_FAILURES)
    parser.add_argument("--score-threshold", type=float, default=DEFAULT_HIGH_SCORE_THRESHOLD)
    parser.add_argument("--recent-seen-days", type=int, default=DEFAULT_RECENT_SEEN_DAYS)
    parser.add_argument("--sol-input-lamports", type=int, default=DEFAULT_SOL_INPUT_LAMPORTS)
    parser.add_argument("--token-input-atomic", type=int, default=DEFAULT_TOKEN_INPUT_ATOMIC)
    args = parser.parse_args()

    max_tokens = max(1, min(int(args.max_tokens), 50))
    cooldown_seconds = max(0.0, float(args.cooldown_seconds))
    stop_after_failures = max(1, int(args.stop_after_failures))

    return run_worker(
        report_path=args.report_path,
        progress_path=args.progress_path,
        max_tokens=max_tokens,
        priority=args.priority,
        resume=bool(args.resume),
        cooldown_seconds=cooldown_seconds,
        stop_after_failures=stop_after_failures,
        score_threshold=float(args.score_threshold),
        recent_seen_days=max(1, int(args.recent_seen_days)),
        sol_input_lamports=max(1, int(args.sol_input_lamports)),
        token_input_atomic=max(1, int(args.token_input_atomic)),
    )


if __name__ == "__main__":
    raise SystemExit(main())
