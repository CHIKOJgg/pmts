# Kill Switch Runbook

Owner: Risk Officer

Use this runbook whenever drawdown, stale data, or manual intervention requires an immediate trading stop.

1. Activate the kill switch.
   - Use the operator endpoint or the management path wired to `RiskEngine.manual_activate()`.
2. Verify the stop.
   - `/ready` should return `503`.
   - Logs should show `KILL SWITCH ACTIVATED` and open-order cancellation activity.
3. Investigate the trigger.
   - Check `pmts_drawdown_pct`.
   - Check feed freshness and exchange error rates.
4. Reset only after the cause is fixed.
   - Call `RiskEngine.reset_kill_switch(token, operator_id=...)`.
   - The reset clears reservations and portfolio state and flushes arb in-flight bookkeeping.
5. Re-verify trading recovery.
   - Confirm new proposals are flowing again for the affected markets.
   - Confirm the system did not retain stale in-flight state.
