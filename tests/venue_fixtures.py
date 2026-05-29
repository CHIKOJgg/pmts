"""
Shared venue client fixtures for cross-platform testing.

This module provides Windows-compatible fixtures and utilities for testing
Polymarket and Opinion venue clients.
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

# ============== Windows-Compatible Temporary Directory Fixtures =============


@pytest.fixture
def temp_dir_windows_safe():
    """Create a temporary directory with Windows-compatible path handling."""
    # Use pathlib for cross-platform path handling
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        yield tmp_path


@pytest.fixture
def test_data_file(temp_dir_windows_safe):
    """Create a test data file with Windows-safe naming."""
    test_file = temp_dir_windows_safe / "test_order_data.json"
    test_file.write_text('{"orderId": "test-123", "status": "open"}')
    yield str(test_file)
    # Cleanup happens automatically with TemporaryDirectory


# ====================== Common Order Submission Fixtures =====================


@pytest.fixture
def polymarket_order_submission():
    """Create a standard Polymarket order submission."""
    import time as time_module

    from execution.models import OrderSubmission
    from src.types import OrderType, Platform, Side, StrategyId

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


@pytest.fixture
def opinion_order_submission():
    """Create a standard Opinion order submission."""
    import time as time_module

    from execution.models import OrderSubmission
    from src.types import OrderType, Platform, Side, StrategyId

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


# ======================= Fake Response Mocks ========================


class _FakeResponse:
    """Cross-platform fake HTTP response for testing."""

    def __init__(self, status: int, payload: Dict | List | None = None, text: str = "") -> None:
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
            from src.errors import ExchangeRejected

            raise ExchangeRejected(
                f"HTTP {self.status}",
                platform="test",
                proposal_id="test",
                status_code=self.status,
                exchange_error=str(self._payload),
            )


class _FakeSession:
    """Cross-platform fake HTTP session for testing."""

    def __init__(self) -> None:
        self.calls: List[tuple[str, str, Dict | None]] = []
        self.response_map: Dict[str, tuple[int, Any]] = {}

    def register_response(self, method: str, path: str, status: int, payload: Any = None):
        """Register a response for a specific method/path combination."""
        key = f"{method.upper()}:{path}"
        self.response_map[key] = (status, payload)

    def _get_response(self, method: str, path: str) -> tuple[int, Any]:
        """Get registered response or use defaults."""
        key = f"{method.upper()}:{path}"

        # Polymarket-specific defaults
        if "polymarket" in path.lower():
            if method == "POST" and path == "/order":
                return 200, {"orderID": "pm-order-123"}
            if method == "DELETE" and path == "/order":
                return 200, {}
            if method == "GET" and path == "/profile":
                return 200, {"ok": True}
            if method == "GET" and path.startswith("/order/"):
                return 200, {"status": "open", "remainingSize": 12.5}
            if method == "GET" and path == "/orders":
                return 200, []

        # Opinion-specific defaults
        if "opinion" in path.lower():
            if method == "POST" and path == "/order":
                return 200, {"orderId": "op-order-123"}
            if method == "DELETE" and path.startswith("/order/"):
                return 200, {}
            if method == "GET" and path == "/orders/open":
                return 200, []
            if method == "GET" and path.startswith("/order/"):
                return 200, {"status": "open", "remainingAmount": 15.0}

        # Generic defaults
        if method == "POST":
            return 200, {"id": f"{method.lower()}-response"}
        if method == "DELETE":
            return 200, {}
        if method == "GET":
            return 200, []

        return 500, {"error": "No registered response"}

    def post(self, path: str, data=None, headers=None, json=None):
        self.calls.append(("POST", path, {"data": data, "headers": headers, "json": json}))
        status, payload = self._get_response("POST", path)
        return _FakeResponse(status, payload)

    def delete(self, path: str, data=None, headers=None, json=None):
        self.calls.append(("DELETE", path, {"data": data, "headers": headers, "json": json}))
        status, payload = self._get_response("DELETE", path)
        return _FakeResponse(status, payload)

    def get(self, path: str, headers=None):
        self.calls.append(("GET", path, {"headers": headers}))
        status, payload = self._get_response("GET", path)
        return _FakeResponse(status, payload)


# ====================== Platform-Specific Fixtures ========================


@pytest.fixture
def polymarket_fake_session():
    """Create a fake session configured for Polymarket."""
    return _FakeSession()


@pytest.fixture
def opinion_fake_session():
    """Create a fake session configured for Opinion."""
    return _FakeSession()


# ==================== Cross-Platform Test Utilities =========================


def normalize_path_for_testing(path_str: str) -> str:
    """
    Normalize path for cross-platform testing.

    On Windows, converts backslashes to forward slashes for consistent comparison.
    """
    # Use os.path.normpath for platform-appropriate normalization
    normalized = os.path.normpath(path_str)
    # For testing consistency, convert to forward slashes
    return normalized.replace("\\", "/")


def assert_windows_safe_dict_comparison(actual: Dict, expected: Dict) -> None:
    """
    Assert dictionary equality with Windows-compatible string handling.

    Converts all keys and string values to ensure consistent comparison
    across platforms.
    """

    def _normalize(obj):
        if isinstance(obj, dict):
            return {str(k): _normalize(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [_normalize(item) for item in obj]
        elif isinstance(obj, str):
            # Normalize path separators for Windows compatibility
            return obj.replace("\\", "/")
        else:
            return obj

    assert _normalize(actual) == _normalize(expected)


def create_test_order_id(platform: str) -> str:
    """
    Create a test order ID that's compatible with all platforms.

    Polymarket uses alphanumeric IDs, Opinion uses various formats.
    This ensures consistent test data across venues.
    """
    timestamp = int(time.time() * 1000)
    return f"test-{platform}-{timestamp}"


# ======================= Cleanup Fixtures =========================


@pytest.fixture
def clean_environ():
    """Fixture to clean environment variables before and after tests."""
    # Store original environ
    original = os.environ.copy()

    yield

    # Restore original environ
    for key in list(os.environ.keys()):
        if key not in original:
            del os.environ[key]
    os.environ.update(original)


# ===================== Contract Test Helpers =======================


@pytest.fixture
def contract_test_marker():
    """
    Marker fixture for contract tests.

    Use to mark tests that verify venue client contracts are satisfied.
    These tests should be run against both sandbox and production endpoints.
    """
    return "contract"


# ===================== Windows Test Marker =======================


def pytest_configure(config):
    """Register custom markers for pytest."""
    config.addinivalue_line("markers", "windows_only: Mark test to run only on Windows platform")
