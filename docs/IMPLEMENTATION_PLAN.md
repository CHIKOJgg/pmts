# PMTS Detailed Implementation Plan: Phases 5-10

This document provides step-by-step implementation guidance for each phase. Each task includes:
- Specific files to create/modify
- Code changes required
- Acceptance criteria
- Estimated effort
- Dependencies

---

## PHASE 5: Production Hardening (Weeks 1-3)

### 5.1 Alerting Integration

**Objective:** Operator receives immediate notification of critical events via Slack, email, and configurable webhooks.

#### Step 5.1.1: Create Alerting Module

**New File:** `infrastructure/alerting.py`

```python
"""infrastructure/alerting.py — Alert routing and notification channels."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional

import aiohttp

logger = logging.getLogger(__name__)


class AlertSeverity(Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AlertChannel(Enum):
    SLACK = "slack"
    EMAIL = "email"
    WEBHOOK = "webhook"


@dataclass
class Alert:
    severity: AlertSeverity
    title: str
    message: str
    source: str
    timestamp: int = field(default_factory=lambda: int(time.time() * 1000))
    metadata: Dict = field(default_factory=dict)
    alert_id: str = field(default_factory=lambda: str(time.time()))


@dataclass
class AlertConfig:
    slack_webhook_url: Optional[str] = None
    slack_channel: str = "#trading-alerts"
    email_smtp_host: str = "smtp.gmail.com"
    email_smtp_port: int = 587
    email_username: Optional[str] = None
    email_password: Optional[str] = None
    email_recipients: List[str] = field(default_factory=list)
    webhook_urls: List[str] = field(default_factory=list)
    rate_limit_per_minute: int = 10
    dedup_window_seconds: int = 300


class AlertRouter:
    """
    Routes alerts to configured channels with rate limiting and deduplication.
    """

    def __init__(self, config: AlertConfig) -> None:
        self._config = config
        self._session: Optional[aiohttp.ClientSession] = None
        self._alert_counts: Dict[str, int] = {}
        self._last_alert_times: Dict[str, int] = {}
        self._total_sent: int = 0
        self._total_suppressed: int = 0

    async def send(self, alert: Alert) -> bool:
        if not self._should_send(alert):
            self._total_suppressed += 1
            return False

        tasks = []
        if self._config.slack_webhook_url:
            tasks.append(self._send_slack(alert))
        if self._config.email_username and self._config.email_recipients:
            tasks.append(self._send_email(alert))
        for url in self._config.webhook_urls:
            tasks.append(self._send_webhook(url, alert))

        if not tasks:
            logger.info("Alert (no channels configured): [%s] %s", alert.severity.value, alert.title)
            return False

        results = await asyncio.gather(*tasks, return_exceptions=True)
        success = all(not isinstance(r, Exception) for r in results)
        if success:
            self._total_sent += 1
        return success

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def _should_send(self, alert: Alert) -> bool:
        now = int(time.time())
        key = f"{alert.title}:{alert.source}"

        if key in self._last_alert_times:
            if now - self._last_alert_times[key] < self._config.dedup_window_seconds:
                return False

        minute_key = f"minute_{now // 60}"
        if self._alert_counts.get(minute_key, 0) >= self._config.rate_limit_per_minute:
            return False

        self._alert_counts[minute_key] = self._alert_counts.get(minute_key, 0) + 1
        self._last_alert_times[key] = now
        return True

    async def _send_slack(self, alert: Alert) -> None:
        session = await self._get_session()
        color = {"info": "#36a64f", "warning": "#ff9500", "critical": "#ff0000"}[alert.severity.value]

        payload = {
            "channel": self._config.slack_channel,
            "attachments": [{
                "color": color,
                "title": f"[{alert.severity.value.upper()}] {alert.title}",
                "text": alert.message,
                "fields": [
                    {"title": "Source", "value": alert.source, "short": True},
                    {"title": "Time", "value": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(alert.timestamp / 1000)), "short": True},
                ],
            }],
        }

        async with session.post(self._config.slack_webhook_url, json=payload) as resp:
            resp.raise_for_status()

    async def _send_email(self, alert: Alert) -> None:
        import smtplib
        from email.mime.text import MIMEText

        msg = MIMEText(f"{alert.message}\n\nSource: {alert.source}\nTime: {alert.timestamp}")
        msg["Subject"] = f"[PMTS {alert.severity.value.upper()}] {alert.title}"
        msg["From"] = self._config.email_username
        msg["To"] = ", ".join(self._config.email_recipients)

        with smtplib.SMTP(self._config.email_smtp_host, self._config.email_smtp_port) as server:
            server.starttls()
            server.login(self._config.email_username, self._config.email_password)
            server.sendmail(msg["From"], self._config.email_recipients, msg.as_string())

    async def _send_webhook(self, url: str, alert: Alert) -> None:
        session = await self._get_session()
        payload = {
            "severity": alert.severity.value,
            "title": alert.title,
            "message": alert.message,
            "source": alert.source,
            "timestamp": alert.timestamp,
            "metadata": alert.metadata,
        }
        async with session.post(url, json=payload) as resp:
            resp.raise_for_status()
```

**Acceptance Criteria:**
- [ ] `AlertRouter` sends to Slack, email, and webhooks
- [ ] Rate limiting suppresses >10 alerts/minute
- [ ] Deduplication suppresses duplicate alerts within 5 minutes
- [ ] Graceful handling of network failures
- [ ] Metrics: `alerts_sent_total`, `alerts_suppressed_total`

#### Step 5.1.2: Add Alert Config to Settings

**Modify:** `config/settings.py`

Add new dataclass:
```python
@dataclass
class AlertConfig:
    slack_webhook_url: str = field(default_factory=lambda: _e("ALERT_SLACK_WEBHOOK", ""))
    email_smtp_host: str = field(default_factory=lambda: _e("ALERT_EMAIL_SMTP_HOST", "smtp.gmail.com"))
    email_smtp_port: int = field(default_factory=lambda: _ei("ALERT_EMAIL_SMTP_PORT", 587))
    email_username: str = field(default_factory=lambda: _e("ALERT_EMAIL_USERNAME", ""))
    email_password: str = field(default_factory=lambda: _e("ALERT_EMAIL_PASSWORD", ""))
    email_recipients: str = field(default_factory=lambda: _e("ALERT_EMAIL_RECIPIENTS", ""))
    webhook_urls: str = field(default_factory=lambda: _e("ALERT_WEBHOOK_URLS", ""))
```

Add to `Settings`:
```python
alerts: AlertConfig = field(default_factory=AlertConfig)
```

#### Step 5.1.3: Wire Alerts into Existing Components

**Modify:** `risk/engine.py`

In `_fire_kill_switch()`:
```python
if self._alert_router:
    alert = Alert(
        severity=AlertSeverity.CRITICAL,
        title="Kill Switch Activated",
        message=f"Drawdown {drawdown:.2%} exceeded kill threshold",
        source="RiskEngine",
        metadata={"drawdown": drawdown, "triggering_id": triggering_id},
    )
    asyncio.create_task(self._alert_router.send(alert))
```

**Modify:** `data/market_data_provider.py`

In `ingest()`, add staleness alert:
```python
if staleness > STALE_THRESHOLD_MS * 5:
    if self._alert_router:
        alert = Alert(
            severity=AlertSeverity.WARNING,
            title="Stale Market Data",
            message=f"Data for {snapshot.market_id} is {staleness}ms old",
            source="MarketDataProvider",
        )
        asyncio.create_task(self._alert_router.send(alert))
```

**Modify:** `execution/engine.py`

In `_execute_submission()`, add API error alert:
```python
if attempt == MAX_SUBMIT_ATTEMPTS - 1:
    if self._alert_router:
        alert = Alert(
            severity=AlertSeverity.WARNING,
            title="Order Submission Failed",
            message=f"Failed to submit order after {MAX_SUBMIT_ATTEMPTS} attempts",
            source="ExecutionEngine",
            metadata={"proposal_id": submission.proposal_id},
        )
        asyncio.create_task(self._alert_router.send(alert))
```

**Modify:** `main.py`

In `run_live()` and `run_paper()`:
```python
from infrastructure.alerting import AlertConfig as AlertCfg, AlertRouter

alert_cfg = AlertCfg(
    slack_webhook_url=settings.alerts.slack_webhook_url or None,
    email_username=settings.alerts.email_username or None,
    email_password=settings.alerts.email_password or None,
    email_recipients=[r.strip() for r in settings.alerts.email_recipients.split(",") if r.strip()],
    webhook_urls=[u.strip() for u in settings.alerts.webhook_urls.split(",") if u.strip()],
)
alert_router = AlertRouter(alert_cfg)

# Pass to components
risk._alert_router = alert_router
mdp._alert_router = alert_router
pm_engine._alert_router = alert_router
op_engine._alert_router = alert_router
```

**Update:** `.env.example`

```env
# ── Alerting ──────────────────────────────────────────────────────────────────
ALERT_SLACK_WEBHOOK=
ALERT_EMAIL_SMTP_HOST=smtp.gmail.com
ALERT_EMAIL_SMTP_PORT=587
ALERT_EMAIL_USERNAME=
ALERT_EMAIL_PASSWORD=
ALERT_EMAIL_RECIPIENTS=
ALERT_WEBHOOK_URLS=
```

**Acceptance Criteria:**
- [ ] Kill switch activation sends Slack + email alert
- [ ] Stale data warning sends Slack alert
- [ ] API errors after retries send warning alert
- [ ] Rate limiting prevents alert spam
- [ ] Deduplication prevents duplicate alerts

**Estimated Effort:** 2-3 days

---

### 5.2 Unified Clock System

**Objective:** Eliminate time skew between backtest and live modes by replacing all `_now_ms()` calls with an injectable `Clock` protocol.

#### Step 5.2.1: Create Clock Protocol

**New File:** `src/clock.py`

```python
"""src/clock.py — Unified time source for live and simulated modes."""
from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    """Protocol for time sources. Implement for live or simulated time."""

    def now_ms(self) -> int:
        """Return current time in milliseconds."""
        ...

    def sleep_ms(self, ms: int) -> "Coroutine":
        """Sleep for the given number of milliseconds."""
        ...


class LiveClock:
    """Wall-clock time source for live trading."""

    def now_ms(self) -> int:
        return int(time.time() * 1000)

    async def sleep_ms(self, ms: int) -> None:
        import asyncio
        await asyncio.sleep(ms / 1000.0)


class SimClock:
    """Simulated time source for backtesting."""

    def __init__(self, start_ms: int = 0) -> None:
        self._now = start_ms

    def now_ms(self) -> int:
        return self._now

    def advance(self, ms: int) -> None:
        """Advance the simulated clock."""
        self._now += ms

    async def sleep_ms(self, ms: int) -> None:
        self.advance(ms)
```

#### Step 5.2.2: Update Each Component

For each file below, the pattern is:
1. Add `clock: Clock` parameter to `__init__`
2. Replace `_now_ms()` calls with `self._clock.now_ms()`
3. Replace `asyncio.sleep()` with `await self._clock.sleep_ms()`

**Files to Update (in order):**

| File | Changes |
|------|---------|
| `data/market_data_provider.py` | Add `clock` param, replace `_now_ms()` in `ingest()`, `get_health()` |
| `engine/feature_engine.py` | Add `clock` param, replace `_now_ms()` |
| `engine/orchestrator.py` | Add `clock` param, replace `_now_ms()` in `_route_to_engine()`, `_handle_arb_terminal()` |
| `execution/engine.py` | Add `clock` param, replace `_now_ms()` in `_execute_submission()`, `_poll_worker()`, `_expiry_worker()`, `_prune_worker()` |
| `execution/order_tracker.py` | Add `clock` param, replace `_now_ms()` in `is_expired()` |
| `portfolio/manager.py` | Add `clock` param, replace `_now_ms()` in `record_fill()`, `_snapshot_loop()` |
| `risk/engine.py` | Add `clock` param, replace `_now_ms()` in `evaluate()` |
| `backtest/engine.py` | Pass `SimClock` to all components, call `clock.advance()` each tick |

**Example Change for `execution/engine.py`:**

```python
# Before
from execution.engine import ...
def _now_ms() -> int:
    return int(time.time() * 1000)

class ExecutionEngine:
    def __init__(self, client, ...):
        ...
    async def _poll_worker(self):
        while not self._stopped:
            now = _now_ms()
            ...
            await asyncio.sleep(interval)

# After
from src.clock import Clock

class ExecutionEngine:
    def __init__(self, client, clock: Clock, ...):
        self._clock = clock
        ...
    async def _poll_worker(self):
        while not self._stopped:
            now = self._clock.now_ms()
            ...
            await self._clock.sleep_ms(int(interval * 1000))
```

#### Step 5.2.3: Update main.py

**Modify:** `main.py`

```python
from src.clock import LiveClock

# In run_live() and run_paper():
clock = LiveClock()

# Pass to all components:
mdp = MarketDataProvider(adapters=[...], clock=clock)
portfolio = PortfolioManager(..., clock=clock)
risk = RiskEngine(..., clock=clock)
pm_engine = ExecutionEngine(pm_client, clock=clock, ...)
op_engine = ExecutionEngine(op_client, clock=clock, ...)
```

#### Step 5.2.4: Update Backtest Engine

**Modify:** `backtest/engine.py`

```python
from src.clock import SimClock

# In run():
clock = SimClock(start_ms=0)

# Each tick:
for tick_data in tick_stream:
    clock.advance(tick_data.interval_ms)
    fv = FeatureVector(..., clock=clock)
    await strategy.on_feature_vector(fv)
```

**Acceptance Criteria:**
- [ ] All `_now_ms()` functions removed from codebase
- [ ] Backtest uses `SimClock`, live uses `LiveClock`
- [ ] Existing tests pass with `LiveClock` injected
- [ ] Backtest determinism preserved with `SimClock`
- [ ] No time-related bugs in either mode

**Estimated Effort:** 3-4 days

---

### 5.3 Type Safety Improvements

**Objective:** Eliminate `Any` types, add Protocols for major components, achieve mypy strict compliance.

#### Step 5.3.1: Create Protocols for Major Components

**New File:** `src/protocols.py`

```python
"""src/protocols.py — Protocol definitions for dependency injection."""
from __future__ import annotations

from typing import Dict, List, Optional, Protocol, Tuple

from src.types import Platform, StrategyId


class PortfolioProvider(Protocol):
    def cash_usdc(self) -> float: ...
    def peak_equity(self) -> float: ...
    def get_portfolio_mtm(self) -> "PortfolioSnapshot": ...
    def get_delta(self, market_id: str, platform: Platform) -> "Delta": ...
    def get_market_exposure_usdc(self, market_id: str) -> float: ...
    def get_price_age_ms(self) -> int: ...


class MarketDataProvider(Protocol):
    def get_snapshot(self, market_id: str, platform: Platform) -> Optional["MarketSnapshot"]: ...
    def get_mid_prices(self, market_id: str, platform: Platform) -> Optional[Tuple[float, float]]: ...
    def get_all_markets(self) -> set[str]: ...
    def get_health(self) -> dict: ...


class PortfolioStore(Protocol):
    def save_fill_and_position(self, fill, position, cash, peak, pnl) -> None: ...
    def load_state(self) -> dict: ...
    def save_order(self, proposal_id, submission_json, exchange_order_id) -> None: ...
    def load_active_orders(self) -> List[Tuple[str, Optional[str], str]]: ...
    def remove_order(self, proposal_id) -> None: ...
    def save_reservation(self, proposal_id, amount, platform, strategy_id) -> None: ...
    def remove_reservation(self, proposal_id) -> None: ...
    def load_reservations(self) -> Dict[str, Tuple[float, Platform, StrategyId]]: ...
    def save_kill_switch(self, active: bool) -> None: ...
    def load_kill_switch(self) -> bool: ...
    def close(self) -> None: ...
    def is_healthy(self) -> bool: ...
```

#### Step 5.3.2: Update ExecutionEngine Types

**Modify:** `execution/engine.py`

```python
# Before
def __init__(
    self,
    client: ExchangeClient,
    risk: Optional[Any] = None,
    store: Optional[Any] = None,
    mdb: Optional[Any] = None,
    ...
):

# After
from src.protocols import PortfolioStore, MarketDataProvider
from risk.engine import RiskEngine

def __init__(
    self,
    client: ExchangeClient,
    risk: Optional[RiskEngine] = None,
    store: Optional[PortfolioStore] = None,
    mdb: Optional[MarketDataProvider] = None,
    ...
):
```

#### Step 5.3.3: Update HealthMonitor Types

**Modify:** `infrastructure/observability.py`

```python
# Before
def __init__(
    self,
    mdp: Any,
    engines: List[Any],
    risk: Any,
    store: Any,
    kill_switch: Any,
    obs_server: Optional[Any] = None,
    ...
):

# After
def __init__(
    self,
    mdp: MarketDataProvider,
    engines: List[ExecutionEngine],
    risk: RiskEngine,
    store: PortfolioStore,
    kill_switch: KillSwitch,
    obs_server: Optional[ObservabilityServer] = None,
    ...
):
```

#### Step 5.3.4: Enable Strict mypy

**Modify:** `pyproject.toml`

```toml
[tool.mypy]
python_version = "3.11"
warn_return_any = true
warn_unused_configs = true
disallow_untyped_defs = true
disallow_incomplete_defs = true
check_untyped_defs = true
ignore_missing_imports = true
strict_optional = true
```

**Acceptance Criteria:**
- [ ] Zero `Any` types in public APIs
- [ ] All major components have Protocol definitions
- [ ] `mypy .` passes with zero errors
- [ ] `mypy --strict` passes on new files
- [ ] No regression in existing test coverage

**Estimated Effort:** 2-3 days

---

### 5.4 Performance and Load Testing

**Objective:** Verify system stability under high message volume and long-running conditions.

#### Step 5.4.1: Create Performance Test Suite

**New File:** `tests/test_performance.py`

```python
"""tests/test_performance.py — Performance and load tests."""
from __future__ import annotations

import asyncio
import time
import tracemalloc
from unittest.mock import AsyncMock, MagicMock

import pytest

from execution.engine import ExecutionEngine
from execution.models import OrderSubmission
from portfolio.manager import PortfolioManager
from risk.engine import RiskEngine
from risk.kill_switch import KillSwitch
from risk.limits import RiskLimits
from src.types import OrderType, Platform, Side, StrategyId


class TestRiskEngineLatency:
    """Benchmark risk engine evaluation latency."""

    @pytest.mark.asyncio
    async def test_evaluate_under_5ms(self):
        pm = MagicMock()
        pm.get_portfolio_mtm.return_value.total_equity_usdc = 10000.0
        pm.cash_usdc = 10000.0
        pm.get_price_age_ms.return_value = 100
        pm.peak_equity = 10000.0
        pm.get_market_exposure_usdc.return_value = 0.0
        pm.get_delta.return_value.net_delta = 0.0

        risk = RiskEngine(pm, KillSwitch("tok"), RiskLimits())

        proposal = OrderProposal(
            "prop-1", "M1", Platform.POLYMARKET, Side.BUY_YES,
            100.0, 0.50, OrderType.LIMIT, StrategyId.MM,
            int(time.time() * 1000) + 60_000, 0
        )

        latencies = []
        for _ in range(1000):
            start = time.perf_counter_ns()
            risk.evaluate(proposal)
            latency_ms = (time.perf_counter_ns() - start) / 1_000_000
            latencies.append(latency_ms)

        p50 = sorted(latencies)[len(latencies) // 2]
        p99 = sorted(latencies)[int(len(latencies) * 0.99)]

        assert p50 < 1.0, f"P50 latency {p50:.2f}ms > 1ms"
        assert p99 < 5.0, f"P99 latency {p99:.2f}ms > 5ms"


class TestMemoryStability:
    """Test for memory leaks over extended operation."""

    @pytest.mark.asyncio
    async def test_no_memory_leak_over_10000_orders(self):
        tracemalloc.start()

        client = MagicMock()
        client.platform = Platform.POLYMARKET
        client.place_order = AsyncMock(side_effect=Exception("No network"))

        engine = ExecutionEngine(client, max_concurrent=10)

        for i in range(10000):
            sub = OrderSubmission(
                order_id=f"ord-{i}",
                proposal_id=f"prop-{i}",
                market_id="M1",
                platform=Platform.POLYMARKET,
                side=Side.BUY_YES,
                size_usdc=100.0,
                limit_price=0.50,
                order_type=OrderType.LIMIT,
                strategy_id=StrategyId.MM,
                expiry_ms=int(time.time() * 1000) + 60_000,
                token_quantity=200.0,
                submitted_at=int(time.time() * 1000),
            )
            await engine.submit(sub)

        # Trigger pruning
        engine._prune_terminal_trackers()

        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        # Peak memory should not exceed 50MB for 10k orders
        assert peak < 50 * 1024 * 1024, f"Peak memory {peak / 1024 / 1024:.1f}MB > 50MB"


class TestThroughput:
    """Test message processing throughput."""

    @pytest.mark.asyncio
    async def test_snapshot_ingestion_throughput(self):
        from data.market_data_provider import MarketDataProvider
        from data.models import MarketSnapshot

        mdp = MarketDataProvider()
        snapshots_processed = 0

        async def count_cb(snapshot):
            nonlocal snapshots_processed
            snapshots_processed += 1

        mdp.add_callback(count_cb)

        start = time.time()
        for i in range(10000):
            snap = MarketSnapshot(
                market_id="M1",
                platform=Platform.POLYMARKET,
                yes_bid=0.50,
                yes_ask=0.51,
                no_bid=0.49,
                no_ask=0.50,
                bid_depth_usdc=1000.0,
                ask_depth_usdc=1000.0,
                taker_fee_bps=20,
                ts=int(time.time() * 1000),
                received_ts=int(time.time() * 1000),
            )
            await mdp.ingest(snap)

        elapsed = time.time() - start
        throughput = snapshots_processed / elapsed

        assert throughput > 1000, f"Throughput {throughput:.0f} snaps/s < 1000"
```

#### Step 5.4.2: Add Benchmark Script

**New File:** `scripts/benchmark.py`

```python
#!/usr/bin/env python3
"""scripts/benchmark.py — Quick benchmark for risk engine and backtest."""
import sys
import time
sys.path.insert(0, ".")

from risk.engine import RiskEngine
from risk.kill_switch import KillSwitch
from risk.limits import RiskLimits
from execution.models import OrderProposal
from src.types import OrderType, Platform, Side, StrategyId
from unittest.mock import MagicMock

def benchmark_risk_engine(iterations=10000):
    pm = MagicMock()
    pm.get_portfolio_mtm.return_value.total_equity_usdc = 10000.0
    pm.cash_usdc = 10000.0
    pm.get_price_age_ms.return_value = 100
    pm.peak_equity = 10000.0
    pm.get_market_exposure_usdc.return_value = 0.0
    pm.get_delta.return_value.net_delta = 0.0

    risk = RiskEngine(pm, KillSwitch("tok"), RiskLimits())
    proposal = OrderProposal(
        "prop-1", "M1", Platform.POLYMARKET, Side.BUY_YES,
        100.0, 0.50, OrderType.LIMIT, StrategyId.MM,
        int(time.time() * 1000) + 60_000, 0
    )

    start = time.perf_counter()
    for i in range(iterations):
        proposal.proposal_id = f"prop-{i}"
        risk.evaluate(proposal)
    elapsed = time.perf_counter() - start

    print(f"Risk Engine: {iterations} evaluations in {elapsed:.3f}s")
    print(f"  Throughput: {iterations / elapsed:.0f} eval/s")
    print(f"  Latency: {elapsed / iterations * 1000:.3f}ms/eval")

if __name__ == "__main__":
    benchmark_risk_engine()
```

**Acceptance Criteria:**
- [ ] Risk engine P50 < 1ms, P99 < 5ms
- [ ] No memory leak over 10k orders (peak < 50MB)
- [ ] Snapshot ingestion > 1000/s
- [ ] Benchmark script runs without errors
- [ ] CI includes performance tests

**Estimated Effort:** 2 days

---

## PHASE 6: Strategy Enhancements (Weeks 3-6)

### 6.1 Advanced Arbitrage Signals

**Objective:** Improve arb detection accuracy with additional signal sources.

#### Step 6.1.1: Add Order Flow Imbalance Analysis

**Modify:** `engine/feature_engine.py`

Add OFI computation:
```python
def _compute_ofi(self, prev_snap: MarketSnapshot, curr_snap: MarketSnapshot) -> float:
    """Compute Order Flow Imbalance."""
    bid_change = curr_snap.bid_depth_usdc - prev_snap.bid_depth_usdc
    ask_change = curr_snap.ask_depth_usdc - prev_snap.ask_depth_usdc
    total = abs(bid_change) + abs(ask_change)
    if total == 0:
        return 0.0
    return (bid_change - ask_change) / total
```

Add to `FeatureVector`:
```python
ofi_pm: float = 0.0
ofi_op: float = 0.0
```

#### Step 6.1.2: Implement Dynamic Min Edge

**Modify:** `strategies/arbitrage.py`

```python
def _compute_min_net_edge(self, fv: FeatureVector) -> float:
    """Adjust minimum edge based on volatility and liquidity."""
    base_edge = self._config.min_net_edge

    if fv.vol_30s is not None and fv.vol_30s > 0.01:
        base_edge *= 1.5

    depth = min(fv.bid_depth_pm, fv.ask_depth_pm, fv.bid_depth_op, fv.ask_depth_op)
    if depth < 100:
        base_edge *= 2.0

    return base_edge
```

#### Step 6.1.3: Add Cross-Market Correlation

**New File:** `strategies/correlation.py`

```python
"""strategies/correlation.py — Cross-market correlation analysis."""
from __future__ import annotations

import numpy as np
from collections import deque
from typing import Dict, List


class CorrelationTracker:
    """Tracks price correlations between markets."""

    def __init__(self, window_size: int = 100) -> None:
        self._window = window_size
        self._prices: Dict[str, deque[float]] = {}

    def update(self, market_id: str, mid_price: float) -> None:
        if market_id not in self._prices:
            self._prices[market_id] = deque(maxlen=self._window)
        self._prices[market_id].append(mid_price)

    def get_correlation(self, market_a: str, market_b: str) -> float:
        if market_a not in self._prices or market_b not in self._prices:
            return 0.0
        prices_a = list(self._prices[market_a])
        prices_b = list(self._prices[market_b])
        min_len = min(len(prices_a), len(prices_b))
        if min_len < 10:
            return 0.0
        return float(np.corrcoef(prices_a[-min_len:], prices_b[-min_len:])[0, 1])
```

**Acceptance Criteria:**
- [ ] OFI computed for both venues
- [ ] Dynamic min edge adjusts based on volatility
- [ ] Correlation tracker tracks price relationships
- [ ] Backtest shows improved arb profitability

**Estimated Effort:** 3-4 days

---

### 6.2 Market Making Improvements

**Objective:** Reduce adverse selection and improve MM profitability.

#### Step 6.2.1: Implement Adaptive Quoting

**Modify:** `strategies/delta_neutral.py`

```python
def _compute_adaptive_quote_size(self, inventory: float, max_inventory: float) -> float:
    """Reduce quote size as inventory approaches limit."""
    inventory_ratio = abs(inventory) / max_inventory
    if inventory_ratio > 0.8:
        return self._config.mm_quote_size_usdc * 0.25
    elif inventory_ratio > 0.6:
        return self._config.mm_quote_size_usdc * 0.5
    elif inventory_ratio > 0.4:
        return self._config.mm_quote_size_usdc * 0.75
    return self._config.mm_quote_size_usdc
```

#### Step 6.2.2: Add Adverse Selection Detection

**Add to `strategies/delta_neutral.py`:**

```python
def _detect_adverse_selection(self, market_id: str, fills: List[FillRecord]) -> bool:
    """Detect if recent fills indicate adverse selection."""
    if len(fills) < 5:
        return False

    recent = fills[-5:]
    same_side = all(f.side == recent[0].side for f in recent)

    if same_side:
        price_move = recent[-1].fill_price - recent[0].fill_price
        if recent[0].side == "BUY_YES" and price_move < -0.02:
            return True
        if recent[0].side == "SELL_YES" and price_move > 0.02:
            return True

    return False

def _widen_spread_for_adverse_selection(self, base_spread: float) -> float:
    return base_spread * 2.0
```

**Acceptance Criteria:**
- [ ] Quote size adapts to inventory level
- [ ] Adverse selection detected within 5 fills
- [ ] Spread widens when adverse selection detected
- [ ] MM profitability improves in backtest

**Estimated Effort:** 3-4 days

---

### 6.3 AI Integration

**Objective:** Wire AI enhancer into live pipeline for regime-aware trading.

#### Step 6.3.1: Wire AI into StrategyEngine

**Modify:** `engine/strategy_engine.py`

```python
async def on_feature_vector(self, fv: FeatureVector) -> None:
    # Enhance with AI
    signal_ctx = await self._ai_enhancer.enhance(fv)

    # Apply signal context to strategy thresholds
    adjusted_fv = self._apply_signal_context(fv, signal_ctx)

    # Run strategies with adjusted features
    if self._config.arb_enabled:
        proposals = self._arb.evaluate(adjusted_fv)
        for p in proposals:
            await self._propose(p)

    if self._config.mm_enabled:
        if not signal_ctx.suppress_mm:
            proposals = self._dn.evaluate_mm(adjusted_fv)
            for p in proposals:
                await self._propose(p)

def _apply_signal_context(self, fv: FeatureVector, ctx: SignalContext) -> FeatureVector:
    """Adjust feature vector based on AI signal context."""
    # Scale arb signal by confidence
    adjusted_arb = fv.arb_signal * ctx.confidence_multiplier

    return fv.model_copy(update={"arb_signal": adjusted_arb})
```

#### Step 6.3.2: Update main.py

**Modify:** `main.py`

```python
# Already wired in current code - verify it's connected:
strategy = StrategyEngine(
    config=strat_cfg,
    arb_config=arb_cfg,
    dn_config=dn_cfg,
    ai_enhancer=ai_enhancer,  # <-- Already connected
)
```

**Acceptance Criteria:**
- [ ] AI enhancer called on every feature vector
- [ ] Signal context modulates strategy behavior
- [ ] Heuristic fallback works when AI disabled
- [ ] Backtest shows improvement with AI vs without

**Estimated Effort:** 2-3 days

---

## PHASE 7: Infrastructure and Scaling (Weeks 6-9)

### 7.1 Multi-Process Architecture

**Objective:** Support multiple independent trading strategies.

#### Step 7.1.1: Create Strategy Runner

**New File:** `engine/strategy_runner.py`

```python
"""engine/strategy_runner.py — Isolated strategy process."""
import asyncio
import multiprocessing
import logging

logger = logging.getLogger(__name__)


def run_strategy_process(
    strategy_id: str,
    config: dict,
    message_queue: multiprocessing.Queue,
    result_queue: multiprocessing.Queue,
) -> None:
    """Entry point for strategy subprocess."""
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
    config: dict,
    message_queue: multiprocessing.Queue,
    result_queue: multiprocessing.Queue,
) -> None:
    """Run strategy loop in subprocess."""
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
```

#### Step 7.1.2: Create Strategy Orchestrator

**New File:** `engine/multi_strategy_orchestrator.py`

```python
"""engine/multi_strategy_orchestrator.py — Manages multiple strategy processes."""
import multiprocessing
from typing import Dict, List


class MultiStrategyOrchestrator:
    """Manages lifecycle of multiple strategy subprocesses."""

    def __init__(self, strategy_configs: List[dict]) -> None:
        self._configs = strategy_configs
        self._processes: Dict[str, multiprocessing.Process] = {}
        self._message_queues: Dict[str, multiprocessing.Queue] = {}
        self._result_queues: Dict[str, multiprocessing.Queue] = {}

    def start_all(self) -> None:
        for config in self._configs:
            sid = config["id"]
            mq = multiprocessing.Queue()
            rq = multiprocessing.Queue()

            proc = multiprocessing.Process(
                target=run_strategy_process,
                args=(sid, config, mq, rq),
                name=f"strategy-{sid}",
            )
            proc.start()

            self._processes[sid] = proc
            self._message_queues[sid] = mq
            self._result_queues[sid] = rq

    def stop_all(self) -> None:
        for sid, proc in self._processes.items():
            self._message_queues[sid].put({"type": "shutdown"})
            proc.join(timeout=10)
            if proc.is_alive():
                proc.terminate()

    def send_market_data(self, sid: str, data: dict) -> None:
        self._message_queues[sid].put({"type": "market_data", "data": data})

    def get_results(self, sid: str) -> List[dict]:
        results = []
        while not self._result_queues[sid].empty():
            results.append(self._result_queues[sid].get())
        return results
```

**Acceptance Criteria:**
- [ ] Multiple strategies run in separate processes
- [ ] Inter-process communication via queues
- [ ] Graceful shutdown of all processes
- [ ] Strategy-level resource isolation

**Estimated Effort:** 5-7 days

---

### 7.2 Postgres Support

**Objective:** Alternative storage backend for production deployments.

#### Step 7.2.1: Create Postgres Store

**New File:** `portfolio/storage_postgres.py`

```python
"""portfolio/storage_postgres.py — PostgreSQL persistence backend."""
from __future__ import annotations

import json
import logging
from typing import Dict, List, Optional, Tuple

import asyncpg

from portfolio.manager import FillRecord, _Position
from src.types import Platform, StrategyId

logger = logging.getLogger(__name__)


class PostgresPortfolioStore:
    """PostgreSQL persistence for PortfolioManager."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: Optional[asyncpg.Pool] = None

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(self._dsn, min_size=2, max_size=10)
        await self._init_db()

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()

    async def _init_db(self) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS positions (
                    market_id TEXT,
                    platform TEXT,
                    yes_qty REAL,
                    no_qty REAL,
                    avg_cost_yes REAL,
                    avg_cost_no REAL,
                    realised_pnl REAL,
                    PRIMARY KEY (market_id, platform)
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS fills (
                    proposal_id TEXT PRIMARY KEY,
                    order_id TEXT,
                    market_id TEXT,
                    platform TEXT,
                    side TEXT,
                    filled_usdc REAL,
                    fill_price REAL,
                    ts BIGINT
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS state (
                    key TEXT PRIMARY KEY,
                    value REAL
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS reservations (
                    proposal_id TEXT PRIMARY KEY,
                    amount REAL,
                    platform TEXT,
                    strategy_id TEXT
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS active_orders (
                    proposal_id TEXT PRIMARY KEY,
                    exchange_order_id TEXT,
                    submission_json TEXT
                )
            """)

    def save_fill_and_position(
        self, fill: FillRecord, position: _Position,
        cash_usdc: float, peak_equity: float, closed_pnl: float
    ) -> None:
        import asyncio
        loop = asyncio.get_event_loop()
        loop.run_until_complete(self._save_fill_and_position_async(
            fill, position, cash_usdc, peak_equity, closed_pnl
        ))

    async def _save_fill_and_position_async(
        self, fill: FillRecord, position: _Position,
        cash_usdc: float, peak_equity: float, closed_pnl: float
    ) -> None:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("""
                    INSERT INTO fills VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    ON CONFLICT (proposal_id) DO NOTHING
                """, fill.proposal_id, fill.order_id, fill.market_id,
                    fill.platform.value, fill.side, fill.filled_usdc,
                    fill.fill_price, fill.ts)

                await conn.execute("""
                    INSERT INTO positions VALUES ($1, $2, $3, $4, $5, $6, $7)
                    ON CONFLICT (market_id, platform) DO UPDATE SET
                        yes_qty = $3, no_qty = $4, avg_cost_yes = $5,
                        avg_cost_no = $6, realised_pnl = $7
                """, position.market_id, position.platform.value,
                    position.yes_qty, position.no_qty,
                    position.avg_cost_yes, position.avg_cost_no,
                    position.realised_pnl)

                await conn.executemany("""
                    INSERT INTO state VALUES ($1, $2)
                    ON CONFLICT (key) DO UPDATE SET value = $2
                """, [
                    ("cash_usdc", cash_usdc),
                    ("peak_equity", peak_equity),
                    ("closed_pnl", closed_pnl),
                ])

    def load_state(self) -> dict:
        import asyncio
        return asyncio.get_event_loop().run_until_complete(self._load_state_async())

    async def _load_state_async(self) -> dict:
        async with self._pool.acquire() as conn:
            state = {"cash_usdc": None, "peak_equity": None, "closed_pnl": 0.0}
            for row in await conn.fetch("SELECT key, value FROM state"):
                state[row["key"]] = row["value"]

            positions = {}
            for row in await conn.fetch("SELECT * FROM positions"):
                plat = Platform(row["platform"])
                pos = _Position(row["market_id"], plat)
                pos.yes_qty = row["yes_qty"]
                pos.no_qty = row["no_qty"]
                pos.avg_cost_yes = row["avg_cost_yes"]
                pos.avg_cost_no = row["avg_cost_no"]
                pos.realised_pnl = row["realised_pnl"]
                positions[(row["market_id"], plat)] = pos

            state["positions"] = positions
            return state

    def is_healthy(self) -> bool:
        import asyncio
        try:
            return asyncio.get_event_loop().run_until_complete(self._check_health())
        except Exception:
            return False

    async def _check_health(self) -> bool:
        if not self._pool:
            return False
        async with self._pool.acquire() as conn:
            try:
                await conn.fetchval("SELECT 1")
                return True
            except Exception:
                return False
```

#### Step 7.2.2: Update Settings

**Modify:** `config/settings.py`

Add to `TradingConfig`:
```python
db_backend: str = field(default_factory=lambda: _e("DB_BACKEND", "sqlite"))
postgres_dsn: str = field(default_factory=lambda: _e("POSTGRES_DSN", ""))
```

#### Step 7.2.3: Update main.py

**Modify:** `main.py`

```python
if settings.trading.db_backend == "postgres":
    from portfolio.storage_postgres import PostgresPortfolioStore
    store = PostgresPortfolioStore(dsn=settings.trading.postgres_dsn)
    await store.connect()
else:
    store = SqlitePortfolioStore(db_path=db_path)
```

**Update:** `requirements.txt`

```
asyncpg==0.29.0
```

**Acceptance Criteria:**
- [ ] PostgresStore implements same interface as SQLiteStore
- [ ] All CRUD operations work with Postgres
- [ ] Connection pooling configured
- [ ] Health check works
- [ ] Switch between SQLite and Postgres via env var

**Estimated Effort:** 3-4 days

---

### 7.3 Redis Integration

**Objective:** Enable distributed state sharing and caching.

#### Step 7.3.1: Create Redis Cache

**New File:** `infrastructure/redis_cache.py`

```python
"""infrastructure/redis_cache.py — Redis-backed cache for market data and AI responses."""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

import redis.asyncio as redis

logger = logging.getLogger(__name__)


class RedisCache:
    """Redis cache with TTL support."""

    def __init__(self, url: str = "redis://localhost:6379/0") -> None:
        self._url = url
        self._client: Optional[redis.Redis] = None

    async def connect(self) -> None:
        self._client = redis.from_url(self._url, decode_responses=True)

    async def close(self) -> None:
        if self._client:
            await self._client.close()

    async def get(self, key: str) -> Optional[Any]:
        if not self._client:
            return None
        try:
            value = await self._client.get(key)
            if value:
                return json.loads(value)
        except Exception as e:
            logger.warning("Redis get failed: %s", e)
        return None

    async def set(self, key: str, value: Any, ttl_seconds: int = 300) -> None:
        if not self._client:
            return
        try:
            await self._client.set(key, json.dumps(value), ex=ttl_seconds)
        except Exception as e:
            logger.warning("Redis set failed: %s", e)

    async def delete(self, key: str) -> None:
        if not self._client:
            return
        try:
            await self._client.delete(key)
        except Exception as e:
            logger.warning("Redis delete failed: %s", e)

    async def publish(self, channel: str, message: dict) -> None:
        if not self._client:
            return
        try:
            await self._client.publish(channel, json.dumps(message))
        except Exception as e:
            logger.warning("Redis publish failed: %s", e)

    async def subscribe(self, channel: str) -> redis.client.PubSub:
        if not self._client:
            raise RuntimeError("Redis not connected")
        pubsub = self._client.pubsub()
        await pubsub.subscribe(channel)
        return pubsub
```

#### Step 7.3.2: Wire Redis into AI Enhancer

**Modify:** `ai/enhancer.py`

```python
async def enhance(self, fv: FeatureVector) -> SignalContext:
    # Check Redis cache first
    if self._redis_cache:
        cached = await self._redis_cache.get(f"ai:{fv.market_id}")
        if cached:
            self.cache_hits += 1
            return SignalContext(**cached)

    # ... existing logic ...

    # Store in Redis cache
    if self._redis_cache:
        await self._redis_cache.set(
            f"ai:{fv.market_id}",
            ctx.__dict__,
            ttl_seconds=self._cfg.cache_ttl_ms // 1000,
        )
```

**Acceptance Criteria:**
- [ ] Redis cache works for AI responses
- [ ] Pub/sub works for inter-process communication
- [ ] Graceful degradation when Redis unavailable
- [ ] TTL-based cache invalidation

**Estimated Effort:** 3-4 days

---

## PHASE 8: Advanced Features (Weeks 9-12)

### 8.1 Market Resolution and Redemption

**Objective:** Automatically handle resolved markets and redeem positions.

#### Step 8.1.1: Create Resolution Monitor

**New File:** `engine/resolution_monitor.py`

```python
"""engine/resolution_monitor.py — Detects and handles market resolutions."""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, Dict, List, Optional

from execution.clients.polymarket import PolymarketClient
from src.types import Platform

logger = logging.getLogger(__name__)


class ResolutionMonitor:
    """Monitors markets for resolution and triggers redemption."""

    def __init__(
        self,
        client: PolymarketClient,
        markets: List[str],
        on_resolution: Callable[[str, str], None],
        poll_interval_s: float = 300.0,
    ) -> None:
        self._client = client
        self._markets = markets
        self._on_resolution = on_resolution
        self._poll_interval_s = poll_interval_s
        self._task: Optional[asyncio.Task] = None
        self._resolved: Dict[str, str] = {}

    async def start(self) -> None:
        self._task = asyncio.create_task(self._poll_loop(), name="resolution-monitor")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _poll_loop(self) -> None:
        while True:
            for market_id in self._markets:
                if market_id in self._resolved:
                    continue

                try:
                    market = await self._client.get_market(market_id)
                    if market and market.get("resolved", False):
                        outcome = market.get("winningOutcome", "unknown")
                        self._resolved[market_id] = outcome
                        logger.critical(
                            "Market resolved: %s -> %s", market_id, outcome
                        )
                        self._on_resolution(market_id, outcome)

                        # Trigger redemption
                        await self._client.redeem_market(market_id)
                        logger.info("Redemption triggered for %s", market_id)
                except Exception as e:
                    logger.error("Resolution check failed for %s: %s", market_id, e)

            await asyncio.sleep(self._poll_interval_s)
```

#### Step 8.1.2: Wire into main.py

**Modify:** `main.py`

```python
from engine.resolution_monitor import ResolutionMonitor

def handle_resolution(market_id: str, outcome: str) -> None:
    asyncio.create_task(orchestrator.handle_market_resolution(market_id, outcome))

resolution_monitor = ResolutionMonitor(
    client=pm_client,
    markets=settings.trading.markets,
    on_resolution=handle_resolution,
)

# In startup:
await resolution_monitor.start()

# In shutdown:
await resolution_monitor.stop()
```

**Acceptance Criteria:**
- [ ] Resolution detected within 5 minutes of market close
- [ ] Redemption triggered automatically
- [ ] Position cleaned up after redemption
- [ ] Resolved markets removed from trading list

**Estimated Effort:** 3-4 days

---

### 8.2 Portfolio Analytics

**Objective:** Provide detailed performance analysis.

#### Step 8.2.1: Create Analytics Engine

**New File:** `portfolio/analytics.py`

```python
"""portfolio/analytics.py — Performance analytics and attribution."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List

from portfolio.manager import FillRecord


@dataclass
class PerformanceMetrics:
    total_return_pct: float
    sharpe_ratio: float
    sortino_ratio: float
    calmar_ratio: float
    max_drawdown_pct: float
    win_rate: float
    avg_win: float
    avg_loss: float
    profit_factor: float
    total_trades: int
    avg_hold_time_ms: float


class PortfolioAnalytics:
    """Computes performance metrics from fill history."""

    def __init__(self) -> None:
        self._fills: List[FillRecord] = []
        self._equity_curve: List[float] = []

    def add_fill(self, fill: FillRecord) -> None:
        self._fills.append(fill)

    def add_equity_point(self, equity: float) -> None:
        self._equity_curve.append(equity)

    def compute_metrics(self, initial_capital: float) -> PerformanceMetrics:
        if not self._equity_curve:
            return self._empty_metrics()

        returns = self._compute_returns()
        total_return = (self._equity_curve[-1] - initial_capital) / initial_capital

        sharpe = self._sharpe_ratio(returns)
        sortino = self._sortino_ratio(returns)
        max_dd = self._max_drawdown()
        calmar = total_return / max_dd if max_dd > 0 else 0.0

        wins, losses = self._categorize_trades()
        win_rate = len(wins) / len(wins + losses) if (wins + losses) else 0.0
        avg_win = sum(wins) / len(wins) if wins else 0.0
        avg_loss = sum(losses) / len(losses) if losses else 0.0
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        return PerformanceMetrics(
            total_return_pct=total_return * 100,
            sharpe_ratio=sharpe,
            sortino_ratio=sortino,
            calmar_ratio=calmar,
            max_drawdown_pct=max_dd * 100,
            win_rate=win_rate * 100,
            avg_win=avg_win,
            avg_loss=avg_loss,
            profit_factor=profit_factor,
            total_trades=len(wins) + len(losses),
            avg_hold_time_ms=0.0,
        )

    def _compute_returns(self) -> List[float]:
        if len(self._equity_curve) < 2:
            return []
        returns = []
        for i in range(1, len(self._equity_curve)):
            prev = self._equity_curve[i - 1]
            curr = self._equity_curve[i]
            if prev > 0:
                returns.append((curr - prev) / prev)
        return returns

    def _sharpe_ratio(self, returns: List[float], risk_free_rate: float = 0.0) -> float:
        if not returns:
            return 0.0
        avg_return = sum(returns) / len(returns)
        std_dev = math.sqrt(sum((r - avg_return) ** 2 for r in returns) / len(returns))
        if std_dev == 0:
            return 0.0
        return (avg_return - risk_free_rate) / std_dev * math.sqrt(252 * 24 * 60)

    def _sortino_ratio(self, returns: List[float], risk_free_rate: float = 0.0) -> float:
        if not returns:
            return 0.0
        avg_return = sum(returns) / len(returns)
        downside = [r for r in returns if r < 0]
        if not downside:
            return float("inf")
        downside_std = math.sqrt(sum(r ** 2 for r in downside) / len(downside))
        if downside_std == 0:
            return 0.0
        return (avg_return - risk_free_rate) / downside_std * math.sqrt(252 * 24 * 60)

    def _max_drawdown(self) -> float:
        if not self._equity_curve:
            return 0.0
        peak = self._equity_curve[0]
        max_dd = 0.0
        for equity in self._equity_curve:
            if equity > peak:
                peak = equity
            dd = (peak - equity) / peak if peak > 0 else 0.0
            max_dd = max(max_dd, dd)
        return max_dd

    def _categorize_trades(self) -> tuple[List[float], List[float]]:
        wins = []
        losses = []
        for fill in self._fills:
            pnl = fill.filled_usdc * (1.0 - fill.fill_price) if "BUY" in fill.side else fill.filled_usdc * fill.fill_price
            if pnl > 0:
                wins.append(pnl)
            else:
                losses.append(pnl)
        return wins, losses

    def _empty_metrics(self) -> PerformanceMetrics:
        return PerformanceMetrics(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0, 0.0)
```

**Acceptance Criteria:**
- [ ] All metrics computed correctly
- [ ] Sharpe, Sortino, Calmar ratios match manual calculation
- [ ] Max drawdown computed correctly
- [ ] Win rate and profit factor accurate
- [ ] Metrics exported to Prometheus/Grafana

**Estimated Effort:** 3-4 days

---

### 8.3 Backtest Improvements

**Objective:** More realistic backtest simulation with historical data.

#### Step 8.3.1: Add Historical Data Loader

**New File:** `backtest/data_loader.py`

```python
"""backtest/data_loader.py — Load historical market data for backtesting."""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from typing import Dict, List

from data.models import MarketSnapshot
from src.types import Platform


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
    """Loads historical data from CSV or JSON files."""

    @staticmethod
    def load_csv(file_path: str) -> Dict[str, List[MarketSnapshot]]:
        """Load historical data from CSV file."""
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
        """Load historical data from JSON file."""
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
                yes_bid=item["yes_bid"],
                yes_ask=item["yes_ask"],
                no_bid=item["no_bid"],
                no_ask=item["no_ask"],
                bid_depth_usdc=item["bid_depth_usdc"],
                ask_depth_usdc=item["ask_depth_usdc"],
                taker_fee_bps=item.get("taker_fee_bps", 20),
                ts=item["timestamp_ms"],
                received_ts=item["timestamp_ms"],
            )
            snapshots[key].append(snap)

        return snapshots
```

#### Step 8.3.2: Update Backtest Engine

**Modify:** `backtest/engine.py`

Add method to use historical data:
```python
@classmethod
def from_historical(
    cls,
    snapshots: Dict[str, List[MarketSnapshot]],
    initial_capital: float,
    risk_limits: RiskLimits,
) -> "BacktestEngine":
    """Create backtest engine from historical data."""
    tick_streams = {}
    for key, snaps in snapshots.items():
        market_id = key.split(":")[0]
        tick_streams[market_id] = snaps

    return cls(
        tick_streams=tick_streams,
        initial_capital=initial_capital,
        risk_limits=risk_limits,
    )
```

**Acceptance Criteria:**
- [ ] CSV and JSON data loaders work
- [ ] Historical data produces same results as synthetic
- [ ] Backtest engine accepts historical data
- [ ] Walk-forward optimization works

**Estimated Effort:** 5-7 days

---

## PHASE 9: Security and Compliance (Weeks 12-14)

### 9.1 Enhanced Security

**Priority Tasks:**

1. **Hardware Wallet Integration** (3 days)
   - Add Ledger/Trezor support via `ledgerblue` or `trezor` libraries
   - Sign orders using hardware device instead of in-memory key
   - Add `--hardware-wallet` CLI flag

2. **Multi-Signature Approval** (2 days)
   - Add `MIN_APPROVALS` config for large orders
   - Implement approval queue for orders > threshold
   - Add approval API endpoint

3. **API Key Rotation** (1 day)
   - Add `rotate_api_key()` method to exchange clients
   - Store old key during transition period
   - Add key expiry warning alerts

4. **Audit Logging** (2 days)
   - Create `infrastructure/audit_log.py`
   - Log all trading actions with timestamp, operator, and details
   - Immutable log storage (append-only file or database)

5. **IP Whitelisting** (1 day)
   - Add `ALLOWED_IPS` config
   - Validate source IP on all API endpoints
   - Log blocked IP attempts

6. **Rate Limiting per Endpoint** (1 day)
   - Add per-endpoint rate limits in exchange clients
   - Implement token bucket per endpoint
   - Add rate limit exceeded alerts

### 9.2 Compliance Reporting

**Priority Tasks:**

1. **Trade Reporting** (2 days)
   - Create `compliance/trade_report.py`
   - Generate CSV with all trades (timestamp, venue, price, size, side)
   - Add daily/weekly/monthly report generation

2. **Position Reporting** (1 day)
   - Generate daily position snapshots
   - Include unrealized P&L by market and venue
   - Export to CSV/JSON

3. **P&L Reporting with Tax Lots** (3 days)
   - Implement FIFO/LIFO tax lot tracking
   - Compute realized/unrealized P&L per lot
   - Generate tax-ready reports

4. **Audit Trail** (1 day)
   - Log all configuration changes
   - Include operator ID, timestamp, old value, new value
   - Immutable audit log storage

5. **Data Retention** (1 day)
   - Implement configurable retention policies
   - Auto-archive old data
   - Add data deletion requests handling

---

## PHASE 10: Long-Term Vision (Months 4-6)

### 10.1 Multi-Venue Support

**Template for New Venue Adapter:**

```python
"""execution/clients/new_venue.py — Template for new exchange client."""
from __future__ import annotations

import logging
from typing import List, Optional

from execution.engine import (
    ExchangeClient,
    OpenOrder,
    OrderStatusResponse,
    PlacedOrderResponse,
)
from execution.models import OrderSubmission
from src.types import Platform

logger = logging.getLogger(__name__)


class NewVenueClient:
    """Template for new exchange client implementation."""

    PLATFORM: Platform = Platform.POLYMARKET  # Update for new venue

    def __init__(
        self,
        api_key: str,
        wallet_private_key: str,
        host: str,
        rate_limit_per_s: int = 10,
        sandbox: bool = False,
    ) -> None:
        self._api_key = api_key
        self._wallet_private_key = wallet_private_key
        self._host = host
        self._sandbox = sandbox

    @property
    def platform(self) -> Platform:
        return self.PLATFORM

    async def place_order(
        self, submission: OrderSubmission, effective_price: float, nonce: Optional[int] = None
    ) -> PlacedOrderResponse:
        # Implement order placement
        raise NotImplementedError

    async def cancel_order(self, exchange_order_id: str, market_id: str) -> bool:
        # Implement order cancellation
        raise NotImplementedError

    async def get_order_status(
        self, exchange_order_id: str, market_id: str
    ) -> OrderStatusResponse:
        # Implement order status query
        raise NotImplementedError

    async def get_open_orders(self, market_ids: Optional[List[str]] = None) -> List[OpenOrder]:
        # Implement open orders query
        raise NotImplementedError

    async def verify_connectivity(self) -> bool:
        # Implement connectivity check
        raise NotImplementedError

    async def close(self) -> None:
        # Implement cleanup
        pass
```

**Venue Priority:**
1. Kalshi (regulated US venue, high liquidity)
2. PredictIt (academic prediction market)
3. Augur (decentralized, Ethereum-based)
4. Gnosis CTF (conditional token framework)

### 10.2 Machine Learning Pipeline

**Architecture:**
```
┌─────────────┐    ┌─────────────┐    ┌─────────────┐    ┌─────────────┐
│  Feature    │───▶│  Training   │───▶│ Validation  │───▶│ Deployment  │
│  Store      │    │  Pipeline   │    │  Pipeline   │    │  Pipeline   │
└─────────────┘    └─────────────┘    └─────────────┘    └─────────────┘
       │                  │                  │                  │
       ▼                  ▼                  ▼                  ▼
┌─────────────┐    ┌─────────────┐    ┌─────────────┐    ┌─────────────┐
│  Live       │◀───│  Model      │◀───│  Backtest   │◀───│  Model      │
│  Inference  │    │  Registry   │    │  Results    │    │  Training   │
└─────────────┘    └─────────────┘    └─────────────┘    └─────────────┘
```

**Key Components:**
- Feature store (Redis + Parquet)
- Training pipeline (scikit-learn, XGBoost)
- Model registry (MLflow)
- Deployment with canary releases
- Drift detection (Evidently AI)

### 10.3 Web Dashboard

**Tech Stack:**
- Frontend: React + TypeScript
- Charts: Recharts or Chart.js
- Real-time: WebSocket
- State: Redux or Zustand
- Styling: Tailwind CSS

**Key Pages:**
1. **Dashboard** — Portfolio overview, equity curve, key metrics
2. **Positions** — Current positions by market and venue
3. **Trades** — Trade history with filters
4. **Strategies** — Strategy configuration and performance
5. **Alerts** — Alert history and configuration
6. **Settings** — System configuration

---

## Implementation Priority Matrix

| Phase | Task | Priority | Effort | Impact |
|-------|------|----------|--------|--------|
| 5.1 | Alerting Integration | HIGH | 2-3 days | HIGH |
| 5.2 | Unified Clock | HIGH | 3-4 days | HIGH |
| 5.3 | Type Safety | MEDIUM | 2-3 days | MEDIUM |
| 5.4 | Performance Testing | MEDIUM | 2 days | MEDIUM |
| 6.1 | Advanced Arb Signals | MEDIUM | 3-4 days | HIGH |
| 6.2 | MM Improvements | MEDIUM | 3-4 days | HIGH |
| 6.3 | AI Integration | MEDIUM | 2-3 days | MEDIUM |
| 7.1 | Multi-Process | LOW | 5-7 days | MEDIUM |
| 7.2 | Postgres Support | LOW | 3-4 days | MEDIUM |
| 7.3 | Redis Integration | LOW | 3-4 days | MEDIUM |
| 8.1 | Market Resolution | LOW | 3-4 days | HIGH |
| 8.2 | Portfolio Analytics | LOW | 3-4 days | MEDIUM |
| 8.3 | Backtest Improvements | LOW | 5-7 days | HIGH |
| 9.1 | Enhanced Security | MEDIUM | 10 days | HIGH |
| 9.2 | Compliance Reporting | LOW | 8 days | MEDIUM |
| 10.1 | Multi-Venue | LOW | 10-14 days/venue | HIGH |
| 10.2 | ML Pipeline | LOW | 14-21 days | HIGH |
| 10.3 | Web Dashboard | LOW | 14-21 days | MEDIUM |

---

## Quick Start: Next 30 Days

### Week 1-2: Phase 5.1 + 5.2
- [ ] Implement alerting module
- [ ] Wire alerts into existing components
- [ ] Create unified clock protocol
- [ ] Update all components to use clock

### Week 3-4: Phase 5.3 + 5.4 + 6.3
- [ ] Create Protocol definitions
- [ ] Eliminate `Any` types
- [ ] Run mypy strict mode
- [ ] Create performance test suite
- [ ] Wire AI enhancer into live pipeline

### Week 5-6: Phase 6.1 + 6.2
- [ ] Add OFI analysis to feature engine
- [ ] Implement dynamic min edge
- [ ] Add adaptive quoting to MM
- [ ] Implement adverse selection detection

**After 6 weeks:** System is production-hardened with alerting, type safety, performance validation, and improved strategies.
