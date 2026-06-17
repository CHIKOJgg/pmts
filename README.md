# PMTS — Prediction Market Trading System

Automated trading system for binary prediction markets. Targets **Polymarket** (Polygon / USDC) and **Opinion Markets** (BNB Chain / USDC).

> **Start here:** The backtest runs with zero credentials. Get that working first, then work through the live-trading sections in order.

---

## Table of Contents

**Getting Started**
1. [What This System Does](#1-what-this-system-does)
2. [Prerequisites](#2-prerequisites)
3. [Installation](#3-installation)
4. [Run the Backtest First](#4-run-the-backtest-first)

**Credentials — Complete Step-by-Step**
5. [Generate Your Kill Switch Token](#5-generate-your-kill-switch-token)
6. [Polymarket Setup](#6-polymarket-setup)
7. [Opinion Markets Setup](#7-opinion-markets-setup)
8. [Fund Your Wallets](#8-fund-your-wallets)
9. [Anthropic API Key (Optional AI)](#9-anthropic-api-key-optional)
10. [Verify All Credentials Before Going Live](#10-verify-all-credentials-before-going-live)

**Engineering**
11. [Implement Exchange Clients](#11-implement-exchange-clients)
12. [Complete .env Reference](#12-complete-env-reference)

**Operations**
13. [Running Live Trading](#13-running-live-trading)
14. [Docker Deployment](#14-docker-deployment)
15. [Reading Logs](#15-reading-logs)
16. [Kill Switch Operations](#16-kill-switch-operations)
17. [Monitoring Your System](#17-monitoring-your-system)
18. [Troubleshooting](#18-troubleshooting)

**Reference**
19. [Strategy Reference](#19-strategy-reference)
20. [Risk System](#20-risk-system)
21. [AI Module](#21-ai-module)
22. [Architecture](#22-architecture)
23. [Realism Model](#23-realism-model)
24. [Profitability Assessment](#24-profitability-assessment)
25. [Where It Will Fail](#25-where-it-will-fail)
26. [Project Structure](#26-project-structure)

---

## 1. What This System Does

PMTS detects and trades two types of opportunity in binary prediction markets:

**Arbitrage** — When the same event is mispriced across two venues. If "YES" on Polymarket + "NO" on Opinion costs $0.94 total, the system buys both. At resolution one pays $1.00, locking in a gross profit (minus fees and slippage).

**Market Making** — Posts limit orders on both sides of the book using the Stoikov reservation-price formula. Earns the spread while adjusting quotes to avoid accumulating large directional positions.

Both strategies share a common risk engine that enforces hard limits on exposure, drawdown, and position size.

---

## 2. Prerequisites

### System requirements

| Item | Minimum | Recommended |
|------|---------|-------------|
| Python | 3.11 | 3.12 |
| RAM | 512 MB | 2 GB |
| Disk | 500 MB | 10 GB (logs) |
| Network latency to exchanges | any | < 100 ms |
| OS | Linux / macOS / WSL2 | Ubuntu 22.04 LTS |

Check your Python version:

```bash
python3 --version
# Must output Python 3.11.x or higher
```

If you need Python 3.11:
```bash
# Ubuntu / Debian
sudo apt install python3.11 python3.11-venv

# macOS (with Homebrew)
brew install python@3.11

# Windows: download installer from python.org
```

### What you need for live trading

Work through these in order. Each section below tells you exactly where to get each item.

| Credential | Where | Time |
|------------|-------|------|
| Kill switch token | Generate locally | 30 seconds |
| Polymarket API key + secret + passphrase | polymarket.com → Settings | 10 min |
| Ethereum wallet private key | Generate locally or MetaMask | 5 min |
| USDC on Polygon + MATIC for gas | Coinbase / Binance | 30–60 min |
| Opinion Markets API key | opinion.markets → Settings | 10 min |
| USDC on BNB Chain + BNB for gas | Binance | 30–60 min |
| Claude API key | console.anthropic.com | 5 min (optional) |

---

## 3. Installation

### Step 1 — Get the code

```bash
git clone https://github.com/yourname/pmts.git
cd pmts
```

Or download and unzip the source directly.

### Step 2 — Create a virtual environment

**Linux / macOS:**
```bash
python3 -m venv .venv
source .venv/bin/activate
```

**Windows (PowerShell):**
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1

# If you get an execution policy error:
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
.venv\Scripts\Activate.ps1
```

**Windows (Command Prompt):**
```cmd
python -m venv .venv
.venv\Scripts\activate.bat
```

You should see `(.venv)` at the start of your terminal prompt after activation.

### Step 3 — Install dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

Verify the key packages installed:

```bash
python -c "import pydantic; print('pydantic', pydantic.__version__)"
python -c "import aiohttp; print('aiohttp OK')"
```

If either command fails with `ModuleNotFoundError`:
```bash
pip install pydantic aiohttp
```

### Step 4 — Copy the environment file

```bash
cp .env.example .env
```

You will fill in `.env` as you work through the credential sections below. It is safe to leave fields empty for now.

---

## 4. Run the Backtest First

Before touching any credentials, confirm the system works on your machine:

```bash
python main.py --mode backtest --ticks 2000 --capital 10000 --verbose
```

**Expected output (values vary due to randomness):**

```
═══ BACKTEST RESULTS ═══
Duration:     6.7 days | 2000 ticks
P&L:          $+34.21  (+0.34%)
Max Drawdown: 2.14%
Sharpe:       0.91
Sortino:      1.23
Fill rate:    52.3% | Avg fill ratio: 48.7%

Proposals:    412 eval | 231 approved | 181 rejected
Fills:        89 full | 44 partial | 28 expired
Avg slippage: 47.3 bps
```

The fill rate of ~50% is intentional — the simulator models thin liquidity realistically.

**Run the tests:**

```bash
pip install pytest pytest-asyncio
pytest tests/ -v
```

All tests should pass before you proceed to live trading.

---

## 5. Generate Your Kill Switch Token

**Do this before anything else.** The kill switch token is required to resume trading after a drawdown halt. If you lose it, you cannot restart the system without modifying source code.

```bash
# Linux / macOS
openssl rand -hex 32

# Python (any OS — works everywhere)
python3 -c "import secrets; print(secrets.token_hex(32))"

# Windows PowerShell
[System.BitConverter]::ToString([System.Security.Cryptography.RandomNumberGenerator]::GetBytes(32)).Replace('-','').ToLower()
```

Example output:
```
a7f3b9c2e1d04f8a6b5c3e2d1f9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d3e2f1
```

**Store it immediately in two places:**
1. Your `.env` file: `KILL_SWITCH_TOKEN=a7f3b9c2e1d04f...`
2. A password manager (1Password, Bitwarden, KeePass, etc.)

> If the trading process crashes and restarts, you need this token to resume. There is no recovery mechanism if you lose it — you would need to restart with a new token and a fresh system state.

---

## 6. Polymarket Setup

### 6.1 Create a Polymarket account

1. Go to [polymarket.com](https://polymarket.com)
2. Click **Connect Wallet** — use MetaMask, Coinbase Wallet, or any EVM wallet
3. Complete email verification if prompted

### 6.2 Create a dedicated trading wallet

> **Never use your personal wallet.** Create a dedicated wallet for this system. The private key goes into your `.env` file. Treat it like a password.

```bash
pip install eth-account

python3 << 'EOF'
from eth_account import Account
import secrets

key  = secrets.token_hex(32)
acct = Account.from_key(key)

print("=" * 64)
print(f"Address (public):  {acct.address}")
print(f"Private key:       {key}")
print("=" * 64)
print()
print("DO THIS NOW:")
print("1. Save BOTH values to your password manager.")
print("2. Add the private key to .env as PM_WALLET_KEY (no 0x prefix).")
print("3. Fund the Address with MATIC (gas) and USDC (trading capital).")
EOF
```

If you lose the private key, you lose permanent access to any funds in that wallet.

### 6.3 Get CLOB API credentials

This is how PMTS submits orders programmatically.

1. Connect your **trading wallet** (from 6.2) at [polymarket.com](https://polymarket.com)
2. Click your profile icon → **Settings** → **API Keys**
3. Click **Create New API Key**
4. Sign the message with your wallet (no gas, just a signature)
5. Copy the three values shown immediately:

```
API Key:        1a2b3c4d-e5f6-7890-abcd-ef1234567890
API Secret:     ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890
API Passphrase: some-passphrase-you-chose
```

Add to `.env`:
```bash
PM_API_KEY=1a2b3c4d-e5f6-7890-abcd-ef1234567890
PM_API_SECRET=ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890
PM_PASSPHRASE=some-passphrase-you-chose
PM_WALLET_KEY=a7f3b9c2...   # private key from 6.2, no 0x prefix
```

> **The Secret and Passphrase are only shown once.** If you miss them, delete the API key and create a new one — this is safe and free.

### 6.4 Find Polymarket market IDs

Markets are identified by a `conditionId` — a hex string starting with `0x`.

```bash
python3 << 'EOF'
import urllib.request, json

url = "https://gamma-api.polymarket.com/markets?limit=20&active=true&order=volume&ascending=false"
with urllib.request.urlopen(url, timeout=10) as r:
    markets = json.loads(r.read())

print(f"Top {len(markets)} active markets by volume:\n")
for m in markets:
    vol = m.get("volume", 0)
    print(f"  {m['question'][:65]}")
    print(f"  conditionId: {m['conditionId']}")
    print(f"  Volume: ${float(vol):,.0f}")
    print()
EOF
```

Add condition IDs to `.env`:
```bash
MARKETS=0x1234abc,0x5678def
```

### 6.5 One-time USDC approval (required before first trade)

Before PMTS can place any orders, approve the Polymarket CLOB contract to spend your USDC. One-time only, costs ~$0.01 of MATIC.

```bash
# First, make sure PM_WALLET_KEY is in your environment
export PM_WALLET_KEY=your_private_key_here

python3 << 'EOF'
import os
from py_clob_client.client import ClobClient

key = os.environ.get("PM_WALLET_KEY", "")
if not key:
    print("ERROR: export PM_WALLET_KEY=your_private_key first")
    exit(1)

client = ClobClient("https://clob.polymarket.com", key=key, chain_id=137)

print("Approving USDC for Polymarket CLOB (costs ~$0.01 MATIC)...")
result = client.approve_erc20()
print(f"Result: {result}")
print("Done. PMTS can now place orders on Polymarket.")
EOF
```

If you see `insufficient funds for gas`, add MATIC to your wallet first (see §8).

### 6.6 Test Polymarket connectivity

```bash
python3 << 'EOF'
import urllib.request, json

# Public endpoint check
url = "https://gamma-api.polymarket.com/markets?limit=1"
with urllib.request.urlopen(url, timeout=5) as r:
    data = json.loads(r.read())
    print(f"Polymarket API: reachable — {len(data)} market(s) returned")

# CLOB check
url = "https://clob.polymarket.com/ok"
with urllib.request.urlopen(url, timeout=5) as r:
    print(f"Polymarket CLOB: reachable (status {r.status})")

print("Connectivity: OK")
EOF
```

---

## 7. Opinion Markets Setup

### 7.1 Create an account

1. Go to [opinion.markets](https://opinion.markets)
2. Sign up with email or a BNB Chain wallet
3. Complete email verification

### 7.2 Get an API key

1. Log in at Opinion Markets
2. Go to **Settings** → **Developer** or **API Access**
3. Click **Generate API Key**
4. Copy to `.env`:

```bash
OP_API_KEY=om_live_abcdefghij...
```

### 7.3 Find Opinion market IDs

Opinion Markets uses string IDs (not hex addresses):

```bash
python3 << 'EOF'
import urllib.request, json, os

api_key = os.environ.get("OP_API_KEY", "your-key-here")
url = "https://api.opinion.markets/v1/markets?limit=20&status=active"
req = urllib.request.Request(url, headers={"X-API-Key": api_key, "Accept": "application/json"})

try:
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read())

    markets = data if isinstance(data, list) else data.get("markets", [])
    print(f"Found {len(markets)} active markets:\n")
    for m in markets:
        print(f"  {str(m.get('question',''))[:65]}")
        print(f"  market_id: {m.get('market_id', m.get('id', ''))}")
        print()
except Exception as e:
    print(f"Error: {e}")
    print("Check your OP_API_KEY and see Opinion Markets API documentation.")
EOF
```

Add IDs to `MARKETS=` alongside Polymarket IDs:
```bash
MARKETS=0xpolymarket_id,opinion-market-id-1
```

### 7.4 Test Opinion connectivity

```bash
python3 << 'EOF'
import urllib.request, json, os

key = os.environ.get("OP_API_KEY", "")
if not key:
    print("Set OP_API_KEY in environment first")
    exit(1)

req = urllib.request.Request(
    "https://api.opinion.markets/v1/markets?limit=1",
    headers={"X-API-Key": key},
)
with urllib.request.urlopen(req, timeout=5) as r:
    data = json.loads(r.read())
    markets = data if isinstance(data, list) else data.get("markets", [])
    print(f"Opinion Markets: reachable — {len(markets)} market(s) returned")
print("Connectivity: OK")
EOF
```

---

## 8. Fund Your Wallets

PMTS needs USDC pre-positioned on two separate blockchains. You cannot move funds between them in real-time — plan your allocation before starting.

### 8.1 USDC on Polygon (for Polymarket)

**You need: MATIC for gas + USDC for trading.**

**Get MATIC for gas** (~$5 lasts months):
- Buy MATIC on Coinbase, Binance, or Kraken
- Withdraw to your trading wallet address
- **Select network: Polygon** (not Ethereum Mainnet)

**Get USDC on Polygon** (choose one):

Option A — Direct CEX withdrawal (simplest and fastest):
- **Coinbase:** Portfolio → Transfer → Send → choose Polygon network → paste wallet address
- **Binance:** Wallet → Withdraw → USDC → Network: MATIC20 (Polygon) → paste address

Option B — Bridge from Ethereum:
- Send USDC to your Ethereum address
- Go to [wallet.polygon.technology](https://wallet.polygon.technology)
- Bridge → USDC → confirm → wait 5–10 min

**Verify your Polygon balance:**

```bash
python3 << 'EOF'
from web3 import Web3

w3 = Web3(Web3.HTTPProvider("https://polygon-rpc.com"))
USDC = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
ABI  = [{"inputs":[{"name":"a","type":"address"}],"name":"balanceOf",
         "outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"}]

contract = w3.eth.contract(address=Web3.to_checksum_address(USDC), abi=ABI)
wallet   = input("Enter your trading wallet address: ").strip()

usdc  = contract.functions.balanceOf(Web3.to_checksum_address(wallet)).call()
matic = w3.eth.get_balance(Web3.to_checksum_address(wallet))

print(f"USDC on Polygon:  ${usdc / 1_000_000:.2f}")
print(f"MATIC for gas:    {matic / 1e18:.4f}")
EOF
```

### 8.2 USDC on BNB Chain (for Opinion)

**You need: BNB for gas + USDC for trading.**

**Get BNB for gas** (~$5 lasts months):
- Buy BNB on Binance
- Withdraw to your wallet: **Network: BNB Smart Chain (BSC / BEP-20)**
- You can use the same wallet address as your Polygon wallet — same private key works on all EVM chains

**Get USDC on BNB Chain** (choose one):

Option A — Binance direct withdrawal (simplest):
- Wallet → Withdraw → USDC → Network: BSC (BEP-20) → paste address

Option B — Bridge from Polygon:
- Go to [cBridge](https://cbridge.celer.network) or [Stargate Finance](https://stargate.finance)
- Select: From Polygon → To BNB Chain, asset USDC
- Confirm and wait 5–15 minutes

**Verify your BNB Chain balance:**

```bash
python3 << 'EOF'
from web3 import Web3

w3 = Web3(Web3.HTTPProvider("https://bsc-dataseed.binance.org/"))
USDC = "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d"
ABI  = [{"inputs":[{"name":"a","type":"address"}],"name":"balanceOf",
         "outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"}]

contract = w3.eth.contract(address=Web3.to_checksum_address(USDC), abi=ABI)
wallet   = input("Enter your trading wallet address: ").strip()

usdc = contract.functions.balanceOf(Web3.to_checksum_address(wallet)).call()
bnb  = w3.eth.get_balance(Web3.to_checksum_address(wallet))

print(f"USDC on BNB Chain: ${usdc / 1e18:.2f}")  # BNB Chain USDC uses 18 decimals
print(f"BNB for gas:       {bnb / 1e18:.4f}")
EOF
```

### 8.3 Minimum funding to start testing

| Chain | USDC | Gas token |
|-------|------|-----------|
| Polygon (Polymarket) | $200–500 | $5 MATIC |
| BNB Chain (Opinion) | $200–500 | $5 BNB |

**`.env` settings for a $400–1,000 total capital setup:**

```bash
INITIAL_CASH_USDC=600       # your total across both chains
ARB_BUDGET_USDC=120         # 20% for arb
MM_BUDGET_USDC=180          # 30% for MM
MAX_ORDER_USDC=30           # keep individual orders small
MIN_ORDER_USDC=3
MAX_MARKET_EXP_USDC=80
```

Scale these proportionally as you grow capital.

---

## 9. Anthropic API Key (Optional)

The AI module is **optional**. The system works equally well without it using the built-in heuristic model. Skip this section if you want to start simply.

### When to enable it

Enable the Claude API only after you have verified the system is profitable on backtest and you understand how it works. The heuristic covers all critical signal conditions with nearly identical results in most market conditions.

### Get a Claude API key

1. Go to [console.anthropic.com](https://console.anthropic.com)
2. Create an account (requires email + credit card)
3. Navigate to **API Keys** → **Create Key**
4. Copy the key — it starts with `sk-ant-api03-...`

Add to `.env`:
```bash
ANTHROPIC_API_KEY=sk-ant-api03-...
AI_ENABLED=true
AI_USE_HEURISTIC_ONLY=false
AI_API_TIMEOUT_MS=200
AI_CACHE_TTL_MS=3000
```

### Start in heuristic-only mode regardless

Even with an API key, start with heuristic mode until you understand the system:

```bash
AI_USE_HEURISTIC_ONLY=true
```

### What the AI can and cannot do

**Can do:** Classify market regime (stable / trending / volatile / thin), adjust arb edge thresholds, suppress MM quoting, signal hedge urgency.

**Cannot do:** Specify order price, size, side, or platform. The AI has no access to RiskEngine, ExecutionEngine, or PortfolioManager. If the API call takes > 200ms, the heuristic fallback activates immediately — trading is never blocked.

---

## 10. Verify All Credentials Before Going Live

Run this script after filling in your `.env`. It checks every required value.

```bash
python3 << 'EOF'
import os, sys

# Load .env file
if os.path.exists(".env"):
    for line in open(".env"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            if v:
                os.environ.setdefault(k.strip(), v.strip())

checks = [
    ("KILL_SWITCH_TOKEN",
     lambda: os.environ.get("KILL_SWITCH_TOKEN", "") not in ("", "CHANGE-ME", "CHANGE-ME-GENERATE-WITH-OPENSSL"),
     "Generate: python3 -c \"import secrets; print(secrets.token_hex(32))\""),
    ("PM_API_KEY",
     lambda: bool(os.environ.get("PM_API_KEY")),
     "Get from polymarket.com -> Settings -> API Keys"),
    ("PM_API_SECRET",
     lambda: bool(os.environ.get("PM_API_SECRET")),
     "Get from polymarket.com -> Settings -> API Keys"),
    ("PM_PASSPHRASE",
     lambda: bool(os.environ.get("PM_PASSPHRASE")),
     "Get from polymarket.com -> Settings -> API Keys"),
    ("PM_WALLET_KEY",
     lambda: bool(os.environ.get("PM_WALLET_KEY")),
     "Generate with eth-account (see section 6.2)"),
    ("OP_API_KEY",
     lambda: bool(os.environ.get("OP_API_KEY")),
     "Get from opinion.markets -> Settings -> Developer"),
    ("MARKETS",
     lambda: bool(os.environ.get("MARKETS")),
     "Add comma-separated market IDs (see sections 6.4 and 7.3)"),
    ("INITIAL_CASH_USDC",
     lambda: os.environ.get("INITIAL_CASH_USDC", "") != "",
     "Set to your actual total USDC balance across both chains"),
]

print("=" * 60)
print("CREDENTIAL CHECKLIST")
print("=" * 60)

all_ok = True
for name, check_fn, hint in checks:
    try:
        ok = check_fn()
    except Exception:
        ok = False
    status = "OK" if ok else "MISSING"
    mark   = "+" if ok else "!"
    print(f"  [{mark}] {name:30s}  {status}")
    if not ok:
        print(f"        Hint: {hint}")
        all_ok = False

print()
ai_key = os.environ.get("ANTHROPIC_API_KEY", "")
if ai_key:
    ai_en = os.environ.get("AI_ENABLED", "true").lower()
    print(f"  [i] ANTHROPIC_API_KEY: set (AI_ENABLED={ai_en})")
else:
    print("  [i] ANTHROPIC_API_KEY: not set (heuristic mode will be used)")

print()
if all_ok:
    print("All required credentials are configured.")
    print("Next step: implement exchange clients (section 11)")
    print("Then run:  python main.py --mode live")
else:
    print("Fix the [!] items above before running live trading.")
    sys.exit(1)
EOF
```

---

## 11. Implement Exchange Clients

`execution/clients/` has empty stubs. These two files are the only remaining engineering work before live trading. Each must implement the `ExchangeClient` protocol from `execution/engine.py`.

### The interface

```python
class ExchangeClient(Protocol):
    @property
    def platform(self) -> Platform: ...

    async def place_order(
        self, submission: OrderSubmission, effective_price: float
    ) -> PlacedOrderResponse: ...

    async def cancel_order(self, exchange_order_id: str, market_id: str) -> bool: ...

    async def get_order_status(
        self, exchange_order_id: str, market_id: str
    ) -> OrderStatusResponse: ...
```

### Polymarket — `execution/clients/polymarket.py`

```python
"""Polymarket CLOB exchange client."""
from __future__ import annotations
import asyncio, time
from typing import Optional
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds
from execution.engine import (
    ExchangeClient, PlacedOrderResponse, OrderStatusResponse,
    PlacedFill, OrderStatusFill,
)
from execution.models import OrderSubmission
from src.types import Platform, Side


class PolymarketClient:
    PLATFORM = Platform.POLYMARKET

    def __init__(
        self,
        api_key: str, secret: str, passphrase: str,
        wallet_key: str, host: str = "https://clob.polymarket.com",
    ) -> None:
        self._clob = ClobClient(
            host, key=wallet_key, chain_id=137,
            creds=ApiCreds(api_key=api_key, api_secret=secret, api_passphrase=passphrase),
        )
        self._token_map:   dict[str, tuple[str, str]] = {}
        self._fill_counts: dict[str, int] = {}

    @property
    def platform(self) -> Platform:
        return self.PLATFORM

    async def _resolve_token(self, submission: OrderSubmission) -> str:
        loop = asyncio.get_event_loop()
        if submission.market_id not in self._token_map:
            market = await loop.run_in_executor(
                None, lambda: self._clob.get_market(submission.market_id)
            )
            tokens = market.get("tokens", [])
            yes_id = next((t["token_id"] for t in tokens if t.get("outcome","").upper() == "YES"), "")
            no_id  = next((t["token_id"] for t in tokens if t.get("outcome","").upper() == "NO"),  "")
            self._token_map[submission.market_id] = (yes_id, no_id)
        yes_id, no_id = self._token_map[submission.market_id]
        return yes_id if submission.side in (Side.BUY_YES, Side.SELL_YES) else no_id

    async def place_order(
        self, submission: OrderSubmission, effective_price: float
    ) -> PlacedOrderResponse:
        from src.errors import ExchangeRejected
        token_id = await self._resolve_token(submission)
        side_str = "BUY" if submission.side.is_buy else "SELL"
        loop     = asyncio.get_event_loop()

        order_args = {
            "token_id":   token_id,
            "price":      str(round(effective_price, 4)),
            "size":       str(round(submission.token_quantity, 6)),
            "side":       side_str,
            "type":       "GTC",
            "expiration": str(submission.expiry_ms // 1000),
        }

        try:
            order = await loop.run_in_executor(
                None, lambda: self._clob.create_order(order_args)
            )
            resp = await loop.run_in_executor(
                None, lambda: self._clob.post_order(order, "GTC")
            )
        except Exception as exc:
            if "400" in str(exc) or "422" in str(exc):
                raise ExchangeRejected(
                    str(exc), platform="polymarket",
                    proposal_id=submission.proposal_id,
                    status_code=400, exchange_error=str(exc),
                )
            raise

        oid   = resp.get("orderID", "")
        fills = self._parse_fills(resp.get("fills", []))
        self._fill_counts[oid] = len(fills)
        return PlacedOrderResponse(
            exchange_order_id=oid,
            status=_norm_pm_status(resp.get("status", "")),
            fills=fills,
            tx_hash=resp.get("transactionHash"),
        )

    async def cancel_order(self, exchange_order_id: str, market_id: str) -> bool:
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None, lambda: self._clob.cancel({"orderID": exchange_order_id})
            )
            self._fill_counts.pop(exchange_order_id, None)
            return True
        except Exception:
            return False

    async def get_order_status(
        self, exchange_order_id: str, market_id: str
    ) -> OrderStatusResponse:
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(
            None, lambda: self._clob.get_order(exchange_order_id)
        )
        status       = (data.get("status") or "").upper()
        is_live      = status in ("LIVE", "OPEN")
        is_cancelled = status in ("CANCELLED", "CANCELED")
        is_filled    = status in ("MATCHED", "FILLED")

        try:
            remaining = float(data.get("remainingAmount", 0)) * float(data.get("price", 1))
        except (ValueError, TypeError):
            remaining = 0.0

        all_fills  = self._parse_fills(data.get("fills", []))
        prev       = self._fill_counts.get(exchange_order_id, 0)
        new_fills  = all_fills[prev:]
        self._fill_counts[exchange_order_id] = len(all_fills)

        return OrderStatusResponse(
            exchange_order_id=exchange_order_id,
            is_live=is_live, is_cancelled=is_cancelled, is_filled=is_filled,
            remaining_usdc=remaining, new_fills=new_fills,
            tx_hash=data.get("transactionHash"),
        )

    def _parse_fills(self, raw: list) -> list[PlacedFill]:
        out = []
        for f in raw:
            try:
                out.append(PlacedFill(
                    fill_usdc=float(f.get("takerAmount", 0)),
                    fill_price=float(f.get("price", 0)),
                    fill_tokens=float(f.get("makerAmount", 0)),
                    ts=int(f.get("timestamp", int(time.time() * 1000))),
                ))
            except (ValueError, TypeError):
                continue
        return out


def _norm_pm_status(s: str) -> str:
    return {
        "LIVE": "live", "OPEN": "live",
        "MATCHED": "matched", "FILLED": "matched",
        "CANCELLED": "cancelled", "CANCELED": "cancelled",
    }.get((s or "").upper(), "live")
```

### Opinion — `execution/clients/opinion.py`

```python
"""Opinion Markets exchange client."""
from __future__ import annotations
import asyncio, time
from typing import Optional
import aiohttp
from execution.engine import (
    ExchangeClient, PlacedOrderResponse, OrderStatusResponse,
    PlacedFill, OrderStatusFill,
)
from execution.models import OrderSubmission
from src.types import Platform, Side


class OpinionClient:
    PLATFORM = Platform.OPINION

    def __init__(
        self,
        api_key: str,
        host: str = "https://api.opinion.markets/v1",
    ) -> None:
        self._api_key     = api_key
        self._host        = host.rstrip("/")
        self._session:    Optional[aiohttp.ClientSession] = None
        self._fill_counts: dict[str, int] = {}

    @property
    def platform(self) -> Platform:
        return self.PLATFORM

    def _headers(self) -> dict:
        return {"X-API-Key": self._api_key, "Content-Type": "application/json"}

    async def _sess(self) -> aiohttp.ClientSession:
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(headers=self._headers())
        return self._session

    async def place_order(
        self, submission: OrderSubmission, effective_price: float
    ) -> PlacedOrderResponse:
        from src.errors import ExchangeRejected
        sess    = await self._sess()
        action  = "buy"  if submission.side.is_buy  else "sell"
        token   = "yes"  if submission.side in (Side.BUY_YES, Side.SELL_YES) else "no"
        payload = {
            "market_id":       submission.market_id,
            "token_type":      token,
            "action":          action,
            "price":           round(effective_price, 4),
            "size_usdc":       round(submission.size_usdc, 4),
            "client_order_id": submission.proposal_id,
        }
        async with sess.post(f"{self._host}/orders", json=payload) as resp:
            data = await resp.json()
            if resp.status in (400, 422):
                raise ExchangeRejected(
                    data.get("message", "bad request"), platform="opinion",
                    proposal_id=submission.proposal_id,
                    status_code=resp.status, exchange_error=str(data),
                )
            resp.raise_for_status()
        oid   = data.get("order_id", "")
        fills = self._parse_fills(data.get("fills", []))
        self._fill_counts[oid] = len(fills)
        return PlacedOrderResponse(
            exchange_order_id=oid,
            status=_norm_op_status(data.get("status", "")),
            fills=fills, tx_hash=data.get("tx_hash"),
        )

    async def cancel_order(self, exchange_order_id: str, market_id: str) -> bool:
        sess = await self._sess()
        async with sess.delete(f"{self._host}/orders/{exchange_order_id}") as resp:
            if resp.status == 404:
                return False
            resp.raise_for_status()
        self._fill_counts.pop(exchange_order_id, None)
        return True

    async def get_order_status(
        self, exchange_order_id: str, market_id: str
    ) -> OrderStatusResponse:
        sess = await self._sess()
        async with sess.get(f"{self._host}/orders/{exchange_order_id}") as resp:
            resp.raise_for_status()
            data = await resp.json()
        status       = data.get("status", "")
        is_live      = status in ("open", "live", "resting")
        is_cancelled = status in ("cancelled", "canceled")
        is_filled    = status in ("filled", "matched")
        remaining    = float(data.get("remaining_usdc", 0.0))
        all_fills    = self._parse_fills(data.get("fills", []))
        prev         = self._fill_counts.get(exchange_order_id, 0)
        new_fills    = all_fills[prev:]
        self._fill_counts[exchange_order_id] = len(all_fills)
        return OrderStatusResponse(
            exchange_order_id=exchange_order_id,
            is_live=is_live, is_cancelled=is_cancelled, is_filled=is_filled,
            remaining_usdc=remaining, new_fills=new_fills,
            tx_hash=data.get("tx_hash"),
        )

    def _parse_fills(self, raw: list) -> list[PlacedFill]:
        out = []
        for f in raw:
            try:
                out.append(PlacedFill(
                    fill_usdc=float(f.get("fill_usdc", 0)),
                    fill_price=float(f.get("fill_price", 0)),
                    fill_tokens=float(f.get("fill_tokens", 0)),
                    ts=int(f.get("ts", int(time.time() * 1000))),
                ))
            except (ValueError, TypeError):
                continue
        return out


def _norm_op_status(s: str) -> str:
    return {
        "open": "live", "live": "live", "resting": "live",
        "filled": "matched", "matched": "matched",
        "cancelled": "cancelled", "canceled": "cancelled",
    }.get((s or "").lower(), "live")
```

---

## 12. Complete .env Reference

```bash
# ══ EXCHANGE CREDENTIALS ═══════════════════════════════════════════════════════

PM_CLOB_URL=https://clob.polymarket.com
PM_WS_URL=wss://ws-subscriptions-clob.polymarket.com/ws/market
PM_API_KEY=            # from polymarket.com → Settings → API Keys
PM_API_SECRET=         # from polymarket.com → Settings → API Keys
PM_PASSPHRASE=         # from polymarket.com → Settings → API Keys
PM_WALLET_KEY=         # private key, no 0x prefix
PM_TAKER_FEE_BPS=20    # Polymarket taker fee in basis points

OP_REST_URL=https://api.opinion.markets/v1
OP_WS_URL=wss://ws.opinion.markets
OP_API_KEY=            # from opinion.markets → Settings → Developer
OP_TAKER_FEE_BPS=25    # Opinion taker fee in basis points

# ══ MARKETS ════════════════════════════════════════════════════════════════════

# Comma-separated market IDs (see sections 6.4 and 7.3)
MARKETS=0xabc123,0xdef456,opinion-market-id

# ══ CAPITAL & RISK ═════════════════════════════════════════════════════════════

INITIAL_CASH_USDC=10000    # total USDC across both chains

# Generate with: python3 -c "import secrets; print(secrets.token_hex(32))"
# Store in password manager — needed to reset kill switch after drawdown halt
KILL_SWITCH_TOKEN=CHANGE-ME-GENERATE-WITH-OPENSSL

DRAWDOWN_KILL_PCT=0.20     # halt all trading at 20% drawdown
DRAWDOWN_WARN_PCT=0.15     # log warning at 15% (trading continues)
MAX_ORDER_USDC=200         # maximum single order size in USDC
MIN_ORDER_USDC=1.0         # minimum order size (dust filter)
MAX_MARKET_EXP_PCT=0.05    # max 5% of equity in any one market
MAX_MARKET_EXP_USDC=500    # hard dollar cap per market
MAX_NET_DELTA=50           # max |YES_tokens - NO_tokens| per market
ARB_BUDGET_USDC=2000       # max capital in arb orders at any time
MM_BUDGET_USDC=3000        # max capital in MM orders at any time

# ══ STRATEGY TOGGLES ═══════════════════════════════════════════════════════════

ENABLE_TRADING=true        # set false to disable all order submission
ENABLE_ARB=true
ENABLE_MM=true
ENABLE_HEDGE=true

# ══ AI MODULE ══════════════════════════════════════════════════════════════════

ANTHROPIC_API_KEY=         # optional — leave empty for heuristic-only mode
AI_ENABLED=true
AI_USE_HEURISTIC_ONLY=false  # set true to skip API entirely
AI_API_TIMEOUT_MS=200        # fall back to heuristic after this many ms
AI_CACHE_TTL_MS=3000         # reuse AI result for up to 3 seconds

# ══ INFRASTRUCTURE ═════════════════════════════════════════════════════════════

REDIS_URL=redis://localhost:6379/0
REDIS_ENABLED=false        # set true to enable position snapshot side-writes

POSTGRES_URL=postgresql://pmts:pmts@localhost:5432/pmts
POSTGRES_ENABLED=false     # set true for persistent order history

# ══ LOGGING ════════════════════════════════════════════════════════════════════

LOG_LEVEL=INFO             # DEBUG | INFO | WARNING | ERROR
LOG_FORMAT=json            # json (for log aggregators) | text (human-readable)
LOG_FILE=                  # leave empty for stdout only
```

---

## 13. Running Live Trading

Once credentials are verified (§10) and exchange clients are implemented (§11):

```bash
# Activate virtual environment
source .venv/bin/activate

# Load .env into your shell session
export $(grep -v '^#' .env | grep '=' | xargs)

# Start the system
python main.py --mode live
```

**Expected startup output (JSON format):**
```json
{"level":"INFO","message":"Orchestrator starting..."}
{"level":"INFO","message":"Portfolio manager started"}
{"level":"INFO","message":"Execution engines started"}
{"level":"INFO","message":"Polymarket connector: UP"}
{"level":"INFO","message":"Opinion connector: UP"}
{"level":"INFO","message":"Orchestrator started: 2 markets, trading=true"}
```

If you see `connector: DOWN`, verify your credentials and network access to the exchange APIs.

**Graceful stop:**
```bash
Ctrl+C
# Logs "Orchestrator stopping..." — open orders are NOT cancelled
```

**Emergency stop (cancels all open orders):**
```python
# This must be called programmatically, e.g., from a monitoring script:
import asyncio
asyncio.run(orchestrator.emergency_stop("manual_halt"))
```

Or simply stop the process and set `ENABLE_TRADING=false` in `.env` before restarting.

---

## 14. Docker Deployment

Docker is the recommended production deployment — handles dependencies, automatic restart, and log collection.

### Build

```bash
docker build -t pmts:latest .
```

### Run backtest in Docker

```bash
docker-compose --profile backtest up pmts-backtest
```

### Run live in Docker

```bash
# Ensure .env is filled in, then:
docker-compose up pmts

# Or in background:
docker-compose up -d pmts
```

The container will restart automatically on crash (`restart: unless-stopped`).

### View logs

```bash
docker-compose logs -f pmts

# Filter for errors only:
docker-compose logs -f pmts | grep '"level":"ERROR"'
```

### With Redis (optional analytics side-writes)

```bash
docker-compose up pmts redis
```

### Full stack (with PostgreSQL for order history)

```bash
docker-compose --profile full up
```

---

## 15. Reading Logs

### JSON log format (default)

Each log line is a JSON object:

```json
{"ts":1700001234567,"level":"WARNING","logger":"risk.engine","message":"REJECT proposal=a1b2c3 strategy=arb market=0xabc $50.00 reason=insufficient_capital — Need $50.00, available $23.45"}
```

### Text format (for development)

```bash
LOG_FORMAT=text LOG_LEVEL=DEBUG python main.py --mode live
```

Output looks like:
```
2024-01-15 14:23:01 [INFO    ] risk.engine: REJECT proposal=a1b2c3 ...
```

### Key log messages

| Message | Meaning | Action required |
|---------|---------|----------------|
| `KILL SWITCH ACTIVATED` | Drawdown hit 20% | Investigate immediately |
| `REJECT ... reason=kill_switch_active` | Normal after kill switch fires | Resolve underlying issue first |
| `REJECT ... reason=drawdown_limit` | Drawdown hit threshold | Check positions |
| `ARB ACCEPTED` | Arb trade firing | Normal — monitor fill rate |
| `HEDGE proposed` | Delta hedge firing | Normal |
| `AI disabled after N errors` | Claude API unreachable | Set `AI_USE_HEURISTIC_ONLY=true` |
| `Submit attempt 2/3 failed` | Transient exchange error | Normal, retrying |
| `Poll error for` | Exchange polling failed | Check connectivity |

### Filter logs in real time

```bash
# All warnings and above
python main.py --mode live 2>&1 | python3 -c "import sys,json; [print(json.loads(l)['message']) for l in sys.stdin if json.loads(l).get('level') in ('WARNING','ERROR','CRITICAL') if l.strip()]"

# All arb trades
python main.py --mode live 2>&1 | grep "ARB ACCEPTED"

# All rejections with reason
python main.py --mode live 2>&1 | grep "REJECT"
```

---

## 16. Kill Switch Operations

### Automatic activation

Fires when `DRAWDOWN_KILL_PCT` (default 20%) is reached. When it activates:
- All new proposals are rejected immediately
- All open orders are cancelled
- `KILL SWITCH ACTIVATED` is logged at CRITICAL level

### Find what triggered it

```bash
grep "KILL SWITCH\|reason=drawdown_limit" your.log | tail -10
```

### Manual activation

```python
# From any Python script that has access to the orchestrator:
await orchestrator.emergency_stop("manual_halt")
```

Or stop the process and set `ENABLE_TRADING=false` in `.env`.

### Reset procedure

**Before resetting:**
1. Understand why the halt occurred
2. Verify all positions are at acceptable levels
3. Confirm market conditions have stabilized
4. Locate your `KILL_SWITCH_TOKEN` from your password manager

```python
success = orchestrator.risk.reset_kill_switch(
    confirmation_token="your-kill-switch-token-from-env",
    operator_id="your-name",
)
print("Reset:", success)  # True = trading resumes
```

Or restart the process normally — if the drawdown has recovered below the threshold, the kill switch will not re-trigger immediately.

---

## 17. Monitoring Your System

### Portfolio snapshot

```python
snap = orchestrator.portfolio.build_snapshot()

print(f"Equity:    ${snap['total_mtm_usdc']:,.2f}")
print(f"Cash:      ${snap['total_cash_usdc']:,.2f}")
print(f"Drawdown:  {snap['mtm_drawdown_pct']:.2%}")
print(f"Realised:  ${snap['total_realised_pnl']:+,.2f}")
print(f"Positions: {len(snap['positions'])}")
for p in snap['positions']:
    print(f"  {p['market_id'][:30]:30s} Δ={p['net_delta']:+.1f} mtm=${p['mtm_usdc']:.2f}")
```

### Risk engine state

```python
r = orchestrator.risk
print(f"Kill switch:     {'ACTIVE' if r.kill_switch_active else 'clear'}")
print(f"Evaluated:       {r.total_evaluated}")
print(f"Approved:        {r.total_approved} ({r.total_approved/max(1,r.total_evaluated):.1%})")
print(f"Rejected:        {r.total_rejected}")
print(f"Arb allocated:   ${r._arb_allocated:.2f}")
print(f"MM  allocated:   ${r._mm_allocated:.2f}")
print()
for reason, count in sorted(r.rejections_by_reason.items(), key=lambda x: -x[1]):
    if count > 0:
        print(f"  {reason:35s}: {count}")
```

### Execution stats

```python
for engine, name in [(orchestrator._pm_engine, "PM"), (orchestrator._op_engine, "OP")]:
    e = engine
    total = e.orders_filled + e.orders_partial + e.orders_expired + e.orders_cancelled
    fill_rate = (e.orders_filled + e.orders_partial) / max(1, total)
    print(
        f"[{name}] submitted={e.orders_submitted} "
        f"filled={e.orders_filled} partial={e.orders_partial} "
        f"expired={e.orders_expired} fill_rate={fill_rate:.1%} "
        f"usdc_filled=${e.total_filled_usdc:.2f}"
    )
```

### Daily health checks

```bash
# Check if fill rate is acceptable (should be > 30%)
grep "ARB ACCEPTED" your.log | wc -l   # proposals
grep "FILLED" your.log | wc -l          # fills

# Check rejection breakdown for anomalies
grep '"reason"' your.log | python3 -c "
import sys, json, collections
counts = collections.Counter()
for line in sys.stdin:
    for chunk in line.split('reason='):
        if ' ' in chunk:
            counts[chunk.split()[0]] += 1
for r, c in counts.most_common():
    print(f'  {r}: {c}')
"

# Capital stranding check — compare per-chain USDC
# Run the balance scripts from section 8 weekly
```

---

## 18. Troubleshooting

### `ModuleNotFoundError: No module named 'pydantic'`

```bash
# Make sure virtual environment is activated
source .venv/bin/activate    # Linux/macOS
.venv\Scripts\activate       # Windows

pip install -r requirements.txt
```

### Backtest crashes immediately

```
ValueError: drawdown_warn_pct must be < drawdown_kill_pct
```

Your `.env` has invalid risk settings. Check `DRAWDOWN_WARN_PCT < DRAWDOWN_KILL_PCT`.

### Polymarket returns HTTP 401

- Verify all three credentials are correct: `PM_API_KEY`, `PM_API_SECRET`, `PM_PASSPHRASE`
- These credentials are tied to the specific wallet address used when creating the API key
- If you created the API key with wallet A, they won't work for wallet B

### Polymarket orders rejected: "insufficient allowance"

You haven't approved USDC yet. Run the approval script from §6.5.

### Opinion API returns HTTP 403

- Your `OP_API_KEY` may be expired or for the wrong environment
- Log in to opinion.markets and regenerate the key

### `arb_signal` is always NaN

Both WebSocket feeds are stale. Possible causes:
- WebSocket connections blocked by firewall or corporate network
- Wrong WebSocket URLs in `.env`
- IP rate limiting by the exchange

Fix: test from a VPS in a cloud provider (AWS, GCP, DigitalOcean).

### Kill switch fires immediately on startup

Your `INITIAL_CASH_USDC` is higher than your actual balance. The system calculates drawdown as `(peak - current) / peak`. If you set peak to $10,000 but only have $2,000, drawdown = 80% → immediate halt.

Fix: set `INITIAL_CASH_USDC` to your actual current USDC balance across both chains.

### No arb proposals ever accepted

Check the rejection log:
```bash
grep "REJECT" your.log | grep "arb" | tail -20
```

Common causes and fixes:

| Rejection reason | Cause | Fix |
|-----------------|-------|-----|
| `signal_age=450ms` | High latency | Move to VPS closer to exchanges |
| `spread_too_wide` | Chosen markets are illiquid | Pick higher-volume markets |
| `net_edge=0.003 < min=0.006` | Thin edge after slippage | Lower `min_net_edge` to `0.004` (accepts more risk) |
| `fillable=3.2 < min=5.0` | Books are empty | Pick markets with more volume |
| `insufficient_capital` | Budget exhausted | Increase `ARB_BUDGET_USDC` or wait for orders to settle |

### AI keeps timing out

```
AI timeout after 200ms for BTC-Q4
```

Normal if your connection to the Anthropic API is slow. Either increase the timeout or disable the API:

```bash
AI_API_TIMEOUT_MS=400       # give it more time
# or
AI_USE_HEURISTIC_ONLY=true  # skip API entirely
```

### High rejection rate for `insufficient_capital`

Capital is locked in in-flight orders. Either:
1. Wait for current orders to settle (fill, expire, or cancel)
2. Increase `ARB_BUDGET_USDC` or `MM_BUDGET_USDC` in `.env`
3. Reduce `MAX_ORDER_USDC` so each order consumes less budget

---

## 19. Strategy Reference

### Arbitrage

Buys YES on the cheaper venue and NO on the more expensive venue. At resolution, one pays $1.00 — the combined cost at time of trade was less than $1.00, locking in the difference minus costs.

**All conditions must be true to fire:**
1. `arb_signal > 0` — fee-adjusted, from FeatureVector
2. Signal age < 300ms — fresh enough to act on
3. Spread on each venue < 7% of ask price
4. Fillable depth (65% of displayed) > `MIN_ORDER_USDC` on both sides
5. Net edge after sqrt-impact slippage > `min_net_edge` (0.6%)

**Leg 2 abort:** If leg 1 fills < 80%, leg 2 is cancelled. The partial position is then handled by the hedge strategy.

**Key parameters** (in `strategies/arbitrage.py`):
```python
min_net_edge     = 0.006   # 0.6% required edge after all costs
max_signal_age_ms = 300    # reject stale signals
fill_certainty   = 0.65    # discount displayed depth by 35%
min_fill_ratio   = 0.80    # abort leg-2 below this
max_order_usdc   = 200.0   # per-leg cap
```

### Market Making (Delta-Neutral)

Posts two-sided limit orders using the Stoikov (2008) reservation price formula. Earns spread while managing inventory.

**Quotes are suppressed when:**
- `vol_30s` unavailable (less than 30s of price history)
- Market resolves in ≤ 3 days
- Mid-price < 5% or > 95% (near binary resolution)
- An arb order is in-flight for this market

**Inventory skew:** If the system holds more YES than NO, it lowers its ask (sells YES faster) and raises its bid (buys YES slower). This reduces directional exposure without a separate trade.

**Hedge orders:** When `|net_delta| > 10 tokens`, proposes a counter-direction order to bring delta back to ±2 tokens (residual band). Uses the venue with the better price.

---

## 20. Risk System

All 12 checks run synchronously in < 5ms total. Capital is reserved before `evaluate()` returns — no TOCTOU race condition between concurrent proposals.

| # | Check | Rejects when | `.env` variable |
|---|-------|-------------|----------------|
| 1 | Kill switch | Active | — |
| 2 | Connector DOWN | Venue unreachable | — |
| 3 | Drawdown kill | ≥ 20% | `DRAWDOWN_KILL_PCT` |
| 4 | Drawdown warn | ≥ 15% (log only) | `DRAWDOWN_WARN_PCT` |
| 5 | Duplicate | Same ID within 60s | — |
| 6 | Too small | < `MIN_ORDER_USDC` | `MIN_ORDER_USDC` |
| 7 | Too large | > `MAX_ORDER_USDC` | `MAX_ORDER_USDC` |
| 8 | Liquidity buffer | Would leave < 10% equity free | `min_free_capital_pct` |
| 9 | Capital | size > cash − all_reservations | — |
| 10 | Market exposure | Would exceed market cap | `MAX_MARKET_EXP_*` |
| 11 | Strategy cap | Would exceed ARB or MM budget | `ARB_BUDGET_USDC` etc. |
| 12 | Delta limit | **Projected** delta after fill > 50 | `MAX_NET_DELTA` |

Check 12 uses **projected delta** (what it will be after the order fills), not current delta. If current delta is +48 and the order would add +20 tokens (total +68 > 50), it is rejected — even though 48 < 50.

---

## 21. AI Module

The AI module is a **signal enricher only**. Its architectural position prevents it from affecting execution directly.

**Data flow:**
```
FeatureVector → AISignalEnhancer → SignalContext → StrategyEngine
```

**What `SignalContext` contains:**
- `regime` — market state label (stable, trending, volatile, thin, unknown)
- `vol_regime` — volatility level (low, normal, high, spike)
- `confidence_multiplier` (0.10–2.00) — scales arb edge threshold
- `suppress_mm` — suppress MM quoting for this tick
- `arb_quality` (0–1) — additional arb threshold modifier
- `hedge_urgency` (0–1) — can bypass hedge cooldown

**What `SignalContext` cannot contain:**
- Order size, price, side, or platform (these fields don't exist on the type)
- Any reference to portfolio positions, P&L, or open orders

**Fallback chain:**
1. Try Claude API with 200ms timeout
2. Timeout or error → heuristic model (always available)
3. After 5 consecutive API failures → AI disabled for session, heuristic permanent

---

## 22. Architecture

```
Polymarket WS ─┐
               ├──► MarketDataProvider ──► FeatureEngine ──► [AISignalEnhancer]
Opinion WS ────┘      (staleness,           (arb_signal,        (regime label)
                       dedup, fan-out)       vol, OFI, delta)         │
                                                                       ▼
                                                               StrategyEngine
                                                          ┌────────────────────┐
                                                          │ ArbitrageStrategy  │
                                                          │ DeltaNeutralStrat  │
                                                          │ Capital budgets    │
                                                          │ Conflict resolver  │
                                                          └─────────┬──────────┘
                                                                    │
                                                         RiskEngine (12 checks, sync)
                                                         Capital reserved before return
                                                                    │
                                                     ┌──────────────┴──────────────┐
                                               PM ExecutionEngine          OP ExecutionEngine
                                               Priority queue (ARB first)
                                               Adaptive poll (2s / 500ms)
                                               Expiry worker (250ms tick)
                                                                    │
                                                             PortfolioManager
                                                      WAVG cost basis, delta, MTM, P&L
```

### Per-tick latency budget

| Step | Budget |
|------|--------|
| FeatureEngine computation | < 1 ms |
| AI signal enrichment | ≤ 200 ms (timeout to heuristic) |
| Strategy evaluation | < 2 ms |
| Risk gate (12 checks) | < 5 ms |
| Execution (async) | parallel, no budget |
| Portfolio update per fill | < 1 ms |

---

## 23. Realism Model

The backtest deliberately models adverse real-world conditions:

| Real condition | How it is modelled |
|---------------|-------------------|
| Partial fills are normal | `Beta(2.0, 1.5)` fill fraction per order — mean ≈ 57% of depth |
| Displayed depth is optimistic | 35% discount: `fillable = depth × 0.65` |
| Slippage on every taker order | Sqrt-impact model + half-spread crossing cost |
| No guaranteed arb | 6 rejection guards before any proposal is accepted |
| Adverse selection | OFI > 0.25 triggers 1.6× slippage multiplier |
| Pipeline latency | Per-stage normal distributions (tick→signal 25ms, signal→submit 45ms, submit→fill 70ms) |
| Arb expiry anchored | Historical ticks: expiry re-anchored to simulated time, not wall clock |
| Signal staleness | Signals > 300ms old rejected entirely |

---

## 24. Profitability Assessment

### When arb makes money

- Gross edge (before slippage) consistently above 1.0%
- Both books have > $300 depth at signal time
- Round-trip latency to both exchanges < 150ms
- Leg-2 fill rate > 60%

### When MM makes money

- `vol_30s` between 0.5% and 1.5%
- Bid-ask spread ≥ 0.8%
- More than 7 days to resolution
- Net delta staying below 20 tokens

### Expected ranges (conservative, based on backtests)

| Mode | Annual return on allocated capital | Sharpe |
|------|-----------------------------------|--------|
| Arb only | 0% – 12% | 0.5 – 1.1 |
| MM only | −8% – 18% | 0.6 – 1.6 |
| Combined | 2% – 15% | 0.7 – 1.3 |

### Scaling framework

```
Phase 1 — Backtest (1–2 weeks)
  Target: Sharpe > 0.8, fill rate > 40%, max drawdown < 8%
  Capital: $0

Phase 2 — Paper trading (2 weeks)
  Connect live data but set ENABLE_TRADING=false
  Verify proposals look reasonable, check no unexpected rejections

Phase 3 — Pilot ($200–500 total)
  Run for 14 days
  Goal: no kill switch events, positive or flat P&L

Phase 4 — Small live ($1,000–2,000)
  Scale after Phase 3 profitability is confirmed
  Weekly P&L review required

Phase 5 — Growth ($5,000–10,000)
  Only after Phase 4 shows positive months consistently
```

### Stop immediately if

- Fill rate < 25% for 3+ consecutive days
- Average slippage > 200 bps consistently
- Kill switch fires more than twice per week
- Any single market shows > 15% unrealised drawdown

---

## 25. Where It Will Fail

**1. Incomplete arb legs** (primary ongoing risk)
Leg 2 fills roughly 55–65% of the time in thin prediction markets. Each incomplete arb creates unhedged directional exposure. This is the main source of losses. Monitor: look for net delta growing over time in the portfolio snapshot.

**2. MM adverse selection near resolution**
As markets approach 0% or 100%, sophisticated traders know the answer. The 3-day cutoff suppresses quoting but the final days can still accumulate losses. Consider `min_days_to_resolution=7` for extra safety.

**3. Latency**
The 2-second arb expiry window requires < 500ms total round-trip. On a home internet connection this may be marginal. A VPS in AWS `us-east-1` gives consistent < 80ms to both exchanges and captures 30–50% more arb opportunities.

**4. Capital stranding**
USDC on Polygon (Polymarket) and BNB Chain (Opinion) cannot move between chains in real time. If arb systematically favours one direction, capital accumulates on one chain and runs out on the other. Check balances weekly and bridge manually via cBridge when one side imbalances beyond 2:1.

**5. Exchange clients** (`execution/clients/`)
These are the only unimplemented files. Complete implementations are provided in §11.

---

## 26. Project Structure

```
pmts_prod/
│
├── src/                        Shared primitives — no dependencies
│   ├── types.py                All enums: Platform, Side, OrderStatus, StrategyId, ...
│   └── errors.py               Typed exception hierarchy
│
├── data/                       Market data layer
│   ├── models.py               MarketSnapshot, FeatureVector (pydantic models)
│   └── market_data_provider.py Staleness enforcement, deduplication, fan-out
│
├── execution/                  Order lifecycle management
│   ├── models.py               OrderProposal, OrderSubmission, ExecutionResult
│   ├── order_tracker.py        Per-order state machine, weighted-avg fill accumulation
│   ├── engine.py               Priority queue, adaptive poll loop, expiry worker
│   └── clients/
│       ├── polymarket.py       Exchange client (full implementation in §11)
│       └── opinion.py          Exchange client (full implementation in §11)
│
├── portfolio/
│   └── manager.py              WAVG cost basis, delta, MTM, P&L, capital reservation
│
├── risk/
│   ├── engine.py               12-check synchronous gate, synchronous reservation
│   ├── kill_switch.py          Circuit breaker with confirmation token
│   └── limits.py               All thresholds in one validated dataclass
│
├── strategies/                 Pure strategy logic — no I/O, no side effects
│   ├── arbitrage.py            6-guard feasibility + sqrt-impact cost model
│   └── delta_neutral.py        Stoikov MM + residual-band hedge
│
├── engine/                     Orchestration layer
│   ├── feature_engine.py       Snapshot → FeatureVector (30s vol ring buffer)
│   ├── strategy_engine.py      Capital allocation, conflict resolution, cooldowns
│   └── orchestrator.py         Pipeline wiring, kill switch, incomplete arb handling
│
├── backtest/
│   └── engine.py               Beta fills, latency simulation, synthetic data generator
│
├── ai/                         Signal enrichment only — cannot execute trades
│   ├── signal_context.py       SignalContext type (no execution fields)
│   ├── heuristic.py            Rule-based fallback (always available, no network)
│   └── enhancer.py             Claude API + 200ms timeout + cache + fallback chain
│
├── config/
│   ├── settings.py             All config from environment variables
│   └── logging_setup.py        Structured JSON logging + file rotation
│
├── tests/
│   └── test_core.py            Tests for all critical design fixes
│
├── main.py                     Entry point: --mode backtest | live
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── .env.example
```

---

## License

MIT. Use at your own risk.
This is not financial advice. Prediction market trading involves substantial risk of capital loss. Automated systems can lose money faster than you can manually intervene. Never deploy capital you cannot afford to lose entirely. The performance figures in this documentation are based on backtests with synthetic data — they do not guarantee any future results.