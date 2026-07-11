import hashlib
import hmac
import json
import logging
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import aiohttp
from eth_account import Account

from execution.rate_limiter import VenueRateLimiter
from infrastructure.retry import async_retry

from execution.clients.base import BaseExchangeClient
from execution.engine import (
    ExchangeClient,
    OpenOrder,
    OrderStatusResponse,
    PlacedOrderResponse,
)
from execution.models import OrderSubmission
from src.errors import ExchangeRejected
from src.enums import Platform

logger = logging.getLogger(__name__)

# Polymarket CLOB Constants
_DEFAULT_HOST: str = "https://clob.polymarket.com"
_REJECTION_STATUS_CODES: frozenset[int] = frozenset({400, 401, 403, 422})

# EIP-712 Domain for Polymarket
_EIP712_DOMAIN = {
    "name": "Polymarket CLOB",
    "version": "1",
    "chainId": 137,
    "verifyingContract": "0x4bFb9717C5521096C6E419519A619717C5521096",
}

_SANDBOX_HOST: str = "https://clob-sandbox.polymarket.com"


class PolymarketClient(BaseExchangeClient):
    """
    Polymarket CLOB REST client implementation.
    Includes HMAC request signing and EIP-712 order signing.
    """

    PLATFORM: Platform = Platform.POLYMARKET

    def __init__(
        self,
        api_key: str,
        secret: str,
        passphrase: str,
        wallet_private_key: str,
        host: Optional[str] = None,
        rate_limit_per_s: int = 10,
        sandbox: bool = False,
        market_id_map: Optional[Dict[str, str]] = None,
    ) -> None:
        if not api_key or not api_key.strip():
            raise ValueError("api_key must not be empty")
        if not secret or not secret.strip():
            raise ValueError("secret must not be empty")
        if not passphrase or not passphrase.strip():
            raise ValueError("passphrase must not be empty")
        if not wallet_private_key or not wallet_private_key.strip():
            raise ValueError("wallet_private_key must not be empty")

        self._api_key = api_key
        self._secret = secret
        self._passphrase = passphrase
        self._wallet_private_key = wallet_private_key
        self._sandbox = sandbox
        self._market_id_map = market_id_map or {}

        if host:
            self._host = host.rstrip("/")
        else:
            self._host = _SANDBOX_HOST if sandbox else _DEFAULT_HOST

        self._address = Account.from_key(wallet_private_key).address
        self._domain = _EIP712_DOMAIN.copy()
        if sandbox:
            self._domain["chainId"] = 80002

        self._session: Optional[aiohttp.ClientSession] = None
        self._limiter = VenueRateLimiter.for_venue("polymarket", rate_limit_per_s)
        self._throttler = self._limiter
        self._last_status_filled_usdc: Dict[str, float] = {}

        logger.info(
            "PolymarketClient initialized: host=%s, address=%s, sandbox=%s", self._host, self._address, self._sandbox
        )

    def _get_auth_headers(self, method: str, path: str, body: str = "") -> Dict[str, str]:
        """Generate HMAC-SHA256 headers for Polymarket L2 Auth."""
        timestamp = str(int(time.time() * 1000))
        message = timestamp + method.upper() + path + body

        signature = hmac.new(self._secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()

        return {
            "POLY-API-KEY": self._api_key,
            "POLY-SIGNATURE": signature,
            "POLY-TIMESTAMP": timestamp,
            "POLY-PASSPHRASE": self._passphrase,
        }

    def _sign_order(self, order: Dict[str, Any]) -> str:
        """Sign order using EIP-712."""
        types = {
            "Order": [
                {"name": "maker", "type": "address"},
                {"name": "taker", "type": "address"},
                {"name": "tokenId", "type": "uint256"},
                {"name": "makerAmount", "type": "uint256"},
                {"name": "takerAmount", "type": "uint256"},
                {"name": "expiration", "type": "uint256"},
                {"name": "nonce", "type": "uint256"},
            ]
        }

        order_to_sign = {
            "maker": self._address,
            "taker": "0x0000000000000000000000000000000000000000",
            "tokenId": int(order["tokenId"], 16) if order["tokenId"].startswith("0x") else int(order["tokenId"]),
            "makerAmount": int(order["makerAmount"]),
            "takerAmount": int(order["takerAmount"]),
            "expiration": int(order["expiration"]),
            "nonce": int(order["nonce"]),
        }

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
            "domain": self._domain,
            "primaryType": "Order",
            "message": order_to_sign,
        }

        signed = Account.sign_typed_data(self._wallet_private_key, full_message=structured_data)
        return str(signed.signature.hex())

    @async_retry(retryable_exceptions=(ConnectionError, TimeoutError, OSError, aiohttp.ClientError))
    async def place_order(
        self, submission: OrderSubmission, effective_price: float, nonce: Optional[int] = None
    ) -> PlacedOrderResponse:
        """Submit a limit order to Polymarket CLOB."""
        await self._limiter.acquire()
        tokens = round(submission.token_quantity)
        usdc_amount = round(submission.size_usdc * 1_000_000)

        if "BUY" in submission.side.value:
            maker_amount = usdc_amount
            taker_amount = tokens
        else:
            maker_amount = tokens
            taker_amount = usdc_amount

        # Use provided nonce for idempotency, fallback to timestamp
        final_nonce = nonce if nonce is not None else int(time.time() * 1000)

        order_params = {
            "maker": self._address,
            "signer": self._address,
            "tokenId": self._market_id_map.get(submission.market_id, submission.market_id),
            "makerAmount": str(maker_amount),
            "takerAmount": str(taker_amount),
            "side": "BUY" if submission.side.is_buy else "SELL",
            "expiration": str(int(time.time()) + 3600),
            "nonce": str(final_nonce),
        }

        order_params["signature"] = self._sign_order(order_params)

        payload = {"order": order_params, "owner": self._address, "orderType": "GTC"}

        body = json.dumps(payload)
        headers = self._get_auth_headers("POST", "/order", body)
        session = await self._get_session()

        async with session.post("/order", data=body, headers=headers) as resp:
            raw = await self._read_json_or_text(resp)
            if resp.status in _REJECTION_STATUS_CODES:
                raise ExchangeRejected(
                    f"Polymarket rejection: {raw.get('error', 'Unknown error')}",
                    platform=self.PLATFORM.value,
                    proposal_id=submission.proposal_id,
                    status_code=resp.status,
                    exchange_error=str(raw),
                )
            resp.raise_for_status()

            return PlacedOrderResponse(exchange_order_id=raw.get("orderID", "N/A"), status="live", fills=[])

    @async_retry(retryable_exceptions=(ConnectionError, TimeoutError, OSError, aiohttp.ClientError))
    async def cancel_order(self, exchange_order_id: str, market_id: str) -> bool:
        """Cancel an order on Polymarket."""
        await self._limiter.acquire()
        venue_market_id = self._market_id_map.get(market_id, market_id)
        payload = {"orderID": exchange_order_id, "market_id": venue_market_id}
        body = json.dumps(payload)
        headers = self._get_auth_headers("DELETE", "/order", body)
        session = await self._get_session()

        async with session.delete("/order", data=body, headers=headers) as resp:
                if resp.status == 404:
                    return True
                if resp.status in _REJECTION_STATUS_CODES:
                    logger.warning("Cancel failed for %s: %s", exchange_order_id, await resp.text())
                    return False
                resp.raise_for_status()
                return True

    @async_retry(retryable_exceptions=(ConnectionError, TimeoutError, OSError, aiohttp.ClientError))
    async def get_order_status(self, exchange_order_id: str, market_id: str) -> OrderStatusResponse:
        """Fetch status and fills for a Polymarket order."""
        await self._limiter.acquire()
        path = f"/order/{exchange_order_id}"
        headers = self._get_auth_headers("GET", path)
        session = await self._get_session()

        async with session.get(path, headers=headers) as resp:
                resp.raise_for_status()
                raw = await self._read_json_or_text(resp)

                status = raw.get("status", "").lower()
                is_filled = status == "filled"
                is_cancelled = status == "canceled"
                is_live = status == "open" or status == "partial"
                remaining = float(raw.get("remainingSize", raw.get("remaining_size", 0.0)) or 0.0)
                original = raw.get("originalSize", raw.get("size", raw.get("makerAmount")))
                filled_raw = raw.get("filledSize", raw.get("filledAmount", raw.get("filled_size")))
                if filled_raw is not None:
                    cumulative_filled = float(filled_raw)
                elif original is not None:
                    cumulative_filled = max(0.0, float(original) - remaining)
                else:
                    cumulative_filled = 0.0

                price = float(raw.get("averagePrice", raw.get("price", raw.get("limitPrice", 0.0))) or 0.0)
                raw_side = raw.get("side", "BUY")
                is_sell = raw_side.upper() == "SELL" if isinstance(raw_side, str) else raw_side == 1
                if is_sell and price > 0:
                    cumulative_filled_usdc = cumulative_filled * price
                else:
                    cumulative_filled_usdc = cumulative_filled

                new_fills = self._compute_fill_delta(exchange_order_id, cumulative_filled_usdc, price)

                return OrderStatusResponse(
                    exchange_order_id=exchange_order_id,
                    is_live=is_live,
                    is_cancelled=is_cancelled,
                    is_filled=is_filled,
                    remaining_usdc=remaining,
                    new_fills=new_fills,
                )

    @async_retry(retryable_exceptions=(ConnectionError, TimeoutError, OSError, aiohttp.ClientError))
    async def get_open_orders(self, market_ids: Optional[List[str]] = None) -> List[OpenOrder]:
        """Fetch all open orders from Polymarket CLOB."""
        await self._limiter.acquire()
        # Polymarket GET /orders returns open orders
        # Query params can include market_id
        path = "/orders"
        if market_ids and len(market_ids) == 1:
            path += f"?market_id={market_ids[0]}"

        headers = self._get_auth_headers("GET", path)
        session = await self._get_session()

        async with session.get(path, headers=headers) as resp:
                resp.raise_for_status()
                raw = await resp.json()

                orders = []
                for o in raw if isinstance(raw, list) else []:
                    orders.append(
                        OpenOrder(
                            exchange_order_id=o.get("orderID", ""),
                            market_id=o.get("tokenId", ""),
                            side=o.get("side", "BUY"),
                            size_usdc=float(o.get("originalSize", 0.0)),
                            filled_usdc=float(o.get("originalSize", 0.0)) - float(o.get("remainingSize", 0.0)),
                            limit_price=float(o.get("price", 0.0)),
                            ts=int(time.time() * 1000),
                        )
                    )
                return orders

    @async_retry(retryable_exceptions=(ConnectionError, TimeoutError, OSError, aiohttp.ClientError))
    async def get_market(self, condition_id: str) -> Optional[Dict[str, Any]]:
        """Best-effort market lookup used by the resolution monitor."""
        await self._limiter.acquire()
        session = await self._get_session()
        path = f"/markets/{condition_id}"
        headers = self._get_auth_headers("GET", path)
        async with session.get(path, headers=headers) as resp:
            if resp.status == 404:
                return None
            resp.raise_for_status()
            raw = await resp.json()
            if not isinstance(raw, dict):
                return None
            return raw

    @async_retry(retryable_exceptions=(ConnectionError, TimeoutError, OSError, aiohttp.ClientError))
    async def redeem_market(self, condition_id: str) -> bool:
        """Best-effort redemption call for a resolved market."""
        await self._limiter.acquire()
        session = await self._get_session()
        path = f"/markets/{condition_id}/redeem"
        headers = self._get_auth_headers("POST", path)
        async with session.post(path, headers=headers) as resp:
            if resp.status in (200, 202, 204, 404):
                return True
            resp.raise_for_status()
            return True

    @async_retry(retryable_exceptions=(ConnectionError, TimeoutError, OSError, aiohttp.ClientError))
    async def verify_connectivity(self) -> bool:
        """Verify API keys by fetching the account profile."""
        try:
            headers = self._get_auth_headers("GET", "/profile")
            session = await self._get_session()
            async with session.get("/profile", headers=headers) as resp:
                if resp.status == 200:
                    logger.info("Polymarket connectivity verified.")
                    return True
                logger.error("Polymarket connectivity failed: %d %s", resp.status, await resp.text())
                return False
        except Exception as exc:
            logger.error("Polymarket connectivity error: %s", exc)
            return False


if TYPE_CHECKING:
    _: ExchangeClient = PolymarketClient(api_key="", secret="", passphrase="", wallet_private_key="0x" + "0" * 64)
