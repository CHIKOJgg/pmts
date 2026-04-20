"""
ai/enhancer.py — AI signal enhancer with Claude API + heuristic fallback.

Architecture:
  FeatureVector → AISignalEnhancer.enhance() → SignalContext

Isolation guarantees:
  - Input:  FeatureVector only (market data, no positions/P&L)
  - Output: SignalContext only (regime labels, confidence — no order fields)
  - No reference to RiskEngine, ExecutionEngine, or PortfolioManager
  - Timeout (default 200ms) → heuristic fallback, trading never blocked

Fallback chain:
  1. Cache hit within TTL → return cached context
  2. Claude API call (200ms timeout)
  3. Timeout or error → heuristic_enhance(fv)
  4. After MAX_ERRORS consecutive failures → AI disabled, heuristic permanent
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from dataclasses import dataclass
from typing import Optional

from data.models import FeatureVector
from ai.signal_context import (
    CONFIDENCE_MIN, CONFIDENCE_MAX,
    MarketRegime, NEUTRAL_CONTEXT, SignalContext, VolRegime,
)
from ai.heuristic import heuristic_enhance

logger = logging.getLogger(__name__)

_MODEL         = "claude-sonnet-4-20250514"
MAX_ERRORS     = 5
API_URL        = "https://api.anthropic.com/v1/messages"


@dataclass
class AIEnhancerConfig:
    enabled:                  bool  = True
    api_timeout_ms:           int   = 200
    cache_ttl_ms:             int   = 3_000
    cache_invalidation_delta: float = 0.005
    max_consecutive_errors:   int   = MAX_ERRORS
    use_heuristic_only:       bool  = False  # skip API, always use heuristic


@dataclass
class _CacheEntry:
    context:    SignalContext
    created_at: int
    arb_signal: float


class AISignalEnhancer:
    """
    Enriches FeatureVectors with AI-derived signal context.

    Drop-in enhancer for StrategyEngine. Returns SignalContext in all cases.
    Never raises. Falls back to heuristic on any failure.
    """

    def __init__(self, config: AIEnhancerConfig = AIEnhancerConfig()) -> None:
        self._cfg        = config
        self._cache:     dict[str, _CacheEntry] = {}
        self._err_count: int  = 0
        self._disabled:  bool = False

        # Metrics
        self.api_calls:           int = 0
        self.cache_hits:          int = 0
        self.heuristic_fallbacks: int = 0
        self.timeouts:            int = 0
        self.api_errors:          int = 0

    async def enhance(self, fv: FeatureVector) -> SignalContext:
        """
        Returns a SignalContext. Never raises. Falls back to heuristic.
        """
        if (not self._cfg.enabled
                or self._cfg.use_heuristic_only
                or self._disabled):
            return heuristic_enhance(fv)

        # Cache check
        cached = self._get_cached(fv)
        if cached is not None:
            self.cache_hits += 1
            return cached

        # API call with timeout
        try:
            ctx = await asyncio.wait_for(
                self._call_api(fv),
                timeout=self._cfg.api_timeout_ms / 1000.0,
            )
            self._err_count = 0
            self.api_calls += 1
            self._set_cache(fv, ctx)
            return ctx

        except asyncio.TimeoutError:
            self.timeouts += 1
            logger.debug("AI timeout after %dms for %s", self._cfg.api_timeout_ms, fv.market_id)

        except Exception as exc:
            self.api_errors += 1
            self._err_count += 1
            logger.warning("AI error for %s: %s", fv.market_id, exc)
            if self._err_count >= self._cfg.max_consecutive_errors:
                self._disabled = True
                logger.error(
                    "AI enhancer disabled after %d consecutive errors — "
                    "heuristic fallback active for this session.",
                    self._err_count,
                )

        self.heuristic_fallbacks += 1
        return heuristic_enhance(fv)

    def re_enable(self) -> None:
        """Re-enable after manual operator intervention."""
        self._disabled   = False
        self._err_count  = 0
        logger.info("AI signal enhancer re-enabled")

    def reload_config(self, config: AIEnhancerConfig) -> None:
        self._cfg = config

    # ── API call ──────────────────────────────────────────────────────────────

    async def _call_api(self, fv: FeatureVector) -> SignalContext:
        """Call Claude API and parse the response."""
        prompt  = _build_prompt(fv)
        payload = json.dumps({
            "model":      _MODEL,
            "max_tokens": 400,
            "messages":   [{"role": "user", "content": prompt}],
        }).encode()

        import urllib.request, ssl

        def _do_request() -> dict:
            req = urllib.request.Request(
                API_URL,
                data=payload,
                headers={
                    "Content-Type":       "application/json",
                    "anthropic-version":  "2023-06-01",
                },
                method="POST",
            )
            ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, context=ctx, timeout=2) as resp:
                return json.loads(resp.read())

        loop = asyncio.get_event_loop()
        raw  = await loop.run_in_executor(None, _do_request)

        text = "".join(
            b.get("text", "")
            for b in raw.get("content", [])
            if b.get("type") == "text"
        ).strip()

        return _parse_response(fv, text)

    # ── Cache ─────────────────────────────────────────────────────────────────

    def _get_cached(self, fv: FeatureVector) -> Optional[SignalContext]:
        entry = self._cache.get(fv.market_id)
        if entry is None:
            return None
        now = _now_ms()
        if now - entry.created_at > self._cfg.cache_ttl_ms:
            del self._cache[fv.market_id]
            return None
        curr = fv.arb_signal if not math.isnan(fv.arb_signal) else 0.0
        if abs(curr - entry.arb_signal) > self._cfg.cache_invalidation_delta:
            del self._cache[fv.market_id]
            return None
        return entry.context

    def _set_cache(self, fv: FeatureVector, ctx: SignalContext) -> None:
        self._cache[fv.market_id] = _CacheEntry(
            context=ctx,
            created_at=_now_ms(),
            arb_signal=fv.arb_signal if not math.isnan(fv.arb_signal) else 0.0,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Prompt and response parsing
# ─────────────────────────────────────────────────────────────────────────────

def _build_prompt(fv: FeatureVector) -> str:
    arb  = f"{fv.arb_signal:.4f}" if not math.isnan(fv.arb_signal) else "N/A"
    vol  = f"{fv.vol_30s:.4f}"    if fv.vol_30s is not None else "N/A"
    days = f"{fv.days_to_resolution:.1f}" if fv.days_to_resolution else "unknown"
    return (
        "You are a signal classifier for a prediction market trading system.\n"
        "Your ONLY role is classification. You cannot recommend trades.\n\n"
        "Return ONLY a valid JSON object with exactly these fields:\n"
        '{"regime":"stable|trending|mean_reverting|volatile|thin|unknown",\n'
        ' "vol_regime":"low|normal|high|spike",\n'
        f' "confidence":<float {CONFIDENCE_MIN:.2f}–{CONFIDENCE_MAX:.2f}>,\n'
        ' "arb_quality":<float 0.0–1.0>,\n'
        ' "hedge_urgency":<float 0.0–1.0>,\n'
        ' "suppress_mm":<bool>,\n'
        ' "reasoning":"<1 sentence>"}\n\n'
        f"Market: {fv.market_id}\n"
        f"PM YES mid={fv.mid_pm:.4f} spread={fv.spread_pm:.4f} OFI={fv.ofi_pm:.3f}\n"
        f"OP YES mid={fv.mid_op:.4f} spread={fv.spread_op:.4f} OFI={fv.ofi_op:.3f}\n"
        f"Arb signal={arb}  Vol-30s={vol}  Days-to-resolution={days}\n"
        f"PM ask depth=${fv.ask_depth_pm:.0f}  OP ask depth=${fv.ask_depth_op:.0f}\n"
        f"Portfolio delta={fv.portfolio_delta:.2f}\n\n"
        "Return ONLY the JSON object, no other text."
    )


def _parse_response(fv: FeatureVector, text: str) -> SignalContext:
    """Parse Claude's JSON response. Raises on parse error (caller falls back)."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines   = cleaned.split("\n")
        end     = -1 if lines[-1].strip() in ("```", "```json") else len(lines)
        cleaned = "\n".join(lines[1:end])

    data = json.loads(cleaned)

    def _regime(v: str) -> MarketRegime:
        try:
            return MarketRegime(v)
        except ValueError:
            return MarketRegime.UNKNOWN

    def _vol_regime(v: str) -> VolRegime:
        try:
            return VolRegime(v)
        except ValueError:
            return VolRegime.NORMAL

    confidence    = float(data.get("confidence",    1.0))
    arb_quality   = float(data.get("arb_quality",   0.5))
    hedge_urgency = float(data.get("hedge_urgency", 0.0))
    suppress_mm   = bool(data.get("suppress_mm", False))
    reasoning     = str(data.get("reasoning", ""))[:300]

    # Clamp to valid ranges
    confidence    = max(CONFIDENCE_MIN, min(CONFIDENCE_MAX, confidence))
    arb_quality   = max(0.0,           min(1.0,             arb_quality))
    hedge_urgency = max(0.0,           min(1.0,             hedge_urgency))

    if reasoning:
        logger.debug("AI[%s]: %s", fv.market_id, reasoning)

    return SignalContext(
        market_id=fv.market_id,
        confidence_multiplier=confidence,
        regime=_regime(data.get("regime", "unknown")),
        vol_regime=_vol_regime(data.get("vol_regime", "normal")),
        suppress_mm=suppress_mm,
        arb_quality=arb_quality,
        hedge_urgency=hedge_urgency,
        model_version="claude-v1",
        inference_ms=0.0,
        feature_count=20,
        is_fallback=False,
    )


def _now_ms() -> int:
    return int(time.time() * 1000)