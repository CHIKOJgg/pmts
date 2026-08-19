"""Tests for the AI signal enhancer."""
from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, patch

import pytest

from ai.enhancer import AIEnhancerConfig, AISignalEnhancer, _parse_response
from ai.signal_context import MarketRegime, SignalContext, VolRegime
from data.models import FeatureVector
from src.enums import Platform


def _make_feature_vector(
    market_id: str = "mkt-1",
    arb_signal: float = 0.01,
    mid_pm: float = 0.50,
    mid_op: float = 0.52,
    spread: float = 0.02,
    vol_30s: float = 0.001,
    days_to_resolution: float = 30.0,
) -> FeatureVector:
    from data.models import VenueSnapshot
    ts = int(time.time() * 1000)
    return FeatureVector(
        market_id=market_id,
        ts=ts,
        computed_ts=ts,
        arb_signal=arb_signal,
        stale_markets=set(),
        venues={
            Platform.POLYMARKET: VenueSnapshot(mid=mid_pm, spread=spread, ofi=0.0, bid_depth=1000.0, ask_depth=1000.0),
            Platform.OPINION: VenueSnapshot(mid=mid_op, spread=spread, ofi=0.0, bid_depth=1000.0, ask_depth=1000.0),
        },
        vol_30s=vol_30s,
        days_to_resolution=days_to_resolution,
        portfolio_delta=0.0,
    )


class TestAISignalEnhancerDisabled:
    @pytest.mark.asyncio
    async def test_returns_heuristic_when_disabled(self) -> None:
        config = AIEnhancerConfig(enabled=False)
        enhancer = AISignalEnhancer(config)
        fv = _make_feature_vector()
        result = await enhancer.enhance(fv)
        assert isinstance(result, SignalContext)
        assert result.is_fallback is True

    @pytest.mark.asyncio
    async def test_returns_heuristic_when_heuristic_only(self) -> None:
        config = AIEnhancerConfig(enabled=True, use_heuristic_only=True)
        enhancer = AISignalEnhancer(config)
        fv = _make_feature_vector()
        result = await enhancer.enhance(fv)
        assert isinstance(result, SignalContext)
        assert result.is_fallback is True


class TestAISignalEnhancerCache:
    @pytest.mark.asyncio
    async def test_cache_hit(self) -> None:
        config = AIEnhancerConfig(
            enabled=True,
            use_heuristic_only=False,
            cache_ttl_ms=10_000,
        )
        enhancer = AISignalEnhancer(config)
        fv = _make_feature_vector()

        mock_ctx = SignalContext(
            market_id=fv.market_id,
            confidence_multiplier=0.8,
            regime=MarketRegime.STABLE,
            vol_regime=VolRegime.NORMAL,
            suppress_mm=False,
            arb_quality=0.5,
            hedge_urgency=0.3,
            model_version="test",
            inference_ms=10.0,
            feature_count=20,
            is_fallback=False,
        )

        with patch.object(enhancer, "_call_api", new_callable=AsyncMock, return_value=mock_ctx):
            await enhancer.enhance(fv)
            assert enhancer.api_calls == 1

            await enhancer.enhance(fv)
            assert enhancer.cache_hits == 1
            assert enhancer.api_calls == 1


class TestAISignalEnhancerErrors:
    @pytest.mark.asyncio
    async def test_disables_after_max_errors(self) -> None:
        config = AIEnhancerConfig(
            enabled=True,
            use_heuristic_only=False,
            max_consecutive_errors=2,
        )
        enhancer = AISignalEnhancer(config)
        fv = _make_feature_vector()

        with patch.object(enhancer, "_call_api", side_effect=Exception("API down")):
            await enhancer.enhance(fv)
            assert enhancer._err_count == 1

            await enhancer.enhance(fv)
            assert enhancer._disabled is True

        result3 = await enhancer.enhance(fv)
        assert isinstance(result3, SignalContext)
        assert enhancer.heuristic_fallbacks >= 2

    @pytest.mark.asyncio
    async def test_re_enable_clears_disabled(self) -> None:
        config = AIEnhancerConfig(
            enabled=True,
            use_heuristic_only=False,
            max_consecutive_errors=1,
        )
        enhancer = AISignalEnhancer(config)
        fv = _make_feature_vector()

        with patch.object(enhancer, "_call_api", side_effect=Exception("API down")):
            await enhancer.enhance(fv)

        assert enhancer._disabled is True
        enhancer.re_enable()
        assert enhancer._disabled is False
        assert enhancer._err_count == 0


