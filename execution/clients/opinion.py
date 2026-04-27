import hmac
import hashlib
import json
import time
import uuid
import logging
from typing import Any, Dict, List, Optional

import aiohttp
from asyncio_throttle import Throttler
from eth_account import Account
from eth_account.messages import encode_typed_data

from execution.engine import (
    ExchangeClient,
    OrderStatusResponse,
    PlacedOrderResponse,
)
from execution.models import OrderSubmission
from src.errors import ExchangeRejected
from src.types import Platform, Side

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
        host:                str = _DEFAULT_HOST,
        rate_limit_per_s:    int = 5,
        ctf_exchange_addr:   str = "0x0000000000000000000000000000000000000000", # Placeholder
    ) -> None:
        self._api_key            = api_key
        self._wallet_private_key = wallet_private_key
        self._host               = host.rstrip("/")
        self._address            = Account.from_key(wallet_private_key).address
        self._ctf_exchange_addr  = ctf_exchange_addr
        
        self._session: Optional[aiohttp.ClientSession] = None
        self._throttler = Throttler(rate_limit_per_s)

        logger.info(
            "OpinionClient initialized: host=%s, address=%s",
            self._host, self._address
        )

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

        domain = _EIP712_DOMAIN.copy()
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
        self, submission: OrderSubmission, effective_price: float
    ) -> PlacedOrderResponse:
        """Submit an order to Opinion Markets."""
        async with self._throttler:
            # Side mapping: 0 for Buy, 1 for Sell (Typical Opinion side mapping)
            side_val = 0 if "BUY" in submission.side.value else 1
            
            # Placeholder amounts
            tokens = int(submission.token_quantity)
            usdc_amount = int(submission.size_usdc * 1_000_000) 
            
            maker_amount, taker_amount = (usdc_amount, tokens) if side_val == 0 else (tokens, usdc_amount)

            order_msg = {
                "salt": int(uuid.uuid4().int >> 64),
                "maker": self._address,
                "signer": self._address,
                "taker": "0x0000000000000000000000000000000000000000",
                "tokenId": int(submission.market_id) if submission.market_id.isdigit() else int(submission.market_id, 16),
                "makerAmount": maker_amount,
                "takerAmount": taker_amount,
                "expiration": int(time.time()) + 3600,
                "nonce": int(time.time() * 1000),
                "feeRateBps": 0, # Placeholder
                "side": side_val,
                "signatureType": 1, # EIP-712
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

def _assert_protocol_compat() -> None:
    dummy = OpinionClient.__new__(OpinionClient)
    if not isinstance(dummy, ExchangeClient):
        raise TypeError("OpinionClient does not satisfy ExchangeClient protocol")

_assert_protocol_compat()
