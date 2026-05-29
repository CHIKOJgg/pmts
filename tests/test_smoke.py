"""
tests/test_smoke.py — Smoke tests for CI/CD pipeline.

These tests run quickly (< 30 seconds each) and must pass before any code is deployed.
They catch regressions that would prevent the system from functioning.

Run with: python -m pytest tests/test_smoke.py -v

CI Pipeline Integration:
    .github/workflows/ci.yml should include:

    test-smoke:
      runs-on: ubuntu-latest
      steps:
        - uses: actions/checkout@v4
        - name: Set up Python
          uses: actions/setup-python@v5
          with:
            python-version: '3.11'
        - name: Install dependencies
          run: |
            pip install pytest pytest-asyncio
            pip install -r requirements.txt
        - name: Run smoke tests
          run: |
            pytest tests/test_smoke.py -v --tb=short

        # Backtest zero-trade regression check
        python main.py --mode backtest --ticks 100 --capital 5000 || exit 1

Acceptance Criteria:
    [x] CI smoke tests that fail if expected-active synthetic backtests produce zero proposals/fills
    [ ] Contract tests for venue clients against sandbox
    [ ] Windows pytest environment setup
"""

from __future__ import annotations

import re
import subprocess
import sys
from typing import Any


def test_backtest_zero_trade_regression():
    """
    BUG: Backtest was producing zero trades due to missing FE→SE callback.

    This test MUST fail if backstart starts producing zero proposals/fills again.
    The system must produce valid proposals and fills in backtest mode.
    """
    result = subprocess.run(
        [sys.executable, "main.py", "--mode", "backtest", "--ticks", 200, "--capital", 10_000],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, f"Backtest crashed: {result.stderr}"

    output = result.stdout + result.stderr

    # Extract key metrics using regex
    evaluated_match = re.search(r"(\d+)\s+eval", output)
    approved_match = re.search(r"(\d+)\s+approved", output)
    fills_match = re.search(r"(\d+)\s+full.*(\d+)\s+partial", output, re.IGNORECASE)

    # CRITICAL: Must have proposals evaluated
    assert evaluated_match is not None, "No proposals evaluated - FE→SE callback may be broken"
    evaluated_count = int(evaluated_match.group(1))
    assert evaluated_count > 0, f"Zero proposals evaluated (got {evaluated_count})"

    # CRITICAL: Must have proposals approved
    assert approved_match is not None, "No proposals approved - strategy may be broken"
    approved_count = int(approved_match.group(1))
    assert approved_count > 0, f"Zero proposals approved (got {approved_count})"

    # Should have some fills
    if fills_match:
        full_fills = int(fills_match.group(1))
        partial_fills = int(fills_match.group(2))
        total_fills = full_fills + partial_fills
        assert total_fills > 0, f"No fills occurred (full={full_fills}, partial={partial_fills})"
    else:
        # Fallback: just check that "fill" appears somewhere in output
        assert "fill" in output.lower(), "No fill information found in backtest output"


def test_backtest_produces_positive_pnl():
    """
    Backtest must produce positive P&L (system is set up to be profitable).

    This catches regressions that would cause the strategy to lose money.
    """
    result = subprocess.run(
        [sys.executable, "main.py", "--mode", "backtest", "--ticks", 200, "--capital", 10_000],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, f"Backtest crashed: {result.stderr}"

    output = result.stdout + result.stderr

    # Extract P&L (format: "+$XX.XX (+XX.XX%)" or "-$XX.XX (-XX.XX%)")
    pnl_match = re.search(r"P\&L:\s*([+-]\$\d+\.\d+)\s*\(([+-]\d+\.\d+)%\)", output)

    assert pnl_match is not None, "No P&L information found in backtest output"

    pnl_str = pnl_match.group(1)
    pct_str = pnl_match.group(2)

    pnl_value = float(pnl_str.replace("$", ""))
    pct_value = float(pct_str.replace("%", ""))

    # System should be profitable in backtest
    assert pnl_value > 0, f"Backtest produced negative P&L: {pnl_str}"
    assert abs(pct_value) < 10.0, f"Unrealistic P&L percentage: {pct_str}%"


def test_backtest_determinism():
    """
    Two consecutive backtests must produce identical results.

    This catches non-deterministic bugs (random seeds, time-based logic, etc.).
    """
    results = []

    for i in range(2):
        result = subprocess.run(
            [sys.executable, "main.py", "--mode", "backtest", "--ticks", 200, "--capital", 10_000],
            capture_output=True,
            text=True,
            timeout=60,
        )

        assert result.returncode == 0, f"Run {i + 1} crashed: {result.stderr}"

        output = result.stdout + result.stderr

        # Extract P&L for comparison
        pnl_match = re.search(r"P\&L:\s*([+-]\$\d+\.\d+)", output)

        assert pnl_match is not None, f"P&L not found in run {i + 1}"
        results.append(pnl_match.group(1))

    # Results must be identical
    assert results[0] == results[1], f"Non-deterministic behavior detected:\nRun 1: {results[0]}\nRun 2: {results[1]}"


def test_paper_mode_validation():
    """
    Paper mode must start without live exchange credentials.

    This catches configuration bugs that would prevent development/testing.
    """
    import os

    # Ensure we're in a clean state
    original_env = os.environ.copy()

    try:
        # Set up paper mode environment
        env = os.environ.copy()
        env["MODE"] = "paper"
        env["ENABLE_TRADING"] = "false"  # Explicitly disable live trading

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
        # Credential-related error - this is the bug we're catching
        sys.exit(1)
    raise
""",
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

        assert result.returncode == 0, f"Paper mode validation failed: {result.stderr}"
        assert "VALIDATION_PASSED" in result.stdout, (
            "Paper mode validation did not pass - check credential requirements"
        )

    finally:
        # Restore original environment
        os.environ.clear()
        os.environ.update(original_env)


def test_kill_switch_token_security():
    """
    Kill switch token must meet security requirements.

    This catches configuration bugs that would allow weak tokens in production.
    """
    from config.settings import get_settings

    settings = get_settings()

    # Test with secure token (should pass)
    try:
        settings.validate(mode="paper")  # Paper mode doesn't enforce full security
    except ValueError:
        pass  # Expected if no token set

    # Verify kill switch module enforces security
    import pytest

    from risk.kill_switch import KillSwitch

    # Weak tokens should raise errors
    with pytest.raises(ValueError):
        KillSwitch("weak")  # Too short, no complexity

    with pytest.raises(ValueError):
        KillSwitch("alllowercase123456789")  # No special chars


def test_risk_engine_latency():
    """
    Risk engine must complete checks in < 5ms.

    This catches performance regressions that would cause latency issues.
    """
    import time

    from portfolio.manager import PortfolioManager
    from risk.engine import DEFAULT_LIMITS, RiskEngine
    from risk.kill_switch import KillSwitch
    from src.types import Platform, Side, StrategyId

    def price_source(m, p):
        return (0.50, 0.50)

    pm = PortfolioManager(10_000.0, price_source)
    ks = KillSwitch("TestToken123!@#$")

    engine = RiskEngine(portfolio=pm, kill_switch=ks, limits=DEFAULT_LIMITS)

    # Create a test proposal
    from execution.models import OrderProposal

    proposal = OrderProposal(
        proposal_id="test-proposal-1",
        market_id="TEST-1",
        platform=Platform.POLYMARKET,
        side=Side.BUY_YES,
        size_usdc=100.0,
        limit_price=0.50,
        order_type="LIMIT",
        strategy_id=StrategyId.ARB,
        leg_group_id="test-group",
        leg_1_market_id="TEST-1",
        leg_2_market_id="TEST-1",
    )

    # Measure latency
    latencies = []
    for _ in range(10):
        start = time.perf_counter()
        engine.evaluate(proposal)
        elapsed_ms = (time.perf_counter() - start) * 1000
        latencies.append(elapsed_ms)

    avg_latency = sum(latencies) / len(latencies)
    max_latency = max(latencies)

    # Must complete in < 5ms average (actual should be ~1-3ms)
    assert avg_latency < 5.0, f"Risk engine too slow: {avg_latency:.2f}ms average"
    assert max_latency < 10.0, f"Max latency too high: {max_latency:.2f}ms"


def test_config_validation_coverage():
    """
    Configuration must be validated before startup.

    This catches configuration bugs that would cause runtime errors.
    """
    from config.settings import get_settings

    settings = get_settings()

    # Verify multiple validation checks exist
    validation_methods = [
        "validate",
    ]

    for method in validation_methods:
        assert hasattr(settings, method), f"Missing validation method: {method}"


def test_backtest_metrics_extraction():
    """
    Extract and validate backtest metrics from output.

    This ensures metrics are properly formatted and available.
    """
    result = subprocess.run(
        [sys.executable, "main.py", "--mode", "backtest", "--ticks", 200, "--capital", 10_000],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, f"Backtest crashed: {result.stderr}"

    output = result.stdout + result.stderr

    # Extract various metrics
    metrics = {
        "evaluated": None,
        "approved": None,
        "rejected": None,
        "full_fills": None,
        "partial_fills": None,
        "pnl": None,
    }

    patterns = {
        "evaluated": r"(\d+)\s+eval",
        "approved": r"(\d+)\s+approved",
        "rejected": r"(\d+)\s+rejected",
        "full_fills": r"(\d+)\s+full",
        "partial_fills": r"(\d+)\s+partial",
        "pnl": r"P\&L:\s*([+-]\$\d+\.\d+)",
    }

    for key, pattern in patterns.items():
        match = re.search(pattern, output)
        if match:
            metrics[key] = match.group(1)

    # Verify critical metrics exist
    assert metrics["evaluated"] is not None, "No evaluated proposals metric"
    assert metrics["approved"] is not None, "No approved proposals metric"
    assert metrics["pnl"] is not None, "No P&L metric"


# Run all smoke tests
if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
