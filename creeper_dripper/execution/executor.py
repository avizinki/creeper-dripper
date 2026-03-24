from __future__ import annotations

import logging
import os

import requests
from solders.keypair import Keypair

from creeper_dripper.clients.jupiter import JupiterClient
from creeper_dripper.config import SOL_MINT, Settings
from creeper_dripper.errors import (
    EXEC_EXECUTE_FAILED,
    EXEC_EXECUTE_UNKNOWN,
    EXEC_NO_ROUTE,
    EXEC_ORDER_FAILED,
    EXEC_QUOTE_FAILED,
    EXEC_TX_CONFIRMED_FAILED,
    EXEC_TX_CONFIRMED_SUCCESS,
)
from creeper_dripper.models import ExecutionResult, ProbeQuote, TokenCandidate
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
            self.events.emit("entry_failed", EXEC_QUOTE_FAILED, token_mint=token.address, error=str(exc))
            return ProbeQuote(input_amount_atomic=lamports, out_amount_atomic=None, price_impact_bps=None, route_ok=False, raw={"error": str(exc)})

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
            self.events.emit("exit_failed", EXEC_QUOTE_FAILED, token_mint=token_mint, error=str(exc))
            return ProbeQuote(input_amount_atomic=requested, out_amount_atomic=None, price_impact_bps=None, route_ok=False, raw={"error": str(exc)})

    def buy(self, token: TokenCandidate, size_sol: float) -> tuple[ExecutionResult, ProbeQuote]:
        requested = max(1, int(size_sol * 1_000_000_000))
        quote = self.quote_buy(token, size_sol)
        if not self._quote_ok(quote):
            self.events.emit("entry_failed", EXEC_NO_ROUTE, token_mint=token.address)
            return ExecutionResult(status="failed", requested_amount=requested, diagnostic_code=EXEC_NO_ROUTE, error="buy_quote_unusable"), quote
        if self.settings.dry_run or not self.settings.live_trading_enabled:
            return ExecutionResult(status="unknown", requested_amount=requested, diagnostic_code=EXEC_EXECUTE_UNKNOWN, error="buy_not_executed_dry_run_or_disabled"), quote
        if self.owner is None or self.owner_address is None:
            return ExecutionResult(status="failed", requested_amount=requested, diagnostic_code=EXEC_ORDER_FAILED, error="missing_wallet_keypair"), quote
        try:
            order = self.jupiter.order(
                input_mint=SOL_MINT,
                output_mint=token.address,
                amount_atomic=requested,
                taker=self.owner_address,
                slippage_bps=self.settings.default_slippage_bps,
            )
        except Exception as exc:
            self.events.emit("entry_failed", EXEC_ORDER_FAILED, token_mint=token.address, error=str(exc))
            return ExecutionResult(status="failed", requested_amount=requested, diagnostic_code=EXEC_ORDER_FAILED, error=str(exc)), quote
        try:
            raw = self.jupiter.execute_order(order=order, owner=self.owner)
        except Exception as exc:
            self.events.emit("entry_failed", EXEC_EXECUTE_UNKNOWN, token_mint=token.address, error=str(exc))
            return ExecutionResult(status="unknown", requested_amount=requested, diagnostic_code=EXEC_EXECUTE_UNKNOWN, error=str(exc)), quote
        return self._normalize_execution_result(raw, requested_amount=requested), quote

    def sell(self, token_mint: str, amount_atomic: int) -> tuple[ExecutionResult, ProbeQuote]:
        requested = max(1, amount_atomic)
        quote = self.quote_sell(token_mint, requested)
        if not self._quote_ok(quote):
            self.events.emit("exit_failed", EXEC_NO_ROUTE, token_mint=token_mint)
            return ExecutionResult(status="failed", requested_amount=requested, diagnostic_code=EXEC_NO_ROUTE, error="sell_quote_unusable"), quote
        if self.settings.dry_run or not self.settings.live_trading_enabled:
            return ExecutionResult(status="unknown", requested_amount=requested, diagnostic_code=EXEC_EXECUTE_UNKNOWN, error="sell_not_executed_dry_run_or_disabled"), quote
        if self.owner is None or self.owner_address is None:
            return ExecutionResult(status="failed", requested_amount=requested, diagnostic_code=EXEC_ORDER_FAILED, error="missing_wallet_keypair"), quote
        try:
            order = self.jupiter.order(
                input_mint=token_mint,
                output_mint=SOL_MINT,
                amount_atomic=requested,
                taker=self.owner_address,
                slippage_bps=self.settings.default_slippage_bps,
            )
        except Exception as exc:
            self.events.emit("exit_failed", EXEC_ORDER_FAILED, token_mint=token_mint, error=str(exc))
            return ExecutionResult(status="failed", requested_amount=requested, diagnostic_code=EXEC_ORDER_FAILED, error=str(exc)), quote
        try:
            raw = self.jupiter.execute_order(order=order, owner=self.owner)
        except Exception as exc:
            self.events.emit("exit_failed", EXEC_EXECUTE_UNKNOWN, token_mint=token_mint, error=str(exc))
            return ExecutionResult(status="unknown", requested_amount=requested, diagnostic_code=EXEC_EXECUTE_UNKNOWN, error=str(exc)), quote
        return self._normalize_execution_result(raw, requested_amount=requested), quote

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
        return quote.price_impact_bps <= self.settings.max_acceptable_price_impact_bps

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
