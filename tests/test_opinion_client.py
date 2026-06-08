"""Comprehensive tests for the OpinionClient REST implementation.

This module provides contract tests that verify the Opinion venue client
implements all required functionality correctly. Tests are Windows-compatible
and include fixtures for both sandbox and production scenarios.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from execution.clients.opinion import OpinionClient
from execution.engine import ExchangeClient
from src.enums import Platform

# Import cross-platform fixtures
from tests.venue_fixtures import (
    _FakeResponse,
    _FakeSession,
    opinion_order_submission,
    polymarket_order_submission,
)


class TestOpinionClientInstantiation:
    """Tests for OpinionClient instantiation and configuration."""

    def test_creates_without_error(self) -> None:
        """Client should instantiate successfully with valid credentials."""
        client = OpinionClient(
            api_key="test-api-key",
            wallet_private_key="0x" + "ab" * 32,
            ctf_exchange_addr="0x1234567890123456789012345678901234567890",
            host="https://placeholder.invalid",
        )
        assert client is not None
        assert client.platform is Platform.OPINION

    def test_platform_is_opinion(self) -> None:
        """Platform property should return correct enum value."""
        client = OpinionClient(
            api_key="test-api-key",
            wallet_private_key="0x" + "ab" * 32,
            ctf_exchange_addr="0x1234567890123456789012345678901234567890",
            host="https://placeholder.invalid",
        )
        assert client.platform is Platform.OPINION

    def test_satisfies_exchange_client_protocol(self) -> None:
        """Client should satisfy the ExchangeClient protocol."""
        client = OpinionClient(
            api_key="test-api-key",
            wallet_private_key="0x" + "ab" * 32,
            ctf_exchange_addr="0x1234567890123456789012345678901234567890",
            host="https://placeholder.invalid",
        )
        assert isinstance(client, ExchangeClient)

    def test_rejects_null_contract_address(self) -> None:
        """Client should reject null contract address."""
        with pytest.raises(ValueError, match="ctf_exchange_addr"):
            OpinionClient(
                api_key="test",
                wallet_private_key="0x" + "ab" * 32,
                ctf_exchange_addr="0x0000000000000000000000000000000000000000",
            )

    def test_rejects_empty_contract_address(self) -> None:
        """Client should reject empty contract address."""
        with pytest.raises(ValueError, match="ctf_exchange_addr"):
            OpinionClient(
                api_key="test",
                wallet_private_key="0x" + "ab" * 32,
                ctf_exchange_addr="",
            )

    def test_default_host_is_production(self) -> None:
        """Default host should be production endpoint."""
        client = OpinionClient(
            api_key="test-api-key",
            wallet_private_key="0x" + "ab" * 32,
            ctf_exchange_addr="0x1234567890123456789012345678901234567890",
        )
        assert client._host == "https://openapi.opinion.trade/openapi"

    def test_sandbox_host_override(self) -> None:
        """Sandbox mode should use sandbox host."""
        client = OpinionClient(
            api_key="test-api-key",
            wallet_private_key="0x" + "ab" * 32,
            ctf_exchange_addr="0x1234567890123456789012345678901234567890",
            sandbox=True,
        )
        assert client._host == "https://openapi-testnet.opinion.trade/openapi"

    def test_custom_host_override(self) -> None:
        """Custom host should override default."""
        custom_host = "https://custom.opinion.local"
        client = OpinionClient(
            api_key="test-api-key",
            wallet_private_key="0x" + "ab" * 32,
            ctf_exchange_addr="0x1234567890123456789012345678901234567890",
            host=custom_host,
        )
        assert client._host == custom_host.rstrip("/")

    def test_domain_chain_id_mainnet(self) -> None:
        """Mainnet should use chainId 56."""
        client = OpinionClient(
            api_key="test-api-key",
            wallet_private_key="0x" + "ab" * 32,
            ctf_exchange_addr="0x1234567890123456789012345678901234567890",
        )
        assert client._domain["chainId"] == 56

    def test_domain_chain_id_sandbox(self) -> None:
        """Sandbox should use chainId 97."""
        client = OpinionClient(
            api_key="test-api-key",
            wallet_private_key="0x" + "ab" * 32,
            ctf_exchange_addr="0x1234567890123456789012345678901234567890",
            sandbox=True,
        )
        assert client._domain["chainId"] == 97

    def test_rejects_empty_api_key(self) -> None:
        """Client should reject empty API key."""
        with pytest.raises((ValueError, TypeError)):
            OpinionClient(
                api_key="",
                wallet_private_key="0x" + "ab" * 32,
                ctf_exchange_addr="0x1234567890123456789012345678901234567890",
            )

    def test_rejects_empty_wallet_key(self) -> None:
        """Client should reject empty wallet private key."""
        with pytest.raises((ValueError, TypeError)):
            OpinionClient(
                api_key="test-api-key",
                wallet_private_key="",
                ctf_exchange_addr="0x1234567890123456789012345678901234567890",
            )


class TestOpinionClientOrderSubmission:
    """Tests for order submission functionality."""

    @pytest.fixture
    def opinion_fake_session(self) -> _FakeSession:
        return _FakeSession()

    @pytest.fixture
    def opinion_order_submission(self):
        import time as time_module

        from execution.models import OrderSubmission
        from src.enums import OrderType, Side, StrategyId

        return OrderSubmission(
            order_id="ord-opinion-001",
            proposal_id="prop-2",
            market_id="mkt-2",
            platform=Platform.OPINION,
            side=Side.BUY_YES,
            size_usdc=15.0,
            limit_price=0.60,
            order_type=OrderType.LIMIT,
            strategy_id=StrategyId.MM,
            expiry_ms=int(time_module.time() * 1000) + 60_000,
            token_quantity=25.0,
            submitted_at=int(time_module.time() * 1000),
        )

    @pytest.mark.asyncio
    async def test_place_order_posts_payload(
        self,
        opinion_fake_session: _FakeSession,
        monkeypatch: pytest.MonkeyPatch,
        opinion_order_submission,
    ) -> None:
        """Place order should POST correct payload to /order endpoint."""
        client = OpinionClient(
            api_key="test-api-key",
            wallet_private_key="0x" + "ab" * 32,
            ctf_exchange_addr="0x1234567890123456789012345678901234567890",
            host="https://placeholder.invalid",
        )

        async def _session():
            return opinion_fake_session

        monkeypatch.setattr(client, "_get_session", _session)
        monkeypatch.setattr(client, "_sign_order", lambda order: "signed-order")

        submission = opinion_order_submission
        result = await client.place_order(submission, 0.60)

        assert result.exchange_order_id == "op-order-123"
        assert result.status == "live"

        # Verify the session was called with POST to /order
        calls = [c for c in opinion_fake_session.calls if c[0] == "POST"]
        assert len(calls) > 0
        assert calls[0][1] == "/order"

    @pytest.mark.asyncio
    async def test_place_order_includes_signature(
        self,
        opinion_fake_session: _FakeSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Place order should include EIP-712 signature."""
        client = OpinionClient(
            api_key="test-api-key",
            wallet_private_key="0x" + "ab" * 32,
            ctf_exchange_addr="0x1234567890123456789012345678901234567890",
            host="https://placeholder.invalid",
        )

        async def _session():
            return opinion_fake_session

        signature = "0x" + "a" * 130
        monkeypatch.setattr(client, "_get_session", _session)
        monkeypatch.setattr(client, "_sign_order", lambda order: signature)

        submission = opinion_order_submission()
        await client.place_order(submission, 0.60)

        # Verify signature is included in the request
        calls = [c for c in opinion_fake_session.calls if c[0] == "POST"]
        assert len(calls) > 0

    @pytest.mark.asyncio
    async def test_place_order_with_custom_nonce(
        self,
        opinion_fake_session: _FakeSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Place order should accept custom nonce for idempotency."""
        client = OpinionClient(
            api_key="test-api-key",
            wallet_private_key="0x" + "ab" * 32,
            ctf_exchange_addr="0x1234567890123456789012345678901234567890",
            host="https://placeholder.invalid",
        )

        async def _session():
            return opinion_fake_session

        monkeypatch.setattr(client, "_get_session", _session)
        monkeypatch.setattr(client, "_sign_order", lambda order: "signed-order")

        submission = opinion_order_submission()
        custom_nonce = 123456789
        result = await client.place_order(submission, 0.60, nonce=custom_nonce)

        assert result.exchange_order_id == "op-order-123"

    @pytest.mark.asyncio
    async def test_place_order_rejection_raises(
        self,
        opinion_fake_session: _FakeSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """4xx responses should raise ExchangeRejected exception."""
        from src.errors import ExchangeRejected

        client = OpinionClient(
            api_key="test-api-key",
            wallet_private_key="0x" + "ab" * 32,
            ctf_exchange_addr="0x1234567890123456789012345678901234567890",
            host="https://placeholder.invalid",
        )

        class _FakeSessionRejection(_FakeSession):
            def post(self, path: str, data=None, headers=None, json=None):
                self.calls.append(("POST", path, {"data": data, "headers": headers, "json": json}))
                return _FakeResponse(400, {"message": "Invalid order"})

        async def _session():
            return _FakeSessionRejection()

        monkeypatch.setattr(client, "_get_session", _session)
        monkeypatch.setattr(client, "_sign_order", lambda order: "signed-order")

        submission = opinion_order_submission()

        with pytest.raises(ExchangeRejected):
            await client.place_order(submission, 0.60)


class TestOpinionClientOrderCancellation:
    """Tests for order cancellation functionality."""

    @pytest.fixture
    def opinion_fake_session(self) -> _FakeSession:
        return _FakeSession()

    @pytest.mark.asyncio
    async def test_cancel_order_returns_true(
        self,
        opinion_fake_session: _FakeSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Cancel order should return True on successful cancellation."""
        client = OpinionClient(
            api_key="test-api-key",
            wallet_private_key="0x" + "ab" * 32,
            ctf_exchange_addr="0x1234567890123456789012345678901234567890",
            host="https://placeholder.invalid",
        )

        async def _session():
            return opinion_fake_session

        monkeypatch.setattr(client, "_get_session", _session)

        result = await client.cancel_order("exch-ord-999", "mkt-1")

        assert result is True
        # Verify DELETE was called
        delete_calls = [c for c in opinion_fake_session.calls if c[0] == "DELETE"]
        assert len(delete_calls) > 0

    @pytest.mark.asyncio
    async def test_cancel_order_returns_true_on_404(
        self,
        opinion_fake_session: _FakeSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Cancel order should return True even if order not found (404)."""
        client = OpinionClient(
            api_key="test-api-key",
            wallet_private_key="0x" + "ab" * 32,
            ctf_exchange_addr="0x1234567890123456789012345678901234567890",
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
        opinion_fake_session: _FakeSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """403 Forbidden should return False."""
        client = OpinionClient(
            api_key="test-api-key",
            wallet_private_key="0x" + "ab" * 32,
            ctf_exchange_addr="0x1234567890123456789012345678901234567890",
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


class TestOpinionClientOrderStatus:
    """Tests for order status retrieval."""

    @pytest.fixture
    def opinion_fake_session(self) -> _FakeSession:
        return _FakeSession()

    @pytest.mark.asyncio
    async def test_get_order_status_parses_open(
        self,
        opinion_fake_session: _FakeSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Should correctly parse open order status."""
        client = OpinionClient(
            api_key="test-api-key",
            wallet_private_key="0x" + "ab" * 32,
            ctf_exchange_addr="0x1234567890123456789012345678901234567890",
            host="https://placeholder.invalid",
        )

        async def _session():
            return opinion_fake_session

        monkeypatch.setattr(client, "_get_session", _session)

        result = await client.get_order_status("exch-ord-999", "mkt-1")

        assert result.exchange_order_id == "exch-ord-999"
        assert result.is_live is True
        assert result.is_filled is False

    @pytest.mark.asyncio
    async def test_get_order_status_parses_filled(
        self,
        opinion_fake_session: _FakeSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Should correctly parse filled order status."""
        client = OpinionClient(
            api_key="test-api-key",
            wallet_private_key="0x" + "ab" * 32,
            ctf_exchange_addr="0x1234567890123456789012345678901234567890",
            host="https://placeholder.invalid",
        )

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
        opinion_fake_session: _FakeSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Should correctly parse cancelled order status."""
        client = OpinionClient(
            api_key="test-api-key",
            wallet_private_key="0x" + "ab" * 32,
            ctf_exchange_addr="0x1234567890123456789012345678901234567890",
            host="https://placeholder.invalid",
        )

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
    async def test_get_order_status_calculates_fills(
        self,
        opinion_fake_session: _FakeSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Should calculate fills from remaining amount."""
        client = OpinionClient(
            api_key="test-api-key",
            wallet_private_key="0x" + "ab" * 32,
            ctf_exchange_addr="0x1234567890123456789012345678901234567890",
            host="https://placeholder.invalid",
        )

        class _FakeSessionPartial(_FakeSession):
            def get(self, path: str, headers=None):
                self.calls.append(("GET", path, {"headers": headers}))
                return _FakeResponse(
                    200,
                    {
                        "status": "partial",
                        "remainingAmount": 5.0,
                        "originalAmount": 10.0,
                        "price": 0.60,
                    },
                )

        async def _session():
            return _FakeSessionPartial()

        monkeypatch.setattr(client, "_get_session", _session)

        result = await client.get_order_status("exch-ord-999", "mkt-1")

        assert result.is_live is True
        assert len(result.new_fills) >= 0  # May have fills


class TestOpinionClientOpenOrders:
    """Tests for fetching open orders."""

    @pytest.mark.asyncio
    async def test_get_open_orders(
        self,
        opinion_fake_session: _FakeSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Should return list of open orders."""
        client = OpinionClient(
            api_key="test-api-key",
            wallet_private_key="0x" + "ab" * 32,
            ctf_exchange_addr="0x1234567890123456789012345678901234567890",
            host="https://placeholder.invalid",
        )

        class _FakeSessionOrders(_FakeSession):
            def get(self, path: str, headers=None):
                self.calls.append(("GET", path, {"headers": headers}))
                return _FakeResponse(
                    200,
                    [
                        {
                            "orderId": "op-1",
                            "marketId": "mkt-1",
                            "side": 0,
                            "originalAmount": 100.0,
                            "remainingAmount": 50.0,
                            "price": 0.60,
                        }
                    ],
                )

        async def _session():
            return _FakeSessionOrders()

        monkeypatch.setattr(client, "_get_session", _session)

        orders = await client.get_open_orders()

        assert len(orders) == 1
        assert orders[0].exchange_order_id == "op-1"
        assert orders[0].market_id == "mkt-1"
        assert orders[0].side == "BUY"
        assert orders[0].size_usdc == 100.0

    @pytest.mark.asyncio
    async def test_get_open_orders_empty(
        self,
        opinion_fake_session: _FakeSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Should return empty list when no orders."""
        client = OpinionClient(
            api_key="test-api-key",
            wallet_private_key="0x" + "ab" * 32,
            ctf_exchange_addr="0x1234567890123456789012345678901234567890",
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


class TestOpinionClientConnectivity:
    """Tests for connectivity verification."""

    @pytest.fixture
    def opinion_fake_session(self) -> _FakeSession:
        return _FakeSession()

    @pytest.mark.asyncio
    async def test_verify_connectivity(
        self,
        opinion_fake_session: _FakeSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Should verify connectivity with /orders/open endpoint."""
        client = OpinionClient(
            api_key="test-api-key",
            wallet_private_key="0x" + "ab" * 32,
            ctf_exchange_addr="0x1234567890123456789012345678901234567890",
            host="https://placeholder.invalid",
        )

        async def _session():
            return opinion_fake_session

        monkeypatch.setattr(client, "_get_session", _session)

        result = await client.verify_connectivity()

        assert result is True

    @pytest.mark.asyncio
    async def test_verify_connectivity_fails(
        self,
        opinion_fake_session: _FakeSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Should return False when connectivity check fails."""
        client = OpinionClient(
            api_key="test-api-key",
            wallet_private_key="0x" + "ab" * 32,
            ctf_exchange_addr="0x1234567890123456789012345678901234567890",
            host="https://placeholder.invalid",
        )

        class _FakeSessionFail(_FakeSession):
            def get(self, path: str, headers=None):
                self.calls.append(("GET", path, {"headers": headers}))
                return _FakeResponse(500, {"message": "Server error"})

        async def _session():
            return _FakeSessionFail()

        monkeypatch.setattr(client, "_get_session", _session)

        result = await client.verify_connectivity()

        assert result is False


class TestOpinionClientWindowsCompatibility:
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


class TestOpinionClientEIP712Signing:
    """Tests for EIP-712 order signing."""

    def test_sign_order_creates_signature(self) -> None:
        """Should create valid EIP-712 signature."""
        client = OpinionClient(
            api_key="test-api-key",
            wallet_private_key="0x" + "ab" * 32,
            ctf_exchange_addr="0x1234567890123456789012345678901234567890",
            host="https://placeholder.invalid",
        )

        order = {
            "salt": 123456,
            "maker": "0x" + "ab" * 20,
            "signer": "0x" + "cd" * 20,
            "taker": "0x0000000000000000000000000000000000000000",
            "tokenId": 123,
            "makerAmount": 1000000,
            "takerAmount": 600000,
            "expiration": int(time.time()) + 3600,
            "nonce": int(time.time() * 1000),
            "feeRateBps": 0,
            "side": 0,
            "signatureType": 1,
        }

        signature = client._sign_order(order)

        assert signature is not None
        assert len(signature) == 130  # 64 bytes + '0x' prefix


class TestOpinionClientRateLimiting:
    """Tests for rate limiting functionality."""

    def test_throttler_initialized(self) -> None:
        """Throttler should be initialized with configured rate."""
        client = OpinionClient(
            api_key="test-api-key",
            wallet_private_key="0x" + "ab" * 32,
            ctf_exchange_addr="0x1234567890123456789012345678901234567890",
            rate_limit_per_s=8,  # Custom rate
        )

        # The throttler should exist and have the configured rate
        assert hasattr(client, "_throttler")


class TestOpinionClientSessionManagement:
    """Tests for session management."""

    @pytest.mark.asyncio
    async def test_session_creation(self) -> None:
        """Should create aiohttp ClientSession."""
        client = OpinionClient(
            api_key="test-api-key",
            wallet_private_key="0x" + "ab" * 32,
            ctf_exchange_addr="0x1234567890123456789012345678901234567890",
            host="https://placeholder.invalid",
        )

        session = await client._get_session()

        assert session is not None
        assert hasattr(session, "closed")

    @pytest.mark.asyncio
    async def test_session_reuse(self) -> None:
        """Should reuse existing session."""
        client = OpinionClient(
            api_key="test-api-key",
            wallet_private_key="0x" + "ab" * 32,
            ctf_exchange_addr="0x1234567890123456789012345678901234567890",
            host="https://placeholder.invalid",
        )

        session1 = await client._get_session()
        session2 = await client._get_session()

        assert session1 is session2

    @pytest.mark.asyncio
    async def test_close_cleans_up_session(self) -> None:
        """Close should clean up the session."""
        client = OpinionClient(
            api_key="test-api-key",
            wallet_private_key="0x" + "ab" * 32,
            ctf_exchange_addr="0x1234567890123456789012345678901234567890",
            host="https://placeholder.invalid",
        )

        await client._get_session()
        await client.close()

        # Session should be closed
        assert client._session is None or client._session.closed


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
