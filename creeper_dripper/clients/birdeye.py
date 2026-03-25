from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from creeper_dripper.clients.birdeye_audit import BirdeyeAuditSession, extract_mint_from_params, sanitize_birdeye_params
from creeper_dripper.errors import BIRDEYE_EXIT_LIQUIDITY_UNSUPPORTED_CHAIN
from creeper_dripper.models import TokenCandidate

LOGGER = logging.getLogger(__name__)

BASE_URL = "https://public-api.birdeye.so"


class _BirdeyeNonRetryableError(RuntimeError):
    """Internal: do not retry this Birdeye failure."""


class BirdeyeClient:
    def __init__(
        self,
        api_key: str,
        chain: str = "solana",
        min_interval_s: float = 0.35,
        *,
        audit_jsonl_path: Path | str | None = None,
    ) -> None:
        self._chain = str(chain or "").strip().lower()
        self._session = requests.Session()
        self._session.headers.update({
            "Accept": "application/json",
            "X-API-KEY": api_key,
            "x-chain": chain,
        })
        self._last_request = 0.0
        self._min_interval_s = min_interval_s
        self._audit_jsonl_path = Path(audit_jsonl_path) if audit_jsonl_path else None
        self._audit_phase = "default"
        self._audit_session = BirdeyeAuditSession()

    def audit_reset(self) -> None:
        """Clear in-memory audit counters (does not delete jsonl file)."""
        self._audit_session = BirdeyeAuditSession()

    def audit_set_phase(self, phase: str) -> None:
        """Phase tag for jsonl lines and discovery-only aggregates (e.g. doctor, discovery, credits_meter)."""
        self._audit_phase = str(phase or "default")

    def audit_snapshot(self) -> BirdeyeAuditSession:
        return self._audit_session

    def _audit_append_jsonl(
        self,
        *,
        method: str,
        path: str,
        params: dict[str, Any] | None,
        status_code: int,
        body_snippet: str,
        attempt: int,
    ) -> None:
        if not self._audit_jsonl_path:
            return
        mint = extract_mint_from_params(params)
        sanitized = sanitize_birdeye_params(params)
        line: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "phase": self._audit_phase,
            "method": method,
            "path": path,
            "http_status": status_code,
            "mint": mint,
            "params_sanitized": sanitized,
            "response_body_snippet": body_snippet if status_code != 200 else "",
            "attempt": attempt,
        }
        if path == "/utils/v1/credits":
            line["kind"] = "credits_meter"
        try:
            self._audit_jsonl_path.parent.mkdir(parents=True, exist_ok=True)
            with self._audit_jsonl_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(line, default=str) + "\n")
        except OSError as exc:
            LOGGER.warning("birdeye_audit_jsonl_write_failed: %s", exc)

        self._audit_session.record(
            path,
            status_code,
            phase=self._audit_phase,
            body_snippet=body_snippet,
            mint=mint,
        )

    def _throttle(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_request
        if elapsed < self._min_interval_s:
            time.sleep(self._min_interval_s - elapsed)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        audit: bool = True,
    ) -> dict[str, Any]:
        url = f"{BASE_URL}{path}"
        max_attempts = 3
        backoff = 0.4
        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                self._throttle()
                response = self._session.request(method, url, params=params, timeout=20)
                self._last_request = time.monotonic()
                status = response.status_code
                body_snippet = ""
                if status != 200:
                    try:
                        body_snippet = (response.text or "")[:300]
                    except Exception:
                        body_snippet = "<unavailable>"
                if self._audit_jsonl_path and audit:
                    self._audit_append_jsonl(
                        method=method,
                        path=path,
                        params=params,
                        status_code=status,
                        body_snippet=body_snippet,
                        attempt=attempt,
                    )

                if status == 401:
                    raise RuntimeError(f"birdeye_unauthorized:{path}")
                if status == 429:
                    raise RuntimeError(f"birdeye_rate_limited:{path}")
                if status == 400:
                    body = ""
                    try:
                        body = response.text
                    except Exception:
                        body = "<unavailable>"
                    lowered = str(body or "").lower()
                    if "chain solana not supported" in lowered or "chain not supported" in lowered:
                        raise _BirdeyeNonRetryableError(
                            f"birdeye_bad_request_non_retryable path={path} params={params} body={body}"
                        )
                    raise RuntimeError(f"birdeye_bad_request path={path} params={params} body={body}")
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise RuntimeError(f"birdeye_malformed_payload:{path}")
                if payload.get("success") is False:
                    raise RuntimeError(f"Birdeye request failed for {path}: {payload}")
                return payload
            except requests.exceptions.Timeout as exc:
                last_exc = exc
            except (requests.exceptions.RequestException, ValueError, RuntimeError) as exc:
                last_exc = exc
                if "birdeye_unauthorized" in str(exc):
                    raise
                if isinstance(exc, _BirdeyeNonRetryableError):
                    raise
            if attempt < max_attempts:
                time.sleep(backoff * attempt)
        raise RuntimeError(f"Birdeye request failed after retries for {path}: {last_exc}")

    def credits_usage(self) -> dict[str, Any]:
        """GET /utils/v1/credits — current-cycle API credit usage (diagnostic / metering)."""
        return self._request("GET", "/utils/v1/credits", params=None)

    @staticmethod
    def _data(payload: dict[str, Any]) -> Any:
        return payload.get("data", payload)

    def trending_tokens(self, *, limit: int = 25, sort_by: str = "rank") -> list[dict[str, Any]]:
        effective_limit = min(limit, 20)
        payload = self._request(
            "GET",
            "/defi/token_trending",
            params={"sort_by": sort_by, "sort_type": "asc", "offset": 0, "limit": effective_limit},
        )
        data = self._data(payload)
        if isinstance(data, dict):
            items = data.get("tokens") or data.get("items") or data.get("list") or []
        else:
            items = data or []
        return list(items)

    def new_listings(self, *, limit: int = 10) -> list[dict[str, Any]]:
        payload = self._request("GET", "/defi/v2/tokens/new_listing", params={"limit": limit, "offset": 0})
        data = self._data(payload)
        items = data.get("items") if isinstance(data, dict) else data
        return list(items or [])

    def wallet_token_list(self, wallet: str, *, ui_amount_mode: str = "scaled") -> dict[str, Any]:
        """
        Wallet portfolio snapshot (visibility only; NOT execution truth).

        Birdeye docs (deprecated endpoint): GET /v1/wallet/token_list?wallet=<addr>&ui_amount_mode=scaled|raw
        """
        wallet = str(wallet or "").strip()
        if not wallet:
            raise ValueError("wallet is required")
        payload = self._request(
            "GET",
            "/v1/wallet/token_list",
            params={"wallet": wallet, "ui_amount_mode": ui_amount_mode},
        )
        data = self._data(payload)
        return data if isinstance(data, dict) else {}

    def token_overview(self, address: str) -> dict[str, Any]:
        payload = self._request("GET", "/defi/token_overview", params={"address": address})
        data = self._data(payload)
        return data if isinstance(data, dict) else {}

    def token_security(self, address: str) -> dict[str, Any]:
        payload = self._request("GET", "/defi/token_security", params={"address": address})
        data = self._data(payload)
        return data if isinstance(data, dict) else {}

    def token_holders(self, address: str) -> dict[str, Any]:
        payload = self._request("GET", "/defi/v3/token/holder", params={"address": address, "limit": 10, "offset": 0})
        data = self._data(payload)
        return data if isinstance(data, dict) else {}

    def token_exit_liquidity(self, address: str) -> dict[str, Any]:
        payload = self._request("GET", "/defi/v3/token/exit-liquidity", params={"address": address})
        data = self._data(payload)
        return data if isinstance(data, dict) else {}

    def token_creation_info(self, address: str) -> dict[str, Any]:
        payload = self._request("GET", "/defi/token_creation_info", params={"address": address})
        data = self._data(payload)
        return data if isinstance(data, dict) else {}

    def build_candidate_light(self, seed: dict[str, Any]) -> TokenCandidate:
        address = str(seed.get("address") or seed.get("token_address") or seed.get("mint") or "").strip()
        symbol = str(seed.get("symbol") or seed.get("baseSymbol") or "?").strip() or "?"
        overview = self.token_overview(address)
        # Phase 1 (cheap): overview-only build to enable early filtering.
        exit_liquidity: dict[str, Any] = {}
        # Proven by direct probe: `/defi/v3/token/exit-liquidity` returns 400 "Chain solana not supported"
        # on Solana for our integration. Skip entirely to avoid CU waste and retry amplification.
        chain = self._chain
        exit_liquidity_available = True
        birdeye_exit_liquidity_supported = True
        exit_liquidity_reason = None
        if chain == "solana":
            exit_liquidity_available = False
            birdeye_exit_liquidity_supported = False
            exit_liquidity_reason = "birdeye_exit_liquidity_skipped_unsupported_chain"
        # Non-solana chains may choose to consult exit-liquidity in heavy enrichment if needed.
        age_hours, age_source, created_at_raw = None, None, None

        candidate = TokenCandidate(
            address=address,
            symbol=symbol,
            name=(overview.get("name") or seed.get("name") or symbol),
            decimals=_intish(overview.get("decimals") or seed.get("decimals")),
            price_usd=_floatish(overview.get("price") or overview.get("priceUsd") or seed.get("price")),
            liquidity_usd=_floatish(overview.get("liquidity") or overview.get("liquidityUsd") or seed.get("liquidity")),
            exit_liquidity_usd=_extract_exit_liquidity(exit_liquidity),
            exit_liquidity_available=exit_liquidity_available,
            exit_liquidity_reason=exit_liquidity_reason,
            birdeye_exit_liquidity_supported=birdeye_exit_liquidity_supported,
            volume_24h_usd=_floatish(overview.get("v24hUSD") or overview.get("volume24hUSD") or seed.get("volume24hUSD") or seed.get("volume24h")),
            volume_1h_usd=_floatish(_nested(overview, ["volume", "h1", "usd"]) or overview.get("v1hUSD") or seed.get("volume1hUSD")),
            change_1h_pct=_floatish(overview.get("priceChange1hPercent") or _nested(overview, ["priceChange", "h1"])),
            change_24h_pct=_floatish(overview.get("priceChange24hPercent") or _nested(overview, ["priceChange", "h24"])),
            buy_1h=_intish(overview.get("buy1h") or _nested(overview, ["trade", "buy1h"])),
            sell_1h=_intish(overview.get("sell1h") or _nested(overview, ["trade", "sell1h"])),
            holder_count=_intish(overview.get("holder") or overview.get("holders")),
            top10_holder_percent=None,
            age_hours=age_hours,
            age_source=age_source,
            created_at_raw=created_at_raw,
            security_mint_mutable=None,
            security_freezable=None,
            raw={
                "seed": seed,
                "overview": overview,
                "exit_liquidity": exit_liquidity,
            },
        )
        if candidate.buy_1h is not None and candidate.sell_1h is not None:
            denom = max(candidate.sell_1h, 1)
            candidate.buy_sell_ratio_1h = candidate.buy_1h / denom
        return candidate

    def enrich_candidate_heavy(self, candidate: TokenCandidate) -> TokenCandidate:
        """
        Phase 2 (expensive): enrich an already-built candidate with heavy endpoints.
        Only call this after cheap prefilter to reduce CU burn.
        """
        address = str(candidate.address or "").strip()
        if not address:
            return candidate

        security = self.token_security(address)
        holders = self.token_holders(address)
        creation = self.token_creation_info(address)
        age_hours, age_source, created_at_raw = _extract_age_info(creation)

        candidate.security_mint_mutable = _boolish(
            security.get("is_mintable") or security.get("mintAuthorityEnabled") or security.get("mutableMetadata")
        )
        candidate.security_freezable = _boolish(security.get("is_freezable") or security.get("freezeAuthorityEnabled"))
        # Prefer holders endpoint for these fields (overview often has only total holders).
        candidate.holder_count = _intish(candidate.holder_count or holders.get("total") or holders.get("holder"))
        candidate.top10_holder_percent = _extract_top10_holder_percent(holders)
        candidate.age_hours = age_hours
        candidate.age_source = age_source
        candidate.created_at_raw = created_at_raw

        candidate.raw = {
            **(candidate.raw or {}),
            "security": security,
            "holders": holders,
            "creation": creation,
        }
        return candidate

    def build_candidate(self, seed: dict[str, Any]) -> TokenCandidate:
        """Compatibility: full build (light + heavy enrichment). Prefer two-phase pipeline in discovery."""
        c = self.build_candidate_light(seed)
        return self.enrich_candidate_heavy(c)


