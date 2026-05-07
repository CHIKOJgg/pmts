"""Tests for the PolymarketClient REST implementation."""
from __future__ import annotations

import time

import pytest

from execution.clients.polymarket import PolymarketClient, _assert_protocol_compat
from execution.engine import ExchangeClient
from src.types import OrderType, Platform, Side, StrategyId


class _FakeResponse:
    def __init__(self, status: int, payload: dict | list | None = None, text: str = "") -> None:
        self.status = status
        self._payload = payload if payload is not None else {}
        self._text = text

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def json(self):
        return self._payload

    async def text(self):
        return self._text

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")


class _FakeSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []

    def post(self, path: str, data=None, headers=None, json=None):
        self.calls.append(("POST", path, {"data": data, "headers": headers, "json": json}))
        return _FakeResponse(200, {"orderID": "order-123"})

    def delete(self, path: str, data=None, headers=None, json=None):
        self.calls.append(("DELETE", path, {"data": data, "headers": headers, "json": json}))
        return _FakeResponse(200, {})

    def get(self, path: str, headers=None):
        self.calls.append(("GET", path, {"headers": headers}))
        if path == "/profile":
            return _FakeResponse(200, {"ok": True})
        if path.startswith("/order/"):
            return _FakeResponse(200, {"status": "open", "remainingSize": 12.5})
        return _FakeResponse(200, [])


@pytest.fixture()
def client() -> PolymarketClient:
    return PolymarketClient(
        api_key="test-api-key",
        secret="test-secret",
        passphrase="test-passphrase",
        wallet_private_key="0x" + "ab" * 32,
        host="https://placeholder.invalid",
    )


@pytest.fixture()
def fake_session() -> _FakeSession:
    return _FakeSession()


def _submission():
    from execution.models import OrderSubmission

    return OrderSubmission(
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


class TestInstantiation:
    def test_creates_without_error(self, client: PolymarketClient) -> None:
        assert client is not None

    def test_platform_is_polymarket(self, client: PolymarketClient) -> None:
        assert client.platform is Platform.POLYMARKET

    def test_satisfies_exchange_client_protocol(self, client: PolymarketClient) -> None:
        assert isinstance(client, ExchangeClient)

    def test_protocol_compat_function_does_not_raise(self) -> None:
        _assert_protocol_compat()


class TestRequests:
    @pytest.mark.asyncio
    async def test_place_order_posts_payload(
        self,
        client: PolymarketClient,
        fake_session: _FakeSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def _session():
            return fake_session

        monkeypatch.setattr(client, "_get_session", _session)
        monkeypatch.setattr(client, "_sign_order", lambda order: "signed-order")

        result = await client.place_order(_submission(), 0.50)

        assert result.exchange_order_id == "order-123"
        assert result.status == "live"
        assert fake_session.calls[0][0] == "POST"
        assert fake_session.calls[0][1] == "/order"

    @pytest.mark.asyncio
    async def test_cancel_order_returns_true(
        self,
        client: PolymarketClient,
        fake_session: _FakeSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def _session():
            return fake_session

        monkeypatch.setattr(client, "_get_session", _session)

        assert await client.cancel_order("exch-ord-999", "mkt-1") is True
        assert fake_session.calls[0][0] == "DELETE"

    @pytest.mark.asyncio
    async def test_get_order_status_parses_open(
        self,
        client: PolymarketClient,
        fake_session: _FakeSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def _session():
            return fake_session

        monkeypatch.setattr(client, "_get_session", _session)

        result = await client.get_order_status("exch-ord-999", "mkt-1")

        assert result.exchange_order_id == "exch-ord-999"
        assert result.is_live is True
        assert result.is_filled is False
        assert result.remaining_usdc == 12.5

    @pytest.mark.asyncio
    async def test_verify_connectivity(
        self,
        client: PolymarketClient,
        fake_session: _FakeSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def _session():
            return fake_session

        monkeypatch.setattr(client, "_get_session", _session)

        assert await client.verify_connectivity() is True