class TestParseResponse:
    def test_parses_valid_response(self) -> None:
        fv = _make_feature_vector()
        response = json.dumps({
            "regime": "trending",
            "vol_regime": "normal",
            "confidence": 0.85,
            "arb_quality": 0.7,
            "hedge_urgency": 0.3,
            "suppress_mm": False,
            "reasoning": "Strong trend detected",
        })

        ctx = _parse_response(fv, response)

        assert ctx.regime == MarketRegime.TRENDING
        assert ctx.vol_regime == VolRegime.NORMAL
        assert ctx.confidence_multiplier == pytest.approx(0.85)
        assert ctx.arb_quality == pytest.approx(0.7)
        assert ctx.hedge_urgency == pytest.approx(0.3)
        assert ctx.suppress_mm is False
        assert ctx.is_fallback is False

    def test_clamps_confidence(self) -> None:
        fv = _make_feature_vector()
        response = json.dumps({
            "regime": "stable",
            "vol_regime": "low",
            "confidence": 2.5,
            "arb_quality": 0.5,
            "hedge_urgency": 0.5,
            "suppress_mm": False,
            "reasoning": "",
        })

        ctx = _parse_response(fv, response)

        assert ctx.confidence_multiplier == pytest.approx(2.0)

    def test_clamps_arb_quality(self) -> None:
        fv = _make_feature_vector()
        response = json.dumps({
            "regime": "stable",
            "vol_regime": "low",
            "confidence": 0.8,
            "arb_quality": -0.1,
            "hedge_urgency": 0.5,
            "suppress_mm": False,
            "reasoning": "",
        })

        ctx = _parse_response(fv, response)

        assert ctx.arb_quality == pytest.approx(0.0)

    def test_handles_unknown_regime(self) -> None:
        fv = _make_feature_vector()
        response = json.dumps({
            "regime": "invalid_regime",
            "vol_regime": "unknown_vol",
            "confidence": 0.8,
            "arb_quality": 0.5,
            "hedge_urgency": 0.5,
            "suppress_mm": False,
            "reasoning": "",
        })

        ctx = _parse_response(fv, response)

        assert ctx.regime == MarketRegime.UNKNOWN
        assert ctx.vol_regime == VolRegime.NORMAL

    def test_strips_markdown_code_blocks(self) -> None:
        fv = _make_feature_vector()
        response = (
            '```json\n{"regime":"stable","vol_regime":"normal","confidence":0.8,'
            '"arb_quality":0.5,"hedge_urgency":0.5,"suppress_mm":false,"reasoning":""}\n```'
        )

        ctx = _parse_response(fv, response)

        assert ctx.regime == MarketRegime.STABLE

    def test_raises_on_invalid_json(self) -> None:
        fv = _make_feature_vector()
        with pytest.raises(Exception):
            _parse_response(fv, "not json")


class TestAIEnhancerConfig:
    def test_default_config(self) -> None:
        config = AIEnhancerConfig()
        assert config.enabled is False
        assert config.use_heuristic_only is False
        assert config.api_timeout_ms == 200
        assert config.cache_ttl_ms == 3000

    def test_custom_config(self) -> None:
        config = AIEnhancerConfig(
            enabled=True,
            use_heuristic_only=True,
            api_timeout_ms=500,
            cache_ttl_ms=5000,
        )
        assert config.enabled is True
        assert config.use_heuristic_only is True
        assert config.api_timeout_ms == 500
