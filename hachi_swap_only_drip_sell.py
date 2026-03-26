"""
hachi_swap_only_drip_sell.py

RETIRED STANDALONE SCRIPT — NO LONGER THE OPERATIONAL SOURCE OF TRUTH
======================================================================
As of 2026-03-26 (commit following bfe1b26 / HACHI-DRIP-KILL-ONCE-AND-FOR-ALL):

- The bot's _run_hachi_dripper() in creeper_dripper/engine/trader.py is the
  authoritative implementation for Hachi drip selling.
- executor.sell_with_probe_fidelity() in creeper_dripper/execution/executor.py
  captures the pattern from this script (select on /order, execute that exact
  order) and is available to the bot's execution path.
- Proof fields (selected_chunk_qty, selected_efficiency, selected_price_impact_bps,
  selected_router, executed_matches_selected) are now emitted in DRIPPER_CHUNK_SELECTED
  and DRIPPER_CHUNK_EXECUTED decisions.

This file is retained for reference only. Do NOT run it in production alongside
the bot — it would double-sell positions. The router allowlist (iris/dflow) from
this script was intentionally NOT ported; the bot gates on price impact instead.
======================================================================

HACHI drip seller using Jupiter Swap API V2.

Behavior:
- each sell cycle: GET /order for several fixed candidate chunk sizes, pick the best route
  by outAmount/inputAmount efficiency, then sign + POST /execute for that quote only
- waits a random time between sells
- no RPC
- no balance fetch
- no confirmation polling
- swap-only: GET /order -> sign -> POST /execute

IMPORTANT:
- This script assumes HACHI decimals = 4
- raw amount = token_amount * 10**4 (e.g. 2M HACHI = 20_000_000_000 raw)

Required env:
- JUPITER_API_KEY=...
- SOLANA_KEYPAIR_PATH=/full/path/to/id.json

Optional env:
- HACHI_MINT=x95HN3DWvbfCBtTjGm587z8suK3ec6cwQwgZNLbWKyp
- MIN_WAIT_SECONDS=90
- MAX_WAIT_SECONDS=420
- MAX_SELLS=0
- STOP_ON_ERROR=1
- DEBUG=0
"""

from __future__ import annotations

import base64
import json
import logging
import os
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests
from requests.exceptions import ConnectionError, RequestException, Timeout
from solders.keypair import Keypair
from solders.message import to_bytes_versioned
from solders.transaction import VersionedTransaction

# =========================================================
# CONFIG
# =========================================================

JUP_BASE_URL = "https://api.jup.ag/swap/v2"
SOL_MINT = "So11111111111111111111111111111111111111112"

TOKEN_SYMBOL = "HACHI"
TOKEN_MINT = os.getenv(
    "HACHI_MINT",
    "x95HN3DWvbfCBtTjGm587z8suK3ec6cwQwgZNLbWKyp",
).strip()

TOKEN_DECIMALS = 4
HACHI_RAW_PER_TOKEN = 10**TOKEN_DECIMALS
SOL_RAW_PER_SOL = 10**9

# Candidate sell sizes (whole tokens, 4 decimals). Raw = tokens * 10**4.
BASE_CANDIDATE_CHUNK_SIZES_TOKENS: list[int] = [5_000_000, 10_000_000, 20_000_000, 50_000_000]
SPIKE_CANDIDATE_CHUNK_SIZES_TOKENS: list[int] = [20_000_000, 50_000_000, 100_000_000, 200_000_000]

REQUIRE_JUPITER_ROUTER_NAMES: set[str] = {"iris", "dflow"}

# Optional floor on outAmount/inputAmount; None = only malformed / optional impact filter apply.
MIN_ACCEPTABLE_OUT_PER_TOKEN_RATIO: Optional[float] = None

# If Jupiter returns a price-impact field (percent), reject quotes above this. None = do not filter on impact.
MAX_PRICE_IMPACT_PCT_REJECT: Optional[float] = 2.0

MIN_ACCEPTABLE_CANDIDATE_COUNT = 1
SPIKE_TRIGGER_RATIO_IMPROVEMENT = 0.05
BEST_SEEN_LOOKBACK_CYCLES = 8
MIN_SPIKE_TRIGGER_BEST_SEEN_IMPROVEMENT = 0.005
NEAR_EQUAL_RATIO_BAND = 0.002
MAX_SPIKE_CHUNK_SIZE_TOKENS = 100_000_000

# Liquidity exhaustion interpretation layer (quote-quality downshift controls).
LIQUIDITY_EXHAUSTION_LOOKBACK = 8
LIQUIDITY_WEAKENING_RATIO_DROP = 0.03
LIQUIDITY_WEAK_RATIO_DROP = 0.06
LIQUIDITY_EXHAUSTED_RATIO_DROP = 0.10
LIQUIDITY_WEAKENING_PRICE_IMPACT_PCT = 0.05
LIQUIDITY_WEAK_PRICE_IMPACT_PCT = 0.15
LIQUIDITY_EXHAUSTED_PRICE_IMPACT_PCT = 0.30
LIQUIDITY_SIZE_DEGRADATION_MIN_DELTA = 0.005
EXHAUSTION_DEFENSIVE_BASE_CANDIDATES: list[int] = [5_000_000, 10_000_000, 20_000_000]
EXHAUSTION_WEAK_BASE_CANDIDATES: list[int] = [5_000_000, 10_000_000]
EXHAUSTION_EXHAUSTED_BASE_CANDIDATES: list[int] = [5_000_000]
EXHAUSTION_DISABLE_SPIKE_IN_WEAK = True
EXHAUSTION_DISABLE_SPIKE_IN_EXHAUSTED = True
EXHAUSTION_FORCE_WAIT_MODE: dict[str, Optional[str]] = {
    "healthy": None,
    "weakening": "normal",
    "weak": "defensive",
    "exhausted": "cooldown",
}

# =========================================================
# ROUND-TRIP (sell -> optional buyback -> re-sell)
# =========================================================

ENABLE_REENTRY = os.getenv("ENABLE_REENTRY", "0") == "1"
REENTRY_MIN_COOLDOWN_SECONDS = 300
REENTRY_MIN_EDGE_PCT = 0.05
REENTRY_MAX_FREE_SOL_USAGE_PCT = 0.35
REENTRY_CANDIDATE_SOL_RAW_PCTS: list[float] = [0.10, 0.20, 0.35]
REENTRY_MIN_SOL_SPEND_RAW = 10_000
REENTRY_REQUIRE_PULLBACK_VS_RECENT_SELL = True

# Re-sell rebought inventory when it has profit edge.
ENABLE_RESELL_REENTRY_LOTS = os.getenv("ENABLE_RESELL_REENTRY_LOTS", "0") == "1"
RESELL_MIN_PROFIT_EDGE_PCT = 0.05
RESELL_REENTRY_COOLDOWN_SECONDS = 180

ENABLE_SELL_ON_STRENGTH_ONLY = True
STRENGTH_LOOKBACK_TRADES = 3
MIN_STRENGTH_IMPROVEMENT_RATIO = 0.03

# Normal-sell strength (softer than legacy rolling-only gate; spike logic unchanged).
NORMAL_SELL_STRENGTH_LOOKBACK = 5
NORMAL_SELL_MAX_RATIO_DROP_FROM_RECENT_BEST = 0.06
NORMAL_SELL_MAX_RATIO_DROP_FROM_RECENT_AVG = 0.05
NORMAL_SELL_FORCE_ALLOW_IF_PRICE_IMPACT_PCT_BELOW = 0.30

PERSIST_RUNTIME_STATE = True
RUNTIME_STATE_PATH = "runtime/hachi_swap_only_drip_sell_state.json"

MIN_WAIT_SECONDS = int(os.getenv("MIN_WAIT_SECONDS", "90"))
MAX_WAIT_SECONDS = int(os.getenv("MAX_WAIT_SECONDS", "420"))

WAIT_MODE_RANGES: dict[str, tuple[int, int]] = {
    "aggressive": (20, 60),
    "normal": (45, 120),
    "defensive": (120, 300),
    "cooldown": (300, 900),
}

_max_sells_env = os.getenv("MAX_SELLS", "0").strip()
MAX_SELLS: Optional[int] = None if _max_sells_env in ("", "0") else int(_max_sells_env)

STOP_ON_ERROR = os.getenv("STOP_ON_ERROR", "1") == "1"
DEBUG = os.getenv("DEBUG", "0") == "1"

JUPITER_API_KEY = os.getenv("JUPITER_API_KEY", "").strip()
SOLANA_KEYPAIR_PATH = os.getenv("SOLANA_KEYPAIR_PATH", "").strip()

if not JUPITER_API_KEY:
    raise RuntimeError("Missing JUPITER_API_KEY")
if not SOLANA_KEYPAIR_PATH:
    raise RuntimeError("Missing SOLANA_KEYPAIR_PATH")
if not TOKEN_MINT:
    raise RuntimeError("Missing HACHI_MINT")

if MIN_WAIT_SECONDS < 1 or MAX_WAIT_SECONDS < 1:
    raise RuntimeError("MIN_WAIT_SECONDS and MAX_WAIT_SECONDS must be >= 1")
