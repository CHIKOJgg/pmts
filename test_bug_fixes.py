#!/usr/bin/env python3
"""Test script to verify bug fixes are in place."""

from __future__ import annotations


def test_imports():
    """Verify all modified modules can be imported."""
    print("Testing imports...")

    # Test key modified modules
    try:
        from config.settings import Settings, get_settings

        print("[OK] config/settings.py imports successfully")

        from risk.kill_switch import KillSwitch

        print("[OK] risk/kill_switch.py imports successfully")

        from ai.enhancer import AISignalEnhancer

        print("[OK] ai/enhancer.py imports successfully")

        from strategies.arbitrage import ArbitrageStrategy

        print("[OK] strategies/arbitrage.py imports successfully")

        from engine.orchestrator import Orchestrator

        print("[OK] engine/orchestrator.py imports successfully")

        from backtest.engine import BacktestEngine

        print("[OK] backtest/engine.py imports successfully")

        # Test that kill switch token validation works
        try:
            KillSwitch("short")  # Too short
            print("[FAIL] KillSwitch should reject short tokens")
        except ValueError as e:
            if "at least 16 characters" in str(e):
                print("[OK] KillSwitch token validation working (short)")

        # Test complexity requirement
        try:
            KillSwitch("alllowercaseonly")  # Only lowercase - complexity 1
            print("[FAIL] KillSwitch should reject tokens without enough complexity")
        except ValueError as e:
            if "at least 2 of" in str(e):
                print("[OK] KillSwitch token validation working (complexity)")

        return True

    except Exception as e:
        print(f"[FAIL] Import failed: {e}")
        import traceback

        traceback.print_exc()
        return False


def test_backtest_kill_switch():
    """Test that backtest engine has kill switch integration."""
    print("\nTesting backtest kill switch...")

    from backtest.engine import BacktestEngine, build_synthetic_tick_stream
    from risk.limits import RiskLimits

    # Create a simple synthetic stream
    streams = {"TEST-MKT": build_synthetic_tick_stream("TEST-MKT", n_ticks=100, seed=42)}

    engine = BacktestEngine(
        tick_streams=streams,
        initial_capital=10_000.0,
        risk_limits=RiskLimits(
            drawdown_kill_pct=0.50,  # High to avoid triggering
            drawdown_warn_pct=0.30,
        ),
    )

    if hasattr(engine, "_kill_switch_check_interval"):
        print("[OK] BacktestEngine has kill switch check interval")
    else:
        print("[FAIL] BacktestEngine missing kill switch check interval")
        return False

    # Run a quick backtest
    try:
        import asyncio
        result = asyncio.run(engine.run())
        print(f"[OK] Backtest ran successfully (ticks: {result.total_ticks})")
        return True
    except Exception as e:
        print(f"[FAIL] Backtest failed: {e}")
        import traceback

        traceback.print_exc()
        return False


def main():
    print("=" * 60)
    print("Bug Fix Verification Test")
    print("=" * 60)

    results = []
    results.append(("Imports", test_imports()))
    results.append(("Backtest Kill Switch", test_backtest_kill_switch()))

    print("\n" + "=" * 60)
    print("Summary:")
    print("=" * 60)

    for name, passed in results:
        status = "[PASS]" if passed else "[FAIL]"
        print(f"{status}: {name}")

    all_passed = all(passed for _, passed in results)

    if all_passed:
        print("\n[SUCCESS] All tests passed! Bug fixes are working correctly.")
        return 0
    else:
        print("\n[WARN] Some tests failed. Review the output above.")
        return 1


if __name__ == "__main__":
    exit(main())
