from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Dict, Optional

from copytrading.models import WhaleProfile, WhaleTradeEvent
from copytrading.whale_registry import get_whale_profiles
from data.market_data_provider import MarketDataProvider
from execution.engine import ExchangeClient
from execution.models import OrderProposal, OrderSubmission
from infrastructure.alerting import Alert, AlertRouter, AlertSeverity
from risk.engine import RiskEngine
from src.clock import Clock, LiveClock
from src.enums import OrderType, Platform, Side, StrategyId

logger = logging.getLogger(__name__)


class CopyExecutor:
    """Receives WhaleTradeEvents and executes mirrored orders."""

    def __init__(
        self,
        exchange_client: ExchangeClient,
        risk_engine: RiskEngine,
        market_data: MarketDataProvider,
        budget_usdc: float = 75.0,
        max_per_trade_usdc: float = 25.0,
        delay_ms: int = 7000,
        follow_mode: str = "proportional",
        exclude_markets: Optional[list[str]] = None,
        max_price_deviation_pct: float = 0.30,
        min_trade_size_usdc: float = 1.0,
        alert_router: Optional[AlertRouter] = None,
        clock: Optional[Clock] = None,
        tracked_wallets: Optional[list[str]] = None,
    ) -> None:
        if budget_usdc <= 0:
            raise ValueError("budget_usdc must be > 0")
        if max_per_trade_usdc <= 0:
            raise ValueError("max_per_trade_usdc must be > 0")
        if max_per_trade_usdc > budget_usdc:
            raise ValueError("max_per_trade_usdc cannot exceed budget_usdc")

        self._client = exchange_client
        self._risk = risk_engine
        self._mdp = market_data
        self._budget_usdc = budget_usdc
        self._max_per_trade_usdc = max_per_trade_usdc
        self._delay_ms = delay_ms
        self._follow_mode = follow_mode
        self._exclude_markets = set(exclude_markets or [])
        self._max_price_deviation = max_price_deviation_pct
        self._min_trade_size = min_trade_size_usdc
        self._alert_router = alert_router
        self._clock = clock or LiveClock()
        self._whale_profiles = get_whale_profiles(tracked_wallets)
        self._whale_map: Dict[str, WhaleProfile] = {
            p.address.lower(): p for p in self._whale_profiles
        }
        self._copy_trades_total: int = 0
        self._copy_trades_skipped: int = 0
        self._whale_trades_detected: int = 0

    @property
    def budget_usdc(self) -> float:
        return self._budget_usdc

    async def handle_whale_trade(self, event: WhaleTradeEvent) -> None:
        self._whale_trades_detected += 1

        if event.market_id in self._exclude_markets:
            self._copy_trades_skipped += 1
            return

        whale = self._whale_map.get(event.wallet_address.lower())
        whale_name: str = (whale.name if whale and whale.name else event.wallet_address[:10])

        copy_size = self._calculate_copy_size(event)
        if copy_size < self._min_trade_size:
            logger.info(
                "Skipping copy trade from %s: size %.2f below min %.2f",
                whale_name, copy_size, self._min_trade_size,
            )
            self._copy_trades_skipped += 1
            return

        copy_size = min(copy_size, self._max_per_trade_usdc)

        current_price = self._get_current_price(event.market_id)
        if current_price is not None and event.price > 0:
            deviation = abs(current_price - event.price) / event.price
            if deviation > self._max_price_deviation:
                logger.info(
                    "Skipping copy from %s: price deviation %.1f%% > %.1f%%",
                    whale_name, deviation * 100, self._max_price_deviation * 100,
                )
                self._copy_trades_skipped += 1
                return

        logger.info(
            "Delay %.1fs before copying %s trade (%.2f USDC) from %s",
            self._delay_ms / 1000,
            event.side, copy_size, whale_name,
        )
        await self._clock.sleep_ms(self._delay_ms)

        await self._execute_copy(event, copy_size, whale_name)

    def _calculate_copy_size(self, event: WhaleTradeEvent) -> float:
        if self._follow_mode == "fixed":
            return self._max_per_trade_usdc
        if self._follow_mode == "exact":
            return min(event.size_usdc, self._max_per_trade_usdc)
        ratio = self._budget_usdc / (self._budget_usdc + event.size_usdc)
        ratio = min(ratio, 0.5)
        return event.size_usdc * ratio

    def _get_current_price(self, market_id: str) -> Optional[float]:
        try:
            snap = self._mdp.get_snapshot(market_id, Platform.POLYMARKET)
            if snap is not None:
                return snap.yes_mid
            return None
        except Exception:
            return None

    async def _execute_copy(
        self, event: WhaleTradeEvent, copy_size: float, whale_name: str,
    ) -> None:
        try:
            side_map = {
                "buy_yes": Side.BUY_YES,
                "buy_no": Side.BUY_NO,
                "sell_yes": Side.SELL_YES,
                "sell_no": Side.SELL_NO,
            }
            side = side_map.get(event.side)
            if side is None:
                logger.warning("Unknown side %s from whale %s", event.side, whale_name)
                self._copy_trades_skipped += 1
                return

            limit_price = self._get_limit_price(event, side)
            if limit_price is None:
                self._copy_trades_skipped += 1
                return

            proposal = OrderProposal(
                proposal_id=f"copy_{int(time.time() * 1000)}_{event.wallet_address[:8]}",
                market_id=event.market_id,
                platform=Platform.POLYMARKET,
                side=side,
                size_usdc=copy_size,
                limit_price=limit_price,
                order_type=OrderType.LIMIT,
                strategy_id=StrategyId.HEDGE,
                expiry_ms=int(time.time() * 1000) + 30_000,
                source_ts=int(time.time() * 1000),
            )

            decision = self._risk.evaluate(proposal)
            if decision.verdict.value != "approved":
                logger.info(
                    "Copy trade rejected by risk: %s (reason=%s)",
                    whale_name, decision.reject_reason,
                )
                self._copy_trades_skipped += 1
                return

            token_qty = round(copy_size / limit_price, 6)
            if token_qty <= 0:
                self._copy_trades_skipped += 1
                return

            submission = OrderSubmission(
                order_id=str(uuid.uuid4()),
                proposal_id=proposal.proposal_id,
                market_id=proposal.market_id,
                platform=proposal.platform,
                side=proposal.side,
                size_usdc=proposal.size_usdc,
                limit_price=proposal.limit_price,
                order_type=proposal.order_type,
                strategy_id=proposal.strategy_id,
                expiry_ms=proposal.expiry_ms,
                token_quantity=token_qty,
                submitted_at=int(time.time() * 1000),
            )

            result = await self._client.place_order(submission, limit_price)
            self._copy_trades_total += 1

            logger.info(
                "Copy trade executed: %s %.2f USDC at %.2f on %s (whale=%s, order=%s)",
                side.value, copy_size, limit_price,
                event.market_id, whale_name, result.exchange_order_id,
            )

            if self._alert_router:
                alert = Alert(
                    severity=AlertSeverity.INFO,
                    title=f"Copy Trade: {whale_name}",
                    message=(
                        f"Copied {side.value} {copy_size:.2f} USDC at {limit_price:.3f} "
                        f"on {event.market_id} following {whale_name}"
                    ),
                    source="copytrading",
                    metadata={
                        "whale": whale_name,
                        "wallet": event.wallet_address,
                        "market": event.market_id,
                        "size_usdc": copy_size,
                        "price": limit_price,
                        "side": side.value,
                    },
                )
                asyncio.create_task(self._alert_router.send(alert))

        except Exception as exc:
            logger.error("Copy trade failed for %s: %s", whale_name, exc)
            self._copy_trades_skipped += 1

    def _get_limit_price(self, event: WhaleTradeEvent, side: Side) -> Optional[float]:
        current_price = self._get_current_price(event.market_id)
        if current_price is not None:
            return current_price
        if event.price > 0:
            return event.price
        return None