if MIN_WAIT_SECONDS > MAX_WAIT_SECONDS:
    raise RuntimeError("MIN_WAIT_SECONDS cannot be greater than MAX_WAIT_SECONDS")
if not BASE_CANDIDATE_CHUNK_SIZES_TOKENS:
    raise RuntimeError("BASE_CANDIDATE_CHUNK_SIZES_TOKENS must be non-empty")
if not SPIKE_CANDIDATE_CHUNK_SIZES_TOKENS:
    raise RuntimeError("SPIKE_CANDIDATE_CHUNK_SIZES_TOKENS must be non-empty")

# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("hachi_swap_only_drip_sell")

#region debug instrumentation (round-trip ledger)
DEBUG_LOG_PATH = "/Users/yoav/git/creepers-bot/.cursor/debug-222fe5.log"
DEBUG_SESSION_ID = "222fe5"
DEBUG_RUN_ID = os.getenv("DEBUG_RUN_ID", "") or f"rt_{int(time.time())}"


def _debug_append_ndjson(
    hypothesis_id: str,
    location: str,
    message: str,
    data: Optional[dict[str, Any]] = None,
) -> None:
    payload: dict[str, Any] = {
        "sessionId": DEBUG_SESSION_ID,
        "runId": DEBUG_RUN_ID,
        "hypothesisId": hypothesis_id,
        "location": location,
        "message": message,
        "data": data or {},
        "timestamp": int(time.time() * 1000),
    }
    try:
        with open(DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        # Never break execution due to debug instrumentation.
        pass

#endregion

# =========================================================
# SESSION / HTTP (resilient transport)
# =========================================================

def build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "hachi-swap-only-drip-sell/1.2",
        "Connection": "close",
    })
    return s


session = build_session()


def reset_session() -> None:
    global session
    try:
        session.close()
    except Exception:
        pass
    session = build_session()


def http_get_with_retry(
    url: str,
    *,
    headers: dict[str, str],
    params: dict[str, str],
    timeout: int,
    attempts: int = 4,
) -> requests.Response:
    last_err: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return session.get(url, headers=headers, params=params, timeout=timeout)
        except KeyboardInterrupt:
            raise
        except (ConnectionError, Timeout) as e:
            last_err = e
            log.warning("GET retry %s/%s failed: %s", attempt, attempts, e)
            reset_session()
            if attempt < attempts:
                time.sleep(min(2 * attempt, 8))
        except RequestException as e:
            last_err = e
            log.warning("GET retry %s/%s failed (request error): %s", attempt, attempts, e)
            reset_session()
            if attempt < attempts:
                time.sleep(min(2 * attempt, 8))
    raise RuntimeError(f"GET failed after {attempts} attempts: {last_err}")


def http_post_with_retry(
    url: str,
    *,
    headers: dict[str, str],
    json_payload: dict[str, Any],
    timeout: int,
    attempts: int = 4,
) -> requests.Response:
    last_err: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return session.post(url, headers=headers, json=json_payload, timeout=timeout)
        except KeyboardInterrupt:
            raise
        except (ConnectionError, Timeout) as e:
            last_err = e
            log.warning("POST retry %s/%s failed: %s", attempt, attempts, e)
            reset_session()
            if attempt < attempts:
                time.sleep(min(2 * attempt, 8))
        except RequestException as e:
            last_err = e
            log.warning("POST retry %s/%s failed (request error): %s", attempt, attempts, e)
            reset_session()
            if attempt < attempts:
                time.sleep(min(2 * attempt, 8))
    raise RuntimeError(f"POST failed after {attempts} attempts: {last_err}")


# =========================================================
# WALLET
# =========================================================

def load_keypair_from_json_file(path: str) -> Keypair:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, list):
            raise ValueError("keypair file must contain a JSON array")

        if len(data) not in (32, 64):
            raise ValueError(f"expected 32 or 64 integers, got {len(data)}")

        secret_bytes = bytes(data)
        return Keypair.from_bytes(secret_bytes)
    except Exception as e:
        raise RuntimeError(f"Failed to load keypair from {path}: {e}") from e

keypair = load_keypair_from_json_file(SOLANA_KEYPAIR_PATH)
wallet_pubkey = str(keypair.pubkey())

# =========================================================
# HELPERS
# =========================================================

def tokens_to_raw(tokens: int, decimals: int) -> int:
    return tokens * (10**decimals)


