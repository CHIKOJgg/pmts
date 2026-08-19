"""ml/feature_store.py — Feature store for ML pipeline."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class FeatureRecord:
    market_id: str
    timestamp_ms: int
    features: Dict[str, float]
    label: Optional[float] = None
    metadata: Optional[Dict[str, Any]] = None

    def __post_init__(self) -> None:
        if self.metadata is None:
            self.metadata = {}


class FeatureStore:
    """In-memory feature store with optional persistence.

    Stores feature vectors for training and inference.
    Supports TTL-based expiration and key-based retrieval.
    """

    def __init__(self, ttl_ms: int = 24 * 60 * 60 * 1000) -> None:
        self._ttl_ms = ttl_ms
        self._store: Dict[str, FeatureRecord] = {}
        self._feature_names: set[str] = set()

    def put(self, record: FeatureRecord) -> None:
        key = self._make_key(record.market_id, record.timestamp_ms)
        self._store[key] = record
        self._feature_names.update(record.features.keys())

    def get(self, market_id: str, timestamp_ms: int) -> Optional[FeatureRecord]:
        key = self._make_key(market_id, timestamp_ms)
        record = self._store.get(key)
        if record and not self._is_expired(record):
            return record
        return None

    def get_recent(self, market_id: str, limit: int = 100) -> List[FeatureRecord]:
        records = [
            r for r in self._store.values()
            if r.market_id == market_id and not self._is_expired(r)
        ]
        records.sort(key=lambda r: r.timestamp_ms, reverse=True)
        return records[:limit]

    def get_all_features(self) -> List[FeatureRecord]:
        return [r for r in self._store.values() if not self._is_expired(r)]

    def get_training_data(self) -> tuple[List[Dict[str, float]], List[float]]:
        labeled = [r for r in self._store.values() if r.label is not None and not self._is_expired(r)]
        labeled.sort(key=lambda r: r.timestamp_ms)
        X = [r.features for r in labeled]
        y: List[float] = [r.label for r in labeled if r.label is not None]
        return X, y

    def cleanup(self) -> int:
        expired_keys = [k for k, r in self._store.items() if self._is_expired(r)]
        for k in expired_keys:
            del self._store[k]
        return len(expired_keys)

    @property
    def feature_names(self) -> set[str]:
        return set(self._feature_names)

    @property
    def size(self) -> int:
        return len(self._store)

    def _make_key(self, market_id: str, timestamp_ms: int) -> str:
        return f"{market_id}:{timestamp_ms}"

    def _is_expired(self, record: FeatureRecord) -> bool:
        return (int(time.time() * 1000) - record.timestamp_ms) > self._ttl_ms

    def save_to_file(self, filepath: str) -> None:
        data = [asdict(r) for r in self._store.values()]
        with open(filepath, "w") as f:
            json.dump(data, f, indent=2)
        logger.info("Saved %d feature records to %s", len(data), filepath)

    def load_from_file(self, filepath: str) -> int:
        with open(filepath, "r") as f:
            data = json.load(f)
        for item in data:
            record = FeatureRecord(**item)
            self.put(record)
        logger.info("Loaded %d feature records from %s", len(data), filepath)
        return len(data)