def _nested(obj: dict[str, Any], path: list[str]) -> Any:
    cur: Any = obj
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _floatish(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _intish(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _boolish(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if s in {"1", "true", "yes", "on"}:
        return True
    if s in {"0", "false", "no", "off"}:
        return False
    return None


def _extract_exit_liquidity(payload: dict[str, Any]) -> float | None:
    candidates = [
        payload.get("exitLiquidityUSD"),
        payload.get("liquidityUsd"),
        payload.get("liquidity"),
        _nested(payload, ["data", "liquidityUsd"]),
    ]
    for item in candidates:
        val = _floatish(item)
        if val is not None:
            return val
    return None


def _extract_top10_holder_percent(payload: dict[str, Any]) -> float | None:
    if not isinstance(payload, dict):
        return None
    items = payload.get("items") or payload.get("holders") or payload.get("list") or []
    total = 0.0
    count = 0
    for item in items:
        if count >= 10:
            break
        pct = _floatish(item.get("ui_amount_percent") or item.get("percentage") or item.get("percent"))
        if pct is None:
            continue
        total += pct
        count += 1
    return total if count else None


def _extract_age_info(payload: dict[str, Any]) -> tuple[float | None, str | None, str | None]:
    for key in ("blockUnixTime", "created_time", "createdAt", "created_at"):
        ts = payload.get(key)
        if ts is None:
            continue
        raw = str(ts)
        try:
            unix_ts = float(ts)
        except (TypeError, ValueError):
            return None, key, raw
        return max(0.0, (time.time() - unix_ts) / 3600.0), key, raw
    return None, None, None