def parse_int_field(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return int(s)
        except ValueError:
            return None
    return None


def parse_float_field(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


def parse_price_impact_pct(order: dict[str, Any]) -> Optional[float]:
    """Return price impact in percent if Jupiter exposes a usable field."""
    for key in ("priceImpactPct", "priceImpact"):
        v = parse_float_field(order.get(key))
        if v is not None:
            return v
    return None


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _state_file_path() -> Path:
    return Path(__file__).resolve().parent.parent / RUNTIME_STATE_PATH


def load_runtime_state() -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "recent_execution_ratios": [],
        "recent_selected_chunk_tokens": [],
        "recent_acceptable_candidate_counts": [],
        "recent_rejected_router_counts": [],
        "recent_rejected_impact_counts": [],
        "last_mode": "base",
        "last_success_at": "",
        "consecutive_skipped_cycles": 0,
        "recent_best_quote_ratios": [],
        # Sell ratios for sell-on-strength / spike trigger (sol_raw per hachi_raw).
        # recent_execution_ratios already tracks that via quote efficiency_ratio.
        "recent_sell_hachi_per_sol_ratios": [],
        # Buy efficiency ratios (hachi_raw per sol_raw).
        "recent_buy_efficiency_ratios": [],
        "force_spike_next_cycle": False,
        "last_selected_ratio": None,
        "last_liquidity_state": "healthy",
        "recent_liquidity_states": [],
        "last_liquidity_reasons": [],
        "last_abs_price_impact_pct": None,
        "last_size_degradation_flag": False,
        # Round-trip ledger (sell -> optional buyback -> re-sell).
        "cumulative_hachi_sold_tokens": 0,
        "cumulative_hachi_sold_raw": 0,
        "cumulative_sol_received_raw": 0,
        "cumulative_hachi_rebought_tokens": 0,
        "cumulative_hachi_rebought_raw": 0,
        "cumulative_sol_spent_on_reentry_raw": 0,
        "cumulative_hachi_resold_after_reentry_tokens": 0,
        "cumulative_hachi_resold_after_reentry_raw": 0,
        # OPEN reentry lots (FIFO) for rebought inventory we can sell again.
        "reentry_open_lots": [],
        "next_lot_id": 1,
        # Action timestamps
        "mode_last_action": "skip",
        "last_sell_at": None,
        "last_buy_at": None,
        "last_resell_reentry_at": None,
    }
    if not PERSIST_RUNTIME_STATE:
        return defaults

    p = _state_file_path()
    if not p.is_file():
        return defaults
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return defaults
        defaults.update(data)
        for k in (
            "recent_execution_ratios",
            "recent_selected_chunk_tokens",
            "recent_acceptable_candidate_counts",
            "recent_rejected_router_counts",
            "recent_rejected_impact_counts",
            "recent_best_quote_ratios",
            "recent_sell_hachi_per_sol_ratios",
            "recent_buy_efficiency_ratios",
            "recent_liquidity_states",
            "last_liquidity_reasons",
        ):
            if not isinstance(defaults.get(k), list):
                defaults[k] = []
        return defaults
    except Exception as e:
        log.warning("Failed to load runtime state: %s", e)
        return defaults


def save_runtime_state(state: dict[str, Any]) -> None:
    if not PERSIST_RUNTIME_STATE:
        return
    p = _state_file_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log.warning("Failed to save runtime state: %s", e)


def append_recent_ratio(state: dict[str, Any], ratio: float, chunk_tokens: int, keep: int = 20) -> None:
    ratios = state.setdefault("recent_execution_ratios", [])
    chunks = state.setdefault("recent_selected_chunk_tokens", [])
    ratios.append(float(ratio))
    chunks.append(int(chunk_tokens))
    if len(ratios) > keep:
        del ratios[:-keep]
    if len(chunks) > keep:
        del chunks[:-keep]


def average_recent_ratio(state: dict[str, Any], lookback: int) -> Optional[float]:
    ratios = state.get("recent_execution_ratios") or []
    if not isinstance(ratios, list):
        return None
    vals = [float(x) for x in ratios if isinstance(x, (int, float))]
    if len(vals) < lookback:
        return None
    recent = vals[-lookback:]
    if not recent:
        return None
    return sum(recent) / len(recent)


def get_recent_sell_ratios(state: dict[str, Any], n: int) -> list[float]:
    """Last up to n executed sell efficiency ratios (SOL raw per HACHI raw), newest at end."""
    ratios = state.get("recent_execution_ratios") or []
    if not isinstance(ratios, list) or n <= 0:
        return []
    vals = [float(x) for x in ratios if isinstance(x, (int, float)) and float(x) > 0]
    if not vals:
        return []
    return vals[-n:] if len(vals) > n else vals


def _push_recent_int_metric(state: dict[str, Any], key: str, value: int, keep: int = 10) -> None:
    arr = state.setdefault(key, [])
    if not isinstance(arr, list):
        arr = []
        state[key] = arr
    arr.append(int(value))
    if len(arr) > keep:
        del arr[:-keep]


def _push_recent_float_metric(state: dict[str, Any], key: str, value: float, keep: int = 20) -> None:
    arr = state.setdefault(key, [])
    if not isinstance(arr, list):
        arr = []
        state[key] = arr
    arr.append(float(value))
    if len(arr) > keep:
        del arr[:-keep]


def should_trigger_spike_mode(current_best_ratio: float, state: dict[str, Any]) -> tuple[bool, str, Optional[float]]:
    rolling_exec_avg = average_recent_ratio(state, STRENGTH_LOOKBACK_TRADES)
    if rolling_exec_avg is not None and rolling_exec_avg > 0:
        if current_best_ratio >= rolling_exec_avg * (1.0 + SPIKE_TRIGGER_RATIO_IMPROVEMENT):
            return (
                True,
                f"best ratio >= rolling executed average by {SPIKE_TRIGGER_RATIO_IMPROVEMENT * 100:.1f}%",
                rolling_exec_avg,
            )

    best_quotes = state.get("recent_best_quote_ratios") or []
    vals = [float(x) for x in best_quotes if isinstance(x, (int, float))]
    if vals:
        ref = max(vals[-BEST_SEEN_LOOKBACK_CYCLES:])
        if current_best_ratio >= ref * (1.0 + MIN_SPIKE_TRIGGER_BEST_SEEN_IMPROVEMENT):
            return (
                True,
                "best ratio exceeds local high by "
                f"{MIN_SPIKE_TRIGGER_BEST_SEEN_IMPROVEMENT * 100:.2f}% (lookback={BEST_SEEN_LOOKBACK_CYCLES})",
                ref,
            )

    return False, "no spike trigger", rolling_exec_avg


def evaluate_candidate(chunk_tokens: int, chunk_raw: int, order: dict[str, Any]) -> dict[str, Any]:
    unsigned_tx_b64 = order.get("transaction")
    request_id = order.get("requestId")
    out_raw = parse_int_field(order.get("outAmount"))
    in_raw = (
        parse_int_field(order.get("inAmount"))
        or parse_int_field(order.get("inputAmount"))
        or chunk_raw
    )
    router = str(order.get("router", "") or "").strip().lower()
    pi = parse_price_impact_pct(order)

    accepted = True
    rejection_reason: Optional[str] = None

    if not unsigned_tx_b64 or not request_id:
        accepted = False
        rejection_reason = "missing transaction/requestId"
    elif out_raw is None or out_raw <= 0:
        accepted = False
        rejection_reason = "invalid outAmount"
    elif in_raw <= 0:
        accepted = False
        rejection_reason = "invalid inputAmount"
    elif not router or router not in REQUIRE_JUPITER_ROUTER_NAMES:
        accepted = False
        rejection_reason = f"router={router or 'unknown'} not allowed"
    elif MAX_PRICE_IMPACT_PCT_REJECT is not None and pi is not None and abs(pi) > MAX_PRICE_IMPACT_PCT_REJECT:
        accepted = False
        rejection_reason = f"abs(priceImpact)={abs(pi):.4f} > {MAX_PRICE_IMPACT_PCT_REJECT}"

    efficiency_ratio = 0.0
    if in_raw > 0 and out_raw is not None:
        efficiency_ratio = out_raw / float(in_raw)
    if (
        accepted
        and MIN_ACCEPTABLE_OUT_PER_TOKEN_RATIO is not None
        and efficiency_ratio < MIN_ACCEPTABLE_OUT_PER_TOKEN_RATIO
    ):
        accepted = False
        rejection_reason = "below MIN_ACCEPTABLE_OUT_PER_TOKEN_RATIO"

    return {
        "chunk_tokens": chunk_tokens,
        "chunk_raw": chunk_raw,
        "order": order,
        "out_amount_raw": out_raw,
        "input_amount_raw": in_raw,
        "efficiency_ratio": efficiency_ratio,
        "router": router,
        "price_impact_pct": pi,
        "accepted": accepted,
        "rejection_reason": rejection_reason,
    }


def should_allow_normal_sell(
    current_ratio: float,
    state: dict[str, Any],
    price_impact_pct: Optional[Any],
) -> tuple[bool, str, Optional[float], Optional[float], Optional[float]]:
    """
    Adaptive normal-sell gate: allow near recent best/avg or when quote impact is very low.
    Returns (allow, reason, recent_best, recent_avg, abs_price_impact_pct).
    """
    if not ENABLE_SELL_ON_STRENGTH_ONLY:
        return True, "strength filter disabled", None, None, None

    abs_pi: Optional[float] = None
    if price_impact_pct is not None:
        try:
            abs_pi = abs(float(price_impact_pct))
        except (TypeError, ValueError):
            abs_pi = None

    if abs_pi is not None and abs_pi <= NORMAL_SELL_FORCE_ALLOW_IF_PRICE_IMPACT_PCT_BELOW:
        return (
            True,
            f"allow: abs(priceImpact)={abs_pi:.4f}% <= force-allow {NORMAL_SELL_FORCE_ALLOW_IF_PRICE_IMPACT_PCT_BELOW}%",
            None,
            None,
            abs_pi,
        )

    recent = get_recent_sell_ratios(state, NORMAL_SELL_STRENGTH_LOOKBACK)
    if not recent:
        return True, "allow: insufficient sell history for strength comparison", None, None, abs_pi

    recent_best = max(recent)
    recent_avg = sum(recent) / len(recent)
    floor_best = recent_best * (1.0 - NORMAL_SELL_MAX_RATIO_DROP_FROM_RECENT_BEST)
    floor_avg = recent_avg * (1.0 - NORMAL_SELL_MAX_RATIO_DROP_FROM_RECENT_AVG)

    if current_ratio >= floor_best:
        return (
            True,
            f"allow: current within {NORMAL_SELL_MAX_RATIO_DROP_FROM_RECENT_BEST * 100:.0f}% of recent best",
            recent_best,
            recent_avg,
            abs_pi,
        )
    if current_ratio >= floor_avg:
        return (
            True,
            f"allow: current within {NORMAL_SELL_MAX_RATIO_DROP_FROM_RECENT_AVG * 100:.0f}% of recent avg",
            recent_best,
            recent_avg,
            abs_pi,
        )

    return (
        False,
        "skip: below recent best/avg floors (and impact force-allow not applicable)",
        recent_best,
        recent_avg,
        abs_pi,
    )


def evaluate_liquidity_state(candidates: list[dict[str, Any]], state: dict[str, Any]) -> dict[str, Any]:
    accepted = [c for c in candidates if c.get("accepted")]
    selected = select_best_candidate(candidates)
    current_ratio: Optional[float] = None
    abs_pi: Optional[float] = None
    if selected is not None:
        current_ratio = float(selected.get("efficiency_ratio") or 0.0)
        pi_val = selected.get("price_impact_pct")
        if pi_val is not None:
            try:
                abs_pi = abs(float(pi_val))
            except (TypeError, ValueError):
                abs_pi = None

    recent = get_recent_sell_ratios(state, LIQUIDITY_EXHAUSTION_LOOKBACK)
    recent_best_ratio: Optional[float] = max(recent) if recent else None
    recent_avg_ratio: Optional[float] = (sum(recent) / len(recent)) if recent else None

    drop_vs_best: Optional[float] = None
    drop_vs_avg: Optional[float] = None
    if current_ratio is not None and recent_best_ratio is not None and recent_best_ratio > 0:
        drop_vs_best = max(0.0, 1.0 - (current_ratio / recent_best_ratio))
    if current_ratio is not None and recent_avg_ratio is not None and recent_avg_ratio > 0:
        drop_vs_avg = max(0.0, 1.0 - (current_ratio / recent_avg_ratio))

    size_degradation = False
    if len(accepted) >= 2:
        ranked = sorted(accepted, key=lambda c: int(c.get("chunk_tokens") or 0))
        small = ranked[0]
        large = ranked[-1]
        small_ratio = float(small.get("efficiency_ratio") or 0.0)
        large_ratio = float(large.get("efficiency_ratio") or 0.0)
        if small_ratio > 0:
            delta = (small_ratio - large_ratio) / small_ratio
            size_degradation = delta >= LIQUIDITY_SIZE_DEGRADATION_MIN_DELTA

    reasons: list[str] = []
    ratio_down_3 = bool(
        (drop_vs_best is not None and drop_vs_best >= LIQUIDITY_WEAKENING_RATIO_DROP)
        or (drop_vs_avg is not None and drop_vs_avg >= LIQUIDITY_WEAKENING_RATIO_DROP)
    )
    ratio_down_6 = bool(
        (drop_vs_best is not None and drop_vs_best >= LIQUIDITY_WEAK_RATIO_DROP)
        or (drop_vs_avg is not None and drop_vs_avg >= LIQUIDITY_WEAK_RATIO_DROP)
    )
    ratio_down_10 = bool(
        (drop_vs_best is not None and drop_vs_best >= LIQUIDITY_EXHAUSTED_RATIO_DROP)
        or (drop_vs_avg is not None and drop_vs_avg >= LIQUIDITY_EXHAUSTED_RATIO_DROP)
    )
    impact_weakening = abs_pi is not None and abs_pi >= LIQUIDITY_WEAKENING_PRICE_IMPACT_PCT
    impact_weak = abs_pi is not None and abs_pi >= LIQUIDITY_WEAK_PRICE_IMPACT_PCT
    impact_exhausted = abs_pi is not None and abs_pi >= LIQUIDITY_EXHAUSTED_PRICE_IMPACT_PCT

    severe_size_plus_weak_ratio = size_degradation and ratio_down_6

    liquidity_state = "healthy"
    if ratio_down_10 or impact_exhausted or severe_size_plus_weak_ratio:
        liquidity_state = "exhausted"
        if ratio_down_10:
            reasons.append("ratio_down_10pct")
        if impact_exhausted:
            reasons.append(f"impact_above_{LIQUIDITY_EXHAUSTED_PRICE_IMPACT_PCT:.2f}pct")
        if severe_size_plus_weak_ratio:
            reasons.append("size_degradation_plus_ratio_down_6pct")
    elif ratio_down_6 or impact_weak or size_degradation:
        liquidity_state = "weak"
        if ratio_down_6:
            reasons.append("ratio_down_6pct")
        if impact_weak:
            reasons.append(f"impact_above_{LIQUIDITY_WEAK_PRICE_IMPACT_PCT:.2f}pct")
        if size_degradation:
            reasons.append("size_degradation")
    elif ratio_down_3 or impact_weakening:
        liquidity_state = "weakening"
        if ratio_down_3:
            reasons.append("ratio_down_3pct")
        if impact_weakening:
            reasons.append(f"impact_above_{LIQUIDITY_WEAKENING_PRICE_IMPACT_PCT:.2f}pct")
    else:
        reasons.append("within_expected_range")

    return {
        "state": liquidity_state,
        "current_ratio": current_ratio,
        "recent_best_ratio": recent_best_ratio,
        "recent_avg_ratio": recent_avg_ratio,
        "abs_price_impact_pct": abs_pi,
        "size_degradation": size_degradation,
        "reasons": reasons,
    }


def iso_to_ts_seconds(iso_val: Any) -> Optional[float]:
    if iso_val is None:
        return None
    if not isinstance(iso_val, str):
        return None
    try:
        # datetime.fromisoformat supports timezone offsets for ISO strings.
        return datetime.fromisoformat(iso_val).timestamp()
    except Exception:
        return None


def is_execute_success(result: dict[str, Any]) -> bool:
    status = result.get("status")
    if result.get("error"):
        return False
    return status in (None, "Success", "Executed", "confirmed", "Confirmed")


def compute_open_reentry_lots(state: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
    lots = state.get("reentry_open_lots") or []
    out: list[tuple[int, dict[str, Any]]] = []
    if not isinstance(lots, list):
        return out
    for idx, lot in enumerate(lots):
        if not isinstance(lot, dict):
            continue
        if str(lot.get("status", "")).upper() == "OPEN":
            out.append((idx, lot))
    return out


def get_total_reentry_held_hachi_raw(state: dict[str, Any]) -> int:
    held = 0
    for _, lot in compute_open_reentry_lots(state):
        held += int(lot.get("remaining_hachi_raw") or 0)
    return held


def compute_free_sol_budget_raw(state: dict[str, Any]) -> int:
    received = int(state.get("cumulative_sol_received_raw") or 0)
    spent = int(state.get("cumulative_sol_spent_on_reentry_raw") or 0)
    return max(0, received - spent)


def compute_free_reentry_hachi_capacity_raw(state: dict[str, Any]) -> int:
    sold_raw = int(state.get("cumulative_hachi_sold_raw") or 0)
    resold_raw = int(state.get("cumulative_hachi_resold_after_reentry_raw") or 0)
    held_raw = get_total_reentry_held_hachi_raw(state)
    return max(0, sold_raw - resold_raw - held_raw)


def average_recent_sell_hachi_per_sol_ratio(state: dict[str, Any], lookback: int) -> Optional[float]:
    vals = state.get("recent_sell_hachi_per_sol_ratios") or []
    if not isinstance(vals, list):
        return None
    usable = [float(x) for x in vals if isinstance(x, (int, float)) and x > 0]
    if len(usable) < lookback:
        return None
    recent = usable[-lookback:]
    if not recent:
        return None
    return sum(recent) / len(recent)


def choose_wait_mode(state: dict[str, Any]) -> tuple[str, str]:
    consecutive_skips = int(state.get("consecutive_skipped_cycles") or 0)
    if consecutive_skips >= 3:
        return "cooldown", "forced cooldown: last 3+ cycles skipped"

    spike_triggered = bool(state.get("last_cycle_spike_triggered"))
    current_ratio_val = state.get("last_selected_ratio")
    current_ratio: Optional[float]
    try:
        current_ratio = float(current_ratio_val) if current_ratio_val is not None else None
    except (TypeError, ValueError):
        current_ratio = None

    recent_avg = average_recent_ratio(state, STRENGTH_LOOKBACK_TRADES)
    if spike_triggered:
        return "aggressive", "spike mode triggered this cycle"
    if current_ratio is None or recent_avg is None or recent_avg <= 0:
        return "normal", "insufficient ratio history for adaptive wait"

    if current_ratio >= recent_avg * 1.01:
        return "aggressive", "current ratio >= recent avg * 1.01"
    if current_ratio >= recent_avg * 0.98:
        return "normal", "current ratio >= recent avg * 0.98"
    if current_ratio >= recent_avg * 0.95:
        return "defensive", "current ratio >= recent avg * 0.95"
    return "cooldown", "current ratio below recent avg * 0.95"


def apply_liquidity_wait_override(mode: str, state: dict[str, Any]) -> tuple[str, str]:
    order = {"aggressive": 0, "normal": 1, "defensive": 2, "cooldown": 3}
    liq_state = str(state.get("last_liquidity_state") or "healthy")
    forced = EXHAUSTION_FORCE_WAIT_MODE.get(liq_state)
    if not forced:
        return mode, "no liquidity wait override"
    base_rank = order.get(mode, 1)
    forced_rank = order.get(forced, 1)
    if forced_rank > base_rank:
        return forced, f"forced by liquidity_state={liq_state}"
    return mode, f"liquidity_state={liq_state} did not require stricter wait"


def random_sleep_for_mode(mode: str, reason: str) -> None:
    min_s, max_s = WAIT_MODE_RANGES.get(mode, WAIT_MODE_RANGES["normal"])
    wait_s = random.randint(min_s, max_s)
    log.info(
        "Adaptive wait: mode=%s reason=%s range=%s-%ss sleep=%ss",
        mode,
        reason,
        min_s,
        max_s,
        wait_s,
    )
    time.sleep(wait_s)

def short_json(data: Any, limit: int = 6000) -> str:
    try:
        return json.dumps(data, indent=2)[:limit]
    except Exception:
        return str(data)[:limit]

# =========================================================
# JUPITER
# =========================================================

def get_order(input_mint: str, output_mint: str, amount_raw: int, taker: str) -> dict[str, Any]:
    headers = {"x-api-key": JUPITER_API_KEY}
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount_raw),
        "taker": taker,
    }

    response = http_get_with_retry(
        f"{JUP_BASE_URL}/order",
        headers=headers,
        params=params,
        timeout=30,
    )

    if response.status_code != 200:
        raise RuntimeError(f"/order failed: {response.status_code} {response.text[:1000]}")

    data = response.json()

    if DEBUG:
        log.info("ORDER RESPONSE:\n%s", short_json(data))

    return data

