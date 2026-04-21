"""execution/clients/polymarket.py — Polymarket ExchangeClient implementation.

Status: SKELETON — all network calls are placeholders.
Auth, endpoint URLs, and payload shapes are marked TODO and must be
filled in from the official Polymarket CLOB API documentation.

Reference (public docs, as of writing):
  https://docs.polymarket.com  (endpoints may change — verify before use)
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import aiohttp

from execution.engine import (
    ExchangeClient,          # Protocol — used for isinstance() checks only
    OrderStatusFill,
    OrderStatusResponse,
    PlacedFill,
    PlacedOrderResponse,
)
from execution.models import OrderSubmission
from src.errors import ExchangeRejected
from src.types import Platform

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Constants — ALL are TODO until the real API docs are reviewed
# ─────────────────────────────────────────────────────────────────────────────

# TODO: Replace with the verified Polymarket CLOB REST base URL.
#       Prod: "https://clob.polymarket.com"  (unconfirmed — must verify)
#       Gamma/staging: unknown — check docs.
_DEFAULT_HOST: str = "TODO_POLYMARKET_CLOB_HOST"

# TODO: Confirm the exact path for each endpoint from official docs.
_ENDPOINT_PLACE_ORDER:  str = "/TODO/orders"           # POST
_ENDPOINT_CANCEL_ORDER: str = "/TODO/orders/{order_id}/cancel"   # DELETE or POST
_ENDPOINT_ORDER_STATUS: str = "/TODO/orders/{order_id}"          # GET

# HTTP 4xx codes that signal a hard business rejection (not a transient error).
# TODO: Validate the exact set from Polymarket's error reference.
_REJECTION_STATUS_CODES: frozenset[int] = frozenset({400, 401, 403, 422})


# ─────────────────────────────────────────────────────────────────────────────
# PolymarketClient
# ─────────────────────────────────────────────────────────────────────────────

class PolymarketClient:
    """
    Polymarket CLOB REST client satisfying the :class:`ExchangeClient` protocol.

    Authentication uses API-key + HMAC signing over the Polymarket CLOB API.
    The wallet private key is required for EIP-712 order signing on-chain.

    All three coroutines raise ``NotImplementedError`` until their API call
    bodies are filled in.  The ``ExchangeRejected`` raise-sites show the exact
    shape the engine expects.

    Parameters
    ----------
    api_key:
        Polymarket CLOB API key.
    secret:
        HMAC secret for request signing.  TODO: confirm signing scheme.
    passphrase:
        Passphrase associated with the API key.  TODO: confirm if required.
    wallet_private_key:
        EVM private key used to sign on-chain orders (EIP-712).
        TODO: confirm whether the REST layer requires this or if signing
        happens client-side only before submission.
    host:
        Override the base REST host, e.g. for staging.
    """

    PLATFORM: Platform = Platform.POLYMARKET

    def __init__(
        self,
        api_key:             str,
        secret:              str,
        passphrase:          str,
        wallet_private_key:  str,
        host:                str = _DEFAULT_HOST,
    ) -> None:
        self._api_key            = api_key
        self._secret             = secret
        self._passphrase         = passphrase
        self._wallet_private_key = wallet_private_key
        self._host               = host.rstrip("/")

        # Lazily created aiohttp session; closed in close().
        self._session: Optional[aiohttp.ClientSession] = None

        # TODO: If Polymarket's SDK/library is used (e.g. py-clob-client),
        #       initialise it here and store on self._clob.

        logger.debug(
            "PolymarketClient created (host=%s, api_key=%.8s…)",
            self._host, self._api_key,
        )

    # ── ExchangeClient protocol property ─────────────────────────────────────

    @property
    def platform(self) -> Platform:
        return self.PLATFORM

    # ── Session management ────────────────────────────────────────────────────

    async def _get_session(self) -> aiohttp.ClientSession:
        """
        Return a lazily-initialised aiohttp session with auth headers attached.

        TODO: Replace the header values below with the real Polymarket
        authentication scheme (HMAC headers, API-key header names, etc.).
        Confirm from the official API docs which headers are required on every
        request.
        """
        if self._session is None or self._session.closed:
            headers: Dict[str, str] = {
                # TODO: Confirm exact header names from Polymarket CLOB docs.
                "POLY-API-KEY":    self._api_key,
                "POLY-PASSPHRASE": self._passphrase,
                # TODO: Add HMAC signature headers (POLY-SIGNATURE, POLY-TIMESTAMP).
                #       The signature is computed per-request from the secret,
                #       so it cannot be placed here at session level.
                "Content-Type": "application/json",
            }
            self._session = aiohttp.ClientSession(
                base_url=self._host,
                headers=headers,
            )
            logger.debug("PolymarketClient: new aiohttp session created")
        return self._session

    async def close(self) -> None:
        """Gracefully close the underlying aiohttp session."""
        if self._session and not self._session.closed:
            await self._session.close()
            logger.debug("PolymarketClient: session closed")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _build_order_payload(
        self, submission: OrderSubmission, effective_price: float
    ) -> Dict[str, Any]:
        """
        Map an :class:`OrderSubmission` to the Polymarket CLOB POST body.

        TODO: Fill in the exact field names from the official docs.
              Polymarket orders are typically EIP-712 signed structs; the
              signing step (wallet_private_key) also goes here.

        Known unknowns
        --------------
        - Exact JSON key names for price, size, side, token ID.
        - Whether ``token_quantity`` or ``size_usdc`` is the primary size field.
        - The EIP-712 struct shape for on-chain signing.
        - Whether ``proposal_id`` maps to ``clientOrderId`` or similar.
        """
        return {
            # TODO: Replace keys with real Polymarket field names.
            "TODO_market_id":    submission.market_id,
            "TODO_side":         submission.side.value,
            "TODO_price":        effective_price,
            "TODO_size":         submission.token_quantity,
            "TODO_order_type":   submission.order_type.value,
            "TODO_client_id":    submission.proposal_id,
            # TODO: Add EIP-712 signature block.
        }

    @staticmethod
    def _parse_placed_response(raw: Dict[str, Any]) -> PlacedOrderResponse:
        """
        Parse the raw JSON from a successful POST order into a
        :class:`PlacedOrderResponse`.

        TODO: Map real field names once API docs are reviewed.

        Known unknowns
        --------------
        - Field name for exchange-assigned order ID.
        - Field name for order status string ("live" / "matched" / etc.).
        - Whether immediate fills are included in the placement response.
        - tx_hash field name (on-chain hash, if applicable).
        """
        return PlacedOrderResponse(
            exchange_order_id=raw.get("TODO_order_id", "UNKNOWN"),   # TODO
            status=raw.get("TODO_status", "live"),                    # TODO
            fills=[
                PlacedFill(
                    fill_usdc=f.get("TODO_usdc",   0.0),   # TODO
                    fill_price=f.get("TODO_price", 0.0),   # TODO
                    fill_tokens=f.get("TODO_size", 0.0),   # TODO
                    ts=f.get("TODO_ts",            0),     # TODO
                )
                for f in raw.get("TODO_fills", [])         # TODO
            ],
            tx_hash=raw.get("TODO_tx_hash"),               # TODO
        )

    @staticmethod
    def _parse_status_response(raw: Dict[str, Any]) -> OrderStatusResponse:
        """
        Parse the raw JSON from a GET order status into an
        :class:`OrderStatusResponse`.

        TODO: Map real field names once API docs are reviewed.

        Known unknowns
        --------------
        - Field names for is_live, is_cancelled, is_filled.
        - Field name for remaining USDC / remaining size.
        - Shape of partial fill entries in the response.
        """
        return OrderStatusResponse(
            exchange_order_id=raw.get("TODO_order_id", "UNKNOWN"),    # TODO
            is_live=raw.get("TODO_is_live", False),                    # TODO
            is_cancelled=raw.get("TODO_is_cancelled", False),          # TODO
            is_filled=raw.get("TODO_is_filled", False),                # TODO
            remaining_usdc=raw.get("TODO_remaining_usdc", 0.0),        # TODO
            new_fills=[
                OrderStatusFill(
                    fill_usdc=f.get("TODO_usdc",   0.0),   # TODO
                    fill_price=f.get("TODO_price", 0.0),   # TODO
                    fill_tokens=f.get("TODO_size", 0.0),   # TODO
                    ts=f.get("TODO_ts",            0),     # TODO
                )
                for f in raw.get("TODO_fills", [])         # TODO
            ],
            tx_hash=raw.get("TODO_tx_hash"),               # TODO
        )

    # ── ExchangeClient methods ────────────────────────────────────────────────

    async def place_order(
        self, submission: OrderSubmission, effective_price: float
    ) -> PlacedOrderResponse:
        """
        Submit a limit order to the Polymarket CLOB.

        Engine contract
        ---------------
        - Return :class:`PlacedOrderResponse` on success.
        - Raise :class:`ExchangeRejected` on 4xx hard-rejection (engine will
          NOT retry).
        - Raise any other ``Exception`` on transient failure (engine WILL retry
          up to ``MAX_SUBMIT_ATTEMPTS`` with exponential back-off).

        TODO checklist
        --------------
        1. Implement ``_build_order_payload`` field mapping.
        2. Add per-request HMAC signature headers (computed from secret +
           timestamp + body hash — exact scheme unknown, check docs).
        3. Implement EIP-712 signing with ``wallet_private_key`` if required.
        4. Confirm the correct HTTP method and endpoint path.
        5. Map the response JSON fields in ``_parse_placed_response``.
        """
        logger.debug(
            "place_order: proposal=%s market=%s side=%s price=%s size_usdc=%s",
            submission.proposal_id[:8], submission.market_id,
            submission.side.value, effective_price, submission.size_usdc,
        )

        session = await self._get_session()
        payload = self._build_order_payload(submission, effective_price)

        # TODO: Add per-request auth signature headers here before the call.
        # TODO: Replace `_ENDPOINT_PLACE_ORDER` with the verified path.
        async with session.post(_ENDPOINT_PLACE_ORDER, json=payload) as resp:
            raw: Dict[str, Any] = await resp.json()

            if resp.status in _REJECTION_STATUS_CODES:
                raise ExchangeRejected(
                    f"Polymarket rejected order: HTTP {resp.status}",
                    platform=self.PLATFORM.value,
                    proposal_id=submission.proposal_id,
                    status_code=resp.status,
                    exchange_error=str(raw),   # TODO: extract real error field
                )

            resp.raise_for_status()   # surface unexpected 5xx as plain Exception

            return self._parse_placed_response(raw)

    async def cancel_order(self, exchange_order_id: str, market_id: str) -> bool:
        """
        Request cancellation of an active Polymarket order.

        Engine contract
        ---------------
        - Return ``True`` if the order is confirmed cancelled or not found.
        - Return ``False`` if the exchange reports it cannot be cancelled
          (e.g. already matched).
        - Raise on transient failure; the engine swallows it with a warning.

        TODO checklist
        --------------
        1. Confirm HTTP method (DELETE vs POST) and endpoint path.
        2. Add per-request auth signature headers.
        3. Determine what "not found" (404) should return — True is safest.
        4. Map the response status field to a bool.
        """
        logger.debug(
            "cancel_order: exchange_order_id=%s market=%s",
            exchange_order_id, market_id,
        )

        session  = await self._get_session()
        endpoint = _ENDPOINT_CANCEL_ORDER.format(order_id=exchange_order_id)

        # TODO: Add per-request auth signature headers here.
        # TODO: Confirm method — Polymarket may use DELETE or a POST to a
        #       /cancel sub-path.  Verify from docs.
        async with session.delete(endpoint) as resp:
            if resp.status == 404:
                # Order not found — treat as already gone → True
                logger.debug("cancel_order: 404 for %s — treating as cancelled", exchange_order_id)
                return True

            if resp.status in _REJECTION_STATUS_CODES:
                # Hard rejection (e.g. already matched) — cannot cancel
                logger.warning(
                    "cancel_order: non-cancellable order %s — HTTP %s",
                    exchange_order_id, resp.status,
                )
                return False

            resp.raise_for_status()

            raw: Dict[str, Any] = await resp.json()
            # TODO: Map real success field name from docs.
            #       e.g. return raw.get("TODO_cancelled", False)
            return bool(raw.get("TODO_cancelled", True))   # TODO

    async def get_order_status(
        self, exchange_order_id: str, market_id: str
    ) -> OrderStatusResponse:
        """
        Fetch the current state and any new fills for a live order.

        Engine contract
        ---------------
        - Return :class:`OrderStatusResponse` always.
        - Raise on transient failures; engine swallows poll errors with DEBUG.

        TODO checklist
        --------------
        1. Confirm endpoint path and HTTP method.
        2. Add per-request auth signature headers.
        3. Map response JSON fields in ``_parse_status_response``.
        4. Confirm how Polymarket reports partial fills in the status payload.
        """
        logger.debug(
            "get_order_status: exchange_order_id=%s market=%s",
            exchange_order_id, market_id,
        )

        session  = await self._get_session()
        endpoint = _ENDPOINT_ORDER_STATUS.format(order_id=exchange_order_id)

        # TODO: Add per-request auth signature headers here.
        async with session.get(endpoint) as resp:
            resp.raise_for_status()
            raw: Dict[str, Any] = await resp.json()
            return self._parse_status_response(raw)


# ─────────────────────────────────────────────────────────────────────────────
# Protocol compatibility assertion (checked at import time in dev/test builds)
# ─────────────────────────────────────────────────────────────────────────────

def _assert_protocol_compat() -> None:
    """
    Raises TypeError at import time if PolymarketClient stops satisfying
    the ExchangeClient runtime-checkable Protocol.
    """
    dummy = PolymarketClient.__new__(PolymarketClient)
    if not isinstance(dummy, ExchangeClient):
        raise TypeError(  # pragma: no cover
            "PolymarketClient no longer satisfies ExchangeClient protocol — "
            "check method signatures."
        )


_assert_protocol_compat()
