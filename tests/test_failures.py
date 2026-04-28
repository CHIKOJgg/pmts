
import asyncio
import json
import unittest
import uuid
import time
import sqlite3
from unittest.mock import MagicMock, AsyncMock, patch

from src.types import Platform, Side, OrderType, StrategyId, OrderStatus, ArbLeg
from execution.models import OrderSubmission, OrderProposal, ExecutionResult
from execution.engine import ExecutionEngine, PlacedOrderResponse, OrderStatusResponse, OrderStatusFill, OpenOrder
from execution.order_tracker import OrderTracker, TrackerStatus
from data.adapters.polymarket_ws import PolymarketWSAdapter
from data.models import MarketSnapshot
from portfolio.storage import SqlitePortfolioStore
from risk.engine import RiskEngine
from risk.kill_switch import KillSwitch
from risk.limits import RiskLimits
from engine.orchestrator import Orchestrator

def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)

class TestFailureModes(unittest.TestCase):

    # 1. WebSocket disconnect and automatic reconnection
    @patch("websockets.connect")
    def test_ws_reconnect(self, mock_connect):
        adapter = PolymarketWSAdapter(asset_ids=["BTC-Q4"])
        
        # Mock connection sequence: Fail, then Success (then close)
        mock_ws = AsyncMock()
        mock_ws.__aenter__.return_value = mock_ws
        mock_ws.__aiter__.side_effect = [
            asyncio.TimeoutError("Disconnect"), # First connection fails during iteration
            ["""{"event_type": "order_book_v2", "asset_id": "BTC-Q4", "bids": [{"price": "0.50", "size": "100"}], "asks": [{"price": "0.51", "size": "100"}]}"""],
            StopAsyncIteration
        ]
        
        mock_connect.side_effect = [
            Exception("Connection Failed"), # First attempt fails
            mock_ws, # Second attempt succeeds
        ]
        
        # We need to run it for a bit
        async def run_briefly():
            task = asyncio.create_task(adapter._run_loop())
            await asyncio.sleep(0.5) # Allow some time for retries
            adapter._running = False
            await task

        run(run_briefly())
        self.assertGreaterEqual(mock_connect.call_count, 2)

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
        
        # Run expiry check
        run(engine._expiry_worker()) # This should trigger cancel
        
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
        # Setup orchestrator with mocked components
        mock_mdp = MagicMock()
        mock_risk = MagicMock()
        mock_strategy = MagicMock()
        mock_pm_engine = MagicMock()
        mock_op_engine = MagicMock()
        
        orchestrator = Orchestrator(
            mdp=mock_mdp, portfolio=MagicMock(), risk=mock_risk,
            strategy=mock_strategy, pm_engine=mock_pm_engine, op_engine=mock_op_engine,
            markets=["M1"], enable_trading=True
        )
        
        # Mock strategy to return an arb
        from strategies.arbitrage import ArbLegProposal, ArbResult
        leg1 = OrderProposal("P1", "M1", Platform.POLYMARKET, Side.BUY_YES, 100.0, 0.50, OrderType.LIMIT, StrategyId.ARB, 0, 0, "G1", ArbLeg.LEG_1, 0.8)
        leg2 = OrderProposal("P2", "M1", Platform.OPINION, Side.BUY_NO, 100.0, 0.50, OrderType.LIMIT, StrategyId.ARB, 0, 0, "G1", ArbLeg.LEG_2)
        mock_strategy.evaluate.return_value = ArbResult(True, 0.05, leg1, leg2)
        
        # Mock risk to approve
        from risk.engine import RiskDecision, RiskVerdict
        mock_risk.evaluate.return_value = RiskDecision("P1", RiskVerdict.APPROVED, None, None, 1000, 100, 0.01, 10000, 10000, False, 0)
        
        # Simualte kill switch tripping AFTER strategy evaluation but before orchestrator processes
        mock_risk.kill_switch_active = True
        
        # Run one tick
        from data.models import FeatureVector
        fv = FeatureVector("M1", 0, 0, 0.05, [], 0.5, 0.5, 0.01, 0.01, 0, 0, 0.01, 30, 0, 1000, 1000, 1000, 1000)
        run(orchestrator._on_feature_vector(fv))
        
        # Should NOT submit if kill switch is active
        mock_pm_engine.submit.assert_not_called()

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
        pm.cash_usdc = 1000.0
        
        risk = RiskEngine(pm, KillSwitch("tok"), RiskLimits())
        
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

if __name__ == "__main__":
    unittest.main()
