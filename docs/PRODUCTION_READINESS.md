# Production Readiness Assessment

Assessment date: 2026-05-28

## Executive Summary

The project is closer to paper-trading readiness than the previous assessment, but it is still not production-ready for live capital.

Several former blockers appear fixed in the current tree:

- The zero-credential backtest path no longer crashes on `PortfolioManager.get_portfolio_mtm()`.
- Trading config field names now align between `main.py` and `config/settings.py`.
- Dry-run mode now evaluates strategy and risk before suppressing submission.
- Alert SMTP host/port are passed from settings.
- Docker Compose exposes the app observability port with `OBSERVABILITY_BIND_HOST=0.0.0.0`.
- Grafana anonymous access is disabled by default.
- `PortfolioManager.get_all_positions()` now exists.

Current recommendation: block live-capital deployment. Allow only developer backtesting and targeted paper-mode debugging after the P0 items in `docs/BUGS_AND_IMPROVEMENTS.md` are addressed.

## Evidence Reviewed

- Entry points: `main.py`
- Configuration: `config/settings.py`, `.env.example`, `docker-compose.yml`
- Backtest: `backtest/engine.py`
- Trading orchestration: `engine/orchestrator.py`, `engine/strategy_engine.py`
- Strategies: `strategies/arbitrage.py`, `strategies/delta_neutral.py`
- Market data: `data/market_data_provider.py`, `data/adapters/polymarket_ws.py`, `data/adapters/opinion_ws.py`
- Execution: `execution/engine.py`, `execution/clients/*`
- Risk and safety: `risk/engine.py`, `risk/kill_switch.py`, `risk/limits.py`
- Portfolio and persistence: `portfolio/manager.py`, `portfolio/storage.py`
- Observability: `infrastructure/observability.py`
- Tests and tooling: `tests/*`, `test_bug_fixes.py`, `pyproject.toml`, `.github/workflows/ci.yml`

## Verification Results

| Check | Result | Notes |
| --- | --- | --- |
| Python compile check | Pass | `python -m compileall -q main.py config data engine execution portfolio risk src strategies ai backtest api infrastructure` completed successfully. |
| Backtest crash smoke | Pass | `python main.py --mode backtest --ticks 50 --capital 1000` completed and produced proposals/fills. |
| Backtest release smoke | Fail | `python main.py --mode backtest --ticks 200 --capital 10000` and `--ticks 2000` completed with zero proposals, zero fills, and `$+0.00` P&L. |
| Pytest suite | Not runnable locally | Current system Python is 3.13 and has no `pytest`: `No module named pytest`. |
| Unittest-compatible partial suite | Fail | `python -m unittest tests.test_integration tests.test_failures -q` ran 83 tests with 2 failures. |
| Compile/runtime encoding | Risk | The unittest run produced a Windows `cp1251` logging encode error for symbols such as `>=`; tests continued, but local verification output is noisy and fragile. |

Failing unittest checks:

- `TestArbitrageStrategyIntegration.test_near_expiry_halves_arb_size`: strategy now rejects `days_to_resolution < 1.0` before applying the half-size behavior expected by the test.
- `TestBacktestSystem.test_backtest_cli_emits_trades`: 2000-tick CLI backtest regressed to zero trades.

## Readiness By Area

### Runtime Correctness

Status: not ready.

Strengths:

- The app compiles.
- A short 50-tick backtest can complete with fills.
- The old sync/async lock crash in portfolio MTM is fixed.

Blocking gaps:

- Release-sized backtests can complete with no proposals or trades, making the published backtest workflow unreliable as a go-live gate.
- The main backtest seed uses Python `hash(market_id)`, which is randomized per process and undermines deterministic release metrics.
- Paper mode uses `settings.validate()` with `ENABLE_TRADING=true` by default, so a paper-only run can require real live credentials even though it uses `PaperTradingClient`.

Production gate:

- Backtest smoke and release backtest both produce non-zero proposal/fill metrics under a fixed seed.
- Paper mode starts from a clean `.env` with no live credentials when explicitly configured for paper-only execution.
- Local and CI verification run under the same supported Python version.

### Trading Safety

Status: partially ready, not proven.

Strengths:

- RiskEngine still performs synchronous risk evaluation and internal capital reservation.
- Kill-switch state persists in SQLite.
- Dry-run mode now releases risk reservations after logging would-submit events.

Blocking gaps:

- Live mode does not call `obs_server.set_kill_switch_config(...)`, so HTTP kill-switch endpoints appear wired but reject all live requests.
- HTTP kill-switch activation calls `risk.manual_activate(...)` but does not currently call the orchestrator emergency cancellation path.
- RiskEngine and PortfolioManager disagree about reserved capital: RiskEngine owns reservations, while `PortfolioManager.reserved_capital` remains effectively unused.

Production gate:

- Manual kill-switch activation must cancel all live open orders and prevent new submissions.
- Kill-switch reset must require a valid token, clear stale state, and be audit logged without exposing secrets.
- Reservation, exposure, and available-capital metrics must match the risk engine's actual gating state.

### Market Data

Status: not ready.

Blocking gap:

- Both WebSocket adapters create `_process_messages(...)` as a task inside `async with websockets.connect(...)` and then immediately leave the context. This closes the socket instead of keeping the stream alive.

Production gate:

- Polymarket and Opinion WebSocket adapters keep a connection open, process messages continuously, reconnect after disconnects, and expose feed-age metrics.
- Paper mode can run for 24 hours with fresh market data for subscribed markets.

### Execution And Fill Accounting

Status: not ready for live capital.

Strengths:

- ExecutionEngine tracks order state, retries, expiry, polling, and startup reconciliation.
- PaperTradingClient gives a useful local execution harness.

Blocking gaps:

- Polymarket and Opinion `get_order_status()` return `new_fills=[]`; partial fills can be missed unless final-status fallback happens to synthesize the remaining fill.
- `portfolio/storage.py` stores fills with `proposal_id` as the primary key, which loses multiple partial fills for the same order.
- Venue clients still contain unverified assumptions around REST paths, auth headers, signing payloads, order IDs, amount scaling, and market identifiers.

Production gate:

- Every venue order lifecycle path is validated against sandbox or recorded fixtures: place, cancel, status, open orders, partial fills, final fills, and non-JSON errors.
- Local fill ledger reconciles to exchange-reported fills to cent-level tolerance.
- Restart reconciliation cannot leave orphaned reservations, missing fills, or duplicate fills.

### Observability And Operations

Status: partially ready.

Strengths:

- `/health`, `/ready`, `/metrics`, `/metrics/json`, `/kill-switch/activate`, and `/kill-switch/reset` routes exist.
- Readiness exchange checks are cached with a TTL.
- Docker Compose publishes app, Prometheus, and Grafana services.

Gaps:

- Live kill-switch HTTP config is not wired.
- Paper/live observability behavior has not been verified with a running service in this environment.
- Runbooks still need to match the final paper/live semantics after the P0 fixes.

Production gate:

- Prometheus target is healthy in Docker Compose.
- Health and readiness endpoints are reachable from host and containers.
- Operator runbooks are tested by someone other than the implementer.

### Security And Configuration

Status: not ready for production.

Strengths:

- Secret-file support exists for sensitive values.
- AI is disabled by default in `.env.example`.
- Docker runs a non-root user in the app image.

Gaps:

- `.env.example` still defaults `ENABLE_TRADING=true`, which is too aggressive for a project whose safe first mode is backtest/paper.
- Grafana still allows the default password fallback `${GF_SECURITY_ADMIN_PASSWORD:-admin}`.
- Kill-switch HTTP endpoints use a body token but no stronger auth boundary, network policy, or operator authorization.

Production gate:

- Production deployment requires non-default monitoring credentials.
- Live trading is opt-in by explicit config.
- Operator endpoints are authenticated, rate-limited, audited, and restricted to an operations network or equivalent control.

## Release Gates

### Gate 0: Developer Verification

Acceptance criteria:

- Fresh checkout on Python 3.11 or 3.12 can create a venv and install dependencies.
- `python -m compileall -q main.py config data engine execution portfolio risk src strategies ai backtest api infrastructure` passes.
- `python -m pytest tests -q` passes locally and in CI.
- `ruff check .` and `mypy . --ignore-missing-imports` pass or have an approved baseline.

Required metrics:

- Test pass rate: 100%.
- Coverage: at least 70% overall.
- Static analysis regressions: 0 new errors.

### Gate 1: Backtest Baseline

