# Bugs And Improvements Backlog

Assessment date: 2026-05-28

This backlog is written as developer-ready work. Priorities:

- P0: blocks paper/live correctness, release verification, or capital safety.
- P1: blocks production deployment or reliable operations.
- P2: hardening needed before scaling.
- P3: cleanup and maintainability.

## P0 Tasks

### TASK-P0-001: Fix release backtest regression to zero trades

Problem:

`python main.py --mode backtest --ticks 200 --capital 10000` and `python main.py --mode backtest --ticks 2000 --capital 10000` complete successfully but produce zero proposals, zero fills, and `$+0.00` P&L. This makes the published backtest gate ineffective.

Files:

- `main.py`
- `backtest/engine.py`
- `engine/strategy_engine.py`
- `strategies/arbitrage.py`
- `tests/test_integration.py`

Acceptance criteria:

- `python main.py --mode backtest --ticks 200 --capital 10000` emits at least one proposal and at least one fill.
- `python main.py --mode backtest --ticks 2000 --capital 10000 --verbose` emits non-zero proposal and fill metrics.
- Add a CI smoke test that fails if expected-active synthetic backtests produce zero proposals or zero fills.
- Backtest summary clearly reports whether a no-trade run is expected or a regression.

Implementation notes:

- Inspect feature generation, strategy cooldowns, `days_to_resolution`, synthetic stream construction, and risk limits together.
- Keep a separate "quiet/no-op scenario" if zero-trade behavior is useful for tests.

### TASK-P0-002: Make backtest seeds deterministic across processes

Problem:

`main.py` seeds synthetic streams with `hash(market_id) % (2**31)`. Python hash randomization means the same CLI command can produce different streams across processes.

Files:

- `main.py`
- `backtest/engine.py`

Acceptance criteria:

- Backtest accepts or internally uses stable integer seeds.
- Same command produces identical summary metrics across separate Python processes.
- Tests cover deterministic stream generation.

Implementation notes:

- Use a stable hash such as SHA-256 truncated to an integer, or define explicit seeds per default market.

### TASK-P0-003: Keep WebSocket adapter connections open

Problem:

Both `PolymarketWSAdapter._run_loop()` and `OpinionWSAdapter._run_loop()` create `_process_messages(ws)` as a task inside `async with websockets.connect(...)`, then leave the context immediately. Exiting the context closes the WebSocket and can leave paper/live mode with no durable market data.

Files:

- `data/adapters/polymarket_ws.py`
- `data/adapters/opinion_ws.py`
- `tests/test_ws_adapters.py`
- `tests/test_failures.py`

Acceptance criteria:

- `_run_loop()` awaits message processing while inside the WebSocket context.
- A mocked adapter test proves the context is not exited until disconnect, stop, or cancellation.
- Reconnect tests prove a disconnect creates a new connection and resumes subscription.
- Paper mode receives fresh snapshots for both platforms for at least 30 minutes in a local smoke/soak test.

Implementation notes:

- Prefer `await self._process_messages(ws)` inside the context.
- Ensure `stop()` cancels the active loop promptly.

### TASK-P0-004: Wire live HTTP kill switch to token config and order cancellation

Problem:

Paper mode calls `obs_server.set_kill_switch_config(...)`; live mode does not. In live mode, `/kill-switch/activate` and `/kill-switch/reset` exist but are not configured with a token. Activation also calls `risk.manual_activate(...)` without calling the orchestrator path that cancels open orders.

Files:

- `main.py`
- `infrastructure/observability.py`
- `engine/orchestrator.py`
- `risk/engine.py`
- `docs/runbooks/kill_switch.md`
- `docs/USER_GUIDE.md`

Acceptance criteria:

- Live mode configures kill-switch HTTP token and reset callback.
- `POST /kill-switch/activate` activates the kill switch and cancels all open orders.
- `POST /kill-switch/reset` requires the token and operator ID, rate-limits attempts, and clears stale runtime state.
- Tests cover live-mode endpoint configuration, bad token, activation cancellation, reset success, and no token leakage in logs.

