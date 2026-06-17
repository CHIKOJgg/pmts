"""engine/strategy_runner.py — Isolated strategy process for multi-strategy orchestration."""

from __future__ import annotations

import asyncio
import json
import logging
import multiprocessing
import queue
from typing import Any, Dict, Optional

from data.models import MarketSnapshot
from src.clock import SimClock
from src.enums import Platform, Side, StrategyId

logger = logging.getLogger(__name__)


def _build_strategy(strategy_id: str, config: Dict[str, Any]) -> Any:
    """Instantiate a strategy based on config."""
    from engine.strategy_engine import StrategyEngine, StrategyConfig
    from strategies.arbitrage import ArbConfig
    from strategies.delta_neutral import DeltaNeutralConfig
    from strategies.correlation import CorrelationTracker

    strat_cfg = StrategyConfig(
        arb_enabled=config.get("arb_enabled", True),
        mm_enabled=config.get("mm_enabled", True),
        hedge_enabled=config.get("hedge_enabled", True),
        arb_budget_usdc=config.get("arb_budget_usdc", 1000.0),
        mm_budget_usdc=config.get("mm_budget_usdc", 1000.0),
    )
    arb_cfg = ArbConfig(
        min_net_edge=config.get("min_net_edge", 0.006),
        max_order_usdc=config.get("max_order_usdc", 200.0),
        min_order_usdc=config.get("min_order_usdc", 5.0),
    )
    dn_cfg = DeltaNeutralConfig(
        hedge_threshold=config.get("hedge_threshold", 10.0),
        mm_quote_size_usdc=config.get("mm_quote_size_usdc", 25.0),
    )

    engine = StrategyEngine(
        config=strat_cfg,
        arb_config=arb_cfg,
        dn_config=dn_cfg,
        ai_enhancer=None,
    )

    correlation = CorrelationTracker(window_size=100)

    return {
        "engine": engine,
        "correlation": correlation,
        "strategy_id": strategy_id,
    }


def run_strategy_process(
    strategy_id: str,
    config: Dict[str, Any],
    message_queue: multiprocessing.Queue[Any],
    result_queue: multiprocessing.Queue[Any],
) -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        loop.run_until_complete(
            _run_strategy(strategy_id, config, message_queue, result_queue)
        )
    except Exception as e:
        logger.error("Strategy %s crashed: %s", strategy_id, e)
        result_queue.put({"error": str(e), "strategy_id": strategy_id})
    finally:
        loop.close()


async def _run_strategy(
    strategy_id: str,
    config: Dict[str, Any],
    message_queue: multiprocessing.Queue[Any],
    result_queue: multiprocessing.Queue[Any],
) -> None:
    logger.info("Strategy %s starting...", strategy_id)

    ctx = _build_strategy(strategy_id, config)
    engine = ctx["engine"]
    correlation = ctx["correlation"]

    proposals: list[Dict[str, Any]] = []

    def on_proposal(proposal: Any) -> None:
        proposals.append({
            "proposal_id": proposal.proposal_id,
            "market_id": proposal.market_id,
            "platform": proposal.platform.value,
            "side": proposal.side.value,
            "size_usdc": proposal.size_usdc,
            "limit_price": proposal.limit_price,
            "strategy_id": proposal.strategy_id.value,
            "leg_group_id": proposal.leg_group_id,
            "leg_number": proposal.leg_number.value if proposal.leg_number else None,
        })

    engine.add_proposal_callback(on_proposal)

    while True:
        try:
            try:
                message = message_queue.get(timeout=1.0)
            except queue.Empty:
                continue

            msg_type = message.get("type", "")

            if msg_type == "shutdown":
                break

            if msg_type == "market_data":
                data = message.get("data", {})
                snapshots = data.get("snapshots", [])
                for snap_dict in snapshots:
                    snap = MarketSnapshot(**snap_dict)
                    correlation.update(snap.market_id, snap.yes_mid)

                result = await _process_market_data(strategy_id, data, engine, correlation, proposals)
                if result:
                    result_queue.put(result)

            elif msg_type == "feature_vector":
                fv_dict = message.get("feature_vector", {})
                from data.models import FeatureVector
                from data.models import VenueSnapshot
                if "venues" in fv_dict:
                    fv_dict["venues"] = {
                        Platform(k) if isinstance(k, str) else k:
                        VenueSnapshot(**v) if isinstance(v, dict) else v
                        for k, v in fv_dict["venues"].items()
                    }
                fv = FeatureVector(**fv_dict)
                mid_pm = fv.venues[Platform.POLYMARKET].mid if Platform.POLYMARKET in fv.venues else 0.5
                mid_op = fv.venues[Platform.OPINION].mid if Platform.OPINION in fv.venues else 0.5
                correlation.update(fv.market_id, (mid_pm + mid_op) / 2)
                await engine.on_feature_vector(fv)
                if proposals:
                    result_queue.put({
                        "strategy_id": strategy_id,
                        "type": "proposals",
                        "proposals": list(proposals),
                    })
                    proposals.clear()

            elif msg_type == "notify_arb_terminal":
                engine.notify_arb_terminal(message.get("size_usdc", 0.0))

            elif msg_type == "notify_mm_terminal":
                engine.notify_mm_terminal(message.get("size_usdc", 0.0))

            elif msg_type == "flush":
                engine.flush_market_state()
                result_queue.put({"strategy_id": strategy_id, "type": "flushed"})
        except Exception as exc:
            logger.error("Strategy %s message loop error: %s", strategy_id, exc, exc_info=True)
            result_queue.put({"error": str(exc), "strategy_id": strategy_id})

    logger.info("Strategy %s stopped.", strategy_id)


async def _process_market_data(
    strategy_id: str,
    data: Dict[str, Any],
    engine: Any,
    correlation: Any,
    proposals: list,
) -> Optional[Dict[str, Any]]:
    fv_data = data.get("feature_vector")
    if fv_data:
        from data.models import FeatureVector
        from data.models import VenueSnapshot

        if "venues" in fv_data:
            fv_data["venues"] = {
                Platform(k) if isinstance(k, str) else k:
                VenueSnapshot(**v) if isinstance(v, dict) else v
                for k, v in fv_data["venues"].items()
            }
        fv = FeatureVector(**fv_data)
        avg_mid = sum(v.mid for v in fv.venues.values()) / max(len(fv.venues), 1)
        correlation.update(fv.market_id, avg_mid)

        corr = correlation.get_all_correlations(fv.market_id)

        await engine.on_feature_vector(fv)

        if proposals:
            result = {
                "strategy_id": strategy_id,
                "type": "proposals",
                "proposals": list(proposals),
                "market_id": fv.market_id,
                "correlations": {k: round(v, 4) for k, v in corr.items()},
            }
            proposals.clear()
            return result

    return None
