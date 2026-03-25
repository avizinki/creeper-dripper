from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Known discovery / wallet paths for reporting (not exhaustive).
SPECIAL_ENDPOINT_PATHS = (
    "/defi/token_overview",
    "/defi/token_creation_info",
    "/defi/token_security",
    "/defi/v3/token/holder",
    "/defi/token_trending",
    "/defi/v2/tokens/new_listing",
    "/v1/wallet/token_list",
    "/defi/v3/token/exit-liquidity",
)


def sanitize_birdeye_params(params: dict[str, Any] | None) -> dict[str, Any]:
    """Redact long sensitive-looking strings; keep mint/address shape for diagnosis."""
    if not params:
        return {}
    out: dict[str, Any] = {}
    for k, v in params.items():
        key = str(k)
        if isinstance(v, str) and key in {"wallet"} and len(v) > 16:
            out[key] = f"{v[:8]}…{v[-8:]}"
        elif isinstance(v, str) and len(v) > 88:
            out[key] = v[:88] + "…"
        else:
            out[key] = v
    return out


def extract_mint_from_params(params: dict[str, Any] | None) -> str | None:
    if not params:
        return None
    for key in ("address", "mint", "token_address"):
        v = params.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    w = params.get("wallet")
    if isinstance(w, str) and w.strip():
        return w.strip()
    return None


def classify_waste_hint(body_snippet: str) -> str | None:
    """Best-effort label from error text (diagnostic only)."""
    if not body_snippet:
        return None
    s = body_snippet.lower()
    if "not supported" in s and "chain" in s:
        return "unsupported_chain"
    if "invalid" in s and ("address" in s or "mint" in s or "param" in s):
        return "malformed_address_or_param"
    if "missing" in s or "required" in s:
        return "missing_param"
    if "bad request" in s:
        return "bad_request_generic"
    return "other_or_unknown"


def extract_credits_api_usage(payload: dict[str, Any] | None) -> int | None:
    if not payload or not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return None
    api = usage.get("api")
    if api is None:
        return None
    try:
        return int(api)
    except (TypeError, ValueError):
        return None


def extract_credits_total_usage(payload: dict[str, Any] | None) -> int | None:
    if not payload or not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return None
    total = usage.get("total")
    if total is None:
        return None
    try:
        return int(total)
    except (TypeError, ValueError):
        return None


@dataclass
class EndpointStats:
    total: int = 0
    count_200: int = 0
    count_400: int = 0
    count_other_non200: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "total": self.total,
            "200": self.count_200,
            "400": self.count_400,
            "other_non200": self.count_other_non200,
        }


@dataclass
class BirdeyeAuditSession:
    """In-memory aggregates for one BirdeyeClient audit run."""

    endpoints: dict[str, EndpointStats] = field(default_factory=dict)
    endpoints_discovery: dict[str, EndpointStats] = field(default_factory=dict)
    mints_400: dict[str, int] = field(default_factory=dict)
    error_samples: dict[str, list[str]] = field(default_factory=dict)

    def _bucket(self, d: dict[str, EndpointStats], path: str) -> EndpointStats:
        if path not in d:
            d[path] = EndpointStats()
        return d[path]

    def record(
        self,
        path: str,
        status_code: int,
        *,
        phase: str,
        body_snippet: str,
        mint: str | None,
    ) -> None:
        ep = self._bucket(self.endpoints, path)
        ep.total += 1
        if status_code == 200:
            ep.count_200 += 1
        elif status_code == 400:
            ep.count_400 += 1
        else:
            ep.count_other_non200 += 1

        if phase == "discovery":
            ed = self._bucket(self.endpoints_discovery, path)
            ed.total += 1
            if status_code == 200:
                ed.count_200 += 1
            elif status_code == 400:
                ed.count_400 += 1
            else:
                ed.count_other_non200 += 1

        if status_code == 400 and mint:
            self.mints_400[mint] = self.mints_400.get(mint, 0) + 1

        if status_code != 200 and body_snippet:
            samples = self.error_samples.setdefault(path, [])
            if len(samples) < 3 and body_snippet not in samples:
                samples.append(body_snippet[:300])

    def top_mints_400(self, n: int = 10) -> list[dict[str, Any]]:
        ranked = sorted(self.mints_400.items(), key=lambda x: (-x[1], x[0]))[:n]
        return [{"mint": m, "count_400": c} for m, c in ranked]


