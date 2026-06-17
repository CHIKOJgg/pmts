# Reconciliation Runbook

Owner: Trading Engineer

Use this runbook when local state and exchange state diverge after a restart or outage.

1. Inspect the mismatch.
   - Compare SQLite `active_orders` against exchange open orders.
   - Check the startup reconciliation logs.
2. Do not delete state blindly.
   - Keep reservations and fills until the mismatch is understood.
3. Let the engine reconcile first.
   - Restart the process.
   - Allow `ExecutionEngine.reconcile()` to rebuild trackers.
   - Allow `RiskEngine.reconcile_reservations()` to reload committed capital.
4. Correct only the stale records.
   - Remove terminal orders from SQLite only after the exchange confirms they are done.
   - Update any broken submission JSON instead of replacing the whole database.
5. Verify closure.
   - Reconciliation should end with no unexpected open orders.
   - The next startup should not resurrect deleted orders.
