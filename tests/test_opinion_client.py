"""Tests for the OpinionClient REST implementation."""
from __future__ import annotations

import time

import pytest

from execution.clients.opinion import OpinionClient
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
        return _FakeResponse(200, {"orderId": "op-order-123"})

    def delete(self, path: str, data=None, headers=None, json=None):
        self.calls.append(("DELETE", path, {"data": data, "headers": headers, "json": json}))
        return _FakeResponse(200, {})

    def get(self, path: str, headers=None):
        self.calls.append(("GET", path, {"headers": headers}))
        if path == "/orders/open":
            return _FakeResponse(200, [])
        if path.startswith("/order/"):
            return _FakeResponse(200, {"status": "open", "remainingAmount": 15.0, "price": 0.50})
        return _FakeResponse(200, [])


@pytest.fixture()
def client() -> OpinionClient:
    return OpinionClient(
        api_key="test-api-key",
        wallet_private_key="0x" + "ab" * 32,
        ctf_exchange_addr="0x1234567890123456789012345678901234567890",
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
        platform=Platform.OPINION,
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
    def test_creates_without_error(self, client: OpinionClient) -> None:
        assert client is not None

    def test_platform_is_opinion(self, client: OpinionClient) -> None:
        assert client.platform is Platform.OPINION

    def test_satisfies_exchange_client_protocol(self, client: OpinionClient) -> None:
        assert isinstance(client, ExchangeClient)

    def test_rejects_null_contract_address(self) -> None:
        with pytest.raises(ValueError, match="ctf_exchange_addr"):
            OpinionClient(
                api_key="test",
                wallet_private_key="0x" + "ab" * 32,
                ctf_exchange_addr="0x0000000000000000000000000000000000000000",
            )

    def test_rejects_empty_contract_address(self) -> None:
        with pytest.raises(ValueError, match="ctf_exchange_addr"):
            OpinionClient(
                api_key="test",
                wallet_private_key="0x" + "ab" * 32,
                ctf_exchange_addr="",
            )


class TestRequests:
    @pytest.mark.asyncio
    async def test_place_order_posts_payload(
        self,
        client: OpinionClient,
        fake_session: _FakeSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def _session():
            return fake_session

        monkeypatch.setattr(client, "_get_session", _session)
        monkeypatch.setattr(client, "_sign_order", lambda order: "signed-order")

        result = await client.place_order(_submission(), 0.50)

        assert result.exchange_order_id == "op-order-123"
        assert result.status == "live"
        assert fake_session.calls[0][0] == "POST"
        assert fake_session.calls[0][1] == "/order"

    @pytest.mark.asyncio
    async def test_cancel_order_returns_true(
        self,
        client: OpinionClient,
        fake_session: _FakeSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def _session():
            return fake_session

        monkeypatch.setattr(client, "_get_session", _session)

        assert await client.cancel_order("exch-ord-999", "mkt-1") is True
        assert fake_session.calls[0][0] == "DELETE"

    @pytest.mark.asyncio
    async def test_cancel_order_returns_true_on_404(
        self,
        client: OpinionClient,
        fake_session: _FakeSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class _FakeSession404(_FakeSession):
            def delete(self, path: str, data=None, headers=None, json=None):
                self.calls.append(("DELETE", path, {"data": data, "headers": headers, "json": json}))
                return _FakeResponse(404)

        async def _session():
            return _FakeSession404()

        monkeypatch.setattr(client, "_get_session", _session)

        assert await client.cancel_order("exch-ord-999", "mkt-1") is True

    @pytest.mark.asyncio
    async def test_get_order_status_parses_open(
        self,
        client: OpinionClient,
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
        assert result.remaining_usdc == 15.0

    @pytest.mark.asyncio
    async def test_get_order_status_parses_filled(
        self,
        client: OpinionClient,
        fake_session: _FakeSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class _FakeSessionFilled(_FakeSession):
            def get(self, path: str, headers=None):
                self.calls.append(("GET", path, {"headers": headers}))
                return _FakeResponse(200, {"status": "filled", "remainingAmount": 0.0})

        async def _session():
            return _FakeSessionFilled()

        monkeypatch.setattr(client, "_get_session", _session)

        result = await client.get_order_status("exch-ord-999", "mkt-1")

        assert result.is_filled is True
        assert result.is_live is False

    @pytest.mark.asyncio
    async def test_get_order_status_parses_cancelled(
        self,
        client: OpinionClient,
        fake_session: _FakeSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class _FakeSessionCancelled(_FakeSession):
            def get(self, path: str, headers=None):
                self.calls.append(("GET", path, {"headers": headers}))
                return _FakeResponse(200, {"status": "canceled", "remainingAmount": 10.0})

        async def _session():
            return _FakeSessionCancelled()

        monkeypatch.setattr(client, "_get_session", _session)

        result = await client.get_order_status("exch-ord-999", "mkt-1")

        assert result.is_cancelled is True
        assert result.is_live is False

    @pytest.mark.asyncio
    async def test_get_open_orders(
        self,
        client: OpinionClient,
        fake_session: _FakeSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class _FakeSessionOrders(_FakeSession):
            def get(self, path: str, headers=None):
                self.calls.append(("GET", path, {"headers": headers}))
                return _FakeResponse(200, [
                    {
                        "orderId": "op-1",
                        "marketId": "mkt-1",
                        "side": 0,
                        "originalAmount": 100.0,
                        "remainingAmount": 50.0,
                        "price": 0.50,
                    }
                ])

        async def _session():
            return _FakeSessionOrders()

        monkeypatch.setattr(client, "_get_session", _session)

        orders = await client.get_open_orders()

        assert len(orders) == 1
        assert orders[0].exchange_order_id == "op-1"
        assert orders[0].market_id == "mkt-1"
        assert orders[0].side == "BUY"
        assert orders[0].size_usdc == 100.0
        assert orders[0].filled_usdc == 50.0

    @pytest.mark.asyncio
    async def test_verify_connectivity(
        self,
        client: OpinionClient,
        fake_session: _FakeSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def _session():
            return fake_session

        monkeypatch.setattr(client, "_get_session", _session)

        assert await client.verify_connectivity() is True

    @pytest.mark.asyncio
    async def test_place_order_rejection_raises(
        self,
        client: OpinionClient,
        fake_session: _FakeSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from src.errors import ExchangeRejected

        class _FakeSessionRejection(_FakeSession):
            def post(self, path: str, data=None, headers=None, json=None):
                self.calls.append(("POST", path, {"data": data, "headers": headers, "json": json}))
                return _FakeResponse(400, {"message": "Invalid order"})

        async def _session():
            return _FakeSessionRejection()

        monkeypatch.setattr(client, "_get_session", _session)

        with pytest.raises(ExchangeRejected):
            await client.place_order(_submission(), 0.50)

    @pytest.mark.asyncio
    async def test_cancel_order_rejection_returns_false(
        self,
        client: OpinionClient,
        fake_session: _FakeSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class _FakeSessionRejection(_FakeSession):
            def delete(self, path: str, data=None, headers=None, json=None):
                self.calls.append(("DELETE", path, {"data": data, "headers": headers, "json": json}))
                return _FakeResponse(403, {"message": "Forbidden"})

        async def _session():
            return _FakeSessionRejection()

        monkeypatch.setattr(client, "_get_session", _session)

        assert await client.cancel_order("exch-ord-999", "mkt-1") is False
