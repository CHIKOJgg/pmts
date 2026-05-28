# Production Readiness Assessment

Assessment date: 2026-05-28

## Executive Summary

The project is not production-ready for live trading yet.

The repository contains many production-oriented building blocks: explicit risk limits, kill-switch persistence, reconciliation hooks, structured logs, Prometheus metrics, Docker packaging, and operational runbooks. However, the current tree has blocking runtime failures in the zero-credential backtest path and in the paper/live startup path. These failures must be fixed before the system can safely enter paper trading, sandbox validation, or small-capital live trading.

Current readiness recommendation: block production release and block live-capital deployment.

## Evidence Reviewed

- Entry points: `main.py`
- Configuration: `config/settings.py`, `.env.example`
- Trading flow: `engine/orchestrator.py`, `engine/strategy_engine.py`, `strategies/*`
- Risk and safety: `risk/engine.py`, `risk/kill_switch.py`, `risk/limits.py`
- Execution: `execution/engine.py`, `execution/clients/*`
- Market data: `data/market_data_provider.py`, `data/adapters/*`
- Portfolio and persistence: `portfolio/manager.py`, `portfolio/storage.py`
- Observability: `infrastructure/observability.py`, `docs/prometheus.yml`, `docker-compose.yml`
- Operations docs: `docs/runbooks/*`, `docs/USER_GUIDE.md`
- Test/config tooling: `pyproject.toml`, `.github/workflows/ci.yml`, `tests/*`

## Verification Results

| Check | Result | Notes |
| --- | --- | --- |
| Python compile check | Pass | `python -m compileall -q main.py config data engine execution portfolio risk src strategies ai backtest api infrastructure` completed successfully. |
| Backtest smoke test | Fail | `python main.py --mode backtest --ticks 20 --capital 1000` fails in `PortfolioManager.get_portfolio_mtm()` because `asyncio.Lock` is used as a synchronous context manager. |
| Local test suite | Not runnable in current local env | System Python has no `pytest`; `.venv` points to a missing WindowsApps Python path. |
| Live/paper startup static check | Fail | `main.py` references `settings.trading.max_market_exposure_pct` and `max_market_exposure_usdc`; `TradingConfig` defines `max_market_exp_pct` and `max_market_exp_usdc`. |
| Runtime health endpoints | Partially implemented | `/health`, `/ready`, `/metrics`, and `/metrics/json` exist in `ObservabilityServer`; management endpoints documented for kill switch are not wired. |
| CI | Present but narrow | CI installs dependencies, runs ruff, mypy, and pytest coverage on Python 3.11. It does not run Docker, backtest smoke, paper-mode startup, readiness, or exchange-client contract checks. |

## Readiness by Area

### 1. Runtime Stability

Status: not ready.

Blocking issues:

- Backtest cannot complete due to a lock misuse in `portfolio/manager.py`.
- Paper/live startup will fail because `main.py` and `TradingConfig` use different risk-limit field names.
- The checked-in virtual environment is broken locally, making developer verification unreliable.

Production gate:

- Backtest must run from a fresh checkout with zero credentials.
- Paper mode must start and stop cleanly with synthetic or sandbox feeds.
- Live mode must fail closed on invalid config before creating exchange clients or starting background tasks.

### 2. Trading Safety

Status: partially ready.

Strengths:

- Synchronous risk gate reserves capital before order submission.
- Risk engine enforces order size, capital, exposure, drawdown, strategy budget, stale MTM, and delta limits.
- Kill switch state persists through SQLite.
- Reconciliation hooks exist for execution engines and risk reservations.

Gaps:

- Release cannot trust these controls until the backtest and startup blockers are fixed.
- Paper runbooks say `ENABLE_TRADING=false` should still evaluate proposals, but `Orchestrator._on_feature_vector()` returns before strategy evaluation when trading is disabled.
- Operator kill-switch HTTP endpoints are documented but not implemented in the running observability server.
- No confirmed sandbox test suite covers partial arb leg fills, restart recovery, exchange outage, and kill-switch reset end to end.

Production gate:

- Kill switch can be activated and reset through an authenticated operator path.
- All open orders are cancelled on kill-switch activation, process termination, and exchange outage.
- Restart reconciliation leaves no orphaned reservations, stale in-flight arb groups, or untracked open orders.

### 3. Market Data and Execution

Status: not ready for live capital.

Strengths:

- WebSocket adapters include reconnect loops and stale-data handling.
- Execution engine tracks order lifecycle, expiry, retries, polling, and persisted active orders.
- Exchange clients implement a common protocol shape.

Gaps:

- Exchange client implementations contain assumptions and placeholders that need sandbox verification against the real APIs.
- Fill parsing currently returns empty `new_fills` in REST status calls, so partial/final fill accounting depends on other paths or may miss fills.
- Market identifiers are reused across Polymarket asset IDs and Opinion market IDs; cross-venue mapping needs an explicit validated registry.
- Docker Compose does not expose the observability server to sibling containers unless `OBSERVABILITY_BIND_HOST=0.0.0.0` is set.

Production gate:

- Sandbox integration verifies order placement, cancellation, status polling, partial fills, open-order listing, and auth signing for each venue.
- Market registry maps logical market IDs to venue-specific condition IDs, token IDs, and outcome tokens.
- Fill accounting reconciles exchange statements to local ledger to the cent.

### 4. Observability and Operations

Status: partially ready.

Strengths:

- Structured JSON logging exists.
- Prometheus metrics and health endpoints exist.
- Grafana/Prometheus compose stack exists.
- Runbooks cover startup, shutdown, go-live, outage, reconciliation, kill switch, and escalation.

Gaps:

- Readiness performs fresh exchange connectivity checks on each request, which can be slow, rate-limited, or fragile.
- Alerting is partially wired; email SMTP host/port settings from `config.settings.AlertConfig` are not passed into `infrastructure.alerting.AlertConfig`.
- Grafana defaults are not production-safe (`admin/admin`, anonymous viewer enabled).
- Runbook assumptions do not all match implemented behavior.

Production gate:

- Dashboards and alerts are validated in paper mode and sandbox mode.
- Readiness is fast, bounded, and safe under repeated scrape/health-check traffic.
- Runbooks are executed by an operator who did not write the code, without improvisation.

### 5. Security and Configuration

Status: not ready for production.

Strengths:

- Secret-file environment variables exist for several sensitive values.
- Config validation catches missing credentials and weak kill-switch tokens.
- Docker runs the app as a non-root user.

Gaps:

- `.env.example` enables AI by default and contains inline comments after empty secret values, which can become literal values depending on env-file parsing.
- Production Grafana defaults are insecure.
- No documented secret rotation procedure, key scope, withdrawal controls, or wallet blast-radius limit.
- No authentication or authorization exists for any future operator management endpoints.

Production gate:

- Production config defaults to no external API calls and no live trading.
- Secrets are mounted from a secret manager or file mount, not committed env files.
- Operator endpoints require authentication, authorization, audit logging, and rate limits.
- Wallets used by the bot have capped balances and documented revocation procedures.

## Release Gates

### Gate 0: Developer Verification

Acceptance criteria:

- Fresh checkout on Python 3.11 or 3.12 can create a venv and install dependencies with one documented command.
- `python -m compileall` passes.
- `ruff check .` passes.
- `mypy . --ignore-missing-imports` passes or has a documented baseline.
- `pytest` passes locally and in CI.
- Coverage is at least 70%, matching `pyproject.toml`.

Required metrics:

- Test pass rate: 100%.
- Coverage: at least 70% overall, at least 85% for `risk`, `execution`, and `portfolio`.
- Static analysis regressions: 0 new ruff or mypy errors.

### Gate 1: Backtest Baseline

Acceptance criteria:

- `python main.py --mode backtest --ticks 2000 --capital 10000 --verbose` completes without credentials.
- Backtest records proposals, approvals, rejections, fills, slippage, P&L, drawdown, and Sharpe/Sortino.
- Backtest result is deterministic when a fixed seed is used.
- Backtest fails the process on unhandled exceptions.

Required metrics:

- Backtest completion rate: 100% over at least 10 repeated runs.
- Unhandled exceptions: 0.
- Max run-to-run metric drift with fixed seed: 0.
- Backtest runtime for 2,000 ticks: less than 30 seconds on a developer machine.

### Gate 2: Paper Trading

Acceptance criteria:

- Paper mode starts from Docker Compose and local Python.
- Paper mode can run for 24 hours without unhandled exceptions.
- `ENABLE_TRADING=false` means strategies evaluate and proposals are logged, but no exchange orders are submitted.
- `--mode paper` never creates real exchange clients and never signs live orders.
- `/health`, `/ready`, `/metrics`, and `/metrics/json` are reachable from host and Prometheus container.

Required metrics:

- Uptime: 24 hours continuous.
- Event loop liveness age: below 10 seconds for 99.9% of samples.
- Market-data staleness: below configured threshold for at least 99% of subscribed markets during normal exchange operation.
- Proposal-to-risk latency: p95 below 5 ms.
- Order lifecycle simulation errors: 0.

### Gate 3: Sandbox Exchange Validation

Acceptance criteria:

- Polymarket sandbox and Opinion sandbox credentials pass connectivity checks.
- Place, cancel, get status, get open orders, and partial-fill paths are validated for both venues.
- Startup reconciliation recovers from a forced process restart while orders are open.
- Kill-switch activation cancels all open orders and prevents new submissions.
- Market resolution removes the market from active trading and redeems/settles positions where supported.

Required metrics:

- Sandbox scenario pass rate: 100%.
- Reconciliation mismatches after restart: 0.
- Open orders remaining after kill-switch cancellation window: 0.
- Fill ledger mismatch versus exchange-reported fills: 0 USDC after rounding tolerance.

### Gate 4: Small-Capital Live

Acceptance criteria:

- Capital is limited to a small explicitly approved wallet balance.
- Initial live config uses conservative limits, for example `MAX_ORDER_USDC=25` and drawdown kill threshold no higher than 10%.
- Operator can activate kill switch and verify all orders cancel.
- Daily reconciliation matches exchange statements.
- Alerts page or alert channel is monitored by an accountable operator.

Required metrics:

- Daily reconciliation mismatch: 0 unresolved mismatches.
- Unhedged arbitrage residual exposure after arb completion: 0 above dust threshold.
- Unexpected overnight positions: 0.
- Critical alert delivery time: p95 below 60 seconds.
- Manual kill-switch time to no open orders: p95 below 30 seconds, measured in sandbox first.

## Production Readiness Scorecard

| Area | Score | Rationale |
| --- | --- | --- |
| Runtime correctness | 1/5 | Backtest and live/paper startup have blocking failures. |
| Risk controls | 3/5 | Good design primitives exist, but unverified under current runtime failures. |
| Execution correctness | 2/5 | Protocol shape exists; real exchange behavior and fill parsing require sandbox proof. |
| Observability | 3/5 | Metrics and health endpoints exist; deployment reachability and alerting gaps remain. |
| Operations | 3/5 | Runbooks exist but some assumptions conflict with implementation. |
| Security | 2/5 | Secret-file support exists; production defaults and management auth need hardening. |
| CI/CD | 2/5 | Basic CI exists; missing smoke, Docker, sandbox, and release-gate jobs. |

Overall: 2/5, not ready for production.

## Minimum Path to Production

1. Fix P0 runtime blockers listed in `docs/BUGS_AND_IMPROVEMENTS.md`.
2. Restore a reproducible developer environment and CI dependency set.
3. Add backtest and paper-mode startup smoke tests to CI.
4. Align runbooks with actual paper/live behavior.
5. Complete sandbox exchange validation for both venues.
6. Harden Docker/observability/security defaults.
7. Run a 24-hour paper soak and a full kill-switch/reconciliation drill.
8. Only then consider a small-capital live trial.
