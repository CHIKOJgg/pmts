from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from data.adapters.synthetic import SyntheticMarketFeedAdapter
from data.market_data_provider import MarketDataProvider
from data.models import MarketSnapshot
from scripts.build_market_registry import _logical_id, _normalize_title, _pair_markets
from scripts.paper_soak import _build_runtime_env, _coverage_ok
from scripts.run_paper_validation import SYNTHETIC_MARKETS, _write_synthetic_registry
from src.enums import Platform


class TestPaperSoakHarness(unittest.TestCase):
    def test_runtime_env_loads_registry_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            registry_path = Path(tmpdir) / "registry.json"
            registry_path.write_text(
                json.dumps(
                    {
                        "BTC-Q4": {"polymarket": "pm-btc", "opinion": "op-btc"},
                        "ETH-Q1": {"polymarket": "pm-eth", "opinion": "op-eth"},
                    }
                ),
                encoding="utf-8",
            )

            args = argparse.Namespace(
                obs_port=18180,
                base_url="http://127.0.0.1:8080",
                market_registry_json=None,
                market_registry_file=str(registry_path),
                markets=None,
            )
            env, base_url = _build_runtime_env(args)

            self.assertEqual(base_url, "http://127.0.0.1:18180")
            self.assertIn("MARKET_REGISTRY_JSON", env)
            self.assertNotIn("MARKETS", env)
            self.assertEqual(json.loads(env["MARKET_REGISTRY_JSON"])["BTC-Q4"]["polymarket"], "pm-btc")

    def test_coverage_gate(self):
        metrics = {
            "market_data": {
                "markets_seen_total": 12,
                "markets_seen_by_platform": {"polymarket": 7, "opinion": 5},
            }
        }
        args = argparse.Namespace(
            min_markets_total=10,
            min_markets_polymarket=5,
            min_markets_opinion=5,
        )

        ok, msg = _coverage_ok(metrics, args)
        self.assertTrue(ok)
        self.assertEqual(msg, "")

        args.min_markets_total = 13
        ok, msg = _coverage_ok(metrics, args)
        self.assertFalse(ok)
        self.assertIn("market coverage too low", msg)

    def test_market_data_counts_by_platform(self):
        provider = MarketDataProvider()
        now = 1_000_000
        snap_pm = MarketSnapshot(
            market_id="BTC-Q4",
            platform=Platform.POLYMARKET,
            yes_bid=0.49,
            yes_ask=0.50,
            no_bid=0.50,
            no_ask=0.51,
            bid_depth_usdc=100.0,
            ask_depth_usdc=100.0,
            taker_fee_bps=20,
            ts=now,
            received_ts=now,
        )
        snap_op = MarketSnapshot(
            market_id="BTC-Q4",
            platform=Platform.OPINION,
            yes_bid=0.48,
            yes_ask=0.49,
            no_bid=0.51,
            no_ask=0.52,
            bid_depth_usdc=100.0,
            ask_depth_usdc=100.0,
            taker_fee_bps=25,
            ts=now,
            received_ts=now,
        )

        import asyncio

        asyncio.run(provider.ingest(snap_pm))
        asyncio.run(provider.ingest(snap_op))

        counts = provider.get_market_counts_by_platform()
        self.assertEqual(provider.get_total_markets_seen(), 2)
        self.assertEqual(counts["polymarket"], 1)
        self.assertEqual(counts["opinion"], 1)

    def test_market_registry_builder_is_stable(self):
        self.assertEqual(_normalize_title("Will BTC reach $100k?"), "will btc reach 100k")
        self.assertEqual(
            _logical_id("Will BTC reach $100k?", "Will BTC reach $100k?", "pm-1", "op-1"),
            _logical_id("Will BTC reach $100k?", "Will BTC reach $100k?", "pm-1", "op-1"),
        )

    def test_market_registry_pairing_matches_similar_titles(self):
        pm = [
            {"id": "pm-1", "title": "Will BTC reach $100k by 2026?", "norm": _normalize_title("Will BTC reach $100k by 2026?")},
        ]
        op = [
            {"id": "op-1", "title": "Will BTC hit 100k by 2026?", "norm": _normalize_title("Will BTC hit 100k by 2026?")},
        ]
        pairs = _pair_markets(pm, op, min_score=0.5)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0].pm["id"], "pm-1")
        self.assertEqual(pairs[0].op["id"], "op-1")

    def test_synthetic_registry_writer(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "market_registry.json"
            _write_synthetic_registry(path, SYNTHETIC_MARKETS[:5])
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(data), 5)
            self.assertIn("BTC-Q4", data)
            self.assertEqual(data["BTC-Q4"]["polymarket"], "pm_btc_q4")

    def test_synthetic_feed_emits_snapshots(self):
        async def run_test():
            provider = MarketDataProvider()
            feed = SyntheticMarketFeedAdapter(
                market_ids=["BTC-Q4", "ETH-Q1"],
                platform=Platform.POLYMARKET,
                taker_fee_bps=20,
                seed=7,
                tick_interval_s=0.05,
            )
            feed.set_snapshot_callback(provider.ingest)
            await feed.start()
            await asyncio.sleep(0.15)
            await feed.stop()
            self.assertGreaterEqual(provider.snapshots_received, 1)
            self.assertIn("BTC-Q4", provider.get_all_markets())

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main(verbosity=2)
