"""engine/multi_strategy_orchestrator.py — Manages multiple strategy processes."""
from __future__ import annotations

import logging
import multiprocessing
import queue
from typing import Any, Dict, List

from engine.strategy_runner import run_strategy_process

logger = logging.getLogger(__name__)


class MultiStrategyOrchestrator:
    def __init__(self, strategy_configs: List[Dict[str, Any]]) -> None:
        self._configs = strategy_configs
        self._processes: Dict[str, multiprocessing.Process] = {}
        self._message_queues: Dict[str, multiprocessing.Queue[Any]] = {}
        self._result_queues: Dict[str, multiprocessing.Queue[Any]] = {}

    def start_all(self) -> None:
        for config in self._configs:
            sid = config["id"]
            mq: multiprocessing.Queue[Any] = multiprocessing.Queue()
            rq: multiprocessing.Queue[Any] = multiprocessing.Queue()

            proc = multiprocessing.Process(
                target=run_strategy_process,
                args=(sid, config, mq, rq),
                name=f"strategy-{sid}",
            )
            proc.start()

            self._processes[sid] = proc
            self._message_queues[sid] = mq
            self._result_queues[sid] = rq
            logger.info("Started strategy process: %s (pid=%d)", sid, proc.pid)

    def stop_all(self) -> None:
        for sid, proc in self._processes.items():
            logger.info("Stopping strategy process: %s", sid)
            self._message_queues[sid].put({"type": "shutdown"})
            proc.join(timeout=5.0)
            if proc.is_alive():
                logger.warning("Strategy %s did not stop gracefully, terminating", sid)
                proc.terminate()
                proc.join(timeout=2.0)

    def send_to_strategy(self, strategy_id: str, message: Dict[str, Any]) -> None:
        if strategy_id in self._message_queues:
            self._message_queues[strategy_id].put(message)
        else:
            logger.error("Unknown strategy_id: %s", strategy_id)

    def get_results(self, strategy_id: str) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        if strategy_id in self._result_queues:
            while not self._result_queues[strategy_id].empty():
                try:
                    results.append(self._result_queues[strategy_id].get_nowait())
                except queue.Empty:
                    break
        return results

    def get_all_results(self) -> Dict[str, List[Dict[str, Any]]]:
        return {sid: self.get_results(sid) for sid in self._result_queues}

    def is_strategy_alive(self, strategy_id: str) -> bool:
        if strategy_id not in self._processes:
            return False
        return self._processes[strategy_id].is_alive()

    def restart_strategy(self, strategy_id: str) -> None:
        config = next((c for c in self._configs if c["id"] == strategy_id), None)
        if config is None:
            logger.error("Cannot restart unknown strategy: %s", strategy_id)
            return

        if strategy_id in self._processes:
            self._message_queues[strategy_id].put({"type": "shutdown"})
            self._processes[strategy_id].join(timeout=5.0)
            if self._processes[strategy_id].is_alive():
                self._processes[strategy_id].terminate()
                self._processes[strategy_id].join(timeout=2.0)

        mq: multiprocessing.Queue[Any] = multiprocessing.Queue()
        rq: multiprocessing.Queue[Any] = multiprocessing.Queue()
        proc = multiprocessing.Process(
            target=run_strategy_process,
            args=(strategy_id, config, mq, rq),
            name=f"strategy-{strategy_id}",
        )
        proc.start()
        self._processes[strategy_id] = proc
        self._message_queues[strategy_id] = mq
        self._result_queues[strategy_id] = rq
        logger.info("Restarted strategy process: %s (pid=%d)", strategy_id, proc.pid)
