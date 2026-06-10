"""engine/feature_engine.py — Converts MarketSnapshots into FeatureVectors."""

from __future__ import annotations

import collections
import logging
import math
import statistics
from typing import Any, Callable, Coroutine, Deque, Dict, List, Optional, Tuple

from data.models import FeatureVector, MarketSnapshot, VenueSnapshot
from portfolio.manager import PortfolioManager
from src.clock import Clock, LiveClock
from src.enums import Platform
from strategies.correlation import CorrelationTracker

logger = logging.getLogger(__name__)

VOL_WINDOW_MS: int = 30_000  # 30-second rolling window for vol
MIN_VOL_TICKS: int = 5  # minimum ticks before vol is reported
STALE_MS: int = 2_000  # snapshots older than this are stale

FEES: Dict[Platform, int] = {
    Platform.POLYMARKET: 20,
    Platform.OPINION: 25,
}

_FV_CB = Callable[[FeatureVector], Coroutine[Any, Any, None]]


class FeatureEngine:
    """
    Converts MarketSnapshot events into FeatureVectors.

    Subscribe to MarketDataProvider via add_callback(fe.on_snapshot).
    The feature engine fans out FeatureVectors to its own subscribers.

    Vol computation: maintains a per-market ring buffer of (ts, yes_mid) tuples.
    Returns None until at least MIN_VOL_TICKS entries exist in the 30s window.
    """

    def __init__(self, portfolio: PortfolioManager, clock: Optional[Clock] = None) -> None:
        self._portfolio = portfolio
        self._clock = clock or LiveClock()
        self._callbacks: List[_FV_CB] = []
        self._snaps: Dict[Tuple[str, Platform], MarketSnapshot] = {}
        self._history: Dict[Tuple[str, Platform], Deque[Tuple[int, float]]] = {}
        self._correlation = CorrelationTracker(window_size=100)
        self.vectors_emitted: int = 0

    def add_callback(self, cb: _FV_CB) -> None:
        self._callbacks.append(cb)

    @property
    def correlation_tracker(self) -> CorrelationTracker:
        return self._correlation

    async def on_snapshot(self, snap: MarketSnapshot) -> None:
        """Process one incoming snapshot and emit a FeatureVector if possible."""
        now = self._clock.now_ms()
        key = (snap.market_id, snap.platform)
        self._snaps[key] = snap

        # Update vol history (per-platform to avoid cross-platform contamination)
        plat_key = (snap.market_id, snap.platform)
        hist = self._history.setdefault(plat_key, collections.deque())
        hist.append((snap.received_ts or snap.ts, snap.yes_mid))
        self._portfolio.record_price_timestamp(snap.market_id, snap.platform, snap.received_ts or snap.ts)

        # Track cross-market price correlations
        self._correlation.update(snap.market_id, snap.yes_mid)

        # Get counterpart snapshot
        other_plat = Platform.OPINION if snap.platform == Platform.POLYMARKET else Platform.POLYMARKET
        other = self._snaps.get((snap.market_id, other_plat))

        # Canonical order: PM first
        pm = snap if snap.platform == Platform.POLYMARKET else other
        op = other if snap.platform == Platform.POLYMARKET else snap

        # Determine stale platforms.
        # Staleness is checked relative to snapshot's own timestamps (not wall-clock)
        # so backtest mode works correctly with historical/simulated timestamps.
        stale: List[Platform] = []
        for s, p in [(pm, Platform.POLYMARKET), (op, Platform.OPINION)]:
            if s is None:
                stale.append(p)
                continue

            # Check if snapshot was marked as stale by MDP
            if s.is_stale:
                stale.append(p)
                continue

            # Check age based on received timestamp
            # Use received_ts if available, otherwise use ts as fallback
            check_ts = s.received_ts or s.ts
            age_ms = now - check_ts

            if age_ms > STALE_MS * 2:  # More lenient for backtest scenarios
                stale.append(p)

        # Compute arb signal
        if stale or pm is None or op is None:
            arb_signal = math.nan
            if not stale:
                stale = [other_plat]
        else:
            fee_pm = FEES[Platform.POLYMARKET] / 10_000
            fee_op = FEES[Platform.OPINION] / 10_000
            arb_signal = 1.0 - pm.yes_ask - op.no_ask - fee_pm - fee_op

        # OFI: (bid_depth - ask_depth) / total_depth
        def _ofi(s: Optional[MarketSnapshot]) -> float:
            if s is None:
                return 0.0
            total = s.bid_depth_usdc + s.ask_depth_usdc
            if total < 1e-6:
                return 0.0
            return (s.bid_depth_usdc - s.ask_depth_usdc) / total

        def _safe(s: Optional[MarketSnapshot], attr: str, fallback: float) -> float:
            return getattr(s, attr) if s is not None else fallback

        vol_30s = self._vol(plat_key, now)
        delta = self._portfolio.get_delta(snap.market_id)

        try:
            vol_regime = self._vol_regime(vol_30s)

            def _build_vs(s: Optional[MarketSnapshot]) -> VenueSnapshot:
                if s is None:
                    return VenueSnapshot(mid=0.5, spread=0.0, ofi=0.0, bid_depth=0.0, ask_depth=0.0)
                return VenueSnapshot(
                    mid=max(0.001, min(0.999, s.yes_mid)),
                    spread=max(0.0, s.yes_spread),
                    ofi=max(-1.0, min(1.0, _ofi(s))),
                    bid_depth=s.bid_depth_usdc,
                    ask_depth=s.ask_depth_usdc,
                )

            venues = {
                Platform.POLYMARKET: _build_vs(pm),
                Platform.OPINION: _build_vs(op),
            }
            fv = FeatureVector(
                market_id=snap.market_id,
                ts=snap.ts,
                computed_ts=now,
                arb_signal=arb_signal,
                stale_markets=stale,
                venues=venues,
                vol_30s=vol_30s,
                vol_regime=vol_regime,
                days_to_resolution=snap.days_to_resolution,
                portfolio_delta=delta.net_delta,
                correlations=self._correlation.get_all_correlations(snap.market_id),
            )
        except Exception as exc:
            logger.debug("FeatureVector build failed for %s: %s", snap.market_id, exc)
            return

        self.vectors_emitted += 1
        for cb in self._callbacks:
            try:
                await cb(fv)
            except Exception as exc:
                logger.error("FeatureVector callback raised: %s", exc, exc_info=True)

    def _vol(self, key: Tuple[str, Platform], now: int) -> Optional[float]:
        hist = self._history.get(key)
        if not hist:
            return None
        cutoff = now - VOL_WINDOW_MS
        while hist and hist[0][0] < cutoff:
            hist.popleft()
        if len(hist) < MIN_VOL_TICKS:
            return None
        try:
            return statistics.stdev(p for _, p in hist)
        except statistics.StatisticsError:
            return None

    def _vol_regime(self, vol_30s: Optional[float]) -> Optional[str]:
        """Classify volatility into regime based on 30s rolling volatility."""
        if vol_30s is None:
            return None
        if vol_30s < 0.005:
            return "LOW"
        elif vol_30s < 0.015:
            return "NORMAL"
        elif vol_30s < 0.04:
            return "HIGH"
        else:
            return "SPIKE"
