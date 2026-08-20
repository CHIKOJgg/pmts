import logging
import time
import uuid
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import aiohttp
from eth_account import Account

from execution.clients.base import BaseExchangeClient
from execution.engine import (
    ExchangeClient,
    OpenOrder,
    OrderStatusResponse,
    PlacedOrderResponse,
)
from execution.models import OrderSubmission
from execution.rate_limiter import VenueRateLimiter
from infrastructure.retry import async_retry
from src.enums import Platform, Side
from src.errors import ExchangeRejected

logger = logging.getLogger(__name__)

# Opinion Markets Constants
_DEFAULT_HOST: str = "https://openapi.opinion.trade/openapi"
_REJECTION_STATUS_CODES: frozenset[int] = frozenset({400, 401, 403, 422})

# EIP-712 Domain for Opinion Markets
_EIP712_DOMAIN = {
    "name": "OPINION CTF Exchange",
    "version": "1",
    "chainId": 56,
    # "verifyingContract": "0x..." # This is dynamic per quote token
}

_SANDBOX_HOST: str = "https://openapi-testnet.opinion.trade/openapi"


class OpinionClient(BaseExchangeClient):
    """
    Opinion Markets REST API Client implementation.
    Includes EIP-712 order signing.
    """

    PLATFORM: Platform = Platform.OPINION

    def __init__(
        self,
        api_key: str,
        wallet_private_key: str,
        ctf_exchange_addr: str,
        host: Optional[str] = None,
        rate_limit_per_s: int = 5,
        sandbox: bool = False,
        market_id_map: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not api_key or not api_key.strip():
            raise ValueError("api_key must not be empty")
        if not wallet_private_key or not wallet_private_key.strip():
            raise ValueError("wallet_private_key must not be empty")
        if not ctf_exchange_addr or ctf_exchange_addr == "0x0000000000000000000000000000000000000000":
            raise ValueError("ctf_exchange_addr must be a valid non-zero contract address")

        self._api_key = api_key
        self._wallet_private_key = wallet_private_key
        self._sandbox = sandbox
        self._market_id_map = market_id_map or {}

        if host:
            self._host = host.rstrip("/")
        else:
            self._host = _SANDBOX_HOST if sandbox else _DEFAULT_HOST

        self._address = Account.from_key(wallet_private_key).address
        self._ctf_exchange_addr = ctf_exchange_addr

        # Update chainId for EIP-712 if sandbox (BSC Testnet is 97)
        self._domain = _EIP712_DOMAIN.copy()
        if sandbox:
            self._domain["chainId"] = 97

        self._session: Optional[aiohttp.ClientSession] = None
        self._limiter = VenueRateLimiter.for_venue("opinion", rate_limit_per_s)
        self._throttler = self._limiter
        self._last_status_filled_usdc: Dict[str, float] = {}

        logger.info(
            "OpinionClient initialized: host=%s, address=%s, sandbox=%s", self._host, self._address, self._sandbox
        )

    def _resolve_token_id(self, submission: OrderSubmission) -> int:
        """Pick the correct token id for the order side (YES vs NO), parsed to int."""
        entry = self._market_id_map.get(submission.market_id)
        if isinstance(entry, dict):
            yes_id = entry.get("YES") or entry.get("yes")
            no_id = entry.get("NO") or entry.get("no")
            chosen = yes_id if submission.side in (Side.BUY_YES, Side.SELL_YES) else no_id
            chosen = chosen or yes_id or no_id or submission.market_id
        else:
            chosen = entry or submission.market_id
        return self._parse_market_id(chosen)

    def _parse_market_id(self, market_id: str) -> int:
        """Parse market_id to int, supporting decimal, hex, or hashed string."""
        if market_id.isdigit():
            return int(market_id)
        if market_id.startswith("0x"):
            return int(market_id, 16)
        return int.from_bytes(market_id.encode()[:8], byteorder="big", signed=False)

    _ERROR_KEY: str = "message"

    def _session_headers(self) -> Dict[str, str]:
        return {"Content-Type": "application/json", "apikey": self._api_key}

    def _sign_order(self, order: Dict[str, Any]) -> str:
        """Sign order using EIP-712."""
        types = {
            "Order": [
                {"name": "salt", "type": "uint256"},
                {"name": "maker", "type": "address"},
                {"name": "signer", "type": "address"},
                {"name": "taker", "type": "address"},
                {"name": "tokenId", "type": "uint256"},
                {"name": "makerAmount", "type": "uint256"},
                {"name": "takerAmount", "type": "uint256"},
                {"name": "expiration", "type": "uint256"},
                {"name": "nonce", "type": "uint256"},
                {"name": "feeRateBps", "type": "uint256"},
                {"name": "side", "type": "uint8"},
                {"name": "signatureType", "type": "uint8"},
            ]
        }

        domain = self._domain.copy()
        domain["verifyingContract"] = self._ctf_exchange_addr

        structured_data = {
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"},
                ],
                **types,
            },
            "domain": domain,
            "primaryType": "Order",
            "message": order,
        }

        signed = Account.sign_typed_data(self._wallet_private_key, full_message=structured_data)
        return str(signed.signature.hex())

    @async_retry(retryable_exceptions=(ConnectionError, TimeoutError, OSError, aiohttp.ClientError))
    async def place_order(
        self, submission: OrderSubmission, effective_price: float, nonce: Optional[int] = None
    ) -> PlacedOrderResponse:
        """Submit an order to Opinion Markets."""
        await self._limiter.acquire()
        # Side mapping: 0 for Buy, 1 for Sell (Typical Opinion side mapping)
        side_val = 0 if submission.side.is_buy else 1

        # Use the (possibly urgency-crossed) effective price to size the order.
        price = effective_price if effective_price and 0.001 <= effective_price <= 0.999 else submission.limit_price
        if not price or price <= 0:
            price = submission.limit_price
        size_usdc = max(0.0, submission.size_usdc)
        token_qty = (
            round((size_usdc / price) * 1_000_000) if price > 0 else round(submission.token_quantity * 1_000_000)
        )

        usdc_amount = round(size_usdc * 1_000_000)
        if submission.side.is_buy:
            maker_amount = usdc_amount
            taker_amount = token_qty
        else:
            maker_amount = token_qty
            taker_amount = usdc_amount

        # Use provided nonce for idempotency; salt is deterministic per nonce so
        # retries are idempotent (a fresh random salt would double-submit on retry).
        final_nonce = nonce if nonce is not None else int(time.time() * 1000)

        token_id = self._resolve_token_id(submission)
        order_msg = {
            "salt": final_nonce,
            "maker": self._address,
            "signer": self._address,
            "taker": "0x0000000000000000000000000000000000000000",
            "tokenId": token_id,
            "makerAmount": maker_amount,
            "takerAmount": taker_amount,
            "expiration": int(time.time()) + 3600,
            "nonce": final_nonce,
            "feeRateBps": 0,
            "side": side_val,
            "signatureType": 1,
        }

        signature = self._sign_order(order_msg)

        payload = {"marketId": venue_market_id, "order": order_msg, "signature": signature}

        session = await self._get_session()
        async with session.post("/order", json=payload) as resp:
                raw = await self._read_json_or_text(resp)
                if resp.status in _REJECTION_STATUS_CODES:
                    raise ExchangeRejected(
                        f"Opinion rejection: {raw.get('message', 'Unknown error')}",
                        platform=self.PLATFORM.value,
                        proposal_id=submission.proposal_id,
                        status_code=resp.status,
                        exchange_error=str(raw),
                    )
                resp.raise_for_status()

                return PlacedOrderResponse(exchange_order_id=raw.get("orderId", raw.get("orderID", "N/A")), status="live", fills=[])

    @async_retry(retryable_exceptions=(ConnectionError, TimeoutError, OSError, aiohttp.ClientError))
    async def cancel_order(self, exchange_order_id: str, market_id: str) -> bool:
        """Cancel an order on Opinion."""
        await self._limiter.acquire()
        session = await self._get_session()
        path = f"/order/{exchange_order_id}"
        async with session.delete(path) as resp:
            if resp.status == 404:
                return True
            if resp.status in _REJECTION_STATUS_CODES:
                return False
            resp.raise_for_status()
            return True

    @async_retry(retryable_exceptions=(ConnectionError, TimeoutError, OSError, aiohttp.ClientError))
    async def get_order_status(self, exchange_order_id: str, market_id: str) -> OrderStatusResponse:
        """Fetch status for an Opinion order."""
        await self._limiter.acquire()
        session = await self._get_session()
        path = f"/order/{exchange_order_id}"
        async with session.get(path) as resp:
            resp.raise_for_status()
            raw = await self._read_json_or_text(resp)

            status = raw.get("status", "").lower()
            remaining = float(raw.get("remainingAmount", raw.get("remaining", 0.0)) or 0.0)
            original = raw.get("originalAmount", raw.get("amount", raw.get("size")))
            filled_raw = raw.get("filledAmount", raw.get("filled", raw.get("filledSize")))
            if filled_raw is not None:
                cumulative_filled = float(filled_raw)
            elif original is not None:
                cumulative_filled = max(0.0, float(original) - remaining)
            else:
                cumulative_filled = 0.0

            price = float(raw.get("averagePrice", raw.get("price", 0.0)) or 0.0)
            # Opinion reports sizes in token units; convert to USDC via price.
            cumulative_filled_usdc = cumulative_filled * price if price > 0 else cumulative_filled
            remaining_usdc = remaining * price if price > 0 else remaining

            new_fills = self._compute_fill_delta(exchange_order_id, cumulative_filled_usdc, price)
            return OrderStatusResponse(
                exchange_order_id=exchange_order_id,
                is_live=status in ("open", "partial", "live", "resting"),
                is_cancelled=status in ("canceled", "cancelled"),
                is_filled=status in ("filled", "matched"),
                remaining_usdc=remaining_usdc,
                new_fills=new_fills,
            )

    @async_retry(retryable_exceptions=(ConnectionError, TimeoutError, OSError, aiohttp.ClientError))
    async def get_open_orders(self, market_ids: Optional[List[str]] = None) -> List[OpenOrder]:
        """Fetch all open orders from Opinion Markets."""
        await self._limiter.acquire()
        session = await self._get_session()
        path = "/orders/open"
        async with session.get(path) as resp:
            resp.raise_for_status()
            raw = await resp.json()

            orders = []
            for o in raw if isinstance(raw, list) else []:
                o_price = float(o.get("price", 1.0)) or 1.0
                o_orig = float(o.get("originalAmount", 0.0))
                o_rem = float(o.get("remainingAmount", 0.0))
                orders.append(
                    OpenOrder(
                        exchange_order_id=o.get("orderId", ""),
                        market_id=o.get("marketId", ""),
                        side="BUY" if o.get("side", 0) == 0 else "SELL",
                        size_usdc=o_orig * o_price,
                        filled_usdc=max(0.0, o_orig - o_rem) * o_price,
                        limit_price=float(o.get("price", 0.0)),
                        ts=int(time.time() * 1000),
                    )
                )
            return orders

    @async_retry(retryable_exceptions=(ConnectionError, TimeoutError, OSError, aiohttp.ClientError))
    async def verify_connectivity(self) -> bool:
        """Verify API keys by fetching the user profile or listing orders."""
        try:
            session = await self._get_session()
            # Fetching open orders as a connectivity check
            async with session.get("/orders/open") as resp:
                if resp.status == 200:
                    logger.info("Opinion connectivity verified.")
                    return True
                logger.error("Opinion connectivity failed: %d %s", resp.status, await resp.text())
                return False
        except Exception as exc:
            logger.error("Opinion connectivity error: %s", exc)
            return False


if TYPE_CHECKING:
    _: ExchangeClient = OpinionClient(api_key="", wallet_private_key="0x" + "0" * 64, ctf_exchange_addr="0x" + "0" * 40)