def build_birdeye_audit_summary_dict(
    session: BirdeyeAuditSession,
    *,
    credits_before: dict[str, Any] | None,
    credits_after: dict[str, Any] | None,
    discovery_summary: dict[str, Any],
    doctor_ok: bool,
) -> dict[str, Any]:
    """Assemble runtime/birdeye_audit_summary.json payload (diagnostic only)."""
    api_before = extract_credits_api_usage(credits_before)
    api_after = extract_credits_api_usage(credits_after)
    total_before = extract_credits_total_usage(credits_before)
    total_after = extract_credits_total_usage(credits_after)

    delta_api: int | None = None
    if api_before is not None and api_after is not None:
        delta_api = api_after - api_before

    delta_total: int | None = None
    if total_before is not None and total_after is not None:
        delta_total = total_after - total_before

    seeds = int(discovery_summary.get("seeds_total") or 0)
    accepted = int(discovery_summary.get("candidates_accepted") or 0)
    builds = int(discovery_summary.get("birdeye_candidate_build_calls") or 0)

    def _est(delta: int | None, denom: int) -> float | None:
        if delta is None or denom <= 0:
            return None
        return round(float(delta) / float(denom), 6)

    endpoint_rows: dict[str, dict[str, int]] = {
        p: s.to_dict() for p, s in sorted(session.endpoints.items())
    }
    endpoint_discovery_rows: dict[str, dict[str, int]] = {
        p: s.to_dict() for p, s in sorted(session.endpoints_discovery.items())
    }

    special: dict[str, Any] = {}
    for p in SPECIAL_ENDPOINT_PATHS:
        st = session.endpoints.get(p)
        special[p] = {
            "called": st is not None and st.total > 0,
            "total_calls": st.total if st else 0,
            "stats": st.to_dict() if st else None,
        }

    waste_hints: dict[str, int] = {}
    for path, snippets in session.error_samples.items():
        for snip in snippets:
            hint = classify_waste_hint(snip)
            if hint:
                waste_hints[hint] = waste_hints.get(hint, 0) + 1

    # Priority: endpoints with most 400s (discovery phase if any, else overall).
    disc = session.endpoints_discovery
    base = disc if len(disc) > 0 else session.endpoints
    by_400 = sorted(
        ((p, s.count_400) for p, s in base.items() if s.count_400 > 0),
        key=lambda x: (-x[1], x[0]),
    )
    disable_first: list[str] = []
    used_discovery = len(session.endpoints_discovery) > 0
    for path, n400 in by_400[:5]:
        if n400 > 0:
            phase_note = "discovery phase" if used_discovery else "all phases in window"
            disable_first.append(f"{path} ({n400} HTTP 400, {phase_note})")

    analysis_400 = (
        "Waste hints aggregate from non-200 body snippets (best-effort): "
        f"{waste_hints or 'none'}. "
        "Unsupported chain messages often come from exit-liquidity on some tiers; "
        "missing/invalid param 400s point to caller bugs or bad seed addresses."
    )

    return {
        "diagnostic": True,
        "doctor_ok": doctor_ok,
        "credits": {
            "usage_api_before": api_before,
            "usage_api_after": api_after,
            "usage_total_before": total_before,
            "usage_total_after": total_after,
            "delta_usage_api": delta_api,
            "delta_usage_total": delta_total,
            "note": "delta_usage_api is data.usage.api after minus before (cumulative account meter). "
            "It approximates consumption between the two reads: one discovery pass (trending, new_listings, "
            "build_candidate chain) plus the trailing GET /utils/v1/credits call; the opening meter read runs "
            "after doctor and is the baseline. Doctor-phase Birdeye calls are not between the two reads.",
        },
        "estimates_per_discovery_cycle": {
            "estimated_cu_api_per_seed": _est(delta_api, seeds),
            "estimated_cu_api_per_accepted_token": _est(delta_api, accepted),
            "estimated_cu_api_per_birdeye_candidate_build": _est(delta_api, builds),
            "seeds_total": seeds,
            "candidates_accepted": accepted,
            "birdeye_candidate_build_calls": builds,
        },
        "endpoints": endpoint_rows,
        "endpoints_discovery_only": endpoint_discovery_rows,
        "top_10_mints_causing_400": session.top_mints_400(10),
        "error_body_samples_by_endpoint": {k: v for k, v in sorted(session.error_samples.items())},
        "special_endpoint_checks": special,
        "likely_waste_categories": waste_hints,
        "analysis_400_causes": analysis_400,
        "disable_or_fix_first": disable_first,
        "discovery_summary": discovery_summary,
    }
