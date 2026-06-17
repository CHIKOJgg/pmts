"""Tests for WebSocket adapters (Polymarket and Opinion)."""
from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from data.adapters.polymarket_ws import PolymarketWSAdapter
from data.adapters.opinion_ws import OpinionWSAdapter
from data.models import MarketSnapshot
from src.enums import Platform


class TestPolymarketWSAdapter:
    def test_platform_is_polymarket(self) -> None:
        adapter = PolymarketWSAdapter(asset_ids=["mkt-1"])
        assert adapter.platform is Platform.POLYMARKET

    def test_set_snapshot_callback(self) -> None:
        adapter = PolymarketWSAdapter(asset_ids=["mkt-1"])
        cb = AsyncMock()
        adapter.set_snapshot_callback(cb)
        assert adapter._callback is cb

    @pytest.mark.asyncio
    async def test_handle_message_parses_order_book(self) -> None:
        adapter = PolymarketWSAdapter(asset_ids=["mkt-1"])
        received_snapshots: list[MarketSnapshot] = []

        async def capture(snapshot: MarketSnapshot) -> None:
            received_snapshots.append(snapshot)

        adapter.set_snapshot_callback(capture)

        message = json.dumps({
            "event_type": "order_book_v2",
            "asset_id": "mkt-1",
            "bids": [{"price": "0.55", "size": "100"}],
            "asks": [{"price": "0.60", "size": "150"}],
            "timestamp": int(time.time() * 1000),
        })

        await adapter._handle_message(message)

        assert len(received_snapshots) == 1
        snap = received_snapshots[0]
        assert snap.market_id == "mkt-1"
        assert snap.platform is Platform.POLYMARKET
        assert snap.yes_bid == 0.55
        assert snap.yes_ask == 0.60
        assert snap.no_bid == pytest.approx(0.40)
        assert snap.no_ask == pytest.approx(0.45)

    @pytest.mark.asyncio
    async def test_handle_message_ignores_non_order_book(self) -> None:
        adapter = PolymarketWSAdapter(asset_ids=["mkt-1"])
        received_snapshots: list[MarketSnapshot] = []

        async def capture(snapshot: MarketSnapshot) -> None:
            received_snapshots.append(snapshot)

        adapter.set_snapshot_callback(capture)

        message = json.dumps({"event_type": "price", "data": {}})
        await adapter._handle_message(message)

        assert len(received_snapshots) == 0

    @pytest.mark.asyncio
    async def test_handle_message_ignores_empty_book(self) -> None:
        adapter = PolymarketWSAdapter(asset_ids=["mkt-1"])
        received_snapshots: list[MarketSnapshot] = []

        async def capture(snapshot: MarketSnapshot) -> None:
            received_snapshots.append(snapshot)

        adapter.set_snapshot_callback(capture)

        message = json.dumps({
            "event_type": "order_book_v2",
            "asset_id": "mkt-1",
            "bids": [],
            "asks": [{"price": "0.60", "size": "150"}],
        })

        await adapter._handle_message(message)

        assert len(received_snapshots) == 0

    @pytest.mark.asyncio
    async def test_handle_message_invalid_json(self) -> None:
        adapter = PolymarketWSAdapter(asset_ids=["mkt-1"])
        await adapter._handle_message("not json")

    @pytest.mark.asyncio
    async def test_start_creates_task(self) -> None:
        adapter = PolymarketWSAdapter(asset_ids=["mkt-1"])
        with patch.object(adapter, "_run_loop", new_callable=AsyncMock):
            await adapter.start()
            assert adapter._running is True
            assert adapter._task is not None
            await adapter.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_task(self) -> None:
        adapter = PolymarketWSAdapter(asset_ids=["mkt-1"])
        with patch.object(adapter, "_run_loop", new_callable=AsyncMock):
            await adapter.start()
            await adapter.stop()
            assert adapter._running is False

    @pytest.mark.asyncio
    async def test_start_idempotent(self) -> None:
        adapter = PolymarketWSAdapter(asset_ids=["mkt-1"])
        with patch.object(adapter, "_run_loop", new_callable=AsyncMock):
            await adapter.start()
            task1 = adapter._task
            await adapter.start()
            assert adapter._task is task1
            await adapter.stop()


