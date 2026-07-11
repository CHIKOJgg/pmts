import pytest

from portfolio.manager import PortfolioManager
from portfolio.storage import SqlitePortfolioStore
from risk.engine import RiskEngine
from risk.kill_switch import KillSwitch


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test_portfolio.db")

@pytest.fixture
def store(db_path):
    return SqlitePortfolioStore(db_path=db_path)

@pytest.fixture
def portfolio():
    # Dummy price source returning 0.5 for everything
    return PortfolioManager(initial_cash_usdc=10000.0, price_source=lambda m, p: (0.5, 0.5))

@pytest.fixture
def kill_switch():
    return KillSwitch(confirmation_token="test-token-secure-123")

@pytest.fixture
def risk_engine(portfolio, kill_switch, store):
    return RiskEngine(portfolio=portfolio, kill_switch=kill_switch, store=store)

def test_kill_switch_persistence_activation(risk_engine, store, kill_switch):
    # 1. Initially inactive
    assert not risk_engine.kill_switch_active
    assert not store.load_kill_switch()

    # 2. Activate
    risk_engine.manual_activate(reason="test_manual")
    assert risk_engine.kill_switch_active
    assert store.load_kill_switch()

def test_kill_switch_persistence_reset(risk_engine, store):
    # 1. Activate
    risk_engine.manual_activate(reason="test_manual")
    assert store.load_kill_switch()

    # 2. Reset
    success = risk_engine.reset_kill_switch(confirmation_token="test-token-secure-123")
    assert success
    assert not risk_engine.kill_switch_active
    assert not store.load_kill_switch()

def test_kill_switch_recovery_on_startup(db_path, portfolio):
    # 1. Create a store and activate kill switch
    store1 = SqlitePortfolioStore(db_path=db_path)
    ks1 = KillSwitch(confirmation_token="test-token-secure-123")
    re1 = RiskEngine(portfolio=portfolio, kill_switch=ks1, store=store1)

    re1.manual_activate(reason="test_manual")
    assert re1.kill_switch_active

    # 2. Simulate process restart by creating new objects with same DB
    store2 = SqlitePortfolioStore(db_path=db_path)
    ks2 = KillSwitch(confirmation_token="test-token-secure-123")
    # This should load the state from store2
    re2 = RiskEngine(portfolio=portfolio, kill_switch=ks2, store=store2)

    assert re2.kill_switch_active, "Kill switch should be active after restart"

def test_kill_switch_no_recovery_if_not_activated(db_path, portfolio):
    # 1. Create a store, leave kill switch inactive
    store1 = SqlitePortfolioStore(db_path=db_path)
    ks1 = KillSwitch(confirmation_token="test-token-secure-123")
    re1 = RiskEngine(portfolio=portfolio, kill_switch=ks1, store=store1)

    assert not re1.kill_switch_active

    # 2. Restart
    store2 = SqlitePortfolioStore(db_path=db_path)
    ks2 = KillSwitch(confirmation_token="test-token-secure-123")
    re2 = RiskEngine(portfolio=portfolio, kill_switch=ks2, store=store2)

    assert not re2.kill_switch_active
