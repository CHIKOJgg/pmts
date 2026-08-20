# PMTS — Production Readiness Assessment

**Directory**: `C:\Users\Honor\Desktop\polymarket-under-openclaw\polymarket-arbitrage`
**Last reviewed**: 2026-07-11
**Verdict**: **RELEASE CANDIDATE (conditional)** — strong engineering foundation, but
the trading edge is **not yet validated on real market data**, and a few hardening
items remain. Not "ready to risk real capital unattended" yet.

---

## What is solid (verified)

- **Architecture** — clean event-loop pipeline: Data → Features → Strategy → Risk →
  Execution → Portfolio. Protocols, dependency injection, no global singletons.
- **RiskEngine** (`risk/engine.py`) — synchronous capital reservation (no TOCTOU),
  12 pre-trade checks, projected delta, soft-kill grace, session-loss limit, dedup.
- **Kill switch** — persisted to SQLite, restored on restart, token-complexity enforced.
- **Arbitrage** (`strategies/arbitrage.py`) — realistic fee + spread + sqrt-impact cost
  model, OFI adverse-selection penalty, both directions enumerated, dynamic min-edge.
- **ExecutionEngine** — priority queue, retry/backoff, adaptive polling, expiry enforcement,
  startup reconciliation against the exchange + SQLite.
- **Order nonce** — monotonic, collision-free counter (fixed: was `now_ms()*1000`, which
  could collide within one millisecond at high throughput).
- **Clients** — Polymarket (HMAC + EIP-712) and Opinion implemented and wired.
- **Tests** — passing (run `pytest` to see the current test count; the suite must pass before live trading). Backtest runs end-to-end. Paper/sandbox modes exist.
- **Config** — env + file-based secrets, strict `validate()` with a real go/no-go gate.

## What blocks an unconditional "production ready"

1. **Edge not proven on real data (highest risk).** The backtest and offline-paper modes
   use `build_synthetic_tick_stream`, which (now) models the same event at the same mid on
   both venues — so P&L reflects transient-noise arbitrage only. There is **no replay of
   real Polymarket/Opinion order books or sandbox captures**. Whether the strategy is
   profitable in production is unknown. *Action: capture live WS streams or run against
   the Polymarket sandbox and replay; only then trust P&L.*
2. **No balance reconciliation vs on-chain/exchange.** MTM is built only from locally
   recorded fills. An undetected lost fill / partial fill during a crash silently drifts
   P&L. *Partially addressed:* `infrastructure/reconciliation.py` now compares locally
   open orders against the exchange open-book and alerts on drift (enable
   `RECONCILE_INTERVAL_S`). On-chain USDC balance checks are still a TODO.*
3. **CI not green in this environment.** 4 tests errored due to a locked Windows
   `.pytest-tmp` dir (now fixed in `conftest.py` via a unique per-session temp root) and
   1 flaky timing test (now made adaptive). A Linux CI workflow (`.github/workflows/ci.yml`)
   was added but is not yet confirmed running on the remote.
4. **Docs were misleading.** `README.md` §11 claimed the exchange clients were empty
   stubs (false), and an earlier readiness summary asserted "READY FOR PRODUCTION" with
   inaccuracies (e.g. "Opinion EIP-712 signing" — Opinion uses a different scheme). Both
   corrected.
5. **Leaked artifacts in git.** `portfolio_paper.db*` and `__pycache__/*` were tracked
   despite `.gitignore` rules (committed before the rules). They are now untracked.

## Remaining hardening checklist (pre-live)

- [ ] Replay real/sandbox market data through the backtest; confirm positive Sharpe with
      realistic costs. Re-tune `MIN_NET_EDGE`, budgets, fees from that.
- [ ] Sandbox e2e test: place + cancel a micro order on Polymarket sandbox and Opinion.
- [ ] Add on-chain/exchange USDC balance reconciliation (compare to `portfolio.cash_usdc`).
- [ ] Confirm the GitHub Actions CI is green on the remote for py3.11/3.12.
- [ ] Verify the kill-switch reset HTTP endpoint is never exposed beyond localhost
      (`OBSERVABILITY_BIND_HOST=127.0.0.1` by default).
- [ ] Chaos test: kill switch mid-arb, partial fills, WS drop during leg-2, nonce reuse.
- [ ] Alarm-channel smoke test (Slack/email/webhook) before enabling `ENABLE_TRADING=true`.
- [ ] Start live with `INITIAL_CASH_USDC` = real balance and a small capital allocation.

## Recommended path

paper-offline → paper (synthetic) → sandbox e2e → real-data backtest → live with tiny
capital + `RECONCILE_INTERVAL_S>0` + alerts on. Do not enable `ENABLE_TRADING=true` until
item 1 is closed.
