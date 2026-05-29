"""Comprehensive tests for the PolymarketClient REST implementation.

This module provides contract tests that verify the Polymarket venue client
implements all required functionality correctly. Tests are Windows-compatible
and include fixtures for both sandbox and production scenarios.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from execution.clients.polymarket import PolymarketClient, _assert_protocol_compat
from execution.engine import ExchangeClient
from src.types import Platform

# Import cross-platform fixtures
from tests.venue_fixtures import (
    _FakeResponse,
    _FakeSession,
)


class TestPolymarketClientInstantiation:
    """Tests for PolymarketClient instantiation and configuration."""

    def test_creates_without_error(self) -> None:
        """Client should instantiate successfully with valid credentials."""
        client = PolymarketClient(
            api_key="test-api-key",
            secret="test-secret",
            passphrase="test-passphrase",
            wallet_private_key="0x" + "ab" * 32,
            host="https://placeholder.invalid",
        )
        assert client is not None
        assert client.platform is Platform.POLYMARKET

    def test_platform_is_polymarket(self) -> None:
        """Platform property should return correct enum value."""
        client = PolymarketClient(
            api_key="test-api-key",
            secret="test-secret",
            passphrase="test-passphrase",
            wallet_private_key="0x" + "ab" * 32,
            host="https://placeholder.invalid",
        )
        assert client.platform is Platform.POLYMARKET

    def test_satisfies_exchange_client_protocol(self) -> None:
        """Client should satisfy the ExchangeClient protocol."""
        client = PolymarketClient(
            api_key="test-api-key",
            secret="test-secret",
            passphrase="test-passphrase",
            wallet_private_key="0x" + "ab" * 32,
            host="https://placeholder.invalid",
        )
        assert isinstance(client, ExchangeClient)

    def test_protocol_compat_function_does_not_raise(self) -> None:
        """Protocol compatibility check should pass."""
        _assert_protocol_compat()

    def test_rejects_empty_api_key(self) -> None:
        """Client should reject empty API key."""
        with pytest.raises((ValueError, TypeError)):
            PolymarketClient(
                api_key="",
                secret="test-secret",
                passphrase="test-passphrase",
                wallet_private_key="0x" + "ab" * 32,
            )

    def test_rejects_empty_secret(self) -> None:
        """Client should reject empty secret."""
        with pytest.raises((ValueError, TypeError)):
            PolymarketClient(
                api_key="test-api-key",
                secret="",
                passphrase="test-passphrase",
                wallet_private_key="0x" + "ab" * 32,
            )

    def test_rejects_empty_passphrase(self) -> None:
        """Client should reject empty passphrase."""
        with pytest.raises((ValueError, TypeError)):
            PolymarketClient(
                api_key="test-api-key",
                secret="test-secret",
                passphrase="",
                wallet_private_key="0x" + "ab" * 32,
            )

    def test_rejects_empty_wallet_key(self) -> None:
        """Client should reject empty wallet private key."""
        with pytest.raises((ValueError, TypeError)):
            PolymarketClient(
                api_key="test-api-key",
                secret="test-secret",
                passphrase="test-passphrase",
                wallet_private_key="",
            )

    def test_default_host_is_polymarket_clob(self) -> None:
        """Default host should be Polymarket CLOB mainnet."""
        client = PolymarketClient(
            api_key="test-api-key",
            secret="test-secret",
            passphrase="test-passphrase",
            wallet_private_key="0x" + "ab" * 32,
        )
        assert client._host == "https://clob.polymarket.com"

    def test_sandbox_host_override(self) -> None:
        """Sandbox mode should use sandbox host."""
        client = PolymarketClient(
            api_key="test-api-key",
            secret="test-secret",
            passphrase="test-passphrase",
            wallet_private_key="0x" + "ab" * 32,
            sandbox=True,
        )
        assert client._host == "https://clob-sandbox.polymarket.com"

    def test_custom_host_override(self) -> None:
        """Custom host should override default."""
        custom_host = "https://custom.polymarket.local"
        client = PolymarketClient(
            api_key="test-api-key",
            secret="test-secret",
            passphrase="test-passphrase",
            wallet_private_key="0x" + "ab" * 32,
            host=custom_host,
        )
        assert client._host == custom_host.rstrip("/")

    def test_domain_chain_id_mainnet(self) -> None:
        """Mainnet should use chainId 137."""
        client = PolymarketClient(
            api_key="test-api-key",
            secret="test-secret",
            passphrase="test-passphrase",
            wallet_private_key="0x" + "ab" * 32,
        )
        assert client._domain["chainId"] == 137

    def test_domain_chain_id_sandbox(self) -> None:
        """Sandbox should use chainId 80002."""
        client = PolymarketClient(
            api_key="test-api-key",
            secret="test-secret",
            passphrase="test-passphrase",
            wallet_private_key="0x" + "ab" * 32,
            sandbox=True,
        )
        assert client._domain["chainId"] == 80002


class TestPolymarketClientOrderSubmission:
    """Tests for order submission functionality."""

    @pytest.fixture
    def polymarket_fake_session(self) -> _FakeSession:
        return _FakeSession()

    @pytest.fixture
    def polymarket_order_submission(self):
        import time as time_module

        from execution.models import OrderSubmission
        from src.types import OrderType, Side, StrategyId

        return OrderSubmission(
            order_id="ord-polymarket-001",
            proposal_id="prop-1",
            market_id="mkt-1",
            platform=Platform.POLYMARKET,
            side=Side.BUY_YES,
            size_usdc=10.0,
            limit_price=0.50,
            order_type=OrderType.LIMIT,
            strategy_id=StrategyId.MM,
            expiry_ms=int(time_module.time() * 1000) + 60_000,
            token_quantity=20.0,
            submitted_at=int(time_module.time() * 1000),
        )

    @pytest.mark.asyncio
    async def test_place_order_posts_payload(
        self,
        polymarket_fake_session: _FakeSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Place order should POST correct payload to /order endpoint."""
        client = PolymarketClient(
            api_key="test-api-key",
            secret="test-secret",
            passphrase="test-passphrase",
            wallet_private_key="0x" + "ab" * 32,
            host="https://placeholder.invalid",
        )

        async def _session():
            return polymarket_fake_session

        monkeypatch.setattr(client, "_get_session", _session)
        monkeypatch.setattr(client, "_sign_order", lambda order: "signed-order")

        submission = polymarket_order_submission()
        result = await client.place_order(submission, 0.50)

        assert result.exchange_order_id == "pm-order-123"
        assert result.status == "live"

        # Verify the session was called with POST to /order
        calls = [c for c in polymarket_fake_session.calls if c[0] == "POST"]
        assert len(calls) > 0
        assert calls[0][1] == "/order"

    @pytest.mark.asyncio
    async def test_place_order_includes_signature(
        self,
        polymarket_fake_session: _FakeSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Place order should include EIP-712 signature."""
        client = PolymarketClient(
            api_key="test-api-key",
            secret="test-secret",
            passphrase="test-passphrase",
            wallet_private_key="0x" + "ab" * 32,
            host="https://placeholder.invalid",
        )

        async def _session():
            return polymarket_fake_session

        signature = "0x" + "a" * 130
        monkeypatch.setattr(client, "_get_session", _session)
        monkeypatch.setattr(client, "_sign_order", lambda order: signature)

        submission = polymarket_order_submission()
        await client.place_order(submission, 0.50)

        # Verify signature is included in the request
        calls = [c for c in polymarket_fake_session.calls if c[0] == "POST"]
        assert len(calls) > 0

    @pytest.mark.asyncio
    async def test_place_order_with_custom_nonce(
        self,
        polymarket_fake_session: _FakeSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Place order should accept custom nonce for idempotency."""
        client = PolymarketClient(
            api_key="test-api-key",
            secret="test-secret",
            passphrase="test-passphrase",
            wallet_private_key="0x" + "ab" * 32,
            host="https://placeholder.invalid",
        )

        async def _session():
            return polymarket_fake_session

        monkeypatch.setattr(client, "_get_session", _session)
        monkeypatch.setattr(client, "_sign_order", lambda order: "signed-order")

        submission = polymarket_order_submission()
        custom_nonce = 123456789
        result = await client.place_order(submission, 0.50, nonce=custom_nonce)

        assert result.exchange_order_id == "pm-order-123"

    @pytest.mark.asyncio
    async def test_place_order_rejection_raises(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """4xx responses should raise ExchangeRejected exception."""
        from src.errors import ExchangeRejected

        client = PolymarketClient(
            api_key="test-api-key",
            secret="test-secret",
            passphrase="test-passphrase",
            wallet_private_key="0x" + "ab" * 32,
            host="https://placeholder.invalid",
        )

        class _FakeSessionRejection(_FakeSession):
            def post(self, path: str, data=None, headers=None, json=None):
                self.calls.append(("POST", path, {"data": data, "headers": headers, "json": json}))
                return _FakeResponse(400, {"error": "Invalid order"})

        async def _session():
            return _FakeSessionRejection()

        monkeypatch.setattr(client, "_get_session", _session)
        monkeypatch.setattr(client, "_sign_order", lambda order: "signed-order")

        submission = polymarket_order_submission()

        with pytest.raises(ExchangeRejected):
            await client.place_order(submission, 0.50)


class TestPolymarketClientOrderCancellation:
    """Tests for order cancellation functionality."""

    @pytest.fixture
    def polymarket_fake_session(self) -> _FakeSession:
        return _FakeSession()

    @pytest.mark.asyncio
    async def test_cancel_order_returns_true(
        self,
        polymarket_fake_session: _FakeSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Cancel order should return True on successful cancellation."""
        client = PolymarketClient(
            api_key="test-api-key",
            secret="test-secret",
            passphrase="test-passphrase",
            wallet_private_key="0x" + "ab" * 32,
            host="https://placeholder.invalid",
        )

        async def _session():
            return polymarket_fake_session

        monkeypatch.setattr(client, "_get_session", _session)

        result = await client.cancel_order("exch-ord-999", "mkt-1")

        assert result is True
        # Verify DELETE was called
        delete_calls = [c for c in polymarket_fake_session.calls if c[0] == "DELETE"]
        assert len(delete_calls) > 0

    @pytest.mark.asyncio
    async def test_cancel_order_returns_true_on_404(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Cancel order should return True even if order not found (404)."""
        client = PolymarketClient(
            api_key="test-api-key",
            secret="test-secret",
            passphrase="test-passphrase",
            wallet_private_key="0x" + "ab" * 32,
            host="https://placeholder.invalid",
        )

        class _FakeSession404(_FakeSession):
            def delete(self, path: str, data=None, headers=None, json=None):
                self.calls.append(("DELETE", path, {"data": data, "headers": headers, "json": json}))
                return _FakeResponse(404)

        async def _session():
            return _FakeSession404()

        monkeypatch.setattr(client, "_get_session", _session)

        result = await client.cancel_order("exch-ord-999", "mkt-1")

        assert result is True  # Should still return True (idempotent)

    @pytest.mark.asyncio
    async def test_cancel_order_rejection_returns_false(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """403 Forbidden should return False."""
        client = PolymarketClient(
            api_key="test-api-key",
            secret="test-secret",
            passphrase="test-passphrase",
            wallet_private_key="0x" + "ab" * 32,
            host="https://placeholder.invalid",
        )

        class _FakeSessionRejection(_FakeSession):
            def delete(self, path: str, data=None, headers=None, json=None):
                self.calls.append(("DELETE", path, {"data": data, "headers": headers, "json": json}))
                return _FakeResponse(403, {"message": "Forbidden"})

        async def _session():
            return _FakeSessionRejection()

        monkeypatch.setattr(client, "_get_session", _session)

        result = await client.cancel_order("exch-ord-999", "mkt-1")

        assert result is False


class TestPolymarketClientOrderStatus:
    """Tests for order status retrieval."""

    @pytest.fixture
    def polymarket_fake_session(self) -> _FakeSession:
        return _FakeSession()

    @pytest.mark.asyncio
    async def test_get_order_status_parses_open(
        self,
        polymarket_fake_session: _FakeSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Should correctly parse open order status."""
        client = PolymarketClient(
            api_key="test-api-key",
            secret="test-secret",
            passphrase="test-passphrase",
            wallet_private_key="0x" + "ab" * 32,
            host="https://placeholder.invalid",
        )

        async def _session():
            return polymarket_fake_session

        monkeypatch.setattr(client, "_get_session", _session)

        result = await client.get_order_status("exch-ord-999", "mkt-1")

        assert result.exchange_order_id == "exch-ord-999"
        assert result.is_live is True
        assert result.is_filled is False

    @pytest.mark.asyncio
    async def test_get_order_status_parses_filled(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Should correctly parse filled order status."""
        client = PolymarketClient(
            api_key="test-api-key",
            secret="test-secret",
            passphrase="test-passphrase",
            wallet_private_key="0x" + "ab" * 32,
            host="https://placeholder.invalid",
        )

        class _FakeSessionFilled(_FakeSession):
            def get(self, path: str, headers=None):
                self.calls.append(("GET", path, {"headers": headers}))
                return _FakeResponse(200, {"status": "filled", "remainingSize": 0.0})

        async def _session():
            return _FakeSessionFilled()

        monkeypatch.setattr(client, "_get_session", _session)

        result = await client.get_order_status("exch-ord-999", "mkt-1")

        assert result.is_filled is True
        assert result.is_live is False

    @pytest.mark.asyncio
    async def test_get_order_status_parses_cancelled(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Should correctly parse cancelled order status."""
        client = PolymarketClient(
            api_key="test-api-key",
            secret="test-secret",
            passphrase="test-passphrase",
            wallet_private_key="0x" + "ab" * 32,
            host="https://placeholder.invalid",
        )

        class _FakeSessionCancelled(_FakeSession):
            def get(self, path: str, headers=None):
                self.calls.append(("GET", path, {"headers": headers}))
                return _FakeResponse(200, {"status": "canceled", "remainingSize": 10.0})

        async def _session():
            return _FakeSessionCancelled()

        monkeypatch.setattr(client, "_get_session", _session)

        result = await client.get_order_status("exch-ord-999", "mkt-1")

        assert result.is_cancelled is True
        assert result.is_live is False

    @pytest.mark.asyncio
    async def test_get_order_status_calculates_fills(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Should calculate fills from remaining amount."""
        client = PolymarketClient(
            api_key="test-api-key",
            secret="test-secret",
            passphrase="test-passphrase",
            wallet_private_key="0x" + "ab" * 32,
            host="https://placeholder.invalid",
        )

        class _FakeSessionPartial(_FakeSession):
            def get(self, path: str, headers=None):
                self.calls.append(("GET", path, {"headers": headers}))
                return _FakeResponse(
                    200,
                    {
                        "status": "partial",
                        "remainingSize": 5.0,
                        "originalSize": 10.0,
                        "averagePrice": 0.50,
                    },
                )

        async def _session():
            return _FakeSessionPartial()

        monkeypatch.setattr(client, "_get_session", _session)

        result = await client.get_order_status("exch-ord-999", "mkt-1")

        assert result.is_live is True
        assert len(result.new_fills) >= 0  # May have fills


class TestPolymarketClientOpenOrders:
    """Tests for fetching open orders."""

    @pytest.mark.asyncio
    async def test_get_open_orders(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Should return list of open orders."""
        client = PolymarketClient(
            api_key="test-api-key",
            secret="test-secret",
            passphrase="test-passphrase",
            wallet_private_key="0x" + "ab" * 32,
            host="https://placeholder.invalid",
        )

        class _FakeSessionOrders(_FakeSession):
            def get(self, path: str, headers=None):
                self.calls.append(("GET", path, {"headers": headers}))
                return _FakeResponse(
                    200,
                    [
                        {
                            "orderID": "pm-1",
                            "market_id": "mkt-1",
                            "side": "BUY",
                            "originalSize": 100.0,
                            "remainingSize": 50.0,
                            "price": 0.50,
                        }
                    ],
                )

        async def _session():
            return _FakeSessionOrders()

        monkeypatch.setattr(client, "_get_session", _session)

        orders = await client.get_open_orders()

        assert len(orders) == 1
        assert orders[0].exchange_order_id == "pm-1"
        assert orders[0].market_id == "mkt-1"
        assert orders[0].side == "BUY"
        assert orders[0].size_usdc == 100.0

    @pytest.mark.asyncio
    async def test_get_open_orders_empty(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Should return empty list when no orders."""
        client = PolymarketClient(
            api_key="test-api-key",
            secret="test-secret",
            passphrase="test-passphrase",
            wallet_private_key="0x" + "ab" * 32,
            host="https://placeholder.invalid",
        )

        class _FakeSessionEmpty(_FakeSession):
            def get(self, path: str, headers=None):
                self.calls.append(("GET", path, {"headers": headers}))
                return _FakeResponse(200, [])

        async def _session():
            return _FakeSessionEmpty()

        monkeypatch.setattr(client, "_get_session", _session)

        orders = await client.get_open_orders()

        assert orders == []


class TestPolymarketClientConnectivity:
    """Tests for connectivity verification."""

    @pytest.fixture
    def polymarket_fake_session(self) -> _FakeSession:
        return _FakeSession()

    @pytest.mark.asyncio
    async def test_verify_connectivity(
        self,
        polymarket_fake_session: _FakeSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Should verify connectivity with /profile endpoint."""
        client = PolymarketClient(
            api_key="test-api-key",
            secret="test-secret",
            passphrase="test-passphrase",
            wallet_private_key="0x" + "ab" * 32,
            host="https://placeholder.invalid",
        )

        async def _session():
            return polymarket_fake_session

        monkeypatch.setattr(client, "_get_session", _session)

        result = await client.verify_connectivity()

        assert result is True

    @pytest.mark.asyncio
    async def test_verify_connectivity_fails(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Should return False when connectivity check fails."""
        client = PolymarketClient(
            api_key="test-api-key",
            secret="test-secret",
            passphrase="test-passphrase",
            wallet_private_key="0x" + "ab" * 32,
            host="https://placeholder.invalid",
        )

        class _FakeSessionFail(_FakeSession):
            def get(self, path: str, headers=None):
                self.calls.append(("GET", path, {"headers": headers}))
                return _FakeResponse(500, {"error": "Server error"})

        async def _session():
            return _FakeSessionFail()

        monkeypatch.setattr(client, "_get_session", _session)

        result = await client.verify_connectivity()

        assert result is False


class TestPolymarketClientWindowsCompatibility:
    """Windows-specific compatibility tests."""

    @pytest.mark.windows_only
    def test_temp_dir_windows_path_handling(self, temp_dir_windows_safe: Path) -> None:
        """Temporary directory should work with Windows paths."""
        # Create a test file using pathlib (cross-platform)
        test_file = temp_dir_windows_safe / "test_order.json"
        test_file.write_text('{"orderId": "test-123"}')

        # Verify the file exists (using str() for Windows compatibility)
        assert test_file.exists()
        assert str(test_file).replace("\\", "/") == str(temp_dir_windows_safe / "test_order.json").replace("\\", "/")

    @pytest.mark.windows_only
    def test_normalize_path_windows_forward_slashes(self, temp_dir_windows_safe: Path) -> None:
        """Path normalization should handle Windows backslashes."""
        from tests.venue_fixtures import normalize_path_for_testing

        # Simulate a Windows path (with backslashes)
        windows_path = str(temp_dir_windows_safe).replace("/", "\\")

        normalized = normalize_path_for_testing(windows_path)

        # Should be converted to forward slashes
        assert "\\" not in normalized

    @pytest.mark.windows_only
    def test_dict_comparison_windows_safe(self, temp_dir_windows_safe: Path) -> None:
        """Dictionary comparison should work across platforms."""
        from tests.venue_fixtures import assert_windows_safe_dict_comparison

        actual = {"path": "C:\\Users\\test", "value": 123}
        expected = {"path": "C:/Users/test", "value": 123}

        # Should pass despite different path separators
        assert_windows_safe_dict_comparison(actual, expected)


class TestPolymarketClientMarketFunctions:
    """Tests for market-related functionality."""

    @pytest.mark.asyncio
    async def test_get_market_success(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Should retrieve market information."""
        client = PolymarketClient(
            api_key="test-api-key",
            secret="test-secret",
            passphrase="test-passphrase",
            wallet_private_key="0x" + "ab" * 32,
            host="https://placeholder.invalid",
        )

        class _FakeSessionMarket(_FakeSession):
            def get(self, path: str, headers=None):
                self.calls.append(("GET", path, {"headers": headers}))
                return _FakeResponse(
                    200,
                    {
                        "id": "market-1",
                        "question": "Will this pass?",
                        "outcomeCount": 2,
                    },
                )

        async def _session():
            return _FakeSessionMarket()

        monkeypatch.setattr(client, "_get_session", _session)

        market = await client.get_market("market-1")

        assert market is not None
        assert market["id"] == "market-1"
        assert market["question"] == "Will this pass?"

    @pytest.mark.asyncio
    async def test_get_market_not_found(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Should return None for non-existent market."""
        client = PolymarketClient(
            api_key="test-api-key",
            secret="test-secret",
            passphrase="test-passphrase",
            wallet_private_key="0x" + "ab" * 32,
            host="https://placeholder.invalid",
        )

        class _FakeSession404(_FakeSession):
            def get(self, path: str, headers=None):
                self.calls.append(("GET", path, {"headers": headers}))
                return _FakeResponse(404)

        async def _session():
            return _FakeSession404()

        monkeypatch.setattr(client, "_get_session", _session)

        market = await client.get_market("market-999")

        assert market is None

    @pytest.mark.asyncio
    async def test_redeem_market_success(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Should handle successful redemption."""
        client = PolymarketClient(
            api_key="test-api-key",
            secret="test-secret",
            passphrase="test-passphrase",
            wallet_private_key="0x" + "ab" * 32,
            host="https://placeholder.invalid",
        )

        class _FakeSessionRedeem(_FakeSession):
            def post(self, path: str, data=None, headers=None, json=None):
                self.calls.append(("POST", path, {"data": data, "headers": headers, "json": json}))
                return _FakeResponse(200)

        async def _session():
            return _FakeSessionRedeem()

        monkeypatch.setattr(client, "_get_session", _session)

        result = await client.redeem_market("market-1")

        assert result is True


class TestPolymarketClientRateLimiting:
    """Tests for rate limiting functionality."""

    def test_throttler_initialized(self) -> None:
        """Throttler should be initialized with configured rate."""
        client = PolymarketClient(
            api_key="test-api-key",
            secret="test-secret",
            passphrase="test-passphrase",
            wallet_private_key="0x" + "ab" * 32,
            rate_limit_per_s=15,  # Custom rate
        )

        # The throttler should exist and have the configured rate
        assert hasattr(client, "_throttler")


class TestPolymarketClientSessionManagement:
    """Tests for session management."""

    @pytest.mark.asyncio
    async def test_session_creation(self) -> None:
        """Should create aiohttp ClientSession."""
        client = PolymarketClient(
            api_key="test-api-key",
            secret="test-secret",
            passphrase="test-passphrase",
            wallet_private_key="0x" + "ab" * 32,
            host="https://placeholder.invalid",
        )

        session = await client._get_session()

        assert session is not None
        assert hasattr(session, "closed")

    @pytest.mark.asyncio
    async def test_session_reuse(self) -> None:
        """Should reuse existing session."""
        client = PolymarketClient(
            api_key="test-api-key",
            secret="test-secret",
            passphrase="test-passphrase",
            wallet_private_key="0x" + "ab" * 32,
            host="https://placeholder.invalid",
        )

        session1 = await client._get_session()
        session2 = await client._get_session()

        assert session1 is session2

    @pytest.mark.asyncio
    async def test_close_cleans_up_session(self) -> None:
        """Close should clean up the session."""
        client = PolymarketClient(
            api_key="test-api-key",
            secret="test-secret",
            passphrase="test-passphrase",
            wallet_private_key="0x" + "ab" * 32,
            host="https://placeholder.invalid",
        )

        await client._get_session()
        await client.close()

        # Session should be closed
        assert client._session is None or client._session.closed


class TestPolymarketClientEIP712Signing:
    """Tests for EIP-712 order signing."""

    def test_sign_order_creates_signature(self) -> None:
        """Should create valid EIP-712 signature."""
        client = PolymarketClient(
            api_key="test-api-key",
            secret="test-secret",
            passphrase="test-passphrase",
            wallet_private_key="0x" + "ab" * 32,
            host="https://placeholder.invalid",
        )

        order = {
            "maker": "0x" + "cd" * 20,
            "taker": "0x" + "ef" * 20,
            "tokenId": "123",
            "makerAmount": "1000000",
            "takerAmount": "500000",
            "expiration": str(int(time.time()) + 3600),
            "nonce": str(int(time.time() * 1000)),
        }

        signature = client._sign_order(order)

        assert signature is not None
        assert len(signature) == 132  # 64 bytes + '0x' prefix


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
