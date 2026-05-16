"""engine/strategy_runner.py — Isolated strategy process."""
from __future__ import annotations

import asyncio
import logging
import multiprocessing
from typing import Any, Dict

logger = logging.getLogger(__name__)


def run_strategy_process(
    strategy_id: str,
    config: Dict[str, Any],
    message_queue: multiprocessing.Queue,
    result_queue: multiprocessing.Queue,
) -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        loop.run_until_complete(_run_strategy(strategy_id, config, message_queue, result_queue))
    except Exception as e:
        logger.error("Strategy %s crashed: %s", strategy_id, e)
        result_queue.put({"error": str(e), "strategy_id": strategy_id})
    finally:
        loop.close()


async def _run_strategy(
    strategy_id: str,
    config: Dict[str, Any],
    message_queue: multiprocessing.Queue,
    result_queue: multiprocessing.Queue,
) -> None:
    logger.info("Strategy %s starting...", strategy_id)

    while True:
        try:
            message = message_queue.get(timeout=1.0)
            if message["type"] == "shutdown":
                break
            if message["type"] == "market_data":
                result = await _process_market_data(strategy_id, message["data"])
                result_queue.put(result)
        except multiprocessing.queues.Empty:
            continue

    logger.info("Strategy %s stopped.", strategy_id)


async def _process_market_data(
    strategy_id: str,
    data: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "strategy_id": strategy_id,
        "type": "market_data_processed",
        "timestamp": data.get("timestamp", 0),
    }
