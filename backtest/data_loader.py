"""backtest/data_loader.py — Load historical market data for backtesting."""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from typing import Dict, List

from data.models import MarketSnapshot
from src.enums import Platform


@dataclass
class HistoricalTick:
    timestamp_ms: int
    market_id: str
    platform: Platform
    yes_bid: float
    yes_ask: float
    no_bid: float
    no_ask: float
    bid_depth_usdc: float
    ask_depth_usdc: float


class HistoricalDataLoader:
    @staticmethod
    def load_csv(file_path: str) -> Dict[str, List[MarketSnapshot]]:
        snapshots: Dict[str, List[MarketSnapshot]] = {}

        with open(file_path, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                market_id = row["market_id"]
                platform = Platform(row["platform"])
                key = f"{market_id}:{platform.value}"

                if key not in snapshots:
                    snapshots[key] = []

                snap = MarketSnapshot(
                    market_id=market_id,
                    platform=platform,
                    yes_bid=float(row["yes_bid"]),
                    yes_ask=float(row["yes_ask"]),
                    no_bid=float(row["no_bid"]),
                    no_ask=float(row["no_ask"]),
                    bid_depth_usdc=float(row["bid_depth_usdc"]),
                    ask_depth_usdc=float(row["ask_depth_usdc"]),
                    taker_fee_bps=int(row.get("taker_fee_bps", 20)),
                    ts=int(row["timestamp_ms"]),
                    received_ts=int(row["timestamp_ms"]),
                )
                snapshots[key].append(snap)

        return snapshots

    @staticmethod
    def load_json(file_path: str) -> Dict[str, List[MarketSnapshot]]:
        with open(file_path, "r") as f:
            data = json.load(f)

        snapshots: Dict[str, List[MarketSnapshot]] = {}

        for item in data:
            market_id = item["market_id"]
            platform = Platform(item["platform"])
            key = f"{market_id}:{platform.value}"

            if key not in snapshots:
                snapshots[key] = []

            snap = MarketSnapshot(
                market_id=market_id,
                platform=platform,
                yes_bid=float(item["yes_bid"]),
                yes_ask=float(item["yes_ask"]),
                no_bid=float(item["no_bid"]),
                no_ask=float(item["no_ask"]),
                bid_depth_usdc=float(item.get("bid_depth_usdc", 100.0)),
                ask_depth_usdc=float(item.get("ask_depth_usdc", 100.0)),
                taker_fee_bps=int(item.get("taker_fee_bps", 20)),
                ts=int(item["timestamp_ms"]),
                received_ts=int(item["timestamp_ms"]),
            )
            snapshots[key].append(snap)

        return snapshots

    @staticmethod
    def merge_and_sort(
        snapshots: Dict[str, List[MarketSnapshot]],
    ) -> List[MarketSnapshot]:
        all_snaps = []
        for snap_list in snapshots.values():
            all_snaps.extend(snap_list)
        all_snaps.sort(key=lambda s: s.ts)
        return all_snaps
