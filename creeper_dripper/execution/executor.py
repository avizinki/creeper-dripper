from __future__ import annotations

import logging
from dataclasses import asdict

from solders.keypair import Keypair

from creeper_dripper.clients.jupiter import JupiterClient
from creeper_dripper.config import SOL_MINT, Settings
from creeper_dripper.models import JupiterExecuteResult, ProbeQuote, TokenCandidate, TradeDecision

LOGGER = logging.getLogger(__name__)


class TradeExecutor:
    def __init__(self, jupiter: JupiterClient, owner: Keypair, settings: Settings) -> None:
        self.jupiter = jupiter
        self.owner = owner
        self.settings = settings
        self.owner_address = str(owner.pubkey())

    def quote_buy(self, token: TokenCandidate, size_sol: float) -> ProbeQuote:
        lamports = max(1, int(size_sol * 1_000_000_000))
        return self.jupiter.probe_quote(
            input_mint=SOL_MINT,
            output_mint=token.address,
            amount_atomic=lamports,
            slippage_bps=self.settings.default_slippage_bps,
        )

    def quote_sell(self, token_mint: str, amount_atomic: int) -> ProbeQuote:
        return self.jupiter.probe_quote(
            input_mint=token_mint,
            output_mint=SOL_MINT,
            amount_atomic=max(1, amount_atomic),
            slippage_bps=self.settings.default_slippage_bps,
        )

    def buy(self, token: TokenCandidate, size_sol: float) -> tuple[JupiterExecuteResult | None, ProbeQuote]:
        quote = self.quote_buy(token, size_sol)
        if not self._quote_ok(quote):
            return None, quote
        if self.settings.dry_run or not self.settings.live_trading_enabled:
            return None, quote
        order = self.jupiter.order(
            input_mint=SOL_MINT,
            output_mint=token.address,
            amount_atomic=max(1, int(size_sol * 1_000_000_000)),
            taker=self.owner_address,
            slippage_bps=self.settings.default_slippage_bps,
        )
        result = self.jupiter.execute_order(order=order, owner=self.owner)
        return result, quote

    def sell(self, token_mint: str, amount_atomic: int) -> tuple[JupiterExecuteResult | None, ProbeQuote]:
        quote = self.quote_sell(token_mint, amount_atomic)
        if not self._quote_ok(quote):
            return None, quote
        if self.settings.dry_run or not self.settings.live_trading_enabled:
            return None, quote
        order = self.jupiter.order(
            input_mint=token_mint,
            output_mint=SOL_MINT,
            amount_atomic=amount_atomic,
            taker=self.owner_address,
            slippage_bps=self.settings.default_slippage_bps,
        )
        result = self.jupiter.execute_order(order=order, owner=self.owner)
        return result, quote

    def _quote_ok(self, quote: ProbeQuote) -> bool:
        if not quote.route_ok or not quote.out_amount_atomic:
            return False
        if quote.price_impact_bps is None:
            return True
        return quote.price_impact_bps <= self.settings.max_acceptable_price_impact_bps
