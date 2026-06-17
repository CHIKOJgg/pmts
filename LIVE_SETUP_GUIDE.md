# PMTS Live Setup Guide

## Prerequisites
- Python 3.11+
- PostgreSQL 15+
- Docker & Docker Compose
- Polymarket & Opinion Markets accounts

## Quick Start
```bash
git clone <repo-url>
cd polymarket-arbitrage
cp .env.example .env
# Edit .env with credentials
python main.py --mode backtest --ticks 200 --capital 10000
```