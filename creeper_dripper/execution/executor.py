from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import requests
from solders.keypair import Keypair
from solders.message import to_bytes_versioned
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
    EXEC_PROVIDER_UNAVAILABLE,
    EXEC_QUOTE_FAILED,
    EXEC_TX_CONFIRMED_FAILED,
    EXEC_TX_CONFIRMED_SUCCESS,
    EXEC_TX_BUILD_FAILED,
    EXEC_TX_SEND_FAILED,
    EXEC_TX_SIGN_FAILED,
    EXEC_SELL_PROCEEDS_UNAVAILABLE,
    EXEC_V2_EXECUTE_FAILED,
    EXEC_V2_ORDER_BUILD_FAILED,
    EXEC_V2_SIGN_FAILED,
    REJECT_JUPITER_BAD_PROBE,
    REJECT_JUPITER_UNTRADABLE,
    SELL_THRESHOLD_UNCOMPUTABLE,
    SETTLEMENT_UNCONFIRMED,
)
from creeper_dripper.models import ExecutionResult, ProbeQuote, TokenCandidate
from creeper_dripper.utils import atomic_write_json, b64, b64decode
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
            route_code = (
                EXEC_PROVIDER_UNAVAILABLE if (quote.raw or {}).get("error") == "jupiter_timeout" else EXEC_NO_ROUTE
            )
            self.events.emit("entry_failed", route_code, token_mint=token.address)
            return (
                ExecutionResult(
                    status="failed",
                    requested_amount=requested,
                    diagnostic_code=route_code,
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
            order = self.build_v2_execution_order(
                input_mint=SOL_MINT,
                output_mint=token.address,
                amount_atomic=requested,
            )
        except Exception as exc:
            self.events.emit("entry_failed", EXEC_V2_ORDER_BUILD_FAILED, token_mint=token.address, error=str(exc))
            return (
                ExecutionResult(
                    status="failed",
                    requested_amount=requested,
                    diagnostic_code=EXEC_V2_ORDER_BUILD_FAILED,
                    error=str(exc),
                    diagnostic_metadata={
                        "phase": "order_build",
                        "side": "buy",
                        "endpoint": "/order",
                        "request_params": {
                            "inputMint": SOL_MINT,
                            "outputMint": token.address,
                            "amount": str(requested),
                            "taker": self.owner_address,
                            "slippageBps": self.settings.default_slippage_bps,
                        },
                        "status_code": getattr(exc, "status_code", None),
                        "response_body": getattr(exc, "body", None),
                        "classification": EXEC_V2_ORDER_BUILD_FAILED,
                    },
                ),
                quote,
            )
        try:
            signature, execute_raw = self.sign_and_execute_v2(order)
        except Exception as exc:
            code = EXEC_V2_EXECUTE_FAILED
            if str(exc).startswith("sign_failed:"):
                code = EXEC_V2_SIGN_FAILED
            self.events.emit("entry_failed", code, token_mint=token.address, error=str(exc))
            return (
                ExecutionResult(
                    status="failed",
                    requested_amount=requested,
                    diagnostic_code=code,
                    error=str(exc),
                    diagnostic_metadata={"phase": "execute", "side": "buy", "classification": code, "endpoint": "/execute"},
                ),
                quote,
            )
        settlement, executed_atomic, settle_meta = self._settle_buy_after_execute(
            token=token,
            order=order,
            quote=quote,
            signature=signature,
            execute_raw=execute_raw if isinstance(execute_raw, dict) else {},
        )
        if settlement == "unknown":
            self.events.emit("entry_failed", SETTLEMENT_UNCONFIRMED, token_mint=token.address, error="buy_settlement_unconfirmed")
            return (
                ExecutionResult(
                    status="unknown",
                    requested_amount=requested,
                    executed_amount=None,
                    diagnostic_code=SETTLEMENT_UNCONFIRMED,
                    error="buy_settlement_unconfirmed",
                    signature=signature,
                    diagnostic_metadata={
                        "phase": "post_execute_settlement",
                        "side": "buy",
                        "classification": SETTLEMENT_UNCONFIRMED,
                        "post_buy_settlement": settle_meta,
                    },
                ),
                quote,
            )
        return (
            ExecutionResult(
                status="success",
                requested_amount=requested,
                executed_amount=executed_atomic,
                output_amount=None,
                diagnostic_code=EXEC_TX_CONFIRMED_SUCCESS,
                signature=signature,
                error=None,
                diagnostic_metadata={
                    "phase": "execute",
                    "side": "buy",
                    "send_status": "submitted",
                    "post_buy_settlement": settle_meta,
                },
            ),
            quote,
        )

    def sell(self, token_mint: str, amount_atomic: int) -> tuple[ExecutionResult, ProbeQuote]:
        requested = max(1, amount_atomic)
        quote = self.quote_sell(token_mint, requested)
        if not self._quote_ok(quote):
            route_code = (
                EXEC_PROVIDER_UNAVAILABLE if (quote.raw or {}).get("error") == "jupiter_timeout" else EXEC_NO_ROUTE
            )
            self.events.emit("exit_failed", route_code, token_mint=token_mint)
            return (
                ExecutionResult(
                    status="failed",
                    requested_amount=requested,
                    diagnostic_code=route_code,
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
            order = self.build_v2_execution_order(
                input_mint=token_mint,
                output_mint=SOL_MINT,
                amount_atomic=requested,
            )
        except Exception as exc:
            self.events.emit("exit_failed", EXEC_V2_ORDER_BUILD_FAILED, token_mint=token_mint, error=str(exc))
            return (
                ExecutionResult(
                    status="failed",
                    requested_amount=requested,
                    diagnostic_code=EXEC_V2_ORDER_BUILD_FAILED,
                    error=str(exc),
                    diagnostic_metadata={
                        "phase": "order_build",
                        "side": "sell",
                        "endpoint": "/order",
                        "request_params": {
                            "inputMint": token_mint,
                            "outputMint": SOL_MINT,
                            "amount": str(requested),
                            "taker": self.owner_address,
                            "slippageBps": self.settings.default_slippage_bps,
                        },
                        "status_code": getattr(exc, "status_code", None),
                        "response_body": getattr(exc, "body", None),
                        "classification": EXEC_V2_ORDER_BUILD_FAILED,
                    },
                ),
                quote,
            )
        try:
            signature, execute_raw = self.sign_and_execute_v2(order)
        except Exception as exc:
            code = EXEC_V2_EXECUTE_FAILED
            if str(exc).startswith("sign_failed:"):
                code = EXEC_V2_SIGN_FAILED
            self.events.emit("exit_failed", code, token_mint=token_mint, error=str(exc))
            return (
                ExecutionResult(
                    status="failed",
                    requested_amount=requested,
                    diagnostic_code=code,
                    error=str(exc),
                    diagnostic_metadata={"phase": "execute", "side": "sell", "classification": code, "endpoint": "/execute"},
                ),
                quote,
            )
        outcome, sold_atomic, out_lamports, settle_meta = self._settle_sell_after_execute(
            token_mint=token_mint,
            requested_qty=requested,
            quote=quote,
            order=order,
            signature=signature,
            execute_raw=execute_raw if isinstance(execute_raw, dict) else {},
        )
        if outcome == "unknown":
            self.events.emit("exit_failed", SETTLEMENT_UNCONFIRMED, token_mint=token_mint, error="sell_settlement_unconfirmed")
            return (
                ExecutionResult(
                    status="unknown",
                    requested_amount=requested,
                    executed_amount=sold_atomic,
                    diagnostic_code=SETTLEMENT_UNCONFIRMED,
                    error="sell_settlement_unconfirmed",
                    signature=signature,
                    diagnostic_metadata={
                        "phase": "post_execute_settlement",
                        "side": "sell",
                        "classification": SETTLEMENT_UNCONFIRMED,
                        "post_sell_settlement": settle_meta,
                    },
                ),
                quote,
            )
        if sold_atomic is None or sold_atomic < 0:
            self.events.emit("exit_failed", SETTLEMENT_UNCONFIRMED, token_mint=token_mint, error="sell_settlement_invalid_amount")
            return (
                ExecutionResult(
                    status="unknown",
                    requested_amount=requested,
                    executed_amount=sold_atomic,
                    diagnostic_code=SETTLEMENT_UNCONFIRMED,
                    error="sell_settlement_invalid_amount",
                    signature=signature,
                    diagnostic_metadata={
                        "phase": "post_execute_settlement",
                        "side": "sell",
                        "classification": SETTLEMENT_UNCONFIRMED,
                        "post_sell_settlement": settle_meta,
                    },
                ),
                quote,
            )
        settle_meta["settlement_confirmed"] = True
        return (
            ExecutionResult(
                status="success",
                requested_amount=requested,
                executed_amount=sold_atomic,
                output_amount=out_lamports,
                diagnostic_code=EXEC_TX_CONFIRMED_SUCCESS,
                signature=signature,
                error=None,
                diagnostic_metadata={
                    "phase": "execute",
                    "side": "sell",
                    "send_status": "submitted",
                    "post_sell_settlement": settle_meta,
                },
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

    def build_v2_execution_order(self, *, input_mint: str, output_mint: str, amount_atomic: int) -> dict:
        if not self.owner_address:
            raise RuntimeError("missing_wallet_keypair")
        return self.jupiter.execution_order_v2(
            input_mint=input_mint,
            output_mint=output_mint,
            amount_atomic=amount_atomic,
            taker=self.owner_address,
            slippage_bps=self.settings.default_slippage_bps,
        )

    @staticmethod
    def _parse_positive_intish(value) -> int | None:
        if value is None or value == "":
            return None
        try:
            n = int(str(value))
        except (TypeError, ValueError):
            return None
        return n if n > 0 else None

    @classmethod
    def _extract_order_expected_out_atomic(cls, order: dict) -> int | None:
        if not isinstance(order, dict):
            return None
        for key in ("outAmount", "otherAmountThreshold"):
            got = cls._parse_positive_intish(order.get(key))
            if got is not None:
                return got
        nested = order.get("quoteResponse")
        if isinstance(nested, dict):
            return cls._parse_positive_intish(nested.get("outAmount"))
        return None

    @classmethod
    def _extract_order_in_atomic(cls, order: dict) -> int | None:
        if not isinstance(order, dict):
            return None
        for key in ("inAmount", "amount"):
            got = cls._parse_positive_intish(order.get(key))
            if got is not None:
                return got
        nested = order.get("quoteResponse")
        if isinstance(nested, dict):
            return cls._parse_positive_intish(nested.get("inAmount"))
        return None

    @staticmethod
    def _extract_execute_response_input_atomic(raw: dict) -> int | None:
        if not isinstance(raw, dict):
            return None
        for key in ("totalInputAmount", "inputAmount", "inAmount"):
            val = raw.get(key)
            if val is None or val == "":
                continue
            try:
                n = int(str(val))
            except (TypeError, ValueError):
                continue
            if n > 0:
                return n
        return None

    def _settle_buy_after_execute(
        self,
        *,
        token: TokenCandidate,
        order: dict,
        quote: ProbeQuote,
        signature: str,
        execute_raw: dict,
    ) -> tuple[str, int | None, dict]:
        """Settlement: Jupiter /execute output → order outAmount → quote probe. Jupiter is the sole truth."""
        order_out = self._extract_order_expected_out_atomic(order)
        ex_out = self._extract_execute_response_output_lamports(execute_raw)
        meta: dict[str, object] = {
            "mint": token.address,
            "signature": signature,
            "decimals": token.decimals,
            "quote_out_amount_atomic": quote.out_amount_atomic,
            "order_out_amount_atomic": order_out,
            "jupiter_execute_out_atomic": ex_out,
        }
        primary: int | None = None
        primary_source: str | None = None
        if ex_out is not None and ex_out > 0:
            primary = int(ex_out)
            primary_source = "jupiter_execute"
        elif order_out is not None:
            primary = int(order_out)
            primary_source = "jupiter_order"
        elif quote.out_amount_atomic is not None and quote.out_amount_atomic > 0:
            primary = int(quote.out_amount_atomic)
            primary_source = "quote_probe"
        meta["primary_amount_atomic"] = primary
        meta["primary_source"] = primary_source
        if primary is not None and primary > 0:
            LOGGER.info(
                "buy_execution_settled_jupiter mint=%s primary=%s source=%s signature=%s",
                token.address,
                primary,
                primary_source,
                signature,
            )
            return "success", primary, meta
        return "unknown", None, meta

    def _settle_sell_after_execute(
        self,
        *,
        token_mint: str,
        requested_qty: int,
        quote: ProbeQuote,
        order: dict,
        signature: str,
        execute_raw: dict,
    ) -> tuple[str, int | None, int | None, dict]:
        """Settlement: Jupiter /execute input → order in → requested size. Jupiter is the sole truth."""
        jup_in = self._extract_execute_response_input_atomic(execute_raw)
        order_in = self._extract_order_in_atomic(order)
        jup_out_lamports = self._extract_execute_response_output_lamports(execute_raw)
        meta: dict[str, object] = {
            "mint": token_mint,
            "signature": signature,
            "requested_sell_qty": requested_qty,
            "jupiter_execute_in_atomic": jup_in,
            "jupiter_execute_out_lamports": jup_out_lamports,
            "order_in_atomic": order_in,
            "quote_in_atomic": quote.input_amount_atomic,
            "quote_out_sol_atomic": quote.out_amount_atomic,
        }
        if jup_in is not None and jup_in > 0:
            sold_primary = min(int(jup_in), requested_qty)
            source = "jupiter_execute"
        elif order_in is not None and order_in > 0:
            sold_primary = min(int(order_in), requested_qty)
            source = "jupiter_order_in"
        else:
            sold_primary = requested_qty
            source = "requested_order_amount"
        meta["sold_atomic_settled"] = sold_primary
        meta["sold_atomic_source"] = source
        meta["out_lamports"] = jup_out_lamports
        meta["proceeds_source"] = "jupiter_execute" if jup_out_lamports is not None else "unavailable"
        if jup_out_lamports is None:
            meta["proceeds_note"] = EXEC_SELL_PROCEEDS_UNAVAILABLE
        meta["settlement_confirmed"] = True
        LOGGER.info(
            "sell_execution_settled_jupiter mint=%s sold=%s source=%s out_lamports=%s signature=%s",
            token_mint,
            sold_primary,
            source,
            jup_out_lamports,
            signature,
        )
        return "success", sold_primary, jup_out_lamports, meta

    @staticmethod
    def _extract_execute_response_output_lamports(raw: dict) -> int | None:
        if not isinstance(raw, dict):
            return None
        for key in ("totalOutputAmount", "outputAmount", "outAmount"):
            val = raw.get(key)
            if val is None or val == "":
                continue
            try:
                n = int(str(val))
            except (TypeError, ValueError):
                continue
            if n > 0:
                return n
        return None

    def sign_and_execute_v2(self, order: dict) -> tuple[str, dict]:
        if self.owner is None:
            raise RuntimeError("sign_failed: missing owner keypair")
        unsigned_tx_b64 = str(order.get("transaction") or "")
        request_id = str(order.get("requestId") or "")
        if not unsigned_tx_b64 or not request_id:
            raise RuntimeError("execute_failed: missing transaction/requestId in order response")
        try:
            tx = VersionedTransaction.from_bytes(b64decode(unsigned_tx_b64))
            sig = self.owner.sign_message(to_bytes_versioned(tx.message))
            signed_tx = VersionedTransaction.populate(tx.message, [sig])
            signed_tx_b64 = b64(bytes(signed_tx))
        except Exception as exc:
            raise RuntimeError(f"sign_failed: {exc}") from exc
        try:
            result = self.jupiter.execute_signed_v2(
                signed_transaction_b64=signed_tx_b64,
                request_id=request_id,
            )
            if result.get("error"):
                raise RuntimeError(f"{result.get('error')}")
            signature = (result.get("signature") or "").strip()
            if not signature:
                raise RuntimeError("missing signature in /execute response")
            raw = dict(result) if isinstance(result, dict) else {}
            return signature, raw
        except Exception as exc:
            raise RuntimeError(f"execute_failed: {exc}") from exc

    def sign_and_send(self, swap_transaction_b64: str) -> str:
        if self.owner is None:
            raise RuntimeError("sign_failed: missing owner keypair")
        tx_metadata: dict = {}
        simulation: dict | None = None
        try:
            tx = VersionedTransaction.from_bytes(b64decode(swap_transaction_b64))
            tx = _partially_sign_for_owner(tx, self.owner)
            tx_metadata = self._tx_metadata(tx)
            LOGGER.info("tx metadata: %s", tx_metadata)
        except Exception as exc:
            raise RuntimeError(f"sign_failed: {exc}") from exc
        try:
            simulation = self._simulate_transaction(tx)
            LOGGER.info("tx simulation: %s", simulation)
        except Exception as exc:
            LOGGER.warning("simulateTransaction failed before send: %s", exc)
            simulation = {
                "ok": False,
                "exception_type": type(exc).__name__,
                "error": str(exc),
            }
        try:
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "sendTransaction",
                "params": [b64(bytes(tx)), {"encoding": "base64", "skipPreflight": False}],
            }
            LOGGER.info("sendTransaction payload submitted to rpc=%s", self._rpc_url)
            response = self._rpc.post(self._rpc_url, json=payload, timeout=20)
            response.raise_for_status()
            body = response.json()
            wrote_artifact = False
            if body.get("error"):
                self._write_tx_failure_artifact(
                    stage="send_rpc_error",
                    tx_metadata=tx_metadata,
                    simulation=simulation,
                    error={
                        "exception_type": "RpcError",
                        "error": str(body.get("error")),
                        "rpc_response_body": body,
                    },
                )
                wrote_artifact = True
                raise RuntimeError(str(body.get("error")))
            signature = (body.get("result") or "").strip()
            if not signature:
                self._write_tx_failure_artifact(
                    stage="send_empty_signature",
                    tx_metadata=tx_metadata,
                    simulation=simulation,
                    error={
                        "exception_type": "RuntimeError",
                        "error": "empty_signature_from_rpc",
                        "rpc_response_body": body,
                    },
                )
                wrote_artifact = True
                raise RuntimeError("empty_signature_from_rpc")
            return signature
        except Exception as exc:
            rpc_body = None
            if isinstance(exc, requests.HTTPError) and exc.response is not None:
                try:
                    rpc_body = exc.response.json()
                except Exception:
                    rpc_body = exc.response.text
            if not locals().get("wrote_artifact", False):
                self._write_tx_failure_artifact(
                    stage="send_exception",
                    tx_metadata=tx_metadata,
                    simulation=simulation,
                    error={
                        "exception_type": type(exc).__name__,
                        "error": str(exc),
                        "rpc_response_body": rpc_body,
                    },
                )
            raise RuntimeError(f"send_failed: {exc}") from exc

    def _simulate_transaction(self, tx: VersionedTransaction) -> dict:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "simulateTransaction",
            "params": [b64(bytes(tx)), {"encoding": "base64", "sigVerify": False}],
        }
        response = self._rpc.post(self._rpc_url, json=payload, timeout=20)
        response.raise_for_status()
        body = response.json()
        value = (body.get("result") or {}).get("value") or {}
        return {
            "ok": value.get("err") is None,
            "err": value.get("err"),
            "logs": value.get("logs") or [],
            "units_consumed": value.get("unitsConsumed"),
        }

    def _tx_metadata(self, tx: VersionedTransaction) -> dict:
        msg = tx.message
        lookups = getattr(msg, "address_table_lookups", None) or []
        instructions = getattr(msg, "instructions", None) or []
        return {
            "version": "legacy" if getattr(msg, "header", None) is None else "v0_or_newer",
            "uses_address_lookup_tables": len(lookups) > 0,
            "address_lookup_table_count": len(lookups),
            "instruction_count": len(instructions),
        }

    def _write_tx_failure_artifact(self, *, stage: str, tx_metadata: dict, simulation: dict | None, error: dict) -> None:
        try:
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            artifact_dir = Path(self.settings.run_dir or self.settings.runtime_dir)
            path = artifact_dir / f"tx_failure_{ts}.json"
            payload = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "run_id": self.settings.run_id,
                "stage": stage,
                "rpc_url": self._rpc_url,
                "tx_metadata": tx_metadata,
                "simulation": simulation,
                "error": error,
            }
            atomic_write_json(path, payload)
            LOGGER.error("tx failure artifact written: %s", path)
        except Exception as artifact_exc:
            LOGGER.error("failed to write tx failure artifact: %s", artifact_exc)

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
            elif side == "sell" and "cannot_compute_other_amount_threshold" in body_text:
                classification = SELL_THRESHOLD_UNCOMPUTABLE
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
