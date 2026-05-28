# Bugs and Improvements Backlog

Assessment date: 2026-05-28

This backlog is written as developer-ready tasks. Priorities use:

- P0: blocks backtest, paper mode, live startup, or capital safety.
- P1: blocks production deployment or reliable operations.
- P2: important hardening before scale-up.
- P3: cleanup or maintainability improvement.

## P0 Tasks

### TASK-P0-001: Fix `PortfolioManager.get_portfolio_mtm()` lock misuse

Problem:

`python main.py --mode backtest --ticks 20 --capital 1000` fails with:

`TypeError: 'Lock' object does not support the context manager protocol`

Root cause:

`portfolio/manager.py` creates `self._lock` as an `asyncio.Lock`, but `get_portfolio_mtm()` uses `with self._lock:` in a synchronous method.

Files:

- `portfolio/manager.py`
- `backtest/engine.py`
- `risk/engine.py`

Acceptance criteria:

- Backtest smoke command completes: `python main.py --mode backtest --ticks 2000 --capital 10000 --verbose`.
- `PortfolioManager.get_portfolio_mtm()` remains safe for synchronous callers in `RiskEngine.evaluate()`.
- Add a regression test that calls `RiskEngine.evaluate()` after positions exist and confirms no lock exception.
- Add a backtest smoke test to CI with a small tick count.

Suggested implementation notes:

- Keep hot-path synchronous reads truly lock-free by copying state without entering `asyncio.Lock`, or introduce a separate synchronous lock for read snapshots.
- Do not make `RiskEngine.evaluate()` async unless the full strategy/orchestrator call chain is intentionally redesigned.

### TASK-P0-002: Align trading config field names used by live and paper startup

Problem:

`main.py` reads:

- `settings.trading.max_market_exposure_pct`
- `settings.trading.max_market_exposure_usdc`

`TradingConfig` defines:

- `max_market_exp_pct`
- `max_market_exp_usdc`

This causes paper/live startup to fail when constructing `RiskLimits`.

Files:

- `main.py`
- `config/settings.py`
- `.env.example`
- `docs/USER_GUIDE.md`

Acceptance criteria:

- `python -c "from config.settings import TradingConfig; t=TradingConfig(); assert hasattr(t, 'max_market_exposure_pct')"` passes, or all call sites consistently use `max_market_exp_pct`.
- `python main.py --mode paper` reaches component startup when supplied valid paper-safe config.
- Tests cover settings-to-`RiskLimits` mapping.
- `.env.example`, docs, and code use one naming convention.

Suggested implementation notes:

- Prefer the longer explicit field names used by `RiskLimits`: `max_market_exposure_pct` and `max_market_exposure_usdc`.
- Keep backward-compatible env var names `MAX_MARKET_EXP_PCT` and `MAX_MARKET_EXP_USDC` unless operators have already migrated.

### TASK-P0-003: Make paper/no-trading mode evaluate proposals without submitting orders

Problem:

Runbooks say `ENABLE_TRADING=false` should still evaluate proposals and log "would have submitted" events. Current implementation passes `enable_trading=settings.trading.enable_trading` into `Orchestrator`, and `Orchestrator._on_feature_vector()` returns before strategy evaluation when `_trading` is false.

Files:

- `engine/orchestrator.py`
- `main.py`
- `docs/runbooks/go-live.md`
- `docs/runbooks/startup.md`

Acceptance criteria:

- With `ENABLE_TRADING=false`, market data still flows through feature computation, strategy evaluation, and risk evaluation.
- No real exchange `place_order()` calls happen when trading is disabled.
- Logs or metrics expose rejected/suppressed submissions as "dry run" or "would submit" events.
- Tests verify that dry-run mode produces proposals but sends zero orders to execution clients.

Suggested implementation notes:

- Separate "strategy evaluation enabled" from "order submission enabled".
- Gate submission in `_route_to_engine()` or an execution boundary, not before feature/strategy evaluation.

## P1 Tasks

### TASK-P1-004: Restore reproducible local developer environment

Problem:

Local test execution failed because system Python has no `pytest`, and `.venv\Scripts\python.exe` points to a missing WindowsApps Python path.

Files:

- `requirements.txt`
- `requirements.in`
- `README.md`
- `.github/workflows/ci.yml`

Acceptance criteria:

- Fresh checkout instructions create a working venv on Windows and Linux.
- `python -m pytest tests -q` runs locally after documented setup.
- Test dependencies are explicitly listed in either a dev requirements file or project optional dependencies.
- README does not rely on a checked-in `.venv`.

Suggested implementation notes:

- Do not commit `.venv`.
- Add `requirements-dev.txt` or `[project.optional-dependencies] dev = [...]`.
- Include `pytest`, `pytest-asyncio`, `ruff`, `mypy`, and `pytest-cov` in the dev dependency set.

### TASK-P1-005: Expose observability correctly in Docker Compose

Problem:

`ObservabilityServer` defaults to `127.0.0.1`. Docker Compose publishes `8080:8080` and Prometheus scrapes `pmts:8080`, but the app service does not set `OBSERVABILITY_BIND_HOST=0.0.0.0`. Health checks from inside the container can pass while Prometheus and host access fail.

Files:

- `main.py`
- `docker-compose.yml`
- `docs/prometheus.yml`
- `docs/USER_GUIDE.md`

Acceptance criteria:

- In Docker Compose, `curl http://localhost:8080/ready` works from the host.
- Prometheus target `pmts:8080` is healthy.
- App still supports secure local-only binding for non-Docker local runs.
- Documentation states which bind host to use for local and Docker deployments.

Suggested implementation notes:

- Set `OBSERVABILITY_BIND_HOST=0.0.0.0` in the Compose `pmts` service.
- Keep local default at `127.0.0.1`.

### TASK-P1-006: Implement or remove documented kill-switch HTTP endpoints

Problem:

Docs reference:

- `POST /kill-switch/activate`
- `POST /kill-switch/reset`

The running `ObservabilityServer` only exposes `/health`, `/ready`, `/metrics`, and `/metrics/json`. `api/server.py` is not wired into `main.py` and does not implement these kill-switch routes.

Files:

- `infrastructure/observability.py`
- `api/server.py`
- `main.py`
- `docs/USER_GUIDE.md`
- `docs/runbooks/kill_switch.md`

Acceptance criteria:

- Either authenticated kill-switch activate/reset endpoints exist and are wired in runtime, or docs are changed to the actual operator path.
- Reset requires the configured kill-switch token and an operator identifier.
- All kill-switch actions are logged with timestamp, operator, source IP or caller identity, and result.
- Tests cover activate, reset failure with bad token, reset success, and order-cancellation side effects.

Security requirements:

- Do not expose unauthenticated state-changing endpoints.
- Rate-limit reset attempts.
- Never log the reset token.

### TASK-P1-007: Validate exchange clients against sandbox APIs and fix fill accounting

Problem:

Exchange clients contain venue-specific assumptions for paths, auth, signing, order IDs, statuses, and amount scaling. Status calls currently return `new_fills=[]`, which can cause portfolio accounting to miss fills unless another path records them.

Files:

- `execution/clients/polymarket.py`
- `execution/clients/opinion.py`
- `execution/engine.py`
- `portfolio/manager.py`

Acceptance criteria:

- Sandbox tests cover `verify_connectivity`, `place_order`, `cancel_order`, `get_order_status`, and `get_open_orders` for each venue.
- Partial fills produce `OrderStatusResponse.new_fills` exactly once per fill.
- Local fill ledger reconciles to sandbox exchange-reported fills.
- Amount scaling is verified for USDC decimals and outcome-token units.
- All client methods handle non-JSON error responses without masking the original status/body.

Suggested implementation notes:

- Add exchange-client contract tests using recorded sandbox fixtures and a separate opt-in live sandbox test suite.
- Make idempotency and client order IDs explicit.

### TASK-P1-008: Add an explicit cross-venue market registry

Problem:

The same `MARKETS` values are passed into Polymarket asset IDs, Opinion market IDs, and internal logical market IDs. Real venues usually use different condition IDs, token IDs, market IDs, and outcome mappings.

Files:

- `config/settings.py`
- `main.py`
- `data/adapters/*`
- `execution/clients/*`
- `strategies/arbitrage.py`

Acceptance criteria:

- Config supports a logical market ID mapped to per-venue identifiers.
- YES/NO outcome token mapping is validated before trading.
- Strategy code consumes logical IDs; adapters and clients consume venue-specific IDs.
- Startup fails closed if a market mapping is incomplete or ambiguous.
- Tests cover mismatched IDs and inverted outcome mappings.

## P2 Tasks

### TASK-P2-009: Harden readiness checks to avoid expensive exchange calls per probe

Problem:

`HealthMonitor.check_readiness()` performs fresh exchange connectivity checks on each readiness request. In production, orchestration probes and Prometheus-style checks can make this expensive, slow, or rate-limited.

Files:

- `infrastructure/observability.py`
- `execution/clients/*`

Acceptance criteria:

- Readiness endpoint returns within 250 ms p95 under normal conditions.
- Exchange connectivity is cached with a short TTL or maintained by a background health task.
- Rate-limited exchange responses do not cascade into process restarts unless a real readiness threshold is breached.
- Tests cover stale cached connectivity and recovery.

### TASK-P2-010: Fix alerting configuration wiring

Problem:

`config.settings.AlertConfig` includes SMTP host and port, but `main.py` does not pass these values into `infrastructure.alerting.AlertConfig`. Operators cannot configure email transport fully through env vars.

Files:

- `main.py`
- `config/settings.py`
- `infrastructure/alerting.py`

Acceptance criteria:

- SMTP host and port from env are honored.
- Alert channel configuration is covered by tests.
- Missing alert channels are logged at startup as a warning in live mode.
- A test alert can be sent in paper/sandbox without live trading.

### TASK-P2-011: Secure production Docker and Grafana defaults

Problem:

Compose uses `GF_SECURITY_ADMIN_PASSWORD=admin` and enables anonymous Grafana viewer access. This is not production-safe.

Files:

- `docker-compose.yml`
- `docs/USER_GUIDE.md`
- `docs/runbooks/startup.md`

Acceptance criteria:

- Production Compose or override file requires a non-default Grafana admin password.
- Anonymous access is disabled by default for production.
- Local demo/dev mode remains easy to run with clearly marked unsafe defaults.
- Docs include a production monitoring hardening checklist.

### TASK-P2-012: Add CI release-gate jobs

Problem:

CI runs lint, mypy, and tests, but does not enforce the release gates needed for this trading system.

Files:

- `.github/workflows/ci.yml`
- `tests/*`

Acceptance criteria:

- CI runs a backtest smoke test.
- CI starts paper mode with fake/synthetic adapters or a fast startup mode.
- CI validates Docker build.
- CI verifies observability routes respond.
- CI uploads coverage and fails below the configured threshold.

### TASK-P2-013: Add reconciliation and restart recovery test coverage

Problem:

Reconciliation logic exists, but production readiness depends on proving that restart mid-order cannot leave orphaned orders, reservations, or arb groups.

Files:

- `execution/engine.py`
- `risk/engine.py`
- `portfolio/storage.py`
- `engine/orchestrator.py`
- `tests/*`

Acceptance criteria:

- Test starts with persisted active orders and reservations, then simulates exchange open-order state.
- Matched open orders are restored to trackers.
- Missing orders release reservations.
- Kill-switch active state persists and blocks proposals after restart.
- Arb leg groups are cleared or reconstructed without duplicate submissions.

## P3 Tasks

### TASK-P3-014: Clean `.env.example` defaults

Problem:

`.env.example` enables AI by default and has inline comments after empty secret values. Depending on env-file parser behavior, those comments can become literal values.

Files:

- `.env.example`
- `docs/USER_GUIDE.md`

Acceptance criteria:

- AI is disabled by default in example config.
- Empty secret variables have no inline comments after `=`.
- Comments are placed on separate lines.
- Example config can be loaded by Docker Compose and local env loaders without surprising values.

### TASK-P3-015: Remove duplicate helper/function definitions

Problem:

There are duplicate function definitions that increase maintenance risk:

- `_e()` appears twice in `config/settings.py`.
- `close()` appears twice in both `execution/clients/polymarket.py` and `execution/clients/opinion.py`.

Files:

- `config/settings.py`
- `execution/clients/polymarket.py`
- `execution/clients/opinion.py`

Acceptance criteria:

- Duplicate definitions are removed.
- Client `close()` consistently closes sessions and clears private key material.
- Tests cover client close behavior.

### TASK-P3-016: Align dashboard API with portfolio manager

Problem:

`api/server.py` calls `portfolio_manager.get_all_positions()`, but `PortfolioManager` does not expose this method. The endpoint also maps `unrealized_pnl` to `realised_pnl`.

Files:

- `api/server.py`
- `portfolio/manager.py`

Acceptance criteria:

- `/positions` works when the API server is wired with a real `PortfolioManager`.
- Response fields distinguish realized and unrealized P&L correctly.
- Tests cover empty and non-empty portfolios.

### TASK-P3-017: Remove binary/null content from README

Problem:

`rg` reports `README.md` as a binary file because it contains a null byte near the end. This can break search, diffs, linters, and documentation tooling.

Files:

- `README.md`

Acceptance criteria:

- `rg` treats `README.md` as text.
- README renders correctly in GitHub or the target markdown renderer.
- Encoding is UTF-8.

## Suggested Delivery Order

1. TASK-P0-001
2. TASK-P0-002
3. TASK-P0-003
4. TASK-P1-004
5. TASK-P1-005
6. TASK-P1-006
7. TASK-P1-007
8. TASK-P1-008
9. P2 hardening tasks
10. P3 cleanup tasks

## Definition of Done for Any Task

- Code change has a focused test.
- Relevant docs or runbooks are updated.
- CI passes.
- The change is verified through at least one command listed in the task acceptance criteria.
- For trading behavior changes, failure mode is fail-closed and explicitly tested.
