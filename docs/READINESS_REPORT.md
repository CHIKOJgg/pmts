# PMTS Readiness Report

**Date:** 2026-06-17
**Scope:** Full codebase audit across 4 rounds

---

## 1. Bugs Fixed (35+ across 4 rounds)

### Round 1 — 14 bugs in 13 files
`strategies/arbitrage.py`, `delta_neutral.py`, `correlation.py`, `portfolio/manager.py`, `analytics.py`, `journal.py`, `execution/clients/polymarket.py`, `opinion.py`, `engine/feature_engine.py`, `execution/models.py`, `compliance/trade_report.py`, `ai/heuristic.py`, `data/models.py`

### Round 2 — 7 trading-breaking bugs
`engine/orchestrator.py`, `engine/strategy_engine.py`, `execution/engine.py`, `execution/order_tracker.py`, `risk/engine.py`, `main.py`

### Round 3 — 9 critical/high bugs
`execution/engine.py` (submit worker death, stop deadlock, expiry finalise, dust fill), `execution/order_tracker.py` (over-fill assert), `engine/orchestrator.py` (budget release, scaled leg 2 leak), `backtest/engine.py` (partial fill capital release, notify_arb_cleared)

### Round 4 — 5 system-breaking bugs
`main.py` (missing PortfolioAnalytics import, store.close not awaited, signal handler), `engine/feature_engine.py` (stdev NaN crash, unprotected arb computation), `risk/engine.py` (load_reservations None guard)

---

## 2. Remaining Known Bugs (Not Fixed)

### CRITICAL (8 bugs that crash the system)

| # | File | Bug | Impact |
|---|------|-----|--------|
| C1 | `execution/clients/polymarket.py:316` | `get_open_orders` bare dict key access | KeyError crash on malformed API response; not retryable. Exchange order book goes silent |
| C2 | `execution/clients/opinion.py:289` | Same as C1 for Opinion | Same impact |
| C3 | `data/adapters/polymarket_ws.py:86` | Subscription failure never detected | Adapter stays connected but processes zero data forever. No alert |
| C4 | `data/adapters/opinion_ws.py:85` | Same as C3 for Opinion | Same impact |
| C5 | `engine/strategy_runner.py:110` | Message loop has no try/except | Single bad message crashes the entire strategy subprocess permanently |
| C6 | `data/models.py:162` | `FeatureVector.model_copy()` corrupts `venues` | `asdict` converts VenueSnapshot to dict; downstream `.mid` crashes |
| C7 | `data/models.py:152` | `arb_signal=inf` passes `isnan` check | Infinite edge treated as valid trade; `inf > 0.0` is True |
| C8 | `data/models.py:152` | `stale_markets=None` bypasses validation | `model_dump()` crashes with `TypeError: 'NoneType' object is not iterable` |

### HIGH (12+ bugs that corrupt trading or lose data)

| # | File | Bug | Impact |
|---|------|-----|--------|
| H1 | `execution/clients/polymarket.py:270` | SELL order fills interpreted as USDC | Tokens counted as USDC; fill_ratio inflated 2x; order prematurely marked FILLED |
| H2 | `execution/clients/opinion.py:251` | Same as H1 for Opinion | Same impact |
| H3 | `execution/clients/polymarket.py:273` | Fill dropped when API returns price=0 | Fill permanently lost; `_last_status_filled_usdc` checkpointed past it |
| H4 | `execution/clients/opinion.py:253` | Same as H3 for Opinion | Same impact |
| H5 | `data/adapters/polymarket_ws.py:108` | Exception handler prevents reconnect backoff | Tight reconnect loop on transient failures; IP ban risk |
| H6 | `data/adapters/opinion_ws.py:108` | Same as H5 for Opinion | Same impact |
| H7 | `data/adapters/polymarket_ws.py:133` | One-sided order book dropped | Markets with only bids or asks produce no snapshots |
| H8 | `data/adapters/opinion_ws.py:135` | Zero-price markets silently dropped | All snapshots lost while price is 0.0 |
| H9 | `risk/engine.py:357` | `evaluate()` never calls `portfolio.reserve_capital()` | PortfolioManager reservation book always shows 0; `available_capital` is dangerously wrong |
| H10 | `risk/engine.py:400` | `_total_reserved` mutated without lock | Lost-update race in concurrent `notify_terminal()` + `evaluate()` |
| H11 | `risk/engine.py:417` | `reconcile_reservations()` can double-count | Progressive capital freeze; every iteration locks more capital |
| H12 | `risk/engine.py:279` | Session PnL excludes redemption PnL | Session loss-limit safety net can be silently bypassed |
| H13 | `risk/engine.py:461` | `reset_kill_switch()` clears `_total_reserved` to 0 | Next orders can over-commit beyond available cash |
| H14 | `portfolio/manager.py:527` | `build_snapshot()` reads state with zero locks | Corrupt position snapshots (tear between cash update and position update) |
| H15 | `portfolio/manager.py:448` | `PORTFOLIO_REALISED_PNL_USDC` misses sell PnL | Metric under-reported when open positions have partial-sell activity |
| H16 | `infrastructure/observability.py:130` | `engine._client.platform` crashes if client is None | Health check endpoint crashes; readiness probe fails |
| H17 | `infrastructure/observability.py:312` | `risk.manual_activate()` crashes if risk is None | Health check crashes |
| H18 | `api/server.py:184` | Accesses private `_on_kill_switch_reset` | AttributeError if method name changes |
| H19 | `ai/heuristic.py:82` | `math.isnan(arb_signal)` crashes on non-float | Falls back to neutral context; AI enhancement silently disabled |