Implementation notes:

- The state-changing HTTP path should call an orchestrator callback, for example `orchestrator.emergency_stop(reason)`, rather than only mutating risk state.

### TASK-P0-005: Fix paper-mode startup so it does not require live credentials

Problem:

`ENABLE_TRADING` defaults to `true`, and `settings.validate()` requires live Polymarket and Opinion credentials when trading is enabled. Paper mode uses `PaperTradingClient`, so a clean paper run should not require live API keys or wallet keys.

Files:

- `main.py`
- `config/settings.py`
- `.env.example`
- `docs/USER_GUIDE.md`
- `docs/runbooks/startup.md`

Acceptance criteria:

- `python main.py --mode paper` can start with paper-safe config and no live exchange credentials.
- Live mode still fails closed when real credentials are missing.
- `.env.example` defaults to safe non-live behavior.
- Tests cover validation behavior for backtest, paper, dry-run live, and live trading.

Implementation notes:

- Separate validation profiles by mode: backtest, paper, live-dry-run, live-submit.
- Consider defaulting `ENABLE_TRADING=false` in `.env.example`.

### TASK-P0-006: Repair fill accounting for partial fills and status polling

Problem:

Venue clients return `new_fills=[]` from `get_order_status()`. `ExecutionEngine` can synthesize a final fill when an order is marked filled, but partial fills before final state are missed. `PaperTradingClient.get_order_status()` also returns no new fills.

Files:

- `execution/engine.py`
- `execution/clients/polymarket.py`
- `execution/clients/opinion.py`
- `execution/clients/paper.py`
- `execution/order_tracker.py`
- `portfolio/manager.py`

Acceptance criteria:

- Partial fills from status polling emit `OrderStatusResponse.new_fills` exactly once.
- Duplicate exchange fill events do not double-count.
- Paper client supports poll-discovered partial fills for tests.
- Portfolio positions and fill metrics match cumulative exchange fill state.
- Tests cover immediate fill, partial fill, duplicate fill, final fill, and cancellation after partial fill.

Implementation notes:

- Track last seen cumulative filled amount or exchange fill IDs per order.
- Ensure idempotency survives process restart where possible.

### TASK-P0-007: Fix persistent fill ledger primary key

Problem:

`portfolio/storage.py` defines `fills.proposal_id` as the primary key and uses `INSERT OR IGNORE`. Multiple partial fills for the same proposal are silently dropped from the ledger.

Files:

- `portfolio/storage.py`
- `portfolio/manager.py`
- `tests/*`

Acceptance criteria:

- Multiple fills for the same proposal are stored as separate ledger rows.
- Ledger rows have a stable unique key, such as exchange fill ID or `(proposal_id, order_id, ts, filled_usdc, fill_price)` with collision handling.
- Existing portfolio state migration is documented or implemented.
- Tests verify two partial fills for one proposal are both persisted.

### TASK-P0-008: Resolve near-expiry arbitrage policy mismatch

Problem:

`tests.test_integration.TestArbitrageStrategyIntegration.test_near_expiry_halves_arb_size` expects markets with less than one day to resolution to be accepted at half size. `ArbConfig.min_days_to_resolution=1.0` now rejects those markets before size reduction.

Files:

- `strategies/arbitrage.py`
- `strategies/delta_neutral.py`
- `tests/test_integration.py`
- `README.md`
- `docs/USER_GUIDE.md`

Acceptance criteria:

- Product policy is explicit: either near-expiry arb is rejected, or it is allowed at reduced size.
- Strategy implementation, tests, README, and runbooks all match that policy.
- If rejected, the old half-size branch is removed or gated behind a lower threshold.
- If allowed, `min_days_to_resolution` is adjusted and tested.

## P1 Tasks