Acceptance criteria:

- `python main.py --mode backtest --ticks 200 --capital 10000` produces non-zero proposals and fills.
- `python main.py --mode backtest --ticks 2000 --capital 10000 --verbose` completes without credentials and emits non-zero trading metrics.
- Results are deterministic with a fixed seed across separate Python processes.
- Backtest fails loudly if no market data, proposals, or fills are produced when the scenario expects activity.

Required metrics:

- Backtest completion rate: 100% over 10 repeated runs.
- Unhandled exceptions: 0.
- Proposal count for release smoke: greater than 0.
- Fill count for release smoke: greater than 0.
- Fixed-seed metric drift across processes: 0.
- Runtime for 2,000 ticks: less than 30 seconds on a developer machine.

### Gate 2: Paper Trading

Acceptance criteria:

- Paper mode starts from local Python and Docker Compose without live exchange credentials.
- `ENABLE_TRADING=false` evaluates features, strategies, and risk while submitting zero exchange orders.
- WebSocket adapters maintain live connections and reconnect cleanly.
- `/health`, `/ready`, `/metrics`, `/metrics/json`, and kill-switch routes behave as documented.

Required metrics:

- 24-hour uptime with no unhandled exceptions.
- Event-loop liveness age below 10 seconds for 99.9% of samples.
- Market-data age below configured stale threshold for at least 99% of subscribed market samples during normal exchange operation.
- Proposal-to-risk latency p95 below 5 ms.
- Submitted real orders in paper/dry-run: 0.

### Gate 3: Sandbox Exchange Validation

Acceptance criteria:

- Sandbox or recorded-contract tests validate both venue clients.
- Partial fills produce exactly-once fill events.
- Startup reconciliation recovers from a forced restart with open orders.
- Kill-switch activation cancels all open orders.
- Market ID and outcome mappings are explicit and validated before trading.

Required metrics:

- Sandbox scenario pass rate: 100%.
- Reconciliation mismatches after restart: 0.
- Open orders remaining after kill-switch cancellation window: 0.
- Fill ledger mismatch versus exchange-reported fills: 0 USDC after rounding tolerance.

### Gate 4: Small-Capital Live Trial

Acceptance criteria:

- Wallet balances are capped to an explicitly approved amount.
- Live config uses conservative limits, for example `MAX_ORDER_USDC=25` and drawdown kill threshold no higher than 10%.
- Operator kill switch is tested before trading.
- Daily reconciliation matches venue statements.
- Alerts are monitored by an accountable operator.

Required metrics:

- Daily reconciliation mismatch: 0 unresolved mismatches.
- Unexpected overnight positions: 0.
- Critical alert delivery p95 below 60 seconds.
- Manual kill-switch time to no open orders p95 below 30 seconds, proven in sandbox first.

## Readiness Scorecard

| Area | Score | Rationale |
| --- | --- | --- |
| Runtime correctness | 2/5 | Compile and short backtest pass, but release backtest and partial tests fail. |
| Risk controls | 3/5 | Strong design, but live kill-switch wiring and metrics consistency need fixes. |
| Market data | 1/5 | WebSocket run loops likely close streams immediately. |
| Execution correctness | 2/5 | Lifecycle engine exists, but venue fill accounting and sandbox proof are missing. |
| Observability | 3/5 | Routes and metrics exist; live kill-switch config and full service verification are incomplete. |
| Operations | 3/5 | Runbooks exist, but must be revalidated after current blockers. |
| Security | 2/5 | Secret handling exists; live defaults and operator endpoint controls need hardening. |
| CI/CD | 3/5 | CI has lint, type, tests, backtest smoke, and Docker build; it needs failing-regression coverage for current blockers. |

Overall: 2/5. Not production-ready.

## Minimum Path To Production

1. Fix all P0 tasks in `docs/BUGS_AND_IMPROVEMENTS.md`.
2. Make local and CI test execution reproducible.
3. Restore deterministic non-zero backtest metrics.
4. Prove paper-mode data ingestion with durable WebSocket connections.
5. Wire live kill-switch endpoints to order cancellation.
6. Validate venue clients and fill accounting against sandbox or recorded fixtures.
7. Run a 24-hour paper soak.
8. Run a kill-switch and restart-reconciliation drill.
9. Only then consider a small-capital live trial.
