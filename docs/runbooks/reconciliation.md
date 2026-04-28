# Reconciliation Mismatch Handling

Procedures for when local state deviates from exchange reality.

## 1. Symptoms
- [ ] Log message: `Reconciliation mismatch for order <ID>`
- [ ] `RiskEngine` reports insufficient capital despite no active orders.
- [ ] SQLite `active_orders` table contains entries that don't exist on the exchange.

## 2. Immediate Action
- [ ] Stop the bot process.
- [ ] Do **NOT** delete the SQLite database immediately.

## 3. Auditing
- [ ] Compare `active_orders` in SQLite with the Exchange's "Open Orders" UI.
  ```bash
  sqlite3 pmts.db "SELECT * FROM active_orders;"
  ```
- [ ] Check for "Zombie" orders: Orders the bot thinks are open but are actually filled or cancelled.

## 4. Manual Correction
- [ ] If an order was filled but not recorded:
    1. Manually insert the fill into `fills` table if necessary.
    2. Remove from `active_orders`.
- [ ] If an order was cancelled but still in `active_orders`:
  ```bash
  sqlite3 pmts.db "DELETE FROM active_orders WHERE proposal_id = 'MISSING-ID';"
  ```

## 5. Resume
- [ ] Restart the bot.
- [ ] Verify the startup reconciliation log shows `Successfully recovered 0 orders` (or the correct expected count).
