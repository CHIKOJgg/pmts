# Go-Live Plan

Owner: Release Manager

Use this plan to advance from paper trading to meaningful live capital only after the sandbox validation checklist is complete.

## Phase 11: Paper Trading
- `ENABLE_TRADING=false`
- System runs end to end, evaluates proposals, and logs every `would have submitted` event.
- Acceptance target: at least 60 percent of arb proposals would have been profitable in hindsight.

## Phase 12-13: Small Capital
- `INITIAL_CASH_USDC=500`
- `MAX_ORDER_USDC=25`
- Drawdown kill switch at 10 percent.
- Reconcile daily against exchange statements.
- Acceptance target: positive after-fee PnL and zero unhedged arb positions.

## Phase 14+: Scale Up
- Increase capital in 2x increments only after the small-capital phase is clean.
- Wait one week between increases.

## No-Go Checklist
- Manual kill switch test passed.
- All runbooks executed once.
- Reconciliation matches exchange to the cent.
- No unexpected overnight positions.
- Grafana alerts fire correctly in test.
