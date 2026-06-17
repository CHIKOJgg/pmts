# PMTS Deployment Plan

Owner: Release Manager

Use this plan to move from sandbox validation to paper trading, then to small-capital live trading, and only then to scale-up. Do not advance phases until the prior phase is clean.

## 1. Preflight and Operator Setup
1. Confirm configuration is present.
   - `MARKETS`
   - exchange API keys
   - `KILL_SWITCH_TOKEN`
   - any mounted `*_FILE` secret paths
2. Confirm the runbook owners are assigned.
   - Ops Primary
   - Risk Officer
   - Ops On-Call
   - Trading Engineer
   - Lead Developer
   - Release Manager
3. Start the monitoring stack.
   - `docker compose up -d pmts prometheus grafana`
4. Verify basic health.
   - `GET /ready`
   - `GET /metrics`
   - Prometheus scrape target is healthy
   - Grafana dashboard loads

## 2. Sandbox Validation Gate
1. Run the sandbox-only validation suite before any live capital.
2. Verify these scenarios:
   - normal arb: leg-1 fills, leg-2 submits and fills
   - partial leg-1 below `min_fill_ratio`: leg-2 never submits
   - kill switch trigger: drawdown forces cancellation
   - kill switch reset: proposals flow again
   - WebSocket disconnect: reconnect and stale suppression
   - process restart mid-trade: recovery on next startup
   - market resolution: resolved market auto-removes and redeems
3. Acceptance criteria.
   - all validation tests pass
   - no scenario leaves stale in-flight state
   - no scenario leaves uncancelled open orders

## 3. Paper Trading Phase
1. Set `ENABLE_TRADING=false`.
2. Run the full system end to end.
3. Confirm the bot evaluates proposals and logs every `would have submitted` event.
4. Measure signal quality in hindsight.
   - target: at least 60% of arb proposals would have been profitable
5. Acceptance criteria.
   - no live order submissions
   - stable feeds
   - clean reconciliation
   - monitoring alerts fire in test

## 4. Small-Capital Live Phase
1. Set the initial live risk profile.
   - `INITIAL_CASH_USDC=500`
   - `MAX_ORDER_USDC=25`
   - drawdown kill threshold: 10%
2. Enable trading for the smallest live market set first.
3. Reconcile daily against exchange statements.
4. Acceptance criteria.
   - positive after-fee P&L
   - zero unhedged arb positions after any arb
   - no unexpected overnight positions
   - no unresolved reconciliation mismatches

## 5. Operational Hardening During Small Capital
1. Execute every runbook exactly as written.
   - startup
   - shutdown
   - kill switch
   - exchange outage
   - reconciliation
   - escalation
2. Verify the kill switch manually at least once.
3. Confirm Grafana alerts fire in test and Prometheus scrape data stays current.
4. Acceptance criteria.
   - runbooks are executable without improvisation
   - all operator actions are auditable

## 6. Scale-Up Phase
1. Increase capital in 2x increments only after the prior phase is clean.
2. Wait one full week between each increase.
3. Re-run the kill switch test, reconciliation, and overnight position checks before each increase.
4. Acceptance criteria.
   - stable P&L
   - no residual risk incidents
   - clean exchange-to-local reconciliation

## 7. Rollback and Incident Response
1. If any phase fails, stop trading with the kill switch first.
2. Cancel open orders after the kill switch trips.
3. Investigate the failure before advancing again.
4. Preserve evidence.
   - logs
   - dashboard screenshots
   - SQLite state
5. Do not increase capital until the failure mode is understood and the runbooks pass again.

## Test Plan
1. Sandbox suite.
   - arb handoff
   - partial-fill abort
   - kill switch trip/reset
   - WS reconnect
   - restart recovery
   - market resolution/removal
2. Paper trading.
   - zero live order submissions
   - `would have submitted` logging
   - signal-quality review
3. Small capital.
   - daily reconciliation
   - drawdown threshold validation
   - no unhedged arb positions
4. Scale-up gate.
   - manual kill switch test
   - all runbooks executed
   - Grafana alerts validated
   - exchange reconciliation matches to the cent

## Assumptions
1. `ENABLE_TRADING=false` means full evaluation without order submission.
2. The Prometheus/Grafana stack and dashboard in this repo are the monitoring baseline.
3. `RiskEngine.reset_kill_switch()` and the orchestrator reset hook are the canonical recovery path.
4. Market resolution is handled through `MarketMonitor` and the exchange client resolution/redemption calls.
5. Capital is only increased after the previous phase is clean.
