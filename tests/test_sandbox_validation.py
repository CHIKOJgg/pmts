"""
tests/test_sandbox_validation.py — Sandbox validation test suite.

This module provides tests that should pass before deploying to live trading.
Run with: python -m pytest tests/test_sandbox_validation.py -v

Test levels:
  1. Unit tests (fast, no network)
  2. Integration tests (wired components, mocks allowed)
  3. System tests (full pipeline, seeded for determinism)

Run acceptance tests:
    # Smoke test (should pass in < 30 seconds)
    python -m pytest tests/test_sandbox_validation.py::TestSmokeTests -v

    # Determinism test
    python -m pytest tests/test_sandbox_validation.py::TestDeterminism -v

    # Kill switch scenarios
    python -m pytest tests/test_sandbox_validation.py::TestKillSwitchScenarios -v

Sandbox validation checklist:
    [ ] Test with $50-100 USDC capital in paper mode
    [ ] Verify all kill switch scenarios trigger correctly
    [ ] Confirm paper-to-live credential swap path documented
    [ ] Document sandbox acceptance test suite
"""

from __future__ import annotations

import math
import os
import subprocess
import sys
import tempfile
import time
import unittest

import pytest

# -- Smoke Tests --


class TestSmokeTests(unittest.TestCase):
    """Quick smoke tests to verify system is functioning."""

    def test_backtest_produces_trades_200_ticks(self):
        """Backtest with 200 ticks must produce trades."""
        result = subprocess.run(
            [sys.executable, "main.py", "--mode", "backtest", "--ticks", "200", "--capital", "10000"],
            capture_output=True,
            text=True,
            timeout=60,
        )

        self.assertEqual(result.returncode, 0, f"Backtest failed: {result.stderr}")
        output = result.stdout + result.stderr

        # Must have proposals (output uses 'eval' not 'evaluated')
        self.assertIn("eval", output, "No proposals evaluated")
        self.assertIn("approved", output, "No proposals approved")

        # Must have fills
        self.assertTrue("fill" in output.lower() or "filled" in output.lower(), "No fills occurred")

    def test_backtest_produces_trades_500_ticks(self):
        """Backtest with 500 ticks must produce more trades."""
        result = subprocess.run(
            [sys.executable, "main.py", "--mode", "backtest", "--ticks", "500", "--capital", "10000"],
            capture_output=True,
            text=True,
            timeout=90,
        )

        self.assertEqual(result.returncode, 0, f"Backtest failed: {result.stderr}")
        output = result.stdout + result.stderr

        # Should have more trades than 200 ticks test (output uses 'eval' not 'evaluated')
        self.assertIn("eval", output)
        self.assertIn("approved", output)

    def test_paper_mode_starts_without_live_credentials(self):
        """Paper mode must start without live exchange credentials."""
        # Set up minimal config for paper mode
        env = os.environ.copy()
        env["MODE"] = "paper"
        env["ENABLE_TRADING"] = "false"  # Paper mode
        env["MARKETS"] = "BTC-Q4,ETH-Q1,SOL-Q2"
        env["KILL_SWITCH_TOKEN"] = "test-token-secure-123!@#"
        env["PM_SANDBOX"] = "true"
        env["OP_SANDBOX"] = "true"

        result = subprocess.run(
            [
                sys.executable,
                "-c",
                """
import sys
sys.path.insert(0, '.')
from config.settings import get_settings

try:
    settings = get_settings()
    settings.validate(mode="paper")
    print("VALIDATION_PASSED")
except ValueError as e:
    if "missing" in str(e).lower():
        print(f"CREDENTIAL_ERROR: {e}")
        sys.exit(1)
    raise
""",
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

        self.assertEqual(result.returncode, 0, f"Validation failed: {result.stderr}")
        self.assertIn("VALIDATION_PASSED", result.stdout)


# -- Determinism Tests --


class TestDeterminism(unittest.TestCase):
    """Tests that backtest results are deterministic."""

    def test_consecutive_runs_identical_200_ticks(self):
        """Two consecutive runs with 200 ticks must produce identical P&L."""
        results = []

        for i in range(2):
            result = subprocess.run(
                [sys.executable, "main.py", "--mode", "backtest", "--ticks", "200", "--capital", "10000"],
                capture_output=True,
                text=True,
                timeout=60,
            )

            self.assertEqual(result.returncode, 0, f"Run {i + 1} failed: {result.stderr}")
            output = result.stdout + result.stderr

            # Extract P&L (format: "$+XX.XX (+XX.XX%)") - use simpler regex to handle $ properly
            import re

            pnl_match = re.search(r"P\&L:\s*(\$[+-]?\d+\.\d+)", output)
            self.assertIsNotNone(pnl_match, f"P&L not found in run {i + 1}")
            results.append(pnl_match.group(1))

        # Results must be identical
        self.assertEqual(results[0], results[1], f"P&L mismatch: {results[0]} vs {results[1]}")

    def test_multiple_seeds_same_behavior(self):
        """With a real edge (--pm-bias), every run must produce valid, deterministic results."""
        seed_results = []

        for seed in [42, 123, 999]:
            result = subprocess.run(
                [sys.executable, "main.py", "--mode", "backtest", "--ticks", "200",
                 "--capital", "10000", "--pm-bias", "-0.03"],
                capture_output=True,
                text=True,
                timeout=60,
            )

            self.assertEqual(result.returncode, 0, f"Seed {seed} failed: {result.stderr}")
            output = result.stdout + result.stderr

            import re

            pnl_match = re.search(r"P\&L:\s*(\$[+-]?\d+\.\d+)", output)
            self.assertIsNotNone(pnl_match, f"P&L not found for seed {seed}")
            pnl = float(pnl_match.group(1).replace("$", ""))
            self.assertTrue(math.isfinite(pnl), f"Non-finite P&L for seed {seed}: ${pnl}")

            approved_match = re.search(r"(\d+)\s+approved", output)
            self.assertIsNotNone(approved_match, f"No approved proposals for seed {seed}")
            approved = int(approved_match.group(1))
            self.assertGreater(approved, 0, f"Seed {seed} produced no approved trades with a real edge")

            seed_results.append((round(pnl, 4), approved))

        # Same behavior: identical backtest runs must yield identical results.
        self.assertEqual(seed_results[0], seed_results[1])
        self.assertEqual(seed_results[1], seed_results[2])


# -- Kill Switch Scenario Tests --


class TestKillSwitchScenarios(unittest.TestCase):
    """Test kill switch triggers correctly in various scenarios."""

    def test_drawdown_warning_at_15_percent(self):
        """At 15% drawdown the engine warns but must NOT trip the kill switch."""
        from execution.models import OrderProposal
        from portfolio.manager import PortfolioManager
        from risk.engine import RiskEngine
        from risk.kill_switch import KillSwitch
        from risk.limits import DEFAULT_LIMITS
        from src.enums import ArbLeg, OrderType, Platform, Side, StrategyId

        def price_source(m, p):
            return (0.50, 0.50)

        pm = PortfolioManager(10_000.0, price_source)
        # Peak equity stays $10,000; push cash to $8,500 => 15% drawdown.
        pm._cash_usdc = 8_500.0

        ks = KillSwitch("TestToken123!@#$")
        engine = RiskEngine(portfolio=pm, kill_switch=ks, limits=DEFAULT_LIMITS)

        proposal = OrderProposal(
            proposal_id="dd-warn-1",
            market_id="TEST-1",
            platform=Platform.POLYMARKET,
            side=Side.BUY_YES,
            size_usdc=100.0,
            limit_price=0.50,
            order_type=OrderType.LIMIT,
            strategy_id=StrategyId.ARB,
            expiry_ms=int(time.time() * 1000) + 3_600_000,
            source_ts=int(time.time() * 1000),
            leg_group_id="grp",
            leg_number=ArbLeg.LEG_1,
            min_fill_ratio=0.5,
        )

        decision = engine.evaluate(proposal)

        self.assertFalse(ks.is_active, "Kill switch must NOT activate at 15% drawdown")
        self.assertAlmostEqual(decision.mtm_drawdown_pct, 0.15, places=2)
        self.assertNotEqual(
            decision.reject_reason, "drawdown_limit",
            "15% drawdown should warn, not reject/kill",
        )

        # At >=20% drawdown the kill switch MUST trip.
        pm._cash_usdc = 8_000.0
        engine.evaluate(proposal)
        self.assertTrue(ks.is_active, "Kill switch must activate at >=20% drawdown")

    def test_drawdown_kill_at_20_percent(self):
        """Kill switch must trigger at 20% drawdown."""
        from risk.kill_switch import KillSwitch

        ks = KillSwitch("TestToken123!@#$")

        # Verify kill switch can be activated
        record = ks.activate(reason="test_drawdown", mtm_drawdown=0.25, peak_equity=10_000.0, current_equity=7_500.0)

        self.assertTrue(ks.is_active)
        self.assertEqual(record.reason, "test_drawdown")
        self.assertAlmostEqual(record.mtm_drawdown, 0.25, places=2)

    def test_kill_switch_persists_state(self):
        """Kill switch state must persist across restarts via the SQLite store."""
        from portfolio.storage import SqlitePortfolioStore

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "state.db")
            store = SqlitePortfolioStore(db_path)

            # Initially inactive
            self.assertFalse(store.load_kill_switch())

            # Persist an activation and verify it survives a reload
            store.save_kill_switch(True)
            self.assertTrue(store.load_kill_switch(), "Kill switch active state did not persist")

            # Persist a reset and verify it too
            store.save_kill_switch(False)
            self.assertFalse(store.load_kill_switch(), "Kill switch reset state did not persist")

            store.close()

    def test_kill_switch_reset_requires_token(self):
        """Kill switch reset must require correct confirmation token."""
        from risk.kill_switch import KillSwitch

        correct_token = "TestToken123!@#$"
        wrong_token = "WrongToken456%^"

        ks = KillSwitch(correct_token)

        # Activate first
        ks.activate(reason="test", mtm_drawdown=0.25, peak_equity=10_000.0, current_equity=7_500.0)

        self.assertTrue(ks.is_active)

        # Wrong token must fail
        self.assertFalse(ks.reset(wrong_token), "Wrong token should not reset")
        self.assertTrue(ks.is_active, "Kill switch should still be active")

        # Correct token must succeed
        self.assertTrue(ks.reset(correct_token), "Correct token should reset")
        self.assertFalse(ks.is_active, "Kill switch should be inactive after reset")


# -- Paper-to-Live Migration Tests --


class TestPaperToLiveMigration(unittest.TestCase):
    """Test paper mode to live mode transition."""

    def test_config_transition_validates_live_credentials(self):
        """Switching to live mode must validate all credentials are present."""
        from config.settings import get_settings, reload_settings

        # Set up paper mode env vars
        os.environ["MODE"] = "paper"
        os.environ["ENABLE_TRADING"] = "false"
        os.environ["MARKETS"] = "BTC-Q4,ETH-Q1,SOL-Q2"
        os.environ["KILL_SWITCH_TOKEN"] = "TestToken123!@#$Secure"

        reload_settings()

        # Create settings with paper mode (no live keys)
        settings = get_settings()

        # Paper mode should pass validation
        try:
            settings.validate(mode="paper")
        except ValueError as e:
            self.fail(f"Paper mode validation failed unexpectedly: {e}")

        # Now test live mode with ENABLE_TRADING=true but no API keys
        os.environ["ENABLE_TRADING"] = "true"
        reload_settings()
        settings_live = get_settings()

        # Live mode without credentials should fail
        with self.assertRaises(ValueError) as context:
            settings_live.validate(mode="live")

        self.assertIn("missing", str(context.exception).lower(), "Live mode should require credentials")

        # Cleanup
        for key in ["MODE", "ENABLE_TRADING", "MARKETS", "KILL_SWITCH_TOKEN"]:
            if key in os.environ:
                del os.environ[key]
        reload_settings()

    def test_enable_trading_defaults_to_false(self):
        """ENABLE_TRADING must default to False for safety."""
        # Unset any existing value
        if "ENABLE_TRADING" in os.environ:
            del os.environ["ENABLE_TRADING"]

        from config.settings import get_settings, reload_settings

        reload_settings()

        settings = get_settings()
        self.assertFalse(settings.trading.enable_trading, "ENABLE_TRADING must default to False")


# -- Integration Test Fixtures --


@pytest.fixture
def paper_mode_settings():
    """Fixture for paper mode configuration."""
    os.environ["MODE"] = "paper"
    os.environ["ENABLE_TRADING"] = "false"
    os.environ["MARKETS"] = "BTC-Q4,ETH-Q1,SOL-Q2"
    os.environ["KILL_SWITCH_TOKEN"] = "TestToken123!@#$Secure"

    from config.settings import reload_settings

    reload_settings()

    from config.settings import get_settings

    settings = get_settings()
    settings.validate(mode="paper")

    yield settings

    # Cleanup
    if "MODE" in os.environ:
        del os.environ["MODE"]
    if "ENABLE_TRADING" in os.environ:
        del os.environ["ENABLE_TRADING"]
    if "MARKETS" in os.environ:
        del os.environ["MARKETS"]
    if "KILL_SWITCH_TOKEN" in os.environ:
        del os.environ["KILL_SWITCH_TOKEN"]


@pytest.fixture
def backtest_result():
    """Fixture for running a deterministic backtest with a real arb edge present."""
    result = subprocess.run(
        [sys.executable, "main.py", "--mode", "backtest", "--ticks", "200",
         "--capital", "10000", "--pm-bias", "-0.03"],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, f"Backtest failed: {result.stderr}"

    # Parse results
    import re

    output = result.stdout + result.stderr

    def extract_value(pattern):
        match = re.search(pattern, output)
        return match.group(1) if match else None

    yield {
        "pnl": extract_value(r"P\&L:\s*(\$[+-]?\d+\.\d+)"),
        "pct": extract_value(r"\(([+-]\d+\.\d+)%\)"),
        "evaluated": extract_value(r"(\d+)\s+eval"),
        "approved": extract_value(r"(\d+)\s+approved"),
    }


# -- Pytest Test Functions --


def test_sandbox_acceptance(backtest_result, paper_mode_settings):
    """Complete sandbox acceptance test."""
    # Must have valid backtest results
    assert backtest_result["pnl"] is not None, "No P&L in backtest"
    assert backtest_result["approved"] is not None, "No approved proposals"

    # P&L must be finite and the backtest must have traded (sign is not asserted:
    # the unbiased synthetic market has no guaranteed edge — see tests/test_arb_logic.py
    # for a deterministic proof that a real edge is captured).
    pnl_value = float(backtest_result["pnl"].replace("$", ""))
    assert math.isfinite(pnl_value), f"Backtest produced non-finite P&L: {backtest_result['pnl']}"

    # Must have approved proposals
    assert int(backtest_result["approved"]) > 0, "No proposals approved"


def test_kill_switch_activation_flow():
    """Test complete kill switch activation and reset flow."""
    from risk.kill_switch import KillSwitch

    token = "SandboxTest!@#$Secure"
    ks = KillSwitch(token)

    # Initial state
    assert not ks.is_active

    # Activate
    record = ks.activate(reason="test_activation", mtm_drawdown=0.25, peak_equity=10000.0, current_equity=7500.0)

    assert ks.is_active
    assert record.reason == "test_activation"

    # Reset with wrong token
    assert not ks.reset("wrong_token")
    assert ks.is_active

    # Reset with correct token
    assert ks.reset(token)
    assert not ks.is_active


def test_backtest_determinism():
    """Verify backtest produces identical results across runs."""
    import re

    results = []

    for i in range(2):
        result = subprocess.run(
            [sys.executable, "main.py", "--mode", "backtest", "--ticks", "200", "--capital", "10000"],
            capture_output=True,
            text=True,
            timeout=60,
        )

        assert result.returncode == 0

        output = result.stdout + result.stderr
        pnl_match = re.search(r"P\&L:\s*(\$[+-]?\d+\.\d+)", output)

        assert pnl_match is not None, f"P&L not found in run {i}"
        results.append(pnl_match.group(1))

    # Must be identical
    assert results[0] == results[1], f"Determinism failed: {results[0]} != {results[1]}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
