from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Generic, TypeVar

LOGGER = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass(slots=True)
class CacheStats:
    hits: int = 0
    misses: int = 0


class TTLCache(Generic[T]):
    def __init__(self, ttl_seconds: int) -> None:
        self.ttl_seconds = max(1, int(ttl_seconds))
        self._store: dict[str, tuple[float, T]] = {}
        self.stats = CacheStats()
        self._trace_enabled = False
        self._trace_limit = 0
        self._trace_cycle = ""
        self._trace_ops: list[dict] = []
        self._trace_seen_keys: set[str] = set()

    def start_trace(self, *, cycle_label: str, max_keys: int = 3) -> None:
        self._trace_enabled = True
        self._trace_limit = max(1, int(max_keys))
        self._trace_cycle = cycle_label
        self._trace_ops = []
        self._trace_seen_keys = set()

    def consume_trace(self) -> list[dict]:
        out = list(self._trace_ops)
        self._trace_enabled = False
        self._trace_cycle = ""
        self._trace_ops = []
        self._trace_seen_keys = set()
        return out

    def get(self, key: str) -> T | None:
        lookup_ts = time.monotonic()
        item = self._store.get(key)
        if item is None:
            self.stats.misses += 1
            LOGGER.debug("cache miss key=%s", key)
            self._trace(
                {
                    "op": "get",
                    "key": key,
                    "result": "miss",
                    "lookup_ts": lookup_ts,
                    "inserted_ts": None,
                    "expires_ts": None,
                }
            )
            return None
        ts, val = item
        expires_ts = ts + self.ttl_seconds
        if (lookup_ts - ts) > self.ttl_seconds:
            self._store.pop(key, None)
            self.stats.misses += 1
            LOGGER.debug("cache expired key=%s", key)
            self._trace(
                {
                    "op": "get",
                    "key": key,
                    "result": "expired",
                    "lookup_ts": lookup_ts,
                    "inserted_ts": ts,
                    "expires_ts": expires_ts,
                }
            )
            return None
        self.stats.hits += 1
        LOGGER.debug("cache hit key=%s", key)
        self._trace(
            {
                "op": "get",
                "key": key,
                "result": "hit",
                "lookup_ts": lookup_ts,
                "inserted_ts": ts,
                "expires_ts": expires_ts,
            }
        )
        return val

    def set(self, key: str, value: T) -> None:
        inserted_ts = time.monotonic()
        self._store[key] = (inserted_ts, value)
        self._trace(
            {
                "op": "set",
                "key": key,
                "result": "stored",
                "lookup_ts": None,
                "inserted_ts": inserted_ts,
                "expires_ts": inserted_ts + self.ttl_seconds,
            }
        )

    def touch_keys(self, keys: list[str]) -> None:
        now_ts = time.monotonic()
        for key in keys:
            item = self._store.get(key)
            if item is None:
                continue
            _, val = item
            self._store[key] = (now_ts, val)

    def _trace(self, payload: dict) -> None:
        if not self._trace_enabled:
            return
        key = str(payload.get("key") or "")
        if key not in self._trace_seen_keys and len(self._trace_seen_keys) >= self._trace_limit:
            return
        if key:
            self._trace_seen_keys.add(key)
        payload["cycle"] = self._trace_cycle
        self._trace_ops.append(payload)

