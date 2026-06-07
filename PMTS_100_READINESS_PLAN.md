# PMTS — 100/100 Production Readiness Execution Plan
**Role**: Senior Software Architect  
**Target**: Polymarket Trading System — 75/100 → 100/100  
**Agent**: Qwen (local, MCP tools)  
**Protocol**: Max 3–4 steps per batch. Read before writing. Execute in order.

---

## Readiness Scoring Map

| Batch | Steps | Category | Score Gain | Running Total |
|-------|-------|----------|------------|---------------|
| 1 | 1–2 | P0 Regression Fixes | +3 | 78/100 |
| 2 | 3–5 | Infrastructure Hardening | +7 | 85/100 |
| 3 | 6–8 | Test Coverage & Rate Limiting | +5 | 90/100 |
| 4 | 9–11 | Feature Completeness | +5 | 95/100 |
| 5 | 12–14 | Go-Live Validation | +5 | 100/100 |

---

## BATCH 1 — P0 Regression Fixes (78/100)

---

### STEP 1 — Diagnose & Patch Backtest Zero-Trade Regression

**Task:** `main.py --mode backtest --ticks 200` exits with zero proposals and zero fills. This makes the primary CI smoke gate a no-op and masks all downstream bugs.

**Tools:** `filesystem`, `bash`

**Instructions:**

1. **Read the call chain in order:**
   - `backtest/engine.py` → `BacktestEngine.__init__()` and `run()` — confirm `FeatureEngine` callback is registered on `StrategyEngine` (look for `se.register_callback(fe.on_feature_vector)` or equivalent).
   - `engine/strategy_engine.py` → `on_feature_vector()` — trace whether `ArbitrageStrategy.evaluate()` is called and under what conditions it returns `None` vs a proposal.
   - `strategies/arbitrage.py` → `ArbConfig.min_net_edge` (default `0.006`) and `ArbConfig.max_spread_fraction` (default `0.07`) — these are the rejection thresholds.
   - `backtest/data_loader.py` or the synthetic generator in `backtest/engine.py` — inspect what `ask_pm`, `bid_pm`, `ask_op`, `bid_op`, `depth_pm`, `depth_op` values are emitted and whether the synthetic spread ever clears `min_net_edge` after fees.

2. **Instrument and reproduce:**
   ```bash
   PYTHONHASHSEED=0 python main.py --mode backtest --ticks 200 --capital 10000 --verbose 2>&1 | grep -E "proposal|feature|arb|edge|reject"
   ```

3. **Locate the failure layer** — exactly one of these is true:
   - **A** — `FeatureEngine` produces `FeatureVector` objects but `StrategyEngine.on_feature_vector` is never called (callback not wired).
   - **B** — `ArbitrageStrategy.evaluate()` is called but `net_edge < min_net_edge` on every tick (synthetic prices too tight or fee model too aggressive).
   - **C** — Proposals are emitted but `RiskEngine.evaluate()` rejects all of them (`capital_reserved` never released from prior run state).

4. **Apply the targeted fix:**
   - For **A**: In `BacktestEngine.__init__`, add `self._feature_engine.register_callback(self._strategy_engine.on_feature_vector)`.
   - For **B**: Lower `min_net_edge` to `0.002` in the backtest-specific `ArbConfig`, or widen synthetic spread in the tick generator by 1.5×.
   - For **C**: Call `risk_engine.release_all_reservations()` at backtest start if that method exists, or inspect `_reservations` dict initialization.

5. **Success criteria:**
   ```bash
   PYTHONHASHSEED=0 python main.py --mode backtest --ticks 200 --capital 10000
   # Must emit: "proposals evaluated: N > 0" and "fills: M > 0"
   ```

---

### STEP 2 — Fix Non-Deterministic Backtest Seeds (TASK-P0-002)

**Task:** `hash(market_id) % (2**31)` in `main.py` / `backtest/engine.py` is subject to Python hash randomization (`PYTHONHASHSEED`). Two consecutive runs of the same command produce different P&L sequences, making the determinism test (`test_backtest_determinism`) unreliable.

**Tools:** `filesystem`, `bash`

**Instructions:**

1. **Read:** Find every occurrence of `hash(` in `main.py`, `backtest/engine.py`, and `backtest/data_loader.py`.

2. **Replace** all `hash(market_id)` seed computations with a stable SHA-256 derivation:
   ```python
   # BEFORE (hash-randomization vulnerable)
   seed = hash(market_id) % (2**31)

   # AFTER (stable across processes)
   import hashlib
   seed = int(hashlib.sha256(market_id.encode()).hexdigest(), 16) % (2**31)
   ```

3. **Verify `PYTHONHASHSEED` independence:**
   ```bash
   PYTHONHASHSEED=0  python main.py --mode backtest --ticks 200 --capital 10000 > run1.txt
   PYTHONHASHSEED=99 python main.py --mode backtest --ticks 200 --capital 10000 > run2.txt
   diff <(grep "P&L\|proposals\|fills" run1.txt) <(grep "P&L\|proposals\|fills" run2.txt)
   # Must produce zero diff
   ```

4. **Success criteria:** Identical summary metrics across at least 3 runs with different `PYTHONHASHSEED` values.

---

## BATCH 2 — Infrastructure Hardening (85/100)

---

### STEP 4 — Unified Clock Injection Across All Production Files

**Task:** `src/clock.py` defines the `Clock` protocol with `LiveClock` and `SimClock`, but 14 production files bypass it and call `time.time()` directly. This causes backtest/live time skew: `SimClock.advance()` cannot control timestamps that call wall-clock directly.

**Tools:** `filesystem`

**Instructions:**

1. **Read** `src/clock.py` to confirm the `Clock` protocol interface (`now_ms() -> int`, `sleep_ms(ms: int) -> None`).

2. **Identify all injection targets** — production files only (not tests, not scripts):
   ```
   execution/order_tracker.py      (16 occurrences — highest priority)
   strategies/delta_neutral.py     (4 occurrences)
   data/adapters/rest_polling.py   (4 occurrences)
   ai/enhancer.py                  (4 occurrences)
   risk/kill_switch.py             (4 occurrences)
   data/adapters/opinion_ws.py     (3 occurrences)
   engine/strategy_engine.py       (3 occurrences)
   data/market_data_provider.py    (2 occurrences)
   engine/feature_engine.py        (2 occurrences)
   engine/orchestrator.py          (2 occurrences)
   portfolio/manager.py            (2 occurrences)
   risk/engine.py                  (2 occurrences)
   execution/engine.py             (2 occurrences — already has clock param)
   ```

3. **Pattern to apply in each file:**
   ```python
   # BEFORE — remove this pattern everywhere
   import time
   def _now_ms() -> int:
       return int(time.time() * 1000)

   # AFTER — inject via constructor
   from src.clock import Clock, LiveClock

   class SomeComponent:
       def __init__(self, ..., clock: Clock = LiveClock()) -> None:
           self._clock = clock

       def _some_method(self):
           now = self._clock.now_ms()   # replaces int(time.time() * 1000)
   ```

4. **Do NOT touch** test files, scripts, or `infrastructure/alerting.py` (uses `time.time()` for dedup — this is intentionally wall-clock).

5. **Verify** backtest `SimClock` is passed through:
   - `backtest/engine.py` must pass `SimClock` instance to every component it constructs.
   - Confirm `SimClock.advance()` is called after each synthetic tick so `now_ms()` returns the simulated time.

6. **Success criteria:**
   ```bash
   grep -r "int(time.time()" --include="*.py" \
     execution/ engine/ strategies/ data/adapters/ portfolio/ risk/ ai/ \
     | grep -v __pycache__ | grep -v test_
   # Must return zero lines
   ```

---

### STEP 5 — Wire Alerting to Orchestrator + Fix Global Rate Limiter

**Task A — Alerting:** `infrastructure/alerting.py` has `AlertRouter` fully implemented. It is never called by `engine/orchestrator.py` or `risk/kill_switch.py` on critical events. Operators are blind when the kill switch fires.

**Task B — Rate Limiter:** `execution/clients/polymarket.py` and `opinion.py` each hold an instance-level `asyncio_throttle.Throttler`. When multiple `ExecutionEngine` instances exist, each has its own counter — the per-venue rate contract is not enforced globally.

**Tools:** `filesystem`

**Instructions (Task A — Alerting):**

1. **Read** `infrastructure/alerting.py` fully. Identify the `AlertRouter.send(alert: Alert)` signature and `Alert` dataclass fields.

2. **In `risk/kill_switch.py`:** Add `AlertRouter` as an optional constructor parameter. Call `alert_router.send(Alert(severity=CRITICAL, title="Kill Switch Activated", ...))` inside `trigger()`.

3. **In `engine/orchestrator.py`:** Add `alert_router: Optional[AlertRouter] = None` parameter. Wire alerts for:
   - WebSocket disconnect > 30 seconds → `WARNING`
   - `ExecutionEngine` consecutive errors > 3 → `WARNING`
   - Orchestrator startup → `INFO`
   - Graceful shutdown → `INFO`

4. **In `main.py`:** Instantiate `AlertRouter(config=AlertConfig(slack_webhook_url=settings.alerting.slack_url, ...))` and pass it to both `KillSwitch` and `Orchestrator`.

**Instructions (Task B — Global Rate Limiter):**

5. **Create `execution/rate_limiter.py`:**
   ```python
   """execution/rate_limiter.py — Venue-level global rate limiter."""
   import asyncio
   import time
   from collections import deque
   from typing import Dict

   class VenueRateLimiter:
       """Global singleton rate limiter per venue.
       
       Unlike asyncio_throttle.Throttler (per-instance), this is shared
       across all clients for the same venue.
       """
       _instances: Dict[str, "VenueRateLimiter"] = {}

       @classmethod
       def for_venue(cls, venue: str, rate_per_s: int) -> "VenueRateLimiter":
           if venue not in cls._instances:
               cls._instances[venue] = cls(rate_per_s)
           return cls._instances[venue]

       def __init__(self, rate_per_s: int) -> None:
           self._rate = rate_per_s
           self._window = 1.0
           self._calls: deque = deque()
           self._lock = asyncio.Lock()

       async def acquire(self) -> None:
           async with self._lock:
               now = time.monotonic()
               cutoff = now - self._window
               while self._calls and self._calls[0] < cutoff:
                   self._calls.popleft()
               if len(self._calls) >= self._rate:
                   sleep_for = self._window - (now - self._calls[0])
                   await asyncio.sleep(max(0, sleep_for))
               self._calls.append(time.monotonic())
   ```

6. **Replace** instance-level `Throttler` in `execution/clients/polymarket.py` and `execution/clients/opinion.py`:
   ```python
   # BEFORE
   from asyncio_throttle import Throttler
   self._throttler = Throttler(rate_limit_per_s=10)

   # AFTER
   from execution.rate_limiter import VenueRateLimiter
   self._limiter = VenueRateLimiter.for_venue("polymarket", rate_per_s=10)
   # Call await self._limiter.acquire() before every API request
   ```

7. **Success criteria:**
   - Kill switch fires → Slack message received within 5 seconds (test with `curl -X POST /kill-switch/trigger`).
   - Two `PolymarketClient` instances share the same `VenueRateLimiter` instance (assert `client1._limiter is client2._limiter`).

---

### STEP 6 — mypy Strict Pass + Eliminate `Any` Types

**Task:** `execution/engine.py` uses `Optional[Any]` for client types. `List` and `Dict` throughout the codebase lack type parameters. `mypy --strict` fails. This is a Phase 5.3 requirement and blocks any serious refactoring.

**Tools:** `filesystem`, `bash`

**Instructions:**

1. **Establish baseline:**
   ```bash
   pip install mypy --break-system-packages
   mypy --strict --ignore-missing-imports \
     execution/ engine/ strategies/ data/ portfolio/ risk/ ai/ src/ \
     2>&1 | tee /tmp/mypy_baseline.txt | tail -5
   ```

2. **Fix in priority order** (highest error count first):
   - **`execution/engine.py`**: Replace `Optional[Any]` client type with `Optional[ExchangeClient]` (the protocol already exists).
   - **`risk/engine.py`**: Add `Dict[str, float]` type params to `_reservations`.
   - **`engine/strategy_engine.py`**: Add `List[OrderProposal]` to return types.
   - **`data/models.py`**: Add `@dataclass` frozen field types.
   - **`portfolio/manager.py`**: Eliminate bare `Dict` in method signatures.

3. **Create `mypy.ini` at project root:**
   ```ini
   [mypy]
   python_version = 3.11
   strict = true
   ignore_missing_imports = true
   exclude = tests/|scripts/|backtest/
   ```

4. **Add mypy to CI** in `.github/workflows/ci.yml`:
   ```yaml
   - name: Type check
     run: mypy --config-file mypy.ini execution/ engine/ strategies/ risk/ portfolio/
   ```

5. **Success criteria:**
   ```bash
   mypy --config-file mypy.ini execution/ engine/ strategies/ risk/ portfolio/
   # Exit code 0. Zero errors in production modules.
   ```

---

## BATCH 3 — Test Coverage & Exchange Contract Tests (90/100)

---

### STEP 7 — Venue Contract Tests with Recorded Fixtures

**Task:** P1-004 from `BUG_BACKLOG.md`. No tests exist for the real `PolymarketClient` or `OpinionClient` protocol surface. Any API contract change (signature, field name, status code) fails silently at runtime.

**Tools:** `filesystem`, `bash`

**Instructions:**

1. **Create `tests/fixtures/` directory** with subdirectories `polymarket/` and `opinion/`.

2. **Record API response fixtures** by running against sandbox (or use the provided sandbox clients):
   ```bash
   python -c "
   import asyncio, json
   from execution.clients.polymarket import PolymarketClient
   # use SANDBOX_CONFIG from .env.example
   async def record():
       client = PolymarketClient(sandbox=True, ...)
       resp = await client.get_order_status('test-order-id')
       json.dump(resp.__dict__, open('tests/fixtures/polymarket/get_order_status_404.json','w'))
   asyncio.run(record())
   "
   ```

3. **Create `tests/test_venue_clients.py`** with three test classes:

   ```python
   # tests/test_venue_clients.py
   import json, pytest
   from unittest.mock import AsyncMock, patch
   from execution.clients.polymarket import PolymarketClient
   from execution.clients.opinion import OpinionClient

   POLY_FIXTURE = json.load(open("tests/fixtures/polymarket/place_order_success.json"))
   OP_FIXTURE   = json.load(open("tests/fixtures/opinion/place_order_success.json"))

   class TestPolymarketClientProtocol:
       """Verify PolymarketClient implements ExchangeClient protocol correctly."""

       @pytest.mark.asyncio
       async def test_place_order_returns_placed_order_response(self, mock_session):
           """PlacedOrderResponse must have exchange_order_id and status fields."""
           ...

       @pytest.mark.asyncio
       async def test_cancel_order_idempotent_on_404(self, mock_session):
           """Cancel on already-cancelled order must not raise — return False."""
           ...

       @pytest.mark.asyncio
       async def test_get_order_status_parses_fills(self, mock_session):
           """OrderStatusResponse.fills must be List[OrderStatusFill]."""
           ...

   class TestOpinionClientProtocol:
       """Mirror tests for OpinionClient."""
       ...

   @pytest.mark.sandbox
   class TestSandboxConnectivity:
       """Live sandbox smoke — skip if SANDBOX credentials missing."""

       @pytest.mark.skipif(not os.getenv("PM_SANDBOX_KEY"), reason="no sandbox key")
       async def test_polymarket_sandbox_connectivity(self):
           client = PolymarketClient(sandbox=True, ...)
           assert await client.verify_connectivity()
   ```

4. **Run and confirm:**
   ```bash
   PYTHONHASHSEED=0 python -m pytest tests/test_venue_clients.py -v -m "not sandbox"
   ```

5. **Success criteria:** All non-sandbox tests green. Fixture files committed to `tests/fixtures/`.

---

### STEP 8 — Sandbox Validation Gate (6 Mandatory Scenarios)

**Task:** P1-001 from `BUG_BACKLOG.md`. The `docs/runbooks/go-live.md` lists 6 mandatory validation scenarios that must pass before live capital. No automated harness enforces them.

**Tools:** `filesystem`, `bash`

**Instructions:**

1. **Read** `docs/runbooks/go-live.md` section "2. Sandbox Validation Gate" and `tests/test_sandbox_validation.py` (check current coverage gaps).

2. **Create `tests/test_sandbox_validation.py`** (or extend existing) with parametrized scenarios:

   | Scenario ID | Description | Acceptance |
   |-------------|-------------|------------|
   | `SV-001` | Normal arb: leg-1 fills → leg-2 submits + fills | Both fills recorded in portfolio |
   | `SV-002` | Partial leg-1 below `min_fill_ratio` → leg-2 never submits | `leg2_submitted == False` |
   | `SV-003` | Kill switch trigger via drawdown | All open orders cancelled, `kill_switch.is_active() == True` |
   | `SV-004` | Kill switch reset with valid token | Proposals resume, `kill_switch.is_active() == False` |
   | `SV-005` | WebSocket disconnect → reconnect + stale suppression | Feed age < stale_threshold after reconnect |
   | `SV-006` | Process restart mid-trade → recovery on next startup | Orphaned orders cancelled, positions reconciled |

3. **Implement each scenario using the paper client** (`execution/clients/paper.py`) and `BacktestEngine`:
   ```python
   @pytest.mark.parametrize("scenario", ["SV-001","SV-002","SV-003","SV-004","SV-005","SV-006"])
   def test_sandbox_scenario(scenario):
       harness = SandboxHarness(scenario_id=scenario)
       result = harness.run(timeout_s=30)
       assert result.passed, f"{scenario}: {result.failure_reason}"
   ```

4. **Run all 6 scenarios:**
   ```bash
   PYTHONHASHSEED=0 python -m pytest tests/test_sandbox_validation.py -v --tb=short
   ```

5. **Success criteria:** All 6 scenarios pass. No scenario leaves stale in-flight state (verify via `portfolio.get_open_orders() == []` at teardown).

---

### STEP 9 — Performance Benchmarks + Load Test (Phase 5.4)

**Task:** No performance baseline exists. `RiskEngine.evaluate()` target is < 5ms. No test catches memory leaks in long-running event loops.

**Tools:** `filesystem`, `bash`

**Instructions:**

1. **Read** `tests/test_performance.py` (check what's already there).

2. **Add three benchmark tests** to `tests/test_performance.py`:

   ```python
   # Benchmark 1: Risk engine latency
   def test_risk_engine_evaluate_under_5ms():
       """P50 latency must be < 5ms, P99 < 20ms."""
       import time
       engine = RiskEngine(...)
       proposal = make_test_proposal()
       latencies = []
       for _ in range(1000):
           t0 = time.perf_counter()
           engine.evaluate(proposal)
           latencies.append((time.perf_counter() - t0) * 1000)
       latencies.sort()
       assert latencies[500] < 5.0,  f"P50={latencies[500]:.2f}ms"
       assert latencies[990] < 20.0, f"P99={latencies[990]:.2f}ms"

   # Benchmark 2: Feature engine throughput
   def test_feature_engine_throughput():
       """Must process >= 100 snapshots/second."""
       ...

   # Benchmark 3: Memory stability over 10k ticks
   def test_no_memory_leak_over_10k_ticks():
       """RSS must not grow more than 50MB over 10,000 synthetic ticks."""
       import tracemalloc
       tracemalloc.start()
       run_backtest(ticks=10_000)
       current, peak = tracemalloc.get_traced_memory()
       tracemalloc.stop()
       assert peak / 1024 / 1024 < 50, f"Peak memory: {peak/1024/1024:.1f}MB"
   ```

3. **Run and record baseline:**
   ```bash
   PYTHONHASHSEED=0 python -m pytest tests/test_performance.py -v --tb=short \
     2>&1 | tee docs/performance_baseline.txt
   ```

4. **Add to CI** with a 10% regression threshold:
   ```yaml
   - name: Performance benchmarks
     run: python -m pytest tests/test_performance.py -v --tb=short
   ```

5. **Success criteria:** All 3 benchmarks pass. `docs/performance_baseline.txt` committed. Risk engine P50 < 5ms confirmed.

---

## BATCH 4 — Feature Completeness (95/100)

---

### STEP 10 — Complete PostgreSQL Backend + Migration Scripts

**Task:** `portfolio/storage_postgres.py` exists but is truncated — `_init_db()` and CRUD methods are incomplete. SQLite is not viable for production multi-process deployments. Phase 7.2 requirement.

**Tools:** `filesystem`, `bash`

**Instructions:**

1. **Read** `portfolio/storage.py` (SQLite implementation) fully. It is the reference interface — `PostgresPortfolioStore` must implement the identical method signatures.

2. **Complete `portfolio/storage_postgres.py`** — implement all methods present in `PortfolioStore`:
   - `_init_db()` — CREATE TABLE IF NOT EXISTS for `positions`, `fills`, `orders`
   - `record_fill(fill: FillRecord)` — upsert using composite SHA256 fill ID
   - `get_fills(market_id: str)` — SELECT with market filter
   - `get_all_positions()` — SELECT all open positions
   - `close_position(market_id, platform)` — UPDATE status
   - `get_portfolio_mtm()` — aggregate query

3. **Create `scripts/migrate_sqlite_to_postgres.py`:**
   ```python
   #!/usr/bin/env python3
   """One-time migration: SQLite → PostgreSQL."""
   import asyncio
   import sqlite3
   from portfolio.storage_postgres import PostgresPortfolioStore

   async def migrate(sqlite_path: str, pg_dsn: str) -> None:
       conn = sqlite3.connect(sqlite_path)
       pg = PostgresPortfolioStore(dsn=pg_dsn)
       await pg.connect()
       # Read all fills from SQLite, write to Postgres
       ...
   ```

4. **Create `scripts/check_postgres.py`** — health check run at startup when `DATABASE_URL` is set:
   ```bash
   python scripts/check_postgres.py
   # Must print: "PostgreSQL connection: OK, schema version: 1"
   ```

5. **Make backend selection configurable** in `main.py`:
   ```python
   if settings.database_url:
       store = PostgresPortfolioStore(dsn=settings.database_url)
       await store.connect()
   else:
       store = SQLitePortfolioStore(path=settings.db_path)
   ```

6. **Success criteria:**
   ```bash
   docker run -e POSTGRES_PASSWORD=test -p 5432:5432 -d postgres:15
   DATABASE_URL=postgresql://postgres:test@localhost/pmts \
     PYTHONHASHSEED=0 python main.py --mode backtest --ticks 50 --capital 1000
   # Must complete with fills persisted to Postgres (verify with psql)
   ```

---

### STEP 11 — Market Resolution Monitor + Auto-Redemption

**Task:** `engine/resolution_monitor.py` exists with a `_poll_loop()` skeleton but `_check_resolution()` is incomplete — it doesn't call the exchange client or trigger portfolio cleanup. Positions in resolved markets are never redeemed. Phase 8.1 requirement.

**Tools:** `filesystem`

**Instructions:**

1. **Read** `engine/resolution_monitor.py` fully. Read `execution/clients/polymarket.py` to find the market resolution query endpoint (likely `GET /markets/{market_id}` checking `resolved: bool` and `outcome: str` fields).

2. **Complete `ResolutionMonitor._check_resolution(market_id)`:**
   ```python
   async def _check_resolution(self, market_id: str) -> None:
       try:
           market_info = await self._client.get_market(market_id)
           if market_info.get("resolved"):
               outcome = market_info["outcome"]   # "YES" or "NO"
               logger.info("Market %s resolved: %s", market_id, outcome)
               self._resolved[market_id] = outcome
               await self._on_resolution(market_id, outcome)
       except Exception as exc:
           logger.warning("Resolution check failed for %s: %s", market_id, exc)
   ```

3. **Implement `on_resolution` callback in `engine/orchestrator.py`:**
   ```python
   async def _on_market_resolution(self, market_id: str, outcome: str) -> None:
       # 1. Cancel all open orders for this market
       await self._cancel_all_for_market(market_id)
       # 2. Remove market from active markets list
       self._markets = [m for m in self._markets if m != market_id]
       # 3. Mark positions as resolved in portfolio
       self._portfolio.mark_resolved(market_id, outcome)
       # 4. Alert operator
       await self._alert_router.send(Alert(
           severity=AlertSeverity.INFO,
           title=f"Market Resolved: {market_id}",
           message=f"Outcome: {outcome}. Positions marked for redemption."
       ))
   ```

4. **Wire `ResolutionMonitor` in `main.py`** — start it alongside `MarketMonitor`.

5. **Add test** in `tests/test_integration.py`:
   ```python
   async def test_resolved_market_cancels_orders_and_alerts():
       # Simulate resolution event → verify open orders cancelled
       ...
   ```

6. **Success criteria:** When a mock market resolves with outcome `"YES"`, all open orders for that market are cancelled within one poll interval, positions are marked `resolved`, and an INFO alert fires.

---

### STEP 12 — Advanced Arbitrage Signals (OFI + Dynamic Edge)

**Task:** Phase 6.1. `strategies/arbitrage.py` uses a fixed `min_net_edge = 0.006`. No order flow imbalance (OFI) analysis. Volatility regimes are computed in `engine/feature_engine.py` but never used to adjust thresholds. This causes both false positives (trading in low-edge environments) and false negatives (passing on fat edges during high volatility).

**Tools:** `filesystem`

**Instructions:**

1. **Read** `engine/feature_engine.py` to confirm `FeatureVector` fields — find `ofi_pm`, `ofi_op`, `vol_regime`, `spread_pct_pm`, `spread_pct_op`. If they're missing, add them.

2. **Add OFI-adjusted edge computation** in `strategies/arbitrage.py`:
   ```python
   def _adjust_for_ofi(self, raw_edge: float, ofi_pm: float, ofi_op: float) -> float:
       """Apply OFI adverse-selection penalty to net edge."""
       ofi_net = ofi_pm - ofi_op   # positive = flow toward PM, favorable for arb
       if abs(ofi_net) > OFI_ADVERSE_THRESHOLD:
           penalty = 1 + (abs(ofi_net) - OFI_ADVERSE_THRESHOLD) * OFI_ADVERSE_MULT
           return raw_edge / penalty
       return raw_edge
   ```

3. **Add dynamic `min_net_edge`** based on volatility regime:
   ```python
   def _dynamic_min_edge(self, vol_regime: str) -> float:
       """Tighten edge requirement in low-vol (thin books), relax in high-vol."""
       return {
           "LOW":    self._config.min_net_edge * 1.5,   # tighter in calm markets
           "MEDIUM": self._config.min_net_edge,
           "HIGH":   self._config.min_net_edge * 0.8,   # relax in volatile markets
       }.get(vol_regime, self._config.min_net_edge)
   ```

4. **Add cross-venue latency arbitrage detection** — if both venues quote the same event and one has a staleness delta > 500ms (from `FeatureVector.feed_age_ms`), flag as potential latency-arb opportunity and increase position size by 20%.

5. **Update `ArbConfig`:**
   ```python
   @dataclass(frozen=True)
   class ArbConfig:
       min_net_edge: float = 0.006
       max_spread_fraction: float = 0.07
       ofi_adverse_threshold: float = 0.25     # NEW
       ofi_adverse_mult: float = 0.60          # NEW — penalty multiplier
       latency_arb_staleness_ms: int = 500     # NEW
       latency_arb_size_boost: float = 1.20    # NEW
   ```

6. **Add regression test** — ensure the new edge computation doesn't regress trade count:
   ```bash
   PYTHONHASHSEED=0 python main.py --mode backtest --ticks 2000 --capital 10000
   # Must emit >= same proposal count as before this change
   ```

7. **Success criteria:** Backtest with 2000 ticks shows non-zero OFI-rejected proposals in logs (`"Proposal rejected: OFI adverse penalty"`), and total edge quality (P&L / #trades) improves vs baseline.

---

## BATCH 5 — Go-Live Validation (100/100)

---

### STEP 13 — 72-Hour Paper Soak Validation

**Task:** `docs/runbooks/paper_soak.md` and `scripts/paper_soak.py` exist. The soak must be run and its results formally recorded before any live capital is deployed. This is the final gate before live.

**Tools:** `filesystem`, `bash`

**Instructions:**

1. **Read** `docs/runbooks/paper_soak.md` fully. Read `scripts/run_paper_validation.py` to understand the harness arguments.

2. **Build the market registry** (minimum 25 pairs):
   ```bash
   python scripts/build_market_registry.py \
     --min-pairs 25 --output market_registry.json
   ```
   Verify output: `jq 'length' market_registry.json` → must be ≥ 25.

3. **Start the monitoring stack:**
   ```bash
   docker compose up -d prometheus grafana
   ```
   Confirm Grafana loads at `http://localhost:3000` with the PMTS dashboard.

4. **Launch the paper soak:**
   ```bash
   python scripts/run_paper_validation.py \
     --duration-hours 72 \
     --sample-seconds 300 \
     --obs-port 18080 \
     --min-registry-pairs 25 \
     --min-markets-total 25 \
     --min-markets-polymarket 10 \
     --min-markets-opinion 10 \
     2>&1 | tee soak_results/paper_soak_$(date +%Y%m%d).log &
   ```

5. **Monitor during the soak (every 6 hours):**
   ```bash
   # Health check
   curl -s http://localhost:18080/health | jq .status
   curl -s http://localhost:18080/ready  | jq .status
   # Feed staleness
   curl -s http://localhost:18080/metrics | grep feed_last_ts
   # Proposal count
   curl -s http://localhost:18080/metrics | grep proposals_total
   ```

6. **Success criteria (all must pass at soak end):**
   - `/health` stayed `ALIVE` for ≥ 71 of 72 hours (one restart allowed)
   - `/ready` stayed `READY` or `DEGRADED` (never `NOT_READY` for > 30 min)
   - Feed age stayed below stale threshold (< 60s) for both venues
   - At least 1 proposal evaluated per 10-minute window
   - Zero `CRITICAL` alerts fired (kill switch never tripped)
   - Final P&L, fills, and drawdown recorded in `soak_results/`

---

### STEP 14 — Small-Capital Live Validation ($500, 48 Hours)

**Task:** The final step before full deployment. Follow `docs/runbooks/go-live.md` section "4. Small-Capital Live Phase" exactly. No code changes — this is an operational validation.

**Tools:** `filesystem`, `bash`

**Instructions:**

1. **Read** `docs/runbooks/go-live.md` section 4 and `docs/runbooks/startup.md`.

2. **Run `scripts/verify_connectivity.py`** — both venues must return `✅`:
   ```bash
   python scripts/verify_connectivity.py
   # Expected: "✅ Polymarket connectivity verified" + "✅ Opinion connectivity verified"
   ```

3. **Configure risk limits for small-capital phase:**
   ```bash
   # In .env
   INITIAL_CASH_USDC=500
   MAX_ORDER_USDC=25
   MAX_DRAWDOWN_PCT=0.10        # tighten to 10% for small-capital phase
   DRAWDOWN_WARN_PCT=0.05
   ENABLE_TRADING=true
   ```

4. **Run for 48 hours:**
   ```bash
   python main.py --mode live \
     2>&1 | tee live_results/small_capital_$(date +%Y%m%d).log
   ```

5. **Acceptance criteria (from `go-live.md`):**
   - Fills reconcile with exchange state after every restart
   - Kill switch activates correctly on 10% drawdown breach
   - No orders left open after kill switch fires
   - At least 3 profitable arb trades executed
   - Alert routing confirmed: Slack message received for every kill-switch event

6. **If any criterion fails:** Stop immediately. Engage `docs/runbooks/kill_switch.md` procedure. Do not advance to full capital.

7. **Success criteria:** All 5 acceptance criteria met. Record results in `live_results/small_capital_summary.md`.

---

### STEP 15 — API Key Rotation Procedure + Final Go-Live Checklist

**Task:** P2-001 from `BUG_BACKLOG.md`. No procedure exists for rotating API keys without downtime. This is a security requirement before scaling. Implement + validate, then run the final readiness checklist.

**Tools:** `filesystem`

**Instructions:**

1. **Create `infrastructure/credentials.py`:**
   ```python
   """infrastructure/credentials.py — Zero-downtime credential rotation."""
   import os
   from dataclasses import dataclass
   from typing import Optional

   @dataclass
   class RotatingCredential:
       """Holds primary + standby credentials. Rotation is atomic."""
       primary_key: str
       primary_secret: str
       standby_key: Optional[str] = None
       standby_secret: Optional[str] = None

       def rotate(self) -> None:
           """Swap primary ↔ standby. Call after confirming standby works."""
           if not self.standby_key:
               raise ValueError("No standby credentials configured")
           self.primary_key, self.standby_key = self.standby_key, self.primary_key
           self.primary_secret, self.standby_secret = self.standby_secret, self.primary_secret

       @classmethod
       def from_env(cls, prefix: str) -> "RotatingCredential":
           return cls(
               primary_key=os.environ[f"{prefix}_API_KEY"],
               primary_secret=os.environ.get(f"{prefix}_API_SECRET", ""),
               standby_key=os.environ.get(f"{prefix}_STANDBY_KEY"),
               standby_secret=os.environ.get(f"{prefix}_STANDBY_SECRET"),
           )
   ```

2. **Add rotation endpoint to `api/server.py`:**
   ```python
   @app.post("/credentials/rotate/{venue}")
   async def rotate_credentials(venue: str, token: str):
       # Verify kill switch token (reuse existing auth)
       # Call credential.rotate() for specified venue
       # Log rotation event to audit_log.py
       ...
   ```

3. **Complete the final readiness checklist** — create `FINAL_READINESS_CHECKLIST.md`:

   ```markdown
   ## PMTS 100/100 Final Readiness Checklist

   ### Code Quality
   - [ ] mypy strict: zero errors in production modules
   - [ ] pytest: all tests green (>= 70% coverage)
   - [ ] No `time.time()` calls in production pipeline (clock-injected)
   - [ ] Global VenueRateLimiter in use (not instance-level Throttler)

   ### Testing
   - [ ] 6 sandbox validation scenarios: all pass
   - [ ] Venue contract tests: all pass (with fixtures)
   - [ ] Performance benchmarks: RiskEngine P50 < 5ms
   - [ ] Zero-trade regression CI test: active

   ### Observability
   - [ ] Prometheus scraping: verified
   - [ ] Grafana dashboard: loading with live data
   - [ ] Alerting: Slack webhook confirmed working (test message received)
   - [ ] Kill switch alert: verified end-to-end

   ### Operations
   - [ ] 72-hour paper soak: PASSED (logs in soak_results/)
   - [ ] Small-capital live phase: PASSED ($500, 48h, logs in live_results/)
   - [ ] API key rotation: procedure documented + tested
   - [ ] go-live runbook: signed off by Ops Primary + Risk Officer
   - [ ] Reconciliation runbook: tested post-restart
   - [ ] Kill switch runbook: tested with real drawdown trigger

   ### Infrastructure
   - [ ] PostgreSQL backend: tested with migration script
   - [ ] Market resolution: tested with mock resolved market
   - [ ] Advanced arb signals: OFI + dynamic edge active in backtest
   ```

4. **Walk through every item in the checklist** — mark it `[x]` only after verified, not assumed.

5. **Final score verification:**
   ```bash
   PYTHONHASHSEED=0 python main.py --mode backtest --ticks 2000 --capital 10000
   python -m pytest --tb=short -q 2>&1 | tail -5
   mypy --config-file mypy.ini execution/ engine/ strategies/ risk/ portfolio/ 2>&1 | tail -3
   ```
   All three must exit clean.

6. **Declare 100/100** when every checkbox in `FINAL_READINESS_CHECKLIST.md` is `[x]`.

---

## Quick Reference: File Map per Step

| Step | Files to READ | Files to CREATE/MODIFY |
|------|--------------|------------------------|
| 4 | `src/clock.py`, `execution/order_tracker.py`, `strategies/delta_neutral.py` | 14 production files (clock injection) |
| 5 | `infrastructure/alerting.py`, `risk/kill_switch.py`, `engine/orchestrator.py` | `execution/rate_limiter.py` (new), `risk/kill_switch.py`, `engine/orchestrator.py`, `main.py` |
| 6 | All production modules | `mypy.ini` (new), `.github/workflows/ci.yml` |
| 7 | `execution/clients/polymarket.py`, `execution/clients/opinion.py` | `tests/test_venue_clients.py` (new), `tests/fixtures/` (new) |
| 8 | `docs/runbooks/go-live.md`, `tests/test_sandbox_validation.py` | `tests/test_sandbox_validation.py` |
| 9 | `tests/test_performance.py` | `tests/test_performance.py`, `docs/performance_baseline.txt` (new) |
| 10 | `portfolio/storage.py`, `portfolio/storage_postgres.py` | `portfolio/storage_postgres.py`, `scripts/migrate_sqlite_to_postgres.py` (new) |
| 11 | `engine/resolution_monitor.py`, `execution/clients/polymarket.py` | `engine/resolution_monitor.py`, `engine/orchestrator.py` |
| 12 | `strategies/arbitrage.py`, `engine/feature_engine.py` | `strategies/arbitrage.py` |
| 13 | `docs/runbooks/paper_soak.md`, `scripts/run_paper_validation.py` | `soak_results/` (new) |
| 14 | `docs/runbooks/go-live.md`, `docs/runbooks/startup.md` | `live_results/small_capital_summary.md` (new) |
| 15 | `api/server.py`, `infrastructure/audit_log.py` | `infrastructure/credentials.py` (new), `FINAL_READINESS_CHECKLIST.md` (new) |

---

> **Agent note for Qwen:**
> - Always `READ` listed files before writing any code.
> - Run `PYTHONHASHSEED=0 python -m pytest tests/ -q` after every step to detect regressions.
> - Steps within a batch can be parallelized; steps across batches cannot (each batch depends on the previous).
> - Steps 13–14 are operational, not coding tasks — they require live exchange access and cannot be unit-tested in isolation.
