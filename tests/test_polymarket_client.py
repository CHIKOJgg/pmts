"""tests/test_polymarket_client.py

Minimal tests for PolymarketClient skeleton.

Scope:
  - Instantiation succeeds with dummy credentials.
  - isinstance() against ExchangeClient Protocol passes.
  - .platform returns Platform.POLYMARKET.
  - All three async methods raise NotImplementedError (placeholder guard).
  - _assert_protocol_compat() does not raise at import time.

No network calls are made; aiohttp is not patched because no session is
created during construction.
"""
from __future__ import annotations

import pytest

from execution.clients.polymarket import PolymarketClient, _assert_protocol_compat
from execution.engine import ExchangeClient
from src.types import Platform


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture()
def client() -> PolymarketClient:
    """A PolymarketClient constructed with dummy credentials (no I/O)."""
    return PolymarketClient(
        api_key="test-api-key",
        secret="test-secret",
        passphrase="test-passphrase",
        wallet_private_key="0x" + "ab" * 32,
        host="https://placeholder.invalid",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Construction & protocol
# ─────────────────────────────────────────────────────────────────────────────

class TestInstantiation:
    def test_creates_without_error(self, client: PolymarketClient) -> None:
        assert client is not None

    def test_no_session_before_first_call(self, client: PolymarketClient) -> None:
        """Session must be lazily created, not at construction time."""
        assert client._session is None

    def test_platform_is_polymarket(self, client: PolymarketClient) -> None:
        assert client.platform is Platform.POLYMARKET

    def test_satisfies_exchange_client_protocol(self, client: PolymarketClient) -> None:
        """Runtime isinstance check against the Protocol must pass."""
        assert isinstance(client, ExchangeClient), (
            "PolymarketClient does not satisfy ExchangeClient protocol"
        )

    def test_protocol_compat_function_does_not_raise(self) -> None:
        """Module-level guard must pass cleanly."""
        _assert_protocol_compat()   # would raise TypeError if signatures diverged


# ─────────────────────────────────────────────────────────────────────────────
# Placeholder methods raise NotImplementedError (network calls)
# ─────────────────────────────────────────────────────────────────────────────

class TestPlaceholderMethods:
    """
    All three interface methods are intentional NotImplementedError stubs.
    These tests confirm the stubs are wired up and will surface clearly if
    the engine tries to call them before real API details are filled in.
    """

    @pytest.mark.asyncio
    async def test_place_order_raises(self, client: PolymarketClient, monkeypatch) -> None:
        # Prevent real session creation so the test fails fast on the right line.
        async def _fake_session(self_):   # noqa: N803
            raise NotImplementedError("TODO: place_order not implemented")
        monkeypatch.setattr(PolymarketClient, "_get_session", _fake_session)

        from execution.models import OrderSubmission
        from src.types import Side, OrderType, StrategyId
        import time

        sub = OrderSubmission(
            order_id="ord-1",
            proposal_id="prop-1",
            market_id="mkt-1",
            platform=Platform.POLYMARKET,
            side=Side.BUY_YES,
            size_usdc=10.0,
            limit_price=0.50,
            order_type=OrderType.LIMIT,
            strategy_id=StrategyId.MM,
            expiry_ms=int(time.time() * 1000) + 60_000,
            token_quantity=20.0,
            submitted_at=int(time.time() * 1000),
        )

        with pytest.raises(NotImplementedError):
            await client.place_order(sub, 0.50)

    @pytest.mark.asyncio
    async def test_cancel_order_raises(self, client: PolymarketClient, monkeypatch) -> None:
        async def _fake_session(self_):
            raise NotImplementedError("TODO: cancel_order not implemented")
        monkeypatch.setattr(PolymarketClient, "_get_session", _fake_session)

        with pytest.raises(NotImplementedError):
            await client.cancel_order("exch-ord-999", "mkt-1")

    @pytest.mark.asyncio
    async def test_get_order_status_raises(self, client: PolymarketClient, monkeypatch) -> None:
        async def _fake_session(self_):
            raise NotImplementedError("TODO: get_order_status not implemented")
        monkeypatch.setattr(PolymarketClient, "_get_session", _fake_session)

        with pytest.raises(NotImplementedError):
            await client.get_order_status("exch-ord-999", "mkt-1")
