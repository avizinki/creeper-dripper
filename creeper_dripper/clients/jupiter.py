from __future__ import annotations

import logging
from typing import Any

import requests
from solders.keypair import Keypair
from solders.message import to_bytes_versioned
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from creeper_dripper.models import JupiterOrder, ProbeQuote
from creeper_dripper.utils import b64decode

LOGGER = logging.getLogger(__name__)

BASE_URL = "https://api.jup.ag/swap/v1"


class JupiterBadRequestError(RuntimeError):
    def __init__(
        self,
        *,
        endpoint: str,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        body: str | None = None,
        status_code: int | None = None,
    ):
        self.endpoint = endpoint
        self.params = params or {}
        self.payload = payload or {}
        self.body = body or ""
        self.status_code = status_code or 400
        super().__init__(
            f"jupiter_bad_request endpoint={endpoint} status_code={self.status_code} "
            f"params={self.params} payload={self.payload} body={self.body}"
        )


class JupiterClient:
    def __init__(self, api_key: str) -> None:
        self._session = requests.Session()
        self._session.headers.update({"x-api-key": api_key, "Accept": "application/json"})

    def _get(self, path: str, *, params: dict[str, Any]) -> dict[str, Any]:
        response = self._session.get(f"{BASE_URL}{path}", params=params, timeout=20)
        if response.status_code == 400:
            body = response.text if response.text is not None else ""
            raise JupiterBadRequestError(endpoint=path, params=params, body=body, status_code=response.status_code)
        response.raise_for_status()
        return response.json()

    def _post(self, path: str, *, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._session.post(
            f"{BASE_URL}{path}",
            json=payload,
            headers={**self._session.headers, "Content-Type": "application/json"},
            timeout=25,
        )
        if response.status_code == 400:
            body = response.text if response.text is not None else ""
            raise JupiterBadRequestError(endpoint=path, payload=payload, body=body, status_code=response.status_code)
        response.raise_for_status()
        return response.json()

    def order(
        self,
        *,
        input_mint: str,
        output_mint: str,
        amount_atomic: int,
        taker: str | None = None,
        slippage_bps: int | None = None,
    ) -> JupiterOrder:
        params = self.build_order_params(
            input_mint=input_mint,
            output_mint=output_mint,
            amount_atomic=amount_atomic,
            taker=taker,
            slippage_bps=slippage_bps,
        )
        raw = self._get("/order", params=params)
        return JupiterOrder(
            request_id=str(raw.get("requestId") or ""),
            transaction_b64=raw.get("transaction"),
            out_amount=_intish(raw.get("outAmount")),
            router=raw.get("router"),
            mode=raw.get("mode"),
            raw=raw,
        )

    @staticmethod
    def build_order_params(
        *,
        input_mint: str,
        output_mint: str,
        amount_atomic: int,
        taker: str | None = None,
        slippage_bps: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount_atomic),
        }
        if taker:
            params["taker"] = taker
        if slippage_bps is not None:
            params["slippageBps"] = str(slippage_bps)
        return params

    def probe_quote(
        self,
        *,
        input_mint: str,
        output_mint: str,
        amount_atomic: int,
        slippage_bps: int | None = None,
    ) -> ProbeQuote:
        order = self.order(
            input_mint=input_mint,
            output_mint=output_mint,
            amount_atomic=amount_atomic,
            taker=None,
            slippage_bps=slippage_bps,
        )
        impact_bps = _extract_price_impact_bps(order.raw)
        return ProbeQuote(
            input_amount_atomic=amount_atomic,
            out_amount_atomic=order.out_amount,
            price_impact_bps=impact_bps,
            route_ok=bool(order.out_amount),
            raw=order.raw,
        )

    def swap_transaction(
        self,
        *,
        quote_response: dict[str, Any],
        user_public_key: str,
        wrap_and_unwrap_sol: bool = True,
    ) -> str:
        payload: dict[str, Any] = {
            "quoteResponse": quote_response,
            "userPublicKey": str(user_public_key),
            "wrapAndUnwrapSol": bool(wrap_and_unwrap_sol),
        }
        raw = self._post("/swap", payload=payload)
        tx_b64 = raw.get("swapTransaction") or raw.get("transaction")
        if not tx_b64:
            raise RuntimeError("Jupiter /swap response missing swapTransaction")
        return str(tx_b64)


def _intish(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _extract_price_impact_bps(raw: dict[str, Any]) -> float | None:
    candidates = [raw.get("priceImpactPct"), raw.get("priceImpact"), raw.get("slippageBps")]
    for candidate in candidates:
        if candidate is None or candidate == "":
            continue
        try:
            val = float(candidate)
        except (TypeError, ValueError):
            continue
        # Jupiter docs show decimal fraction for priceImpactPct.
        if val <= 1.0:
            return val * 10_000.0
        return val
    return None


def _partially_sign_for_owner(raw_tx: VersionedTransaction, owner: Keypair) -> VersionedTransaction:
    message = raw_tx.message
    account_keys = list(message.account_keys)
    owner_index = next((idx for idx, key in enumerate(account_keys) if key == owner.pubkey()), None)
    if owner_index is None:
        raise RuntimeError("Owner pubkey not found in Jupiter transaction account keys")
    sigs = list(raw_tx.signatures)
    while len(sigs) < len(account_keys):
        sigs.append(Signature.default())
    sigs[owner_index] = owner.sign_message(to_bytes_versioned(message))
    raw_tx.signatures = sigs
    return raw_tx