### TASK-P1-009: Add explicit cross-venue market registry

Problem:

The same `MARKETS` values are used as Polymarket WebSocket asset IDs, Polymarket order token IDs, Opinion market IDs, and internal logical market IDs. Real venues usually require separate condition IDs, token IDs, outcome mappings, and market IDs.

Files:

- `config/settings.py`
- `main.py`
- `data/adapters/*`
- `execution/clients/*`
- `strategies/arbitrage.py`

Acceptance criteria:

- Config supports logical market IDs mapped to per-venue identifiers.
- YES/NO outcome token mapping is validated before trading.
- Strategies consume logical IDs; adapters and clients consume venue-specific IDs.
- Startup fails closed when mappings are incomplete, inverted, or ambiguous.
- Tests cover mismatched IDs and inverted outcome mappings.

### TASK-P1-010: Validate venue clients against sandbox or recorded fixtures

Problem:

The Polymarket and Opinion clients contain assumptions around endpoints, auth headers, signing payloads, order IDs, amount scaling, and response bodies. These are not production-safe without sandbox or recorded contract tests.

Files:

- `execution/clients/polymarket.py`
- `execution/clients/opinion.py`
- `tests/test_polymarket_client.py`
- `tests/test_opinion_client.py`

Acceptance criteria:

- Contract tests cover `verify_connectivity`, `place_order`, `cancel_order`, `get_order_status`, and `get_open_orders` for both venues.
- Non-JSON error responses preserve status and body in logs/exceptions.
- USDC and token amount scaling is verified for each venue.
- Sandbox tests are opt-in and cannot accidentally place live orders.

### TASK-P1-011: Align risk reservations with portfolio and metrics

Problem:

RiskEngine maintains the real reservation table, but `PortfolioManager.reserved_capital` is not updated on approval. The metrics provider reports `portfolio.reserved_capital`, which can show zero while RiskEngine has active reservations.

Files:

- `risk/engine.py`
- `portfolio/manager.py`
- `main.py`
- `infrastructure/observability.py`

Acceptance criteria:

- There is one authoritative reservation source, or all views are synchronized.
- `/metrics/json` reports the same reserved capital used by the risk gate.
- Tests verify reservation metrics after approval and after terminal release.

### TASK-P1-012: Restore reproducible local test environment

Problem:

Local `python -m pytest tests -q` failed because the active Python has no `pytest`. The command used Python 3.13, while CI uses Python 3.11 and the project recommends 3.11/3.12.

Files:

- `requirements-dev.txt`
- `pyproject.toml`
- `README.md`
- `.github/workflows/ci.yml`

Acceptance criteria:

- Fresh checkout instructions create a working Python 3.11 or 3.12 venv on Windows and Linux.
- `python -m pytest tests -q` runs after documented setup.
- README tells developers not to rely on a checked-in `.venv`.
- CI and local docs use the same command shape.

### TASK-P1-013: Fix Windows logging encoding fragility

Problem:

The partial unittest run produced a `UnicodeEncodeError` on Windows `cp1251` output when logging symbols such as `>=`. This can hide useful test output and make local verification brittle.

Files:

- `config/logging_setup.py`
- `risk/engine.py`
- tests and docs that assert output

Acceptance criteria:

- Test logging works on Windows code pages without encode errors.
- Either logs use ASCII-safe symbols or handlers are configured for UTF-8 with replacement behavior.
- CI includes at least one check that exercises warning/error logs.

### TASK-P1-014: Add paper-mode service smoke test

Problem:

CI builds Docker and runs a backtest smoke, but it does not start paper mode and verify observability endpoints.

Files:

- `.github/workflows/ci.yml`
- `main.py`
- `infrastructure/observability.py`
- `tests/*`

Acceptance criteria:

- CI starts paper mode with fake/synthetic adapters or a fast startup mode.
- CI verifies `/health`, `/ready`, `/metrics`, and `/metrics/json`.
- The smoke test requires zero live credentials.
- The test fails if market-data adapters never produce fresh snapshots.