def sign_order_transaction(unsigned_tx_b64: str) -> str:
    try:
        tx_bytes = base64.b64decode(unsigned_tx_b64)
        tx = VersionedTransaction.from_bytes(tx_bytes)

        message_bytes = to_bytes_versioned(tx.message)
        signature = keypair.sign_message(message_bytes)

        signed_tx = VersionedTransaction.populate(tx.message, [signature])
        return base64.b64encode(bytes(signed_tx)).decode("utf-8")
    except Exception as e:
        raise RuntimeError(f"Failed to sign transaction: {e}") from e

def execute_order(signed_tx_b64: str, request_id: str) -> dict[str, Any]:
    headers = {
        "x-api-key": JUPITER_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {
        "signedTransaction": signed_tx_b64,
        "requestId": request_id,
    }

    response = http_post_with_retry(
        f"{JUP_BASE_URL}/execute",
        headers=headers,
        json_payload=payload,
        timeout=60,
    )

    if response.status_code != 200:
        raise RuntimeError(f"/execute failed: {response.status_code} {response.text[:1000]}")

    data = response.json()

    if DEBUG:
        log.info("EXECUTE RESPONSE:\n%s", short_json(data))

    return data


def build_candidate_orders(taker: str, candidate_sizes_tokens: list[int]) -> list[dict[str, Any]]:
    """
    GET /order for each candidate size. Does not execute.
    Returns list of dicts with chunk_tokens, chunk_raw, order, metrics.
    """
    out: list[dict[str, Any]] = []
    for chunk_tokens in candidate_sizes_tokens:
        chunk_raw = tokens_to_raw(chunk_tokens, TOKEN_DECIMALS)
        try:
            order = get_order(
                input_mint=TOKEN_MINT,
                output_mint=SOL_MINT,
                amount_raw=chunk_raw,
                taker=taker,
            )
        except Exception as e:
            log.warning("Candidate %s %s: /order failed: %s", chunk_tokens, TOKEN_SYMBOL, e)
            continue

        row = evaluate_candidate(chunk_tokens, chunk_raw, order)
        out.append(row)
        if row["accepted"]:
            log.info(
                "Candidate %s %s -> out_raw=%s ratio=%.12f router=%s priceImpact=%s accepted=yes",
                row["chunk_tokens"],
                TOKEN_SYMBOL,
                row["out_amount_raw"],
                row["efficiency_ratio"],
                row["router"],
                row["price_impact_pct"],
            )
        else:
            log.warning(
                "Candidate %s %s rejected: %s | out_raw=%s ratio=%.12f router=%s priceImpact=%s",
                row["chunk_tokens"],
                TOKEN_SYMBOL,
                row["rejection_reason"],
                row["out_amount_raw"],
                row["efficiency_ratio"],
                row["router"] or "unknown",
                row["price_impact_pct"],
            )
    return out


def select_best_candidate(candidates: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Best ratio first; larger size only among near-equal ratios."""
    accepted = [c for c in candidates if c.get("accepted")]
    if not accepted:
        return None
    best_ratio = max(float(c["efficiency_ratio"]) for c in accepted)
    threshold = best_ratio * (1.0 - NEAR_EQUAL_RATIO_BAND)
    near_equal = [c for c in accepted if float(c["efficiency_ratio"]) >= threshold]
    return max(near_equal, key=lambda c: c["chunk_tokens"])


def execute_prepared_order(order: dict[str, Any]) -> dict[str, Any]:
    """Sign and POST /execute using a /order response dict (no second /order)."""
    unsigned_tx_b64 = order.get("transaction")
    request_id = order.get("requestId")

    if not unsigned_tx_b64:
        raise RuntimeError("Missing transaction in /order response")
    if not request_id:
        raise RuntimeError("Missing requestId in /order response")

    signed_tx_b64 = sign_order_transaction(unsigned_tx_b64)
    execution = execute_order(signed_tx_b64, str(request_id))

    return {
        "request_id": request_id,
        "signature": execution.get("signature"),
        "status": execution.get("status"),
        "code": execution.get("code"),
        "error": execution.get("error"),
        "router": order.get("router"),
        "mode": order.get("mode"),
        "quoted_out_amount_raw": order.get("outAmount"),
        "input_amount_result_raw": execution.get("inputAmountResult"),
        "output_amount_result_raw": execution.get("outputAmountResult"),
    }


def try_resell_reentry_lots(runtime_state: dict[str, Any], now_iso: str, now_ts: float) -> bool:
    if not ENABLE_RESELL_REENTRY_LOTS:
        return False

    open_lots = compute_open_reentry_lots(runtime_state)
    if not open_lots:
        return False

    last_resell_ts = iso_to_ts_seconds(runtime_state.get("last_resell_reentry_at"))
    if last_resell_ts is not None and (now_ts - last_resell_ts) < RESELL_REENTRY_COOLDOWN_SECONDS:
        return False

    for lot_idx, lot in open_lots:
        remaining_hachi_raw = int(lot.get("remaining_hachi_raw") or 0)
        if remaining_hachi_raw <= 0:
            continue

        remaining_hachi_tokens = remaining_hachi_raw // HACHI_RAW_PER_TOKEN
        if remaining_hachi_tokens <= 0:
            continue

        bought_hachi_raw = int(lot.get("bought_hachi_raw") or 0)
        spent_sol_raw = int(lot.get("spent_sol_raw") or 0)
        if bought_hachi_raw <= 0 or spent_sol_raw <= 0:
            log.warning("Lot %s missing basis numbers; skipping resell", lot.get("lot_id"))
            continue

        basis_sol_per_hachi_raw = spent_sol_raw / bought_hachi_raw
        min_sol_per_hachi_raw = basis_sol_per_hachi_raw * (1.0 + RESELL_MIN_PROFIT_EDGE_PCT)

        candidate_sizes_tokens = [
            c for c in BASE_CANDIDATE_CHUNK_SIZES_TOKENS if c <= remaining_hachi_tokens
        ]
        if not candidate_sizes_tokens:
            continue

        built = build_candidate_orders(wallet_pubkey, candidate_sizes_tokens)

        accepted_profitable: list[dict[str, Any]] = []
        for c in built:
            if not c.get("accepted"):
                continue
            curr_sol_per_hachi_raw = float(c.get("efficiency_ratio") or 0.0)
            edge = (curr_sol_per_hachi_raw / basis_sol_per_hachi_raw - 1.0) if basis_sol_per_hachi_raw > 0 else 0.0
            ok = curr_sol_per_hachi_raw >= min_sol_per_hachi_raw
            log.info(
                "Resell lot %s basis_sol_per_hachi=%.12f cand=%s HACHI->SOL ratio=%.12f edge=%.2f%% accepted=%s",
                lot.get("lot_id"),
                basis_sol_per_hachi_raw,
                c["chunk_tokens"],
                curr_sol_per_hachi_raw,
                edge * 100.0,
                ok,
            )
            if ok:
                accepted_profitable.append(c)

        if not accepted_profitable:
            continue

        selected = select_best_candidate(accepted_profitable)
        if selected is None:
            continue

        log.info(
            "Selected resell candidate for lot %s: %s HACHI (ratio=%.12f) basis_sol_per_hachi=%.12f",
            lot.get("lot_id"),
            selected["chunk_tokens"],
            float(selected.get("efficiency_ratio") or 0.0),
            basis_sol_per_hachi_raw,
        )

        result = execute_prepared_order(selected["order"])
        if not is_execute_success(result):
            log.warning("Resell execute failed: status=%s error=%s", result.get("status"), result.get("error"))
            if STOP_ON_ERROR:
                raise RuntimeError(f"Resell execute failure: {result.get('error') or result.get('status')}")
            return False

        actual_sold_hachi_raw = parse_int_field(result.get("input_amount_result_raw")) or int(selected.get("chunk_raw") or 0)
        actual_sol_received_raw = parse_int_field(result.get("output_amount_result_raw")) or int(selected.get("out_amount_raw") or 0)
        if actual_sold_hachi_raw <= 0 or actual_sol_received_raw <= 0:
            log.warning("Resell parse failed for lot %s; skipping ledger update", lot.get("lot_id"))
            return False

        runtime_state["cumulative_sol_received_raw"] = int(runtime_state.get("cumulative_sol_received_raw") or 0) + actual_sol_received_raw
        runtime_state["cumulative_hachi_resold_after_reentry_raw"] = int(runtime_state.get("cumulative_hachi_resold_after_reentry_raw") or 0) + actual_sold_hachi_raw
        runtime_state["cumulative_hachi_resold_after_reentry_tokens"] = int(runtime_state.get("cumulative_hachi_resold_after_reentry_tokens") or 0) + (actual_sold_hachi_raw // HACHI_RAW_PER_TOKEN)

        #region debug: resell reentry ledger update visibility
        _debug_append_ndjson(
            hypothesis_id="H4",
            location="try_resell_reentry_lots",
            message="ledger_updated_after_resell_reentry_lot",
            data={
                "lot_id": lot.get("lot_id"),
                "actual_sold_hachi_raw": actual_sold_hachi_raw,
                "actual_sol_received_raw": actual_sol_received_raw,
                "cumulative_sol_received_raw": runtime_state.get("cumulative_sol_received_raw"),
                "cumulative_hachi_resold_after_reentry_raw": runtime_state.get(
                    "cumulative_hachi_resold_after_reentry_raw"
                ),
                "free_sol_budget_raw_post": compute_free_sol_budget_raw(runtime_state),
                "free_reentry_capacity_raw_post": compute_free_reentry_hachi_capacity_raw(runtime_state),
            },
        )
        #endregion

        lot["resold_hachi_raw"] = int(lot.get("resold_hachi_raw") or 0) + actual_sold_hachi_raw
        lot["received_sol_raw_from_resell"] = int(lot.get("received_sol_raw_from_resell") or 0) + actual_sol_received_raw
        lot["remaining_hachi_raw"] = max(0, int(lot.get("remaining_hachi_raw") or 0) - actual_sold_hachi_raw)
        if int(lot["remaining_hachi_raw"]) <= 0:
            lot["status"] = "CLOSED"

        runtime_state["reentry_open_lots"][lot_idx] = lot

        append_recent_ratio(
            runtime_state,
            ratio=float(selected["efficiency_ratio"]),
            chunk_tokens=int(selected["chunk_tokens"]),
        )
        if actual_sol_received_raw > 0:
            _push_recent_float_metric(
                runtime_state,
                "recent_sell_hachi_per_sol_ratios",
                float(actual_sold_hachi_raw) / float(actual_sol_received_raw),
            )

        runtime_state["last_sell_at"] = now_iso
        runtime_state["last_resell_reentry_at"] = now_iso
        runtime_state["mode_last_action"] = "sell"
        runtime_state["last_success_at"] = now_iso
        runtime_state["consecutive_skipped_cycles"] = 0

        log.info(
            "Resell executed | lot=%s | sold_hachi_raw=%s received_sol_raw=%s status=%s sig=%s",
            lot.get("lot_id"),
            actual_sold_hachi_raw,
            actual_sol_received_raw,
            result.get("status"),
            result.get("signature"),
        )
        return True

    return False


def try_normal_sell(runtime_state: dict[str, Any], now_iso: str, now_ts: float) -> bool:
    mode = "base"
    candidate_sizes = BASE_CANDIDATE_CHUNK_SIZES_TOKENS
    runtime_state["last_cycle_spike_triggered"] = False

    built = build_candidate_orders(wallet_pubkey, candidate_sizes)
    accepted_count = sum(1 for c in built if c.get("accepted"))
    rejected_router_count = sum(
        1 for c in built if not c.get("accepted") and "router=" in str(c.get("rejection_reason", ""))
    )
    rejected_impact_count = sum(
        1 for c in built if not c.get("accepted") and "priceImpact" in str(c.get("rejection_reason", ""))
    )
    _push_recent_int_metric(runtime_state, "recent_acceptable_candidate_counts", accepted_count)
    _push_recent_int_metric(runtime_state, "recent_rejected_router_counts", rejected_router_count)
    _push_recent_int_metric(runtime_state, "recent_rejected_impact_counts", rejected_impact_count)

    liquidity_eval = evaluate_liquidity_state(built, runtime_state)
    liquidity_state = str(liquidity_eval["state"])
    runtime_state["last_liquidity_state"] = liquidity_state
    runtime_state["last_liquidity_reasons"] = list(liquidity_eval.get("reasons") or [])
    runtime_state["last_abs_price_impact_pct"] = liquidity_eval.get("abs_price_impact_pct")
    runtime_state["last_size_degradation_flag"] = bool(liquidity_eval.get("size_degradation"))
    liq_hist = runtime_state.setdefault("recent_liquidity_states", [])
    if not isinstance(liq_hist, list):
        liq_hist = []
        runtime_state["recent_liquidity_states"] = liq_hist
    liq_hist.append(liquidity_state)
    if len(liq_hist) > 20:
        del liq_hist[:-20]

    log.info(
        "Liquidity state=%s | reasons=%s | current_ratio=%s recent_best=%s recent_avg=%s abs_price_impact_pct=%s size_degradation=%s",
        liquidity_state,
        runtime_state["last_liquidity_reasons"],
        (
            f"{float(liquidity_eval['current_ratio']):.12f}"
            if liquidity_eval.get("current_ratio") is not None
            else "n/a"
        ),
        (
            f"{float(liquidity_eval['recent_best_ratio']):.12f}"
            if liquidity_eval.get("recent_best_ratio") is not None
            else "n/a"
        ),
        (
            f"{float(liquidity_eval['recent_avg_ratio']):.12f}"
            if liquidity_eval.get("recent_avg_ratio") is not None
            else "n/a"
        ),
        (
            f"{float(liquidity_eval['abs_price_impact_pct']):.4f}"
            if liquidity_eval.get("abs_price_impact_pct") is not None
            else "n/a"
        ),
        "yes" if liquidity_eval.get("size_degradation") else "no",
    )

    downshift_candidates: Optional[list[int]] = None
    if liquidity_state == "weakening":
        downshift_candidates = EXHAUSTION_DEFENSIVE_BASE_CANDIDATES
    elif liquidity_state == "weak":
        downshift_candidates = EXHAUSTION_WEAK_BASE_CANDIDATES
    elif liquidity_state == "exhausted":
        downshift_candidates = EXHAUSTION_EXHAUSTED_BASE_CANDIDATES

    if downshift_candidates is not None:
        candidate_sizes = [c for c in downshift_candidates if c in BASE_CANDIDATE_CHUNK_SIZES_TOKENS]
        if not candidate_sizes:
            candidate_sizes = BASE_CANDIDATE_CHUNK_SIZES_TOKENS
        log.info(
            "Downshifted candidate set due to liquidity state=%s: %s",
            liquidity_state,
            candidate_sizes,
        )
        built = build_candidate_orders(wallet_pubkey, candidate_sizes)

    selected = select_best_candidate(built)
    if selected is None:
        return False

    runtime_state["last_selected_ratio"] = selected["efficiency_ratio"]

    exhausted_hard_skip = False
    if liquidity_state == "exhausted":
        curr = float(selected["efficiency_ratio"])
        recent_best_ratio = liquidity_eval.get("recent_best_ratio")
        recent_avg_ratio = liquidity_eval.get("recent_avg_ratio")
        if (
            curr is not None
            and recent_best_ratio is not None
            and recent_avg_ratio is not None
            and float(curr) < float(recent_best_ratio) * (1.0 - LIQUIDITY_EXHAUSTED_RATIO_DROP)
            and float(curr) < float(recent_avg_ratio) * (1.0 - LIQUIDITY_EXHAUSTED_RATIO_DROP)
        ):
            exhausted_hard_skip = True
    if exhausted_hard_skip:
        log.info(
            "Liquidity state=exhausted | reasons=%s | skipping sell (hard exhaustion guard)",
            runtime_state["last_liquidity_reasons"],
        )
        return False

    spike_triggered, spike_reason, spike_ref = should_trigger_spike_mode(
        float(selected["efficiency_ratio"]),
        runtime_state,
    )
    if liquidity_state == "weak" and EXHAUSTION_DISABLE_SPIKE_IN_WEAK:
        spike_triggered = False
        log.info("Spike mode disabled due to liquidity state=weak")
    if liquidity_state == "exhausted" and EXHAUSTION_DISABLE_SPIKE_IN_EXHAUSTED:
        spike_triggered = False
        log.info("Spike mode disabled due to liquidity state=exhausted")

    if spike_triggered:
        mode = "spike"
        runtime_state["last_cycle_spike_triggered"] = True
        candidate_sizes = [
            c for c in SPIKE_CANDIDATE_CHUNK_SIZES_TOKENS if c <= MAX_SPIKE_CHUNK_SIZE_TOKENS
        ]
        runtime_state["last_mode"] = mode
        log.info(
            "Spike mode triggered: %s | current_ratio=%.12f ref=%s",
            spike_reason,
            float(selected["efficiency_ratio"]),
            f"{spike_ref:.12f}" if spike_ref is not None else "n/a",
        )
        log.info(
            "Mode this cycle: %s | candidate sizes: %s (testing %s /order quotes) | spike_cap=%s",
            mode,
            candidate_sizes,
            len(candidate_sizes),
            MAX_SPIKE_CHUNK_SIZE_TOKENS,
        )
        built = build_candidate_orders(wallet_pubkey, candidate_sizes)
        selected = select_best_candidate(built)
        if selected is None:
            return False

    runtime_state["last_selected_ratio"] = selected["efficiency_ratio"]
    _push_recent_float_metric(runtime_state, "recent_best_quote_ratios", float(selected["efficiency_ratio"]))

    allow_sell, strength_reason, recent_best, recent_avg, abs_pi = should_allow_normal_sell(
        float(selected["efficiency_ratio"]),
        runtime_state,
        selected.get("price_impact_pct"),
    )
    if not allow_sell:
        log.info(
            "Skip normal sell: %s | current_ratio=%.12f recent_best=%s recent_avg=%s abs_priceImpact%%=%s",
            strength_reason,
            float(selected["efficiency_ratio"]),
            f"{recent_best:.12f}" if recent_best is not None else "n/a",
            f"{recent_avg:.12f}" if recent_avg is not None else "n/a",
            f"{abs_pi:.4f}" if abs_pi is not None else "n/a",
        )
        return False

    log.info(
        "Allow normal sell: %s | current_ratio=%.12f recent_best=%s recent_avg=%s abs_priceImpact%%=%s",
        strength_reason,
        float(selected["efficiency_ratio"]),
        f"{recent_best:.12f}" if recent_best is not None else "n/a",
        f"{recent_avg:.12f}" if recent_avg is not None else "n/a",
        f"{abs_pi:.4f}" if abs_pi is not None else "n/a",
    )

    log.info(
        "Selected normal sell: %s %s (mode=%s, ratio=%.12f out_raw=%s router=%s priceImpact=%s)",
        selected["chunk_tokens"],
        TOKEN_SYMBOL,
        mode,
        float(selected["efficiency_ratio"]),
        selected["out_amount_raw"],
        selected.get("router"),
        selected.get("price_impact_pct"),
    )

    result = execute_prepared_order(selected["order"])
    if not is_execute_success(result):
        log.warning(
            "Normal sell execute failed: status=%s error=%s", result.get("status"), result.get("error")
        )
        if STOP_ON_ERROR:
            raise RuntimeError(
                f"Normal sell execute failure: {result.get('error') or result.get('status')}"
            )
        return False

    actual_sold_hachi_raw = parse_int_field(result.get("input_amount_result_raw")) or int(selected.get("chunk_raw") or 0)
    actual_sol_received_raw = parse_int_field(result.get("output_amount_result_raw")) or int(selected.get("out_amount_raw") or 0)
    if actual_sold_hachi_raw <= 0 or actual_sol_received_raw <= 0:
        log.warning("Normal sell parse failed; skipping ledger update")
        return False

    runtime_state["cumulative_hachi_sold_raw"] = int(runtime_state.get("cumulative_hachi_sold_raw") or 0) + actual_sold_hachi_raw
    runtime_state["cumulative_hachi_sold_tokens"] = int(runtime_state.get("cumulative_hachi_sold_tokens") or 0) + (actual_sold_hachi_raw // HACHI_RAW_PER_TOKEN)
    runtime_state["cumulative_sol_received_raw"] = int(runtime_state.get("cumulative_sol_received_raw") or 0) + actual_sol_received_raw

    #region debug: normal sell ledger update visibility
    _debug_append_ndjson(
        hypothesis_id="H2",
        location="try_normal_sell",
        message="ledger_updated_after_normal_sell",
        data={
            "actual_sold_hachi_raw": actual_sold_hachi_raw,
            "actual_sol_received_raw": actual_sol_received_raw,
            "cumulative_hachi_sold_raw": runtime_state.get("cumulative_hachi_sold_raw"),
            "cumulative_sol_received_raw": runtime_state.get("cumulative_sol_received_raw"),
            "free_sol_budget_raw_post": compute_free_sol_budget_raw(runtime_state),
            "free_reentry_capacity_raw_post": compute_free_reentry_hachi_capacity_raw(runtime_state),
        },
    )
    #endregion

    append_recent_ratio(
        runtime_state,
        ratio=float(selected["efficiency_ratio"]),
        chunk_tokens=int(selected["chunk_tokens"]),
    )
    if actual_sol_received_raw > 0:
        _push_recent_float_metric(
            runtime_state,
            "recent_sell_hachi_per_sol_ratios",
            float(actual_sold_hachi_raw) / float(actual_sol_received_raw),
        )

    runtime_state["last_sell_at"] = now_iso
    runtime_state["mode_last_action"] = "sell"
    runtime_state["last_success_at"] = now_iso
    runtime_state["consecutive_skipped_cycles"] = 0

    log.info(
        "Normal sell executed | sold_hachi_raw=%s received_sol_raw=%s status=%s sig=%s router=%s",
        actual_sold_hachi_raw,
        actual_sol_received_raw,
        result.get("status"),
        result.get("signature"),
        result.get("router"),
    )

    return True


def try_reentry_buyback(runtime_state: dict[str, Any], now_iso: str, now_ts: float) -> bool:
    if not ENABLE_REENTRY:
        return False

    last_sell_ts = iso_to_ts_seconds(runtime_state.get("last_sell_at"))
    if last_sell_ts is None:
        return False
    if (now_ts - last_sell_ts) < REENTRY_MIN_COOLDOWN_SECONDS:
        return False

    cumulative_sold_raw = int(runtime_state.get("cumulative_hachi_sold_raw") or 0)
    if cumulative_sold_raw <= 0:
        return False

    free_sol_budget_raw = compute_free_sol_budget_raw(runtime_state)
    if free_sol_budget_raw < REENTRY_MIN_SOL_SPEND_RAW:
        return False

    free_reentry_capacity_raw = compute_free_reentry_hachi_capacity_raw(runtime_state)
    if free_reentry_capacity_raw <= 0:
        return False

    avg_sell_hachi_per_sol = average_recent_sell_hachi_per_sol_ratio(
        runtime_state,
        STRENGTH_LOOKBACK_TRADES,
    )
    if avg_sell_hachi_per_sol is None or avg_sell_hachi_per_sol <= 0:
        return False

    cap_sol_raw = int(free_sol_budget_raw * REENTRY_MAX_FREE_SOL_USAGE_PCT)
    candidate_sol_raws: list[int] = []
    for pct in REENTRY_CANDIDATE_SOL_RAW_PCTS:
        amt = int(free_sol_budget_raw * pct)
        if amt <= 0:
            continue
        if amt > cap_sol_raw:
            continue
        if amt < REENTRY_MIN_SOL_SPEND_RAW:
            continue
        candidate_sol_raws.append(amt)

    candidate_sol_raws = sorted(set(candidate_sol_raws))
    if not candidate_sol_raws:
        return False

    accepted: list[dict[str, Any]] = []
    for sol_raw in candidate_sol_raws:
        try:
            order = get_order(
                input_mint=SOL_MINT,
                output_mint=TOKEN_MINT,
                amount_raw=sol_raw,
                taker=wallet_pubkey,
            )
        except Exception as e:
            log.warning("Buyback candidate sol_raw=%s /order failed: %s", sol_raw, e)
            continue

        c = evaluate_candidate(sol_raw, sol_raw, order)
        if not c.get("accepted"):
            log.info("Buyback candidate rejected: sol_raw=%s reason=%s", sol_raw, c.get("rejection_reason"))
            continue

        out_hachi_raw = int(c.get("out_amount_raw") or 0)
        if out_hachi_raw <= 0:
            log.info("Buyback candidate rejected: sol_raw=%s missing out_hachi_raw", sol_raw)
            continue
        if out_hachi_raw > free_reentry_capacity_raw:
            log.info(
                "Buyback candidate rejected: sol_raw=%s out_hachi_raw=%s exceeds free_capacity_raw=%s",
                sol_raw,
                out_hachi_raw,
                free_reentry_capacity_raw,
            )
            continue

        buy_ratio = float(c.get("efficiency_ratio") or 0.0)  # hachi_raw per sol_raw
        edge = (buy_ratio / avg_sell_hachi_per_sol - 1.0) if avg_sell_hachi_per_sol > 0 else 0.0
        ok = (not REENTRY_REQUIRE_PULLBACK_VS_RECENT_SELL) or (edge >= REENTRY_MIN_EDGE_PCT)
        if not ok:
            log.info(
                "Buyback candidate rejected: sol_raw=%s ratio=%.12f edge=%.2f%% < min_edge=%.2f%%",
                sol_raw,
                buy_ratio,
                edge * 100.0,
                REENTRY_MIN_EDGE_PCT * 100.0,
            )
            continue

        log.info(
            "Buyback accepted candidate sol_raw=%s out_hachi_raw=%s ratio=%.12f edge=%.2f%% router=%s priceImpact=%s",
            sol_raw,
            out_hachi_raw,
            buy_ratio,
            edge * 100.0,
            c.get("router"),
            c.get("price_impact_pct"),
        )
        accepted.append(c)

    if not accepted:
        return False

    selected = select_best_candidate(accepted)
    if selected is None:
        return False

    result = execute_prepared_order(selected["order"])
    if not is_execute_success(result):
        log.warning("Buyback execute failed: status=%s error=%s", result.get("status"), result.get("error"))
        if STOP_ON_ERROR:
            raise RuntimeError(f"Buyback execute failure: {result.get('error') or result.get('status')}")
        return False

    actual_sol_spent_raw = parse_int_field(result.get("input_amount_result_raw")) or int(selected.get("chunk_raw") or 0)
    actual_hachi_received_raw = parse_int_field(result.get("output_amount_result_raw")) or int(selected.get("out_amount_raw") or 0)
    if actual_sol_spent_raw <= 0 or actual_hachi_received_raw <= 0:
        log.warning("Buyback parse failed; skipping ledger update")
        return False

    runtime_state["cumulative_hachi_rebought_raw"] = int(runtime_state.get("cumulative_hachi_rebought_raw") or 0) + actual_hachi_received_raw
    runtime_state["cumulative_hachi_rebought_tokens"] = int(runtime_state.get("cumulative_hachi_rebought_tokens") or 0) + (actual_hachi_received_raw // HACHI_RAW_PER_TOKEN)
    runtime_state["cumulative_sol_spent_on_reentry_raw"] = int(runtime_state.get("cumulative_sol_spent_on_reentry_raw") or 0) + actual_sol_spent_raw

    #region debug: buyback ledger update visibility
    _debug_append_ndjson(
        hypothesis_id="H3",
        location="try_reentry_buyback",
        message="ledger_updated_after_buyback",
        data={
            "actual_sol_spent_raw": actual_sol_spent_raw,
            "actual_hachi_received_raw": actual_hachi_received_raw,
            "cumulative_sol_spent_on_reentry_raw": runtime_state.get("cumulative_sol_spent_on_reentry_raw"),
            "free_sol_budget_raw_post": compute_free_sol_budget_raw(runtime_state),
            "free_reentry_capacity_raw_post": compute_free_reentry_hachi_capacity_raw(runtime_state),
            "open_lots_post": len(compute_open_reentry_lots(runtime_state)),
        },
    )
    #endregion

    lot_id = f"lot_{int(runtime_state.get('next_lot_id') or 1)}"
    runtime_state["next_lot_id"] = int(runtime_state.get("next_lot_id") or 1) + 1

    buy_ratio_actual = float(actual_hachi_received_raw) / float(actual_sol_spent_raw) if actual_sol_spent_raw > 0 else 0.0
    lot = {
        "lot_id": lot_id,
        "bought_hachi_raw": actual_hachi_received_raw,
        "spent_sol_raw": actual_sol_spent_raw,
        "effective_buy_ratio_hachi_per_sol_raw": buy_ratio_actual,
        "bought_at": now_iso,
        "status": "OPEN",
        "remaining_hachi_raw": actual_hachi_received_raw,
        "resold_hachi_raw": 0,
        "received_sol_raw_from_resell": 0,
    }
    runtime_state.setdefault("reentry_open_lots", []).append(lot)

    _push_recent_float_metric(runtime_state, "recent_buy_efficiency_ratios", buy_ratio_actual)
    runtime_state["last_buy_at"] = now_iso
    runtime_state["mode_last_action"] = "buy"
    runtime_state["last_success_at"] = now_iso
    runtime_state["consecutive_skipped_cycles"] = 0

    log.info(
        "Buyback executed | lot=%s spent_sol_raw=%s received_hachi_raw=%s ratio=%.12f status=%s sig=%s",
        lot_id,
        actual_sol_spent_raw,
        actual_hachi_received_raw,
        buy_ratio_actual,
        result.get("status"),
        result.get("signature"),
    )
    return True


# =========================================================
# MAIN LOOP
# =========================================================

def run() -> int:
    runtime_state = load_runtime_state()
    live_spike_candidates = [
        c for c in SPIKE_CANDIDATE_CHUNK_SIZES_TOKENS if c <= MAX_SPIKE_CHUNK_SIZE_TOKENS
    ]
    log.info(
        "Starting HACHI round-trip engine | wallet=%s | mint=%s | decimals=%s | base_candidates=%s | spike_candidates=%s | wait=%s-%ss | max_sells=%s",
        wallet_pubkey,
        TOKEN_MINT,
        TOKEN_DECIMALS,
        BASE_CANDIDATE_CHUNK_SIZES_TOKENS,
        SPIKE_CANDIDATE_CHUNK_SIZES_TOKENS,
        MIN_WAIT_SECONDS,
        MAX_WAIT_SECONDS,
        MAX_SELLS if MAX_SELLS is not None else "unlimited",
    )
    log.info("Startup candidate envelope | base_sell_sizes=%s", BASE_CANDIDATE_CHUNK_SIZES_TOKENS)
    log.info(
        "Startup candidate envelope | spike_sell_sizes_live=%s (configured=%s cap=%s)",
        live_spike_candidates,
        SPIKE_CANDIDATE_CHUNK_SIZES_TOKENS,
        MAX_SPIKE_CHUNK_SIZE_TOKENS,
    )
    log.info(
        "Startup candidate envelope | reentry_sol_usage_pcts=%s (max_free_sol_usage_pct=%.2f)",
        REENTRY_CANDIDATE_SOL_RAW_PCTS,
        REENTRY_MAX_FREE_SOL_USAGE_PCT,
    )

    sells_done = 0

    while True:
        if MAX_SELLS is not None and sells_done >= MAX_SELLS:
            log.info("Reached MAX_SELLS=%s, stopping", MAX_SELLS)
            break

        now_iso = utc_now_iso()
        now_ts = time.time()

        open_lots = compute_open_reentry_lots(runtime_state)
        open_lots_count = len(open_lots)
        free_sol_budget_raw = compute_free_sol_budget_raw(runtime_state)
        free_reentry_capacity_raw = compute_free_reentry_hachi_capacity_raw(runtime_state)
        recent_avg_sol_per_hachi = average_recent_ratio(runtime_state, STRENGTH_LOOKBACK_TRADES)
        recent_avg_hachi_per_sol = average_recent_sell_hachi_per_sol_ratio(runtime_state, STRENGTH_LOOKBACK_TRADES)

        action = "skip"
        try:
            log.info(
                "Cycle status | free_sol_raw=%s free_sol=%.6f | free_reentry_capacity_hachi_raw=%s open_lots=%s | avg_sell_sol_per_hachi=%s avg_sell_hachi_per_sol=%s",
                free_sol_budget_raw,
                float(free_sol_budget_raw) / SOL_RAW_PER_SOL,
                free_reentry_capacity_raw,
                open_lots_count,
                f"{recent_avg_sol_per_hachi:.12f}" if recent_avg_sol_per_hachi is not None else "n/a",
                f"{recent_avg_hachi_per_sol:.12f}" if recent_avg_hachi_per_sol is not None else "n/a",
            )

            # 1) Resell rebought inventory (OPEN lots)
            if open_lots_count > 0 and try_resell_reentry_lots(runtime_state, now_iso, now_ts):
                sells_done += 1
                action = "resell_reentry"
            else:
                # 2) Normal sell (base inventory)
                if try_normal_sell(runtime_state, now_iso, now_ts):
                    sells_done += 1
                    action = "normal_sell"
                else:
                    # 3) Buyback (reentry)
                    if try_reentry_buyback(runtime_state, now_iso, now_ts):
                        action = "buyback"

        except KeyboardInterrupt:
            log.warning("Interrupted by user")
            break
        except Exception as e:
            log.exception("Cycle error: %s", e)
            if STOP_ON_ERROR:
                break

        #region debug: action chosen log should reflect post-action ledger
        open_lots_post = compute_open_reentry_lots(runtime_state)
        open_lots_count_post = len(open_lots_post)
        free_sol_budget_raw_post = compute_free_sol_budget_raw(runtime_state)
        free_reentry_capacity_raw_post = compute_free_reentry_hachi_capacity_raw(runtime_state)
        _debug_append_ndjson(
            hypothesis_id="H1",
            location="run",
            message="action_chosen_post_action_state",
            data={
                "action": action,
                "open_lots_pre": open_lots_count,
                "open_lots_post": open_lots_count_post,
                "free_sol_budget_raw_pre": free_sol_budget_raw,
                "free_sol_budget_raw_post": free_sol_budget_raw_post,
                "free_reentry_capacity_raw_pre": free_reentry_capacity_raw,
                "free_reentry_capacity_raw_post": free_reentry_capacity_raw_post,
            },
        )
        #endregion

        log.info(
            "Action chosen: %s (post) | open_lots=%s | free_sol_budget_raw=%s | free_reentry_capacity_raw=%s",
            action,
            open_lots_count_post,
            free_sol_budget_raw_post,
            free_reentry_capacity_raw_post,
        )

        log.info(
            "Post-action state | free_sol_budget_raw=%s | free_reentry_capacity_raw=%s | open_lots=%s",
            free_sol_budget_raw_post,
            free_reentry_capacity_raw_post,
            open_lots_count_post,
        )

        if action == "skip":
            runtime_state["consecutive_skipped_cycles"] = int(runtime_state.get("consecutive_skipped_cycles", 0)) + 1
            runtime_state["mode_last_action"] = "skip"
            if ENABLE_REENTRY:
                last_sell_ts = iso_to_ts_seconds(runtime_state.get("last_sell_at"))
                free_sol_budget_raw2 = compute_free_sol_budget_raw(runtime_state)
                free_reentry_capacity_raw2 = compute_free_reentry_hachi_capacity_raw(runtime_state)
                avg_sell_ratio2 = average_recent_sell_hachi_per_sol_ratio(runtime_state, STRENGTH_LOOKBACK_TRADES)
                if last_sell_ts is None:
                    log.info("Skip reason: buyback last_sell_at missing")
                elif (now_ts - last_sell_ts) < REENTRY_MIN_COOLDOWN_SECONDS:
                    log.info(
                        "Skip reason: buyback cooldown active (%.0fs < %ss)",
                        now_ts - last_sell_ts,
                        REENTRY_MIN_COOLDOWN_SECONDS,
                    )
                elif free_sol_budget_raw2 < REENTRY_MIN_SOL_SPEND_RAW:
                    log.info(
                        "Skip reason: buyback insufficient free SOL (free=%s < min=%s)",
                        free_sol_budget_raw2,
                        REENTRY_MIN_SOL_SPEND_RAW,
                    )
                elif free_reentry_capacity_raw2 <= 0:
                    log.info(
                        "Skip reason: buyback capacity exhausted (free_capacity_raw=%s)",
                        free_reentry_capacity_raw2,
                    )
                elif avg_sell_ratio2 is None:
                    log.info("Skip reason: buyback missing sell-ratio history")
                else:
                    log.info("Skip reason: no acceptable buyback quote passed filters/edge check")

        wait_mode, wait_reason = choose_wait_mode(runtime_state)
        wait_mode_final, wait_mode_override_reason = apply_liquidity_wait_override(wait_mode, runtime_state)
        if wait_mode_final != wait_mode:
            wait_reason = f"{wait_reason}; {wait_mode_override_reason}"
        else:
            wait_reason = f"{wait_reason}; {wait_mode_override_reason}"
        save_runtime_state(runtime_state)
        random_sleep_for_mode(wait_mode_final, wait_reason)

    log.info("Finished after %s sells (includes reentry lot resells)", sells_done)
    return 0

if __name__ == "__main__":
    raise SystemExit(run())
