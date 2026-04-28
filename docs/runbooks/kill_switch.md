# Kill Switch Activation & Reset

Use this runbook when the system exhibits erratic behavior or during extreme market volatility.

## 1. Manual Activation
If the bot is running but you need to stop trading immediately:
- [ ] Trigger the emergency stop via the management endpoint (if available) or by sending a `POST` request with the `KILL_SWITCH_TOKEN`.
- [ ] Alternatively, manually update the SQLite state:
  ```bash
  sqlite3 pmts.db "UPDATE kill_switch SET active = 1, reason = 'Manual intervention' WHERE id = 'global';"
  ```

## 2. Verification
- [ ] Check logs for `EMERGENCY STOP` or `Kill switch active`.
- [ ] Verify `/ready` endpoint returns `503 Service Unavailable`.
- [ ] Confirm all open orders are being cancelled.

## 3. Investigation
- [ ] Resolve the root cause (exchange error, data feed lag, etc.).
- [ ] Check `pmts_drawdown_pct` in metrics to see if it was an auto-trip.

## 4. Reset Procedure
The bot will **NOT** resume automatically after a kill switch trip.
- [ ] Deactivate the switch in SQLite:
  ```bash
  sqlite3 pmts.db "UPDATE kill_switch SET active = 0, reason = NULL WHERE id = 'global';"
  ```
- [ ] Restart the bot process to reload the state and perform reconciliation.
