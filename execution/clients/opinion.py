import time
import uuid
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

class OpinionClient:
    """
    Opinion Markets REST API Client implementation.
    Includes EIP-712 order signing.
    """

    PLATFORM: Platform = Platform.OPINION

    def __init__(
        self,
        api_key:             str,
        wallet_private_key:  str,
        ctf_exchange_addr:   str,
        host:                Optional[str] = None,
        rate_limit_per_s:    int = 5,
        sandbox:             bool = False,
    ) -> None:
        if not ctf_exchange_addr or ctf_exchange_addr == "0x0000000000000000000000000000000000000000":
            raise ValueError("ctf_exchange_addr must be a valid non-zero contract address")

        self._api_key            = api_key
        self._wallet_private_key = wallet_private_key
        self._sandbox            = sandbox
        
        if host:
            self._host = host.rstrip("/")
        else:
            self._host = _SANDBOX_HOST if sandbox else _DEFAULT_HOST

        self._address            = Account.from_key(wallet_private_key).address
        self._ctf_exchange_addr  = ctf_exchange_addr
        
        # Update chainId for EIP-712 if sandbox (BSC Testnet is 97)
        self._domain = _EIP712_DOMAIN.copy()
        if sandbox:
            self._domain["chainId"] = 97

        self._session: Optional[aiohttp.ClientSession] = None
        self._throttler = Throttler(rate_limit_per_s)

        logger.info(
            "OpinionClient initialized: host=%s, address=%s, sandbox=%s",
            self._host, self._address, self._sandbox
        )

    def _parse_market_id(self, market_id: str) -> int:
        """Parse market_id to int, supporting decimal, hex, or hashed string."""
        if market_id.isdigit():
            return int(market_id)
        if market_id.startswith("0x"):
            return int(market_id, 16)
        return int.from_bytes(market_id.encode()[:8], byteorder="big", signed=False)

    @property
    def platform(self) -> Platform:
        return self.PLATFORM

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                base_url=self._host,
                headers={
                    "Content-Type": "application/json",
                    "apikey": self._api_key
                }
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._wallet_private_key = None

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
                **types
            },
            "domain": domain,
            "primaryType": "Order",
            "message": order,
        }
        
        signed = Account.sign_typed_data(self._wallet_private_key, full_message=structured_data)
        return signed.signature.hex()

    async def place_order(
        self, submission: OrderSubmission, effective_price: float, nonce: Optional[int] = None
    ) -> PlacedOrderResponse:
        """Submit an order to Opinion Markets."""
        async with self._throttler:
            # Side mapping: 0 for Buy, 1 for Sell (Typical Opinion side mapping)
            side_val = 0 if "BUY" in submission.side.value else 1
            
            # Placeholder amounts
            tokens = int(submission.token_quantity)
            usdc_amount = int(submission.size_usdc * 1_000_000) 
            
            maker_amount, taker_amount = (usdc_amount, tokens) if side_val == 0 else (tokens, usdc_amount)

            # Use provided nonce for idempotency
            final_nonce = nonce if nonce is not None else int(time.time() * 1000)

            order_msg = {
                "salt": int(uuid.uuid4().int >> 64),
                "maker": self._address,
                "signer": self._address,
                "taker": "0x0000000000000000000000000000000000000000",
                "tokenId": self._parse_market_id(submission.market_id),
                "makerAmount": maker_amount,
                "takerAmount": taker_amount,
                "expiration": int(time.time()) + 3600,
                "nonce": final_nonce,
                "feeRateBps": 0,
                "side": side_val,
                "signatureType": 1,
            }
            
            signature = self._sign_order(order_msg)
            
            payload = {
                "marketId": submission.market_id,
                "order": order_msg,
                "signature": signature
            }
            
            session = await self._get_session()
            async with session.post("/order", json=payload) as resp:
                raw = await resp.json()
                if resp.status in _REJECTION_STATUS_CODES:
                    raise ExchangeRejected(
                        f"Opinion rejection: {raw.get('message', 'Unknown error')}",
                        platform=self.PLATFORM.value,
                        proposal_id=submission.proposal_id,
                        status_code=resp.status,
                        exchange_error=str(raw)
                    )
                resp.raise_for_status()
                
                return PlacedOrderResponse(
                    exchange_order_id=raw.get("orderId", "N/A"),
                    status="live",
                    fills=[]
                )

    async def cancel_order(self, exchange_order_id: str, market_id: str) -> bool:
        """Cancel an order on Opinion."""
        async with self._throttler:
            session = await self._get_session()
            path = f"/order/{exchange_order_id}"
            async with session.delete(path) as resp:
                if resp.status == 404:
                    return True
                if resp.status in _REJECTION_STATUS_CODES:
                    return False
                resp.raise_for_status()
                return True

    async def get_order_status(
        self, exchange_order_id: str, market_id: str
    ) -> OrderStatusResponse:
        """Fetch status for an Opinion order."""
        async with self._throttler:
            session = await self._get_session()
            path = f"/order/{exchange_order_id}"
            async with session.get(path) as resp:
                resp.raise_for_status()
                raw = await resp.json()
                
                status = raw.get("status", "").lower()
                return OrderStatusResponse(
                    exchange_order_id=exchange_order_id,
                    is_live=status in ["open", "partial"],
                    is_cancelled=status == "canceled",
                    is_filled=status == "filled",
                    remaining_usdc=float(raw.get("remainingAmount", 0.0)),
                    new_fills=[]
                )

    async def get_open_orders(self, market_ids: Optional[List[str]] = None) -> List[OpenOrder]:
        """Fetch all open orders from Opinion Markets."""
        async with self._throttler:
            session = await self._get_session()
            path = "/orders/open"
            async with session.get(path) as resp:
                resp.raise_for_status()
                raw = await resp.json()
                
                # Assume raw is a list of open orders
                orders = []
                for o in raw:
                    orders.append(OpenOrder(
                        exchange_order_id=o["orderId"],
                        market_id=o["marketId"],
                        side="BUY" if o["side"] == 0 else "SELL",
                        size_usdc=float(o.get("originalAmount", 0.0)),
                        filled_usdc=float(o.get("originalAmount", 0.0)) - float(o.get("remainingAmount", 0.0)),
                        limit_price=float(o.get("price", 0.0)),
                        ts=int(time.time() * 1000)
                    ))
                return orders

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
    _: ExchangeClient = OpinionClient(api_key="", wallet_private_key="0x" + "0"*64)
