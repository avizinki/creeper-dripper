from __future__ import annotations

import logging
import os

import requests
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

from creeper_dripper.clients.jupiter import JupiterClient, _partially_sign_for_owner
from creeper_dripper.config import SOL_MINT, Settings
from creeper_dripper.errors import (
    EXEC_EXECUTE_FAILED,
    EXEC_EXECUTE_UNKNOWN,
    EXEC_SKIPPED_DRY_RUN,
    EXEC_SKIPPED_LIVE_DISABLED,
    EXEC_NO_ROUTE,
    EXEC_ORDER_FAILED,
    EXEC_QUOTE_FAILED,
    EXEC_TX_CONFIRMED_FAILED,
    EXEC_TX_CONFIRMED_SUCCESS,
    EXEC_TX_BUILD_FAILED,
    EXEC_TX_SEND_FAILED,
    EXEC_TX_SIGN_FAILED,
    REJECT_JUPITER_BAD_PROBE,
    REJECT_JUPITER_UNTRADABLE,
)
from creeper_dripper.models import ExecutionResult, ProbeQuote, TokenCandidate
from creeper_dripper.utils import b64, b64decode
from creeper_dripper.observability import EventCollector

LOGGER = logging.getLogger(__name__)


class TradeExecutor:
    def __init__(self, jupiter: JupiterClient, owner: Keypair | None, settings: Settings) -> None:
        self.jupiter = jupiter
        self.owner = owner
        self.settings = settings
        self.owner_address = str(owner.pubkey()) if owner is not None else None
        self._rpc_url = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
        self._rpc = requests.Session()
        self.events = EventCollector()

    def quote_buy(self, token: TokenCandidate, size_sol: float) -> ProbeQuote:
        lamports = max(1, int(size_sol * 1_000_000_000))
        try:
            return self.jupiter.probe_quote(
                input_mint=SOL_MINT,
                output_mint=token.address,
                amount_atomic=lamports,
                slippage_bps=self.settings.default_slippage_bps,
            )
        except Exception as exc:
            details = self._probe_error_details(exc, side="buy")
            self.events.emit(
                "entry_failed",
                details["classification"],
                token_mint=token.address,
                endpoint=details["endpoint"],
                request_params=details["params"],
                status_code=details["status_code"],
                response_body=details["response_body"],
            )
            return ProbeQuote(input_amount_atomic=lamports, out_amount_atomic=None, price_impact_bps=None, route_ok=False, raw={"error": str(exc), **details})

    def quote_sell(self, token_mint: str, amount_atomic: int) -> ProbeQuote:
        requested = max(1, amount_atomic)
        try:
            return self.jupiter.probe_quote(
                input_mint=token_mint,
                output_mint=SOL_MINT,
                amount_atomic=requested,
                slippage_bps=self.settings.default_slippage_bps,
            )
        except Exception as exc:
            details = self._probe_error_details(exc, side="sell")
            self.events.emit(
                "exit_failed",
                details["classification"],
                token_mint=token_mint,
                endpoint=details["endpoint"],
                request_params=details["params"],
                status_code=details["status_code"],
                response_body=details["response_body"],
            )
            return ProbeQuote(input_amount_atomic=requested, out_amount_atomic=None, price_impact_bps=None, route_ok=False, raw={"error": str(exc), **details})

    def buy(self, token: TokenCandidate, size_sol: float) -> tuple[ExecutionResult, ProbeQuote]:
        requested = max(1, int(size_sol * 1_000_000_000))
        quote = self.quote_buy(token, size_sol)
        if not self._quote_ok(quote):
            self.events.emit("entry_failed", EXEC_NO_ROUTE, token_mint=token.address)
            return (
                ExecutionResult(
                    status="failed",
                    requested_amount=requested,
                    diagnostic_code=EXEC_NO_ROUTE,
                    error="buy_quote_unusable",
                    diagnostic_metadata={"phase": "pre_entry_probe", "side": "buy", **(quote.raw or {})},
                ),
                quote,
            )
        if self.settings.dry_run:
            return (
                ExecutionResult(
                    status="skipped",
                    requested_amount=requested,
                    diagnostic_code=EXEC_SKIPPED_DRY_RUN,
                    error="buy_not_executed_dry_run",
                    diagnostic_metadata={"phase": "mode_gate", "side": "buy", "dry_run": True, "live_trading_enabled": self.settings.live_trading_enabled},
                ),
                quote,
            )
        if not self.settings.live_trading_enabled:
            return (
                ExecutionResult(
                    status="skipped",
                    requested_amount=requested,
                    diagnostic_code=EXEC_SKIPPED_LIVE_DISABLED,
                    error="buy_not_executed_live_disabled",
                    diagnostic_metadata={"phase": "mode_gate", "side": "buy", "dry_run": self.settings.dry_run, "live_trading_enabled": False},
                ),
                quote,
            )
        if self.owner is None or self.owner_address is None:
            return (
                ExecutionResult(
                    status="failed",
                    requested_amount=requested,
                    diagnostic_code=EXEC_ORDER_FAILED,
                    error="missing_wallet_keypair",
                    diagnostic_metadata={"phase": "order_build", "side": "buy"},
                ),
                quote,
            )
        try:
            swap_tx_b64 = self.build_swap_transaction(quote_response=quote.raw or {})
        except Exception as exc:
            self.events.emit("entry_failed", EXEC_TX_BUILD_FAILED, token_mint=token.address, error=str(exc))
            return (
                ExecutionResult(
                    status="failed",
                    requested_amount=requested,
                    diagnostic_code=EXEC_TX_BUILD_FAILED,
                    error=str(exc),
                    diagnostic_metadata={
                        "phase": "order_build",
                        "side": "buy",
                        "endpoint": "/swap",
                        "request_params": {
                            "quoteResponse": quote.raw or {},
                            "userPublicKey": self.owner_address,
                            "wrapAndUnwrapSol": True,
                        },
                        "status_code": getattr(exc, "status_code", None),
                        "response_body": getattr(exc, "body", None),
                        "classification": EXEC_TX_BUILD_FAILED,
                    },
                ),
                quote,
            )
        try:
            signature = self.sign_and_send(swap_tx_b64)
        except Exception as exc:
            code = EXEC_TX_SEND_FAILED
            if "sign" in str(exc).lower():
                code = EXEC_TX_SIGN_FAILED
            self.events.emit("entry_failed", code, token_mint=token.address, error=str(exc))
            return (
                ExecutionResult(
                    status="failed",
                    requested_amount=requested,
                    diagnostic_code=code,
                    error=str(exc),
                    diagnostic_metadata={"phase": "execute", "side": "buy", "classification": code},
                ),
                quote,
            )
        return (
            ExecutionResult(
                status="success",
                requested_amount=requested,
                executed_amount=quote.out_amount_atomic,
                output_amount=None,
                diagnostic_code=EXEC_TX_CONFIRMED_SUCCESS,
                signature=signature,
                error=None,
                diagnostic_metadata={"phase": "execute", "side": "buy", "send_status": "submitted"},
            ),
            quote,
        )

    def sell(self, token_mint: str, amount_atomic: int) -> tuple[ExecutionResult, ProbeQuote]:
        requested = max(1, amount_atomic)
        quote = self.quote_sell(token_mint, requested)
        if not self._quote_ok(quote):
            self.events.emit("exit_failed", EXEC_NO_ROUTE, token_mint=token_mint)
            return (
                ExecutionResult(
                    status="failed",
                    requested_amount=requested,
                    diagnostic_code=EXEC_NO_ROUTE,
                    error="sell_quote_unusable",
                    diagnostic_metadata={"phase": "pre_entry_probe", "side": "sell", **(quote.raw or {})},
                ),
                quote,
            )
        if self.settings.dry_run:
            return (
                ExecutionResult(
                    status="skipped",
                    requested_amount=requested,
                    diagnostic_code=EXEC_SKIPPED_DRY_RUN,
                    error="sell_not_executed_dry_run",
                    diagnostic_metadata={"phase": "mode_gate", "side": "sell", "dry_run": True, "live_trading_enabled": self.settings.live_trading_enabled},
                ),
                quote,
            )
        if not self.settings.live_trading_enabled:
            return (
                ExecutionResult(
                    status="skipped",
                    requested_amount=requested,
                    diagnostic_code=EXEC_SKIPPED_LIVE_DISABLED,
                    error="sell_not_executed_live_disabled",
                    diagnostic_metadata={"phase": "mode_gate", "side": "sell", "dry_run": self.settings.dry_run, "live_trading_enabled": False},
                ),
                quote,
            )
        if self.owner is None or self.owner_address is None:
            return (
                ExecutionResult(
                    status="failed",
                    requested_amount=requested,
                    diagnostic_code=EXEC_ORDER_FAILED,
                    error="missing_wallet_keypair",
                    diagnostic_metadata={"phase": "order_build", "side": "sell"},
                ),
                quote,
            )
        try:
            swap_tx_b64 = self.build_swap_transaction(quote_response=quote.raw or {})
        except Exception as exc:
            self.events.emit("exit_failed", EXEC_TX_BUILD_FAILED, token_mint=token_mint, error=str(exc))
            return (
                ExecutionResult(
                    status="failed",
                    requested_amount=requested,
                    diagnostic_code=EXEC_TX_BUILD_FAILED,
                    error=str(exc),
                    diagnostic_metadata={
                        "phase": "order_build",
                        "side": "sell",
                        "endpoint": "/swap",
                        "request_params": {
                            "quoteResponse": quote.raw or {},
                            "userPublicKey": self.owner_address,
                            "wrapAndUnwrapSol": True,
                        },
                        "status_code": getattr(exc, "status_code", None),
                        "response_body": getattr(exc, "body", None),
                        "classification": EXEC_TX_BUILD_FAILED,
                    },
                ),
                quote,
            )
        try:
            signature = self.sign_and_send(swap_tx_b64)
        except Exception as exc:
            code = EXEC_TX_SEND_FAILED
            if "sign" in str(exc).lower():
                code = EXEC_TX_SIGN_FAILED
            self.events.emit("exit_failed", code, token_mint=token_mint, error=str(exc))
            return (
                ExecutionResult(
                    status="failed",
                    requested_amount=requested,
                    diagnostic_code=code,
                    error=str(exc),
                    diagnostic_metadata={"phase": "execute", "side": "sell", "classification": code},
                ),
                quote,
            )
        return (
            ExecutionResult(
                status="success",
                requested_amount=requested,
                executed_amount=requested,
                output_amount=quote.out_amount_atomic,
                diagnostic_code=EXEC_TX_CONFIRMED_SUCCESS,
                signature=signature,
                error=None,
                diagnostic_metadata={"phase": "execute", "side": "sell", "send_status": "submitted"},
            ),
            quote,
        )

    def build_swap_transaction(self, *, quote_response: dict) -> str:
        if not self.owner_address:
            raise RuntimeError("missing_wallet_keypair")
        return self.jupiter.swap_transaction(
            quote_response=quote_response,
            user_public_key=self.owner_address,
            wrap_and_unwrap_sol=True,
        )

    def sign_and_send(self, swap_transaction_b64: str) -> str:
        if self.owner is None:
            raise RuntimeError("sign_failed: missing owner keypair")
        try:
            tx = VersionedTransaction.from_bytes(b64decode(swap_transaction_b64))
            tx = _partially_sign_for_owner(tx, self.owner)
        except Exception as exc:
            raise RuntimeError(f"sign_failed: {exc}") from exc
        try:
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "sendTransaction",
                "params": [b64(bytes(tx)), {"encoding": "base64", "skipPreflight": False}],
            }
            response = self._rpc.post(self._rpc_url, json=payload, timeout=20)
            response.raise_for_status()
            body = response.json()
            if body.get("error"):
                raise RuntimeError(str(body.get("error")))
            signature = (body.get("result") or "").strip()
            if not signature:
                raise RuntimeError("empty_signature_from_rpc")
            return signature
        except Exception as exc:
            raise RuntimeError(f"send_failed: {exc}") from exc

    def wallet_token_balance_atomic(self, token_mint: str) -> int | None:
        if not self.owner_address:
            return None
        try:
            response = self._rpc.post(
                self._rpc_url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getTokenAccountsByOwner",
                    "params": [
                        self.owner_address,
                        {"mint": token_mint},
                        {"encoding": "jsonParsed"},
                    ],
                },
                timeout=20,
            )
            response.raise_for_status()
            payload = response.json()
            values = ((payload.get("result") or {}).get("value") or [])
            total = 0
            for item in values:
                amount = (
                    (((item.get("account") or {}).get("data") or {}).get("parsed") or {})
                    .get("info", {})
                    .get("tokenAmount", {})
                    .get("amount")
                )
                if amount is None:
                    continue
                total += int(str(amount))
            return total
        except Exception as exc:
            LOGGER.warning("wallet balance fetch failed for %s: %s", token_mint, exc)
            return None

    def transaction_status(self, signature: str) -> str | None:
        if not signature:
            return None
        try:
            response = self._rpc.post(
                self._rpc_url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getSignatureStatuses",
                    "params": [[signature], {"searchTransactionHistory": True}],
                },
                timeout=20,
            )
            response.raise_for_status()
            payload = response.json()
            statuses = ((payload.get("result") or {}).get("value") or [])
            if not statuses or statuses[0] is None:
                return None
            status = statuses[0]
            if status.get("err") is not None:
                self.events.emit("exit_failed", EXEC_TX_CONFIRMED_FAILED, signature=signature)
                return "failed"
            confirmation = status.get("confirmationStatus")
            if confirmation in {"processed", "confirmed", "finalized"}:
                self.events.emit("exit_success", EXEC_TX_CONFIRMED_SUCCESS, signature=signature, confirmation=confirmation)
                return "success"
            return None
        except Exception as exc:
            LOGGER.warning("transaction status fetch failed for signature=%s: %s", signature, exc)
            return None

    def _quote_ok(self, quote: ProbeQuote) -> bool:
        if not quote.route_ok or not quote.out_amount_atomic:
            return False
        if quote.price_impact_bps is None:
            return True
        if abs(float(quote.price_impact_bps)) >= 5_000:
            return False
        return quote.price_impact_bps <= self.settings.max_acceptable_price_impact_bps

    @staticmethod
    def _probe_error_details(exc: Exception, *, side: str) -> dict:
        endpoint = None
        params = {}
        status_code = None
        response_body = None
        classification = EXEC_QUOTE_FAILED
        body_text = str(exc).lower()
        if hasattr(exc, "endpoint"):
            endpoint = getattr(exc, "endpoint", None)
            params = getattr(exc, "params", {}) or {}
            status_code = getattr(exc, "status_code", None)
            response_body = getattr(exc, "body", None)
            body_text = (response_body or str(exc) or "").lower()
            if "no route" in body_text or "route not found" in body_text:
                classification = EXEC_NO_ROUTE
            elif "not tradable" in body_text or "cannot be traded" in body_text or "tradable" in body_text:
                classification = REJECT_JUPITER_UNTRADABLE
            else:
                classification = REJECT_JUPITER_BAD_PROBE
        return {
            "classification": classification,
            "side": side,
            "endpoint": endpoint,
            "params": params,
            "status_code": status_code,
            "response_body": response_body,
        }

    @staticmethod
    def _normalize_execution_result(raw_result, *, requested_amount: int) -> ExecutionResult:
        if raw_result.output_amount_result is not None:
            executed = raw_result.input_amount_result if raw_result.input_amount_result is not None else requested_amount
            return ExecutionResult(
                status="success",
                requested_amount=requested_amount,
                executed_amount=executed,
                output_amount=raw_result.output_amount_result,
                diagnostic_code=EXEC_TX_CONFIRMED_SUCCESS,
                signature=raw_result.signature,
                error=raw_result.error,
                is_partial=executed < requested_amount,
            )
        if raw_result.error:
            return ExecutionResult(
                status="failed",
                requested_amount=requested_amount,
                executed_amount=raw_result.input_amount_result,
                output_amount=raw_result.output_amount_result,
                diagnostic_code=EXEC_EXECUTE_FAILED,
                signature=raw_result.signature,
                error=raw_result.error,
                is_partial=bool(raw_result.input_amount_result and raw_result.input_amount_result < requested_amount),
            )
        return ExecutionResult(
            status="unknown",
            requested_amount=requested_amount,
            executed_amount=raw_result.input_amount_result,
            output_amount=raw_result.output_amount_result,
            diagnostic_code=EXEC_EXECUTE_UNKNOWN,
            signature=raw_result.signature,
            error=raw_result.error,
            is_partial=bool(raw_result.input_amount_result and raw_result.input_amount_result < requested_amount),
        )