## P2 Tasks

### TASK-P2-015: Harden operator endpoint security

Problem:

Kill-switch endpoints currently use a JSON body token. That is better than unauthenticated endpoints, but not enough for production exposure.

Files:

- `infrastructure/observability.py`
- `docker-compose.yml`
- `docs/runbooks/kill_switch.md`

Acceptance criteria:

- Endpoints are protected by network restriction, auth middleware, or an equivalent operator-only boundary.
- Reset attempts are rate-limited by source and operator identity.
- Token is never logged.
- Audit log includes timestamp, operator, source, action, and result.

### TASK-P2-016: Remove production default Grafana password fallback

Problem:

`docker-compose.yml` still allows `GF_SECURITY_ADMIN_PASSWORD` to default to `admin`.

Files:

- `docker-compose.yml`
- `docs/USER_GUIDE.md`
- `docs/runbooks/startup.md`

Acceptance criteria:

- Production deployment requires a non-default Grafana admin password.
- Local demo/developer mode remains easy but clearly marked as unsafe.
- Documentation includes a monitoring hardening checklist.

### TASK-P2-017: Improve readiness semantics

Problem:

Readiness requires at least one alive feed and exchange connectivity. That is good for live trading, but paper/dry-run startup and health probes may need mode-specific readiness details.

Files:

- `infrastructure/observability.py`
- `main.py`
- `docs/runbooks/startup.md`

Acceptance criteria:

- Readiness response distinguishes live, paper, degraded, and not-ready states.
- Readiness remains fast under repeated probes.
- Exchange rate limits do not cause unnecessary restarts.

### TASK-P2-018: Add restart reconciliation end-to-end tests

Problem:

Reconciliation logic exists, but production readiness requires proving that restart during active orders cannot leave orphaned orders, stale reservations, or duplicate fills.

Files:

- `execution/engine.py`
- `risk/engine.py`
- `portfolio/storage.py`
- `engine/orchestrator.py`
- `tests/*`

Acceptance criteria:

- Test starts with persisted active orders and reservations, then simulates exchange open-order state.
- Matched open orders are restored to trackers.
- Missing terminal orders release reservations.
- Kill-switch active state persists and blocks proposals after restart.
- Arb leg groups are cleared or reconstructed without duplicate submissions.

## P3 Tasks

### TASK-P3-019: Remove duplicate client `close()` definitions

Problem:

`execution/clients/polymarket.py` and `execution/clients/opinion.py` each define `close()` twice. One Polymarket definition clears the private key and the later one overwrites it.

Files:

- `execution/clients/polymarket.py`
- `execution/clients/opinion.py`

Acceptance criteria:

- Each client has one `close()` method.
- Close shuts down sessions and clears private key material.
- Tests cover close idempotency.

### TASK-P3-020: Clean stale architecture docs

Problem:

`architectural_audit.md` still says live clients are `NotImplementedError` skeletons, which no longer matches the current tree.

Files:

- `architectural_audit.md`
- `README.md`
- `docs/*`

Acceptance criteria:

- Architecture docs reflect current implementation status.
- Stale statements about unimplemented clients are removed or marked historical.
- New production blockers link to this backlog instead.

## Suggested Delivery Order

1. TASK-P0-003
2. TASK-P0-001
3. TASK-P0-002
4. TASK-P0-004
5. TASK-P0-005
6. TASK-P0-006
7. TASK-P0-007
8. TASK-P0-008
9. P1 sandbox, registry, and test-environment tasks
10. P2 security/readiness/reconciliation hardening
11. P3 cleanup

## Definition Of Done For Any Task

- Code change has a focused test.
- Relevant docs or runbooks are updated.
- CI passes.
- The task acceptance criteria are verified with the listed command or an equivalent automated check.
- Trading behavior changes fail closed and include an explicit failure-mode test.
