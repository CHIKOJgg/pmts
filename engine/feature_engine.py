"""engine/feature_engine.py — Converts MarketSnapshots into FeatureVectors."""
from __future__ import annotations

import asyncio
import collections
import logging
import math
import statistics
import time
from typing import Callable, Coroutine, Deque, Dict, List, Optional, Tuple

from data.models import FeatureVector, MarketSnapshot
from portfolio.manager import PortfolioManager
from src.types import Platform

logger = logging.getLogger(__name__)

VOL_WINDOW_MS: int = 30_000     # 30-second rolling window for vol
MIN_VOL_TICKS: int = 5          # minimum ticks before vol is reported
STALE_MS:      int = 2_000      # snapshots older than this are stale

FEES: Dict[Platform, int] = {
    Platform.POLYMARKET: 20,
    Platform.OPINION:    25,
}

_FV_CB = Callable[[FeatureVector], Coroutine]


class FeatureEngine:
    """
    Converts MarketSnapshot events into FeatureVectors.

    Subscribe to MarketDataProvider via add_callback(fe.on_snapshot).
    The feature engine fans out FeatureVectors to its own subscribers.

    Vol computation: maintains a per-market ring buffer of (ts, yes_mid) tuples.
    Returns None until at least MIN_VOL_TICKS entries exist in the 30s window.
    """

    def __init__(self, portfolio: PortfolioManager) -> None:
        self._portfolio  = portfolio
        self._callbacks: List[_FV_CB] = []
        self._snaps:     Dict[Tuple[str, Platform], MarketSnapshot] = {}
        self._history:   Dict[str, Deque[Tuple[int, float]]] = {}
        self.vectors_emitted: int = 0

    def add_callback(self, cb: _FV_CB) -> None:
        self._callbacks.append(cb)

    async def on_snapshot(self, snap: MarketSnapshot) -> None:
        """Process one incoming snapshot and emit a FeatureVector if possible."""
        now  = _now_ms()
        key  = (snap.market_id, snap.platform)
        self._snaps[key] = snap

        # Update vol history
        hist = self._history.setdefault(snap.market_id, collections.deque())
        hist.append((snap.received_ts or snap.ts, snap.yes_mid))

        # Get counterpart snapshot
        other_plat = (
            Platform.OPINION if snap.platform == Platform.POLYMARKET
            else Platform.POLYMARKET
        )
        other = self._snaps.get((snap.market_id, other_plat))

        # Canonical order: PM first
        pm = snap  if snap.platform == Platform.POLYMARKET else other
        op = other if snap.platform == Platform.POLYMARKET else snap

        # Determine stale platforms.
        # Staleness is checked relative to snapshot's own timestamps (not wall-clock)
        # so backtest mode works correctly with historical/simulated timestamps.
        stale: List[Platform] = []
        for s, p in [(pm, Platform.POLYMARKET), (op, Platform.OPINION)]:
            if s is None:
                stale.append(p)
            elif s.is_stale:
                stale.append(p)
            elif (s.received_ts - s.ts) > STALE_MS:
                # Snapshot was already stale when received (from MDP staleness check)
                stale.append(p)

        # Compute arb signal
        if stale or pm is None or op is None:
            arb_signal = math.nan
            if not stale:
                stale = [other_plat]
        else:
            fee_pm = FEES[Platform.POLYMARKET] / 10_000
            fee_op = FEES[Platform.OPINION]    / 10_000
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

        vol_30s = self._vol(snap.market_id, now)
        delta   = self._portfolio.get_delta(snap.market_id)

        try:
            fv = FeatureVector(
                market_id=snap.market_id,
                ts=snap.ts,
                computed_ts=now,
                arb_signal=arb_signal,
                stale_markets=stale,
                mid_pm=max(0.001, min(0.999, _safe(pm, "yes_mid", 0.5))),
                mid_op=max(0.001, min(0.999, _safe(op, "yes_mid", 0.5))),
                spread_pm=max(0.0, _safe(pm, "yes_spread", 0.0)),
                spread_op=max(0.0, _safe(op, "yes_spread", 0.0)),
                ofi_pm=max(-1.0, min(1.0, _ofi(pm))),
                ofi_op=max(-1.0, min(1.0, _ofi(op))),
                vol_30s=vol_30s,
                days_to_resolution=snap.days_to_resolution,
                portfolio_delta=delta.net_delta,
                bid_depth_pm=_safe(pm, "bid_depth_usdc", 0.0),
                ask_depth_pm=_safe(pm, "ask_depth_usdc", 0.0),
                bid_depth_op=_safe(op, "bid_depth_usdc", 0.0),
                ask_depth_op=_safe(op, "ask_depth_usdc", 0.0),
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

    def _vol(self, market_id: str, now: int) -> Optional[float]:
        hist = self._history.get(market_id)
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


def _now_ms() -> int:
    return int(time.time() * 1000)