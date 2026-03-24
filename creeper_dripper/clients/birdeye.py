from __future__ import annotations

import logging
import time
from typing import Any

import requests

from creeper_dripper.models import TokenCandidate

LOGGER = logging.getLogger(__name__)

BASE_URL = "https://public-api.birdeye.so"


class BirdeyeClient:
    def __init__(self, api_key: str, chain: str = "solana", min_interval_s: float = 0.35) -> None:
        self._session = requests.Session()
        self._session.headers.update({
            "Accept": "application/json",
            "X-API-KEY": api_key,
            "x-chain": chain,
        })
        self._last_request = 0.0
        self._min_interval_s = min_interval_s

    def _throttle(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_request
        if elapsed < self._min_interval_s:
            time.sleep(self._min_interval_s - elapsed)

    def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{BASE_URL}{path}"
        max_attempts = 3
        backoff = 0.4
        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                self._throttle()
                response = self._session.request(method, url, params=params, timeout=20)
                self._last_request = time.monotonic()
                if response.status_code == 401:
                    raise RuntimeError(f"birdeye_unauthorized:{path}")
                if response.status_code == 429:
                    raise RuntimeError(f"birdeye_rate_limited:{path}")
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
            if attempt < max_attempts:
                time.sleep(backoff * attempt)
        raise RuntimeError(f"Birdeye request failed after retries for {path}: {last_exc}")

    @staticmethod
    def _data(payload: dict[str, Any]) -> Any:
        return payload.get("data", payload)

    def trending_tokens(self, *, limit: int = 25, sort_by: str = "rank") -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            "/defi/token_trending",
            params={"sort_by": sort_by, "sort_type": "asc", "offset": 0, "limit": limit, "interval": "24h"},
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

    def build_candidate(self, seed: dict[str, Any]) -> TokenCandidate:
        address = str(seed.get("address") or seed.get("token_address") or seed.get("mint") or "").strip()
        symbol = str(seed.get("symbol") or seed.get("baseSymbol") or "?").strip() or "?"
        overview = self.token_overview(address)
        security = self.token_security(address)
        holders = self.token_holders(address)
        exit_liquidity = self.token_exit_liquidity(address)
        creation = self.token_creation_info(address)

        candidate = TokenCandidate(
            address=address,
            symbol=symbol,
            name=(overview.get("name") or seed.get("name") or symbol),
            decimals=_intish(overview.get("decimals") or seed.get("decimals")),
            price_usd=_floatish(overview.get("price") or overview.get("priceUsd") or seed.get("price")),
            liquidity_usd=_floatish(overview.get("liquidity") or overview.get("liquidityUsd") or seed.get("liquidity")),
            exit_liquidity_usd=_extract_exit_liquidity(exit_liquidity),
            volume_24h_usd=_floatish(overview.get("v24hUSD") or overview.get("volume24hUSD") or seed.get("volume24hUSD") or seed.get("volume24h")),
            volume_1h_usd=_floatish(_nested(overview, ["volume", "h1", "usd"]) or overview.get("v1hUSD") or seed.get("volume1hUSD")),
            change_1h_pct=_floatish(overview.get("priceChange1hPercent") or _nested(overview, ["priceChange", "h1"])),
            change_24h_pct=_floatish(overview.get("priceChange24hPercent") or _nested(overview, ["priceChange", "h24"])),
            buy_1h=_intish(overview.get("buy1h") or _nested(overview, ["trade", "buy1h"])),
            sell_1h=_intish(overview.get("sell1h") or _nested(overview, ["trade", "sell1h"])),
            holder_count=_intish(overview.get("holder") or overview.get("holders") or holders.get("total")),
            top10_holder_percent=_extract_top10_holder_percent(holders),
            age_hours=_extract_age_hours(creation),
            security_mint_mutable=_boolish(security.get("is_mintable") or security.get("mintAuthorityEnabled") or security.get("mutableMetadata")),
            security_freezable=_boolish(security.get("is_freezable") or security.get("freezeAuthorityEnabled")),
            raw={
                "seed": seed,
                "overview": overview,
                "security": security,
                "holders": holders,
                "exit_liquidity": exit_liquidity,
                "creation": creation,
            },
        )
        if candidate.buy_1h is not None and candidate.sell_1h is not None:
            denom = max(candidate.sell_1h, 1)
            candidate.buy_sell_ratio_1h = candidate.buy_1h / denom
        return candidate


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


def _extract_age_hours(payload: dict[str, Any]) -> float | None:
    ts = (
        payload.get("blockUnixTime")
        or payload.get("created_time")
        or payload.get("createdAt")
        or payload.get("created_at")
    )
    if ts is None:
        return None
    try:
        unix_ts = float(ts)
    except (TypeError, ValueError):
        return None
    return max(0.0, (time.time() - unix_ts) / 3600.0)
