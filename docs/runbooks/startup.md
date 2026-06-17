# Startup Runbook

Owner: Ops Primary

Use this runbook to bring the system online in live or paper mode.

1. Verify configuration.
   - Confirm `MARKETS` is set.
   - Confirm `KILL_SWITCH_TOKEN` is set.
   - Confirm trading keys are present or the corresponding `*_FILE` secrets are mounted.
2. Check infrastructure.
   - Confirm SQLite is healthy.
   - Confirm ports `8080`, `9090`, and `3000` are free if you are starting the monitoring stack locally.
3. Start the stack.
   - `docker compose up -d pmts prometheus grafana`
   - Or run `python main.py --mode live`
4. Verify startup.
   - Confirm `/ready` returns `200`.
   - Confirm `/metrics` exposes fresh `pmts_feed_last_ts_seconds` values.
   - Confirm logs show reconciliation completed for both exchanges.
5. Paper trading check.
   - If `ENABLE_TRADING=false`, confirm proposals are evaluated but no orders are submitted.