class TestOpinionWSAdapter:
    def test_platform_is_opinion(self) -> None:
        adapter = OpinionWSAdapter(market_ids=["mkt-1"])
        assert adapter.platform is Platform.OPINION

    def test_set_snapshot_callback(self) -> None:
        adapter = OpinionWSAdapter(market_ids=["mkt-1"])
        cb = AsyncMock()
        adapter.set_snapshot_callback(cb)
        assert adapter._callback is cb

    @pytest.mark.asyncio
    async def test_handle_message_parses_ticker(self) -> None:
        adapter = OpinionWSAdapter(market_ids=["mkt-1"])
        received_snapshots: list[MarketSnapshot] = []

        async def capture(snapshot: MarketSnapshot) -> None:
            received_snapshots.append(snapshot)

        adapter.set_snapshot_callback(capture)

        message = json.dumps({
            "stream": "ticker@mkt-1",
            "data": {
                "b": "0.55",
                "a": "0.60",
                "B": "100",
                "A": "150",
                "t": int(time.time() * 1000),
            },
        })

        await adapter._handle_message(message)

        assert len(received_snapshots) == 1
        snap = received_snapshots[0]
        assert snap.market_id == "mkt-1"
        assert snap.platform is Platform.OPINION
        assert snap.yes_bid == 0.55
        assert snap.yes_ask == 0.60
        assert snap.no_bid == pytest.approx(0.40)
        assert snap.no_ask == pytest.approx(0.45)

    @pytest.mark.asyncio
    async def test_handle_message_ignores_non_ticker(self) -> None:
        adapter = OpinionWSAdapter(market_ids=["mkt-1"])
        received_snapshots: list[MarketSnapshot] = []

        async def capture(snapshot: MarketSnapshot) -> None:
            received_snapshots.append(snapshot)

        adapter.set_snapshot_callback(capture)

        message = json.dumps({"stream": "depth@mkt-1", "data": {}})
        await adapter._handle_message(message)

        assert len(received_snapshots) == 0

    @pytest.mark.asyncio
    async def test_handle_message_ignores_zero_prices(self) -> None:
        adapter = OpinionWSAdapter(market_ids=["mkt-1"])
        received_snapshots: list[MarketSnapshot] = []

        async def capture(snapshot: MarketSnapshot) -> None:
            received_snapshots.append(snapshot)

        adapter.set_snapshot_callback(capture)

        message = json.dumps({
            "stream": "ticker@mkt-1",
            "data": {
                "b": "0",
                "a": "0",
                "B": "100",
                "A": "150",
                "t": int(time.time() * 1000),
            },
        })

        await adapter._handle_message(message)

        assert len(received_snapshots) == 0

    @pytest.mark.asyncio
    async def test_handle_message_invalid_json(self) -> None:
        adapter = OpinionWSAdapter(market_ids=["mkt-1"])
        await adapter._handle_message("not json")

    @pytest.mark.asyncio
    async def test_start_creates_task(self) -> None:
        adapter = OpinionWSAdapter(market_ids=["mkt-1"])
        with patch.object(adapter, "_run_loop", new_callable=AsyncMock):
            await adapter.start()
            assert adapter._running is True
            assert adapter._task is not None
            await adapter.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_task(self) -> None:
        adapter = OpinionWSAdapter(market_ids=["mkt-1"])
        with patch.object(adapter, "_run_loop", new_callable=AsyncMock):
            await adapter.start()
            await adapter.stop()
            assert adapter._running is False

    @pytest.mark.asyncio
    async def test_start_idempotent(self) -> None:
        adapter = OpinionWSAdapter(market_ids=["mkt-1"])
        with patch.object(adapter, "_run_loop", new_callable=AsyncMock):
            await adapter.start()
            task1 = adapter._task
            await adapter.start()
            assert adapter._task is task1
            await adapter.stop()