---

## 3. System Readiness Assessment

### Pipeline Integrity
| Stage | Status | Notes |
|-------|--------|-------|
| Data Ingestion | ⚠️ RISK | WS adapters CRITICAL bugs C3-C4: subscription failure goes undetected. H5-H6: tight reconnect loop on transient failures |
| Feature Computation | ✅ FIXED | stdev NaN crash fixed. try/except extended. Fees use .get() |
| Strategy Evaluation | ⚠️ RISK | C6-C8: FeatureVector construction has latent crashes. H19: AI enhancer fallback crash |
| Risk Gate | ❌ NOT READY | H9-H13: capital accounting fundamentally broken. reserve_capital() never called |
| Order Execution | ⚠️ RISK | C1-C2: get_open_orders crashes on malformed data. H1-H4: SELL order fill units wrong |
| Portfolio Tracking | ⚠️ RISK | H14-H15: lockless reads and wrong metric. Must fix before trusting PnL |
| Graceful Shutdown | ✅ FIXED | Signal handling, store.close(), worker tasks all addressed |

### Risk Level: HIGH — NOT PRODUCTION READY

The system can start and process data without immediate crashes, but **5 critical subsystems have known bugs** that will corrupt financial data during normal operation:

1. **Risk Engine capital accounting** (H9-H13) — capital reservation is broken. Reserve/release pairing is incomplete. The `PortfolioManager` believes all cash is free. Kill switch reset silently leaks all reservations.

2. **Exchange client fill handling** (H1-H4) — every SELL order has wrong fill accounting. This affects roughly 50% of orders (sell side of arb pairs). Fill ratios inflated, premature FILLED status, wrong PnL.

3. **WebSocket adapters** (C3-C4, H5-H8) — silent data loss if subscription fails; no backoff on disconnect; one-sided books and zero prices silently dropped.

4. **`get_open_orders` crash** (C1-C2) — first malformed exchange response crashes reconciliation. Lost orders never recovered.

5. **PortfolioManager lockless reads** (H14) — position snapshots can be torn. Any monitoring/dashboard consumption of snapshot data is unreliable.

### Recommended Fix Order
1. **H9-H13** (Risk Engine capital) — affects every trade's capital check
2. **H1-H4** (SELL order fill units) — corrupts every sell-side fill
3. **C1-C2** (get_open_orders KeyError) — first malformed response crashes system
4. **C3-C4** (WS subscription failure) — silent data loss
5. **H14** (build_snapshot locks) — monitoring integrity
6. Remaining issues

---

## 4. Test Coverage Gaps
- No tests exist for exchange client response parsing (malformed JSON, missing keys, zero prices)
- No tests for WS adapter reconnection behavior
- No integration tests for RiskEngine + PortfolioManager capital reservation pairing
- No tests for SELL order fill accounting
- Tests cannot run (py.test not installed in environment)

---

## 5. Conclusion

| Metric | Status |
|--------|--------|
| System starts without crash | ✅ |
| Feature pipeline runs | ✅ |
| Strategy generates signals | ✅ |
| Capital reservation correct | ❌ NOT READY |
| Fill accounting correct | ❌ NOT READY |
| Market data always flows | ❌ RISK |
| Graceful shutdown works | ✅ |
| Backtest produces trades | ✅ |
| Production Ready | ❌ NO |

**Estimated effort to production-ready:** 3-5 days for the 5 highest-priority fixes (H9-H13, H1-H4, C1-C2), plus 2-3 days for sandbox validation and CI setup.
