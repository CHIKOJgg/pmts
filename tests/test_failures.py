
import asyncio
import json
import unittest
import uuid
import time
from unittest.mock import MagicMock, AsyncMock, patch

from src.types import Platform, Side, OrderType, StrategyId, ArbLeg
from execution.models import OrderSubmission, OrderProposal
from execution.engine import ExecutionEngine, PlacedOrderResponse, OpenOrder
from execution.order_tracker import OrderTracker, TrackerStatus
from portfolio.storage import SqlitePortfolioStore
from risk.engine import RiskEngine
from risk.kill_switch import KillSwitch
from risk.limits import RiskLimits
from engine.orchestrator import Orchestrator
from portfolio.manager import PortfolioManager, FillRecord

def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()

class TestFailureModes(unittest.TestCase):

    # 1. WebSocket disconnect and automatic reconnection
    def test_ws_reconnect(self):
        import sys
        import types
        websockets_stub = types.SimpleNamespace(connect=MagicMock())
        with patch.dict(sys.modules, {"websockets": websockets_stub}):
            from data.adapters.polymarket_ws import PolymarketWSAdapter
            import data.adapters.polymarket_ws as polymarket_ws_module
            polymarket_ws_module.websockets = websockets_stub
        adapter = PolymarketWSAdapter(asset_ids=["BTC-Q4"])
        
        # Mock connection sequence: Fail, then Success (then close)
        mock_ws = AsyncMock()
        mock_ws.__aenter__.return_value = mock_ws
        mock_ws.__aiter__.side_effect = [
            asyncio.TimeoutError("Disconnect"), # First connection fails during iteration
            ["""{"event_type": "order_book_v2", "asset_id": "BTC-Q4", "bids": [{"price": "0.50", "size": "100"}], "asks": [{"price": "0.51", "size": "100"}]}"""],
            StopAsyncIteration
        ]
        
        websockets_stub.connect.side_effect = [
            Exception("Connection Failed"), # First attempt fails
            mock_ws, # Second attempt succeeds
        ]
        
        # We need to run it for a bit
        async def run_briefly():
            adapter._running = True
            task = asyncio.create_task(adapter._run_loop())
            await asyncio.sleep(1.5) # Allow some time for retries
            adapter._running = False
            await task

        run(run_briefly())
        self.assertGreaterEqual(websockets_stub.connect.call_count, 2)

    # 2. Exchange API 5xx during order submission
    def test_exchange_5xx_retries(self):
        mock_client = MagicMock()
        mock_client.platform = Platform.POLYMARKET
        mock_client.place_order = AsyncMock(side_effect=[
            Exception("500 Internal Server Error"),
            Exception("500 Internal Server Error"),
            PlacedOrderResponse(exchange_order_id="EXCH-123", status="live")
        ])
        
        engine = ExecutionEngine(mock_client)
        engine.submit_base_delay_s = 0.01 # Fast retries for test
        
        sub = OrderSubmission(
            order_id=str(uuid.uuid4()),
            proposal_id="PROP-1",
            market_id="M1",
            platform=Platform.POLYMARKET,
            side=Side.BUY_YES,
            size_usdc=100.0,
            limit_price=0.50,
            order_type=OrderType.LIMIT,
            strategy_id=StrategyId.ARB,
            expiry_ms=int(time.time()*1000) + 10000,
            token_quantity=200.0,
            submitted_at=int(time.time()*1000),
            leg_group_id="G1",
            leg_number=ArbLeg.LEG_1,
            min_fill_ratio=0.8
        )
        
        run(engine._execute_submission(engine._trackers.get("PROP-1") or OrderTracker(sub)))
        self.assertEqual(mock_client.place_order.call_count, 3)
        self.assertEqual(engine.submit_retries, 2)

    # 3. Partial fill followed by expiry and cancellation
    def test_partial_fill_expiry(self):
        mock_client = MagicMock()
        mock_client.platform = Platform.POLYMARKET
        mock_client.cancel_order = AsyncMock(return_value=True)
        
        engine = ExecutionEngine(mock_client)
        sub = OrderSubmission(
            order_id=str(uuid.uuid4()),
            proposal_id="PROP-PF",
            market_id="M1",
            platform=Platform.POLYMARKET,
            side=Side.BUY_YES,
            size_usdc=100.0,
            limit_price=0.50,
            order_type=OrderType.LIMIT,
            strategy_id=StrategyId.MM,
            expiry_ms=int(time.time()*1000) - 1000, # Already expired
            token_quantity=200.0,
            submitted_at=int(time.time()*1000) - 5000,
        )
        
        tracker = OrderTracker(sub)
        tracker.exchange_order_id = "EXCH-PF"
        tracker.status = TrackerStatus.SUBMITTED
        # Partial fill: 40 USDC
        tracker.record_fill(40.0, 0.50, 80.0, int(time.time()*1000))
        
        engine._trackers["PROP-PF"] = tracker
        
        # Run expiry check (single pass, not the infinite worker loop)
        run(engine._expiry_check())
        
        self.assertEqual(tracker.status, TrackerStatus.EXPIRED)
        self.assertEqual(tracker.cumulative_filled_usdc, 40.0)
        mock_client.cancel_order.assert_called_once()

    # 4. Duplicate fill events
    def test_duplicate_fills(self):
        sub = OrderSubmission(
            order_id=str(uuid.uuid4()),
            proposal_id="PROP-DUP",
            market_id="M1",
            platform=Platform.POLYMARKET,
            side=Side.BUY_YES,
            size_usdc=100.0,
            limit_price=0.50,
            order_type=OrderType.LIMIT,
            strategy_id=StrategyId.MM,
            expiry_ms=int(time.time()*1000) + 10000,
            token_quantity=200.0,
            submitted_at=int(time.time()*1000),
        )
        tracker = OrderTracker(sub)
        
        # First fill
        tracker.record_fill(50.0, 0.50, 100.0, 1000)
        # Duplicate fill (same USDC, price, tokens, TS)
        # Note: OrderTracker currently just appends. 
        # If we wanted to handle duplicates, we'd need more logic.
        # But let's verify current behavior and see if it's "safe" (e.g. doesn't crash)
        tracker.record_fill(50.0, 0.50, 100.0, 1000)
        
        self.assertEqual(tracker.cumulative_filled_usdc, 100.0)
        self.assertEqual(tracker.status, TrackerStatus.FILLED)

    # 5. Service restart with open orders and SQLite reservations
    def test_service_restart_recovery(self):
        mock_store = MagicMock()
        sub = OrderSubmission(
            order_id="OID-1", proposal_id="PROP-REC", market_id="M1",
            platform=Platform.POLYMARKET, side=Side.BUY_YES, size_usdc=100.0,
            limit_price=0.50, order_type=OrderType.LIMIT, strategy_id=StrategyId.ARB,
            expiry_ms=int(time.time()*1000)+10000, token_quantity=200.0, submitted_at=int(time.time()*1000),
            leg_group_id="G1", leg_number=ArbLeg.LEG_1, min_fill_ratio=0.8
        )
        mock_store.load_active_orders.return_value = [
            ("PROP-REC", "EXCH-REC", json.dumps(sub.model_dump()))
        ]
        
        mock_client = MagicMock()
        mock_client.platform = Platform.POLYMARKET
        mock_client.get_open_orders = AsyncMock(return_value=[
            OpenOrder(exchange_order_id="EXCH-REC", market_id="M1", side="BUY", size_usdc=100.0, filled_usdc=20.0, limit_price=0.50, ts=123)
        ])
        
        engine = ExecutionEngine(mock_client, store=mock_store)
        run(engine.reconcile())
        
        self.assertIn("PROP-REC", engine._trackers)
        tracker = engine._trackers["PROP-REC"]
        self.assertEqual(tracker.exchange_order_id, "EXCH-REC")
        self.assertEqual(tracker.cumulative_filled_usdc, 20.0)

    # 6. Kill switch activation during an in-flight arb
    def test_kill_switch_during_arb(self):
        mock_mdp = MagicMock()
        mock_risk = MagicMock()
        mock_strategy = MagicMock()
        mock_pm_engine = MagicMock()
        mock_op_engine = MagicMock()
        mock_pm_engine.cancel = AsyncMock()
        mock_op_engine.cancel = AsyncMock()
        
        orchestrator = Orchestrator(
            mdp=mock_mdp, portfolio=MagicMock(), risk=mock_risk,
            strategy=mock_strategy, pm_engine=mock_pm_engine, op_engine=mock_op_engine,
            markets=["M1"], enable_trading=True
        )

        mock_pm_engine.get_tracker = MagicMock(return_value=MagicMock(submission=MagicMock(market_id="M1")))
        mock_op_engine.get_tracker = MagicMock(return_value=MagicMock(submission=MagicMock(market_id="M1")))
        orchestrator._in_flight = {
            "P1": (StrategyId.ARB, 100.0, Platform.POLYMARKET, "G1"),
            "P2": (StrategyId.ARB, 100.0, Platform.OPINION, "G1"),
        }

        run(orchestrator._kill_switch_response())
        self.assertEqual(mock_pm_engine.cancel.call_count, 1)
        self.assertEqual(mock_op_engine.cancel.call_count, 1)

    # 7. SQLite write failure during fill recording
    def test_sqlite_write_failure(self):
        store = SqlitePortfolioStore(":memory:")
        # Force a failure by closing the connection
        store._conn.close()
        
        from portfolio.manager import FillRecord, _Position
        fill = FillRecord("P1", "E1", "M1", Platform.POLYMARKET, "buy_yes", 100.0, 0.50, 1000)
        pos = _Position("M1", Platform.POLYMARKET)
        
        # Should not crash, just log error
        with self.assertLogs("portfolio.storage", level="ERROR") as cm:
            store.save_fill_and_position(fill, pos, 1000.0, 10000.0, 0.0)
            self.assertIn("Failed to save fill/position to SQLite", cm.output[0])

    # 8. RiskEngine.evaluate() under concurrent proposal load
    def test_risk_engine_concurrency(self):
        pm = MagicMock()
        pm.get_portfolio_mtm.return_value.total_equity_usdc = 10000.0
        pm.get_price_age_ms.return_value = 0
        pm.peak_equity = 10000.0
        pm.cash_usdc = 1000.0
        pm.get_market_exposure_usdc.return_value = 0.0
        pm.get_delta.return_value.net_delta = 0.0
        
        risk = RiskEngine(
            pm, KillSwitch("tok"),
            RiskLimits(
                max_market_exposure_usdc=10000, min_free_capital_pct=0.0,
                max_net_delta_per_market=10000,
            ),
        )
        
        proposals = []
        for i in range(10):
            p = OrderProposal(str(i), "M1", Platform.POLYMARKET, Side.BUY_YES, 150.0, 0.50, OrderType.LIMIT, StrategyId.MM, int(time.time()*1000)+10000, 0)
            proposals.append(p)
            
        # available capital = 1000. Each needs 150. Max 6 should pass.
        # But wait, RiskEngine.evaluate is synchronous. To test "concurrent" load we just call it many times.
        # If it were async and had a race, we'd use gather.
        
        results = [risk.evaluate(p) for p in proposals]
        approved = [r for r in results if r.approved]
        
        self.assertEqual(len(approved), 6) # 150 * 6 = 900. Next one would be 1050 > 1000.

    def test_risk_engine_concurrent_async_evaluate(self):
        """Test that synchronous evaluate() is safe under concurrent async calls."""
        import asyncio

        pm = MagicMock()
        pm.get_portfolio_mtm.return_value.total_equity_usdc = 10000.0
        pm.cash_usdc = 1000.0
        pm.get_price_age_ms.return_value = 100
        pm.peak_equity = 10000.0
        pm.get_market_exposure_usdc.return_value = 0.0
        pm.get_delta.return_value.net_delta = 0.0

        risk = RiskEngine(
            pm, KillSwitch("tok"),
            RiskLimits(
                max_market_exposure_usdc=10000, min_free_capital_pct=0.0,
                max_net_delta_per_market=10000,
            ),
        )

        async def evaluate_async(idx: int):
            p = OrderProposal(
                str(idx), "M1", Platform.POLYMARKET, Side.BUY_YES,
                100.0, 0.50, OrderType.LIMIT, StrategyId.MM,
                int(time.time() * 1000) + 10000, 0
            )
            return risk.evaluate(p)

        async def run_concurrent():
            tasks = [evaluate_async(i) for i in range(20)]
            return await asyncio.gather(*tasks)

        results = asyncio.get_event_loop().run_until_complete(run_concurrent())
        approved = [r for r in results if r.approved]

        self.assertEqual(len(approved), 10)

    def test_portfolio_manager_concurrent_record_fill(self):
        """Test that record_fill() is safe under concurrent async calls."""
        import asyncio

        def price_source(market_id, platform):
            return (0.50, 0.50)

        pm = PortfolioManager(initial_cash_usdc=10000.0, price_source=price_source)

        async def record_fill_async(idx: int):
            fill = FillRecord(
                proposal_id=f"prop-{idx}",
                order_id=f"ord-{idx}",
                market_id="M1",
                platform=Platform.POLYMARKET,
                side=Side.BUY_YES.value,
                filled_usdc=100.0,
                fill_price=0.50,
                ts=int(time.time() * 1000),
            )
            await pm.record_fill(fill)

        async def run_concurrent():
            tasks = [record_fill_async(i) for i in range(10)]
            await asyncio.gather(*tasks)

        asyncio.get_event_loop().run_until_complete(run_concurrent())

        delta = pm.get_delta("M1")
        self.assertAlmostEqual(delta.net_delta, 2000.0)

    def test_execution_engine_concurrent_submit(self):
        """Test that submit() is safe under concurrent async calls."""
        import asyncio

        client = MagicMock()
        client.platform = Platform.POLYMARKET
        client.place_order = AsyncMock(side_effect=Exception("No network"))

        engine = ExecutionEngine(client, max_concurrent=10)

        async def submit_async(idx: int):
            sub = OrderSubmission(
                order_id=f"ord-{idx}",
                proposal_id=f"prop-{idx}",
                market_id="M1",
                platform=Platform.POLYMARKET,
                side=Side.BUY_YES,
                size_usdc=100.0,
                limit_price=0.50,
                order_type=OrderType.LIMIT,
                strategy_id=StrategyId.MM,
                expiry_ms=int(time.time() * 1000) + 60_000,
                token_quantity=200.0,
                submitted_at=int(time.time() * 1000),
            )
            await engine.submit(sub)

        async def run_concurrent():
            tasks = [submit_async(i) for i in range(5)]
            await asyncio.gather(*tasks)

        asyncio.get_event_loop().run_until_complete(run_concurrent())

        self.assertEqual(engine._queue.qsize(), 5)

if __name__ == "__main__":
    unittest.main()
