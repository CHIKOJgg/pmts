import hashlib
import hmac
import json
import time
import logging
from typing import Any, Dict, List, Optional, TYPE_CHECKING

import aiohttp
from asyncio_throttle import Throttler
from eth_account import Account

from execution.engine import (
    ExchangeClient,
    OpenOrder,
    OrderStatusResponse,
    PlacedOrderResponse,
)
from execution.models import OrderSubmission
from src.errors import ExchangeRejected
from src.types import Platform

logger = logging.getLogger(__name__)

# Polymarket CLOB Constants
_DEFAULT_HOST: str = "https://clob.polymarket.com"
_REJECTION_STATUS_CODES: frozenset[int] = frozenset({400, 401, 403, 422})

# EIP-712 Domain for Polymarket
_EIP712_DOMAIN = {
    "name": "Polymarket CLOB",
    "version": "1",
    "chainId": 137,
    "verifyingContract": "0x4bFb9717C5521096C6E419519A619717C5521096" 
}

_SANDBOX_HOST: str = "https://clob-sandbox.polymarket.com"

class PolymarketClient:
    """
    Polymarket CLOB REST client implementation.
    Includes HMAC request signing and EIP-712 order signing.
    """

    PLATFORM: Platform = Platform.POLYMARKET

    def __init__(
        self,
        api_key:             str,
        secret:              str,
        passphrase:          str,
        wallet_private_key:  str,
        host:                Optional[str] = None,
        rate_limit_per_s:    int = 10,
        sandbox:             bool = False,
    ) -> None:
        self._api_key            = api_key
        self._secret             = secret
        self._passphrase         = passphrase
        self._wallet_private_key = wallet_private_key
        self._sandbox            = sandbox
        
        if host:
            self._host = host.rstrip("/")
        else:
            self._host = _SANDBOX_HOST if sandbox else _DEFAULT_HOST

        self._address            = Account.from_key(wallet_private_key).address
        
        # Update chainId for EIP-712 if sandbox (Polygon Amoy is 80002)
        self._domain = _EIP712_DOMAIN.copy()
        if sandbox:
            self._domain["chainId"] = 80002

        self._session: Optional[aiohttp.ClientSession] = None
        self._throttler = Throttler(rate_limit_per_s)

        logger.info(
            "PolymarketClient initialized: host=%s, address=%s, sandbox=%s",
            self._host, self._address, self._sandbox
        )

    @property
    def platform(self) -> Platform:
        return self.PLATFORM

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                base_url=self._host,
                headers={"Content-Type": "application/json"}
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def _get_auth_headers(self, method: str, path: str, body: str = "") -> Dict[str, str]:
        """Generate HMAC-SHA256 headers for Polymarket L2 Auth."""
        timestamp = str(int(time.time() * 1000))
        message = timestamp + method.upper() + path + body
        
        signature = hmac.new(
            self._secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()

        return {
            "POLY-API-KEY":    self._api_key,
            "POLY-SIGNATURE":  signature,
            "POLY-TIMESTAMP":  timestamp,
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
                **types
            },
            "domain": self._domain,
            "primaryType": "Order",
            "message": order_to_sign,
        }
        
        signed = Account.sign_typed_data(self._wallet_private_key, full_message=structured_data)
        return signed.signature.hex()

    async def place_order(
        self, submission: OrderSubmission, effective_price: float, nonce: Optional[int] = None
    ) -> PlacedOrderResponse:
        """Submit a limit order to Polymarket CLOB."""
        async with self._throttler:
            tokens = int(submission.token_quantity)
            usdc_amount = int(submission.size_usdc * 1_000_000) 
            
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
                "tokenId": submission.market_id,
                "makerAmount": str(maker_amount),
                "takerAmount": str(taker_amount),
                "side": "BUY" if "BUY" in submission.side.value else "SELL",
                "expiration": str(int(time.time()) + 3600),
                "nonce": str(final_nonce),
            }
            
            order_params["signature"] = self._sign_order(order_params)
            
            payload = {
                "order": order_params,
                "owner": self._address,
                "orderType": "GTC"
            }
            
            body = json.dumps(payload)
            headers = self._get_auth_headers("POST", "/order", body)
            session = await self._get_session()
            
            async with session.post("/order", data=body, headers=headers) as resp:
                raw = await resp.json()
                if resp.status in _REJECTION_STATUS_CODES:
                    raise ExchangeRejected(
                        f"Polymarket rejection: {raw.get('error', 'Unknown error')}",
                        platform=self.PLATFORM.value,
                        proposal_id=submission.proposal_id,
                        status_code=resp.status,
                        exchange_error=str(raw)
                    )
                resp.raise_for_status()
                
                return PlacedOrderResponse(
                    exchange_order_id=raw.get("orderID", "N/A"),
                    status="live",
                    fills=[] 
                )

    async def cancel_order(self, exchange_order_id: str, market_id: str) -> bool:
        """Cancel an order on Polymarket."""
        async with self._throttler:
            payload = {"orderID": exchange_order_id}
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

    async def get_order_status(
        self, exchange_order_id: str, market_id: str
    ) -> OrderStatusResponse:
        """Fetch status and fills for a Polymarket order."""
        async with self._throttler:
            path = f"/order/{exchange_order_id}"
            headers = self._get_auth_headers("GET", path)
            session = await self._get_session()
            
            async with session.get(path, headers=headers) as resp:
                resp.raise_for_status()
                raw = await resp.json()
                
                status = raw.get("status", "").lower()
                is_filled = status == "filled"
                is_cancelled = status == "canceled"
                is_live = status == "open" or status == "partial"
                
                return OrderStatusResponse(
                    exchange_order_id=exchange_order_id,
                    is_live=is_live,
                    is_cancelled=is_cancelled,
                    is_filled=is_filled,
                    remaining_usdc=float(raw.get("remainingSize", 0.0)),
                    new_fills=[] 
                )

    async def get_open_orders(self, market_ids: Optional[List[str]] = None) -> List[OpenOrder]:
        """Fetch all open orders from Polymarket CLOB."""
        async with self._throttler:
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
                
                # Polymarket returns a list of order objects
                orders = []
                for o in raw:
                    orders.append(OpenOrder(
                        exchange_order_id=o["orderID"],
                        market_id=o["tokenId"],
                        side=o["side"],
                        size_usdc=float(o.get("originalSize", 0.0)),
                        filled_usdc=float(o.get("originalSize", 0.0)) - float(o.get("remainingSize", 0.0)),
                        limit_price=float(o.get("price", 0.0)),
                        ts=int(time.time() * 1000) # Fallback ts
                    ))
                return orders

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
    _: ExchangeClient = PolymarketClient(api_key="", secret="", passphrase="", wallet_private_key="0x" + "0"*64)


def _assert_protocol_compat() -> None:
    """Import-time guard used by tests to ensure protocol shape stays aligned."""
    client = PolymarketClient(
        api_key="",
        secret="",
        passphrase="",
        wallet_private_key="0x" + "ab" * 32,
        host="https://placeholder.invalid",
    )
    if not isinstance(client, ExchangeClient):
        raise TypeError("PolymarketClient does not satisfy ExchangeClient protocol")
