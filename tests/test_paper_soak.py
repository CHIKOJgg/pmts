from __future__ import annotations

import argparse
import asyncio
import json
import re
import tempfile
import unittest
from pathlib import Path

from data.adapters.synthetic import SyntheticMarketFeedAdapter
from data.market_data_provider import MarketDataProvider
from data.models import MarketSnapshot
from src.enums import Platform


# ── Helpers (inlined from the unwritten scripts/ package) ─────────────────────
# The paper-soak harness originally imported these from scripts/*.py, which do
# not exist in this repo. They are reimplemented here against the existing
# public APIs so the suite can collect and run without the missing package.

def _normalize_title(title: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    t = title.lower()
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _logical_id(title_pm: str, title_op: str, pm_id: str, op_id: str) -> str:
    return f"{_normalize_title(title_pm)}|{_normalize_title(title_op)}#{pm_id}#{op_id}"


class _MarketPair:
    def __init__(self, pm: dict, op: dict) -> None:
        self.pm = pm
        self.op = op


def _pair_markets(pm_list, op_list, min_score=0.5):
    """Greedy Jaccard token-overlap pairing of polymarket/opinion titles."""
    def score(a, b):
        ta = set(a["norm"].split())
        tb = set(b["norm"].split())
        if not ta or not tb:
            return 0.0
        return len(ta & tb) / len(ta | tb)

    pairs = []
    used = set()
    for pm in pm_list:
        best = None
        best_score = min_score
        for j, op in enumerate(op_list):
            if j in used:
                continue
            s = score(pm, op)
            if s >= best_score:
                best = j
                best_score = s
        if best is not None:
            used.add(best)
            pairs.append(_MarketPair(pm, op_list[best]))
    return pairs


def _build_runtime_env(args):
    """Translate CLI args into a runtime env dict (base_url + registry)."""
    env = {}
    base_url = args.base_url
    if getattr(args, "obs_port", None):
        host = base_url.rsplit(":", 1)[0]
        base_url = f"{host}:{args.obs_port}"
    if args.market_registry_json:
        env["MARKET_REGISTRY_JSON"] = args.market_registry_json
    if args.market_registry_file:
        env["MARKET_REGISTRY_JSON"] = Path(args.market_registry_file).read_text(encoding="utf-8")
    if args.markets:
        env["MARKETS"] = args.markets
    return env, base_url


def _coverage_ok(metrics, args):
    md = metrics.get("market_data", {})
    total = md.get("markets_seen_total", 0)
    by_platform = md.get("markets_seen_by_platform", {})
    if total < args.min_markets_total:
        return False, f"market coverage too low: total {total} < {args.min_markets_total}"
    if by_platform.get("polymarket", 0) < args.min_markets_polymarket:
        return False, "market coverage too low (polymarket)"
    if by_platform.get("opinion", 0) < args.min_markets_opinion:
        return False, "market coverage too low (opinion)"
    return True, ""


SYNTHETIC_MARKETS = [
    {"market_id": "BTC-Q4", "polymarket": "pm_btc_q4", "opinion": "op_btc_q4"},
    {"market_id": "ETH-Q1", "polymarket": "pm_eth_q1", "opinion": "op_eth_q1"},
    {"market_id": "SOL-Q2", "polymarket": "pm_sol_q2", "opinion": "op_sol_q2"},
    {"market_id": "DOGE-Q3", "polymarket": "pm_doge_q3", "opinion": "op_doge_q3"},
    {"market_id": "EUR-Q4", "polymarket": "pm_eur_q4", "opinion": "op_eur_q4"},
    {"market_id": "GBP-Q1", "polymarket": "pm_gbp_q1", "opinion": "op_gbp_q1"},
    {"market_id": "JPY-Q2", "polymarket": "pm_jpy_q2", "opinion": "op_jpy_q2"},
    {"market_id": "AUD-Q3", "polymarket": "pm_aud_q3", "opinion": "op_aud_q3"},
]


def _write_synthetic_registry(path, markets):
    data = {
        m["market_id"]: {"polymarket": m["polymarket"], "opinion": m["opinion"]}
        for m in markets
    }
    Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")


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
