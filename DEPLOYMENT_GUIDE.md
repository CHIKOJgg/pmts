# PMTS Deployment Guide — Пошаговое руководство по развёртыванию

## Содержание
1. [Что такое PMTS?](#1-что-такое-pmts)
2. [Предварительные требования](#2-предварительные-требования)
3. [Установка и настройка окружения](#3-установка-и-настройка-окружения)
4. [Конфигурация (.env) — подробно](#4-конфигурация-env--подробно)
5. [Market Registry — реестр рынков](#5-market-registry--реестр-рынков)
6. [Режимы запуска](#6-режимы-запуска)
7. [Observability — мониторинг и управление](#7-observability--мониторинг-и-управление)
8. [Docker + Docker Compose](#8-docker--docker-compose)
9. [Systemd — запуск как служба](#9-systemd--запуск-как-служба)
10. [PostgreSQL — настройка БД](#10-postgresql--настройка-бд)
11. [Pre-launch checklist](#11-pre-launch-checklist)
12. [Troubleshooting — частые проблемы](#12-troubleshooting--частые-проблемы)
13. [Логирование и отладка](#13-логирование-и-отладка)
14. [Бэкапы и восстановление](#14-бэкапы-и-восстановление)

---

## 1. Что такое PMTS?

**PMTS** (Prediction Market Trading System) — арбитражная система для торговли на рынках предсказаний. Она ищет ценовые расхождения между двумя биржами — **Polymarket** и **Opinion Markets** — и автоматически заключает арбитражные сделки, а также выполняет маркет-мейкинг.

Архитектура:

```
Polymarket WS ─┐
                ├─ MarketDataProvider ─┐
Opinion WS ─────┘                      │
                                       ├─ StrategyEngine (арбитраж + MM) ─┐
Polymarket REST ─┐                     │                                   ├─ RiskEngine ─┐
                  ├─ ExecutionEngine ──┘                                   │              ├─ Orchestrator
Opinion REST ─────┘                                                        │              │
                                                                           ├─ KillSwitch  │
PortfolioManager ──────────────────────────────────────────────────────────┘              │
                                                                                          │
ObservabilityServer (порт 8080) ──────────────────────────────────────────────────────────┘
```

---

## 2. Предварительные требования

| Компонент | Версия | Зачем |
|-----------|--------|-------|
| **Python** | 3.11+ | Система написана на Python с asyncio |
| **Git** | любая | Для клонирования репозитория |
| **SQLite** | встроен в Python | Хранилище по умолчанию (портфель, резервации, kill switch) |
| **PostgreSQL** | 14+ (опционально) | Для продакшена вместо SQLite |
| **Redis** | 7+ (опционально) | Кэширование рыночных данных |
| **Docker** | любая (опционально) | Контейнеризация |

**Проверка:**

```powershell
python --version  # >= 3.11
git --version
sqlite3 --version
```

---

## 3. Установка и настройка окружения

### Шаг 1: Клонирование

```bash
git clone <url-вашего-репозитория>
cd polymarket-arbitrage
```

Если у вас zip-архив — просто распакуйте и перейдите в папку.

### Шаг 2: Виртуальное окружение

Зачем: изолировать зависимости проекта от системного Python.

**Windows (PowerShell):**
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

**Linux/macOS:**
```bash
python -m venv .venv
source .venv/bin/activate
```

После активации в начале строки появится `(.venv)`.

### Шаг 3: Установка зависимостей

```bash
pip install -r requirements.txt
```

**requirements.txt** (создаётся, если отсутствует):
```
aiohttp>=3.14
websockets>=16.0
pydantic>=2.13
python-dotenv>=1.0
eth-account>=0.13
pytest>=9.0
pytest-asyncio>=1.4
```

Что ставится:
- **aiohttp** — HTTP-клиент для REST API бирж
- **websockets** — подключение к WebSocket-каналам для получения цен в реальном времени
- **pydantic** — валидация данных (внутренняя, не используется напрямую)
- **python-dotenv** — загрузка .env (установлена, но НЕ используется автоматически)
- **eth-account** — работа с Ethereum-кошельками (подпись транзакций)
- **pytest / pytest-asyncio** — для запуска тестов

### Шаг 4: Проверка установки

```bash
python -c "import aiohttp, websockets, pydantic; print('OK')"
```

Если ошибок нет — всё установлено корректно.

### Шаг 5: Запуск тестов (рекомендуется)

```bash
python -m pytest tests/ -v
```

Ожидается: **313 тестов, все PASSED**. Если какие-то падают — проверьте, что виртуальное окружение активировано и все зависимости установлены.

---

## 4. Конфигурация (.env) — подробно

### Шаг 1: Создание .env

```bash
cp .env.example .env
```

### Шаг 2: Минимальная настройка (для paper-offline и backtest)

Для режимов, не требующих подключения к биржам, нужно всего 3 параметра:

```ini
# Какие рынки торговать (логические ID, разделённые запятыми)
MARKETS=BTC-Q4,ETH-Q1,SOL-Q2

# Токен для аварийного выключателя (Kill Switch)
# Требования: >= 16 символов, минимум 2 типа символов из:
#   - заглавные буквы (A-Z)
#   - строчные буквы (a-z)
#   - цифры (0-9)
#   - спецсимволы (!@#$%^&*)
KILL_SWITCH_TOKEN=MySecureToken1234!@#$

# Стартовый капитал в USDC (для бумажного счёта)
INITIAL_CASH_USDC=10000
```

**Почему нужны эти параметры:**
- `MARKETS` — какие рынки система будет отслеживать и торговать. Если не указать — `settings.validate()` упадёт с ошибкой.
- `KILL_SWITCH_TOKEN` — защита от случайной/злонамеренной остановки. Kill Switch — это механизм аварийного глушения всех позиций. Токен подтверждает, что команда "стоп" отдана авторизованным лицом.
- `INITIAL_CASH_USDC` — виртуальный баланс, с которого начинает бумажный счёт.

### Шаг 3: Дополнительные параметры (с пояснениями)

Все параметры можно посмотреть в `.env.example`. Ниже — ключевые с пояснениями:

```ini
# ── Настройки стратегии ──
ENABLE_TRADING=false         # true = реальные ордера (только для live-режима!)
ENABLE_ARB=true              # включить арбитражную стратегию
ENABLE_MM=true               # включить маркет-мейкинг
ENABLE_HEDGE=true            # включить хеджирование дельты

# ── Риск-менеджмент ──
DRAWDOWN_KILL_PCT=0.20       # просадка 20% → Kill Switch активируется автоматически
DRAWDOWN_WARN_PCT=0.15       # просадка 15% → предупреждение в логи
MAX_ORDER_USDC=200           # максимальный размер одного ордера
MIN_ORDER_USDC=1.0           # минимальный размер ордера
MAX_NET_DELTA=50             # максимальная чистая дельта на рынок (в штуках контрактов)
ARB_BUDGET_USDC=2000         # бюджет на арбитраж
MM_BUDGET_USDC=3000          # бюджет на маркет-мейкинг
MIN_NET_EDGE=0.006           # минимальный чистый спред для арбитража (0.6%)
                             # Если спред между биржами меньше — сделка не заключается

# ── AI-усилитель сигналов (опционально) ──
AI_ENABLED=false             # включить AI-усиление сигналов (требует API-ключа)
AI_PROVIDER=anthropic        # "anthropic" (Claude) или "openrouter"
ANTHROPIC_API_KEY=           # API-ключ Claude
# ── Или OpenRouter ──
# AI_PROVIDER=openrouter
# OPENROUTER_API_KEY=sk-...
# OPENROUTER_MODEL=anthropic/claude-sonnet-4

# ── Логирование ──
LOG_LEVEL=INFO               # DEBUG, INFO, WARNING, ERROR
LOG_FORMAT=json              # "json" или "text"
LOG_FILE=logs/pmts.log       # путь к файлу лога (пусто = только stdout)

# ── Оповещения (опционально) ──
ALERT_SLACK_WEBHOOK=         # Slack webhook URL для алертов
ALERT_EMAIL_RECIPIENTS=      # email-адреса через запятую
```

### Шаг 4: Live-режим (полные credentials)

Когда будете готовы к реальной торговле:

```ini
# Polymarket
PM_API_KEY=ваш_api_key
PM_API_SECRET=ваш_api_secret
PM_PASSPHRASE=ваша_парольная_фраза
PM_WALLET_KEY=ваш_приватный_ключ_кошелька
PM_CLOB_URL=https://clob.polymarket.com
PM_WS_URL=wss://ws-subscriptions-clob.polymarket.com/ws/market
PM_TAKER_FEE_BPS=20          # комиссия тейкера в базисных пунктах (0.2%)
PM_SANDBOX=false             # true = тестовая сеть Polymarket

# Opinion Markets
OP_API_KEY=ваш_api_key
OP_WALLET_KEY=ваш_приватный_ключ
OP_CTF_EXCHANGE_ADDR=0x...   # адрес контракта CTF Exchange
OP_REST_URL=https://api.opinion.markets/v1
OP_WS_URL=wss://ws.opinion.markets
OP_TAKER_FEE_BPS=25          # комиссия тейкера (0.25%)

# Включение реальной торговли:
ENABLE_TRADING=true

# База данных (рекомендуется PostgreSQL для продакшена)
DATABASE_URL=postgresql://pmts:pass@localhost:5432/pmts
```

### Шаг 5: Безопасное хранение секретов

Вместо указания ключей прямо в `.env` можно использовать файловые переменные:

```ini
PM_API_KEY_FILE=/run/secrets/pm_api_key
```

Система прочитает содержимое файла. Это удобно при использовании Docker Swarm Secrets или Kubernetes Secrets.

---

## 5. Market Registry — реестр рынков

### Что это и зачем

Market Registry (`market_registry.json`) — это JSON-файл, который связывает логические ID рынков (например, `BTC-Q4`) с реальными ID на каждой бирже.

**Зачем он нужен:**
- Polymarket и Opinion Markets используют разные идентификаторы для одного и того же рынка
- Для подписки на WebSocket-каналы нужны CLOB token ID от Polymarket и venue ID от Opinion
- Система использует registry для построения маршрутизации ордеров

### Структура

```json
{
  "BTC-Q4": {
    "polymarket": "pm_btc_q4",
    "opinion": "op_btc_q4",
    "question": "Will BTC be above $X by Q4?",
    "pm_yes_token": "0x...токен_YES...",
    "pm_no_token": "0x...токен_NO...",
    "pair_score": 0.95
  },
  "ETH-Q1": {
    "polymarket": "pm_eth_q1",
    "opinion": "op_eth_q1",
    "question": "Will ETH be above $Y by Q1?",
    "pm_yes_token": "0x...",
    "pm_no_token": "0x...",
    "pair_score": 0.92
  }
}
```

**Поля:**
- `polymarket` — venue ID на Polymarket (строка)
- `opinion` — venue ID на Opinion Markets (строка)
- `question` — описание рынка (для логов)
- `pm_yes_token` — CLOB token ID YES-исхода (нужен для WebSocket-подписки)
- `pm_no_token` — CLOB token ID NO-исхода
- `pair_score` — оценка корреляции пары (0.0-1.0), используется для фильтрации

### Способ загрузки

1. Автоматически из `market_registry.json` в корне проекта (если файл существует)
2. Из переменной окружения `MARKET_REGISTRY_JSON` (JSON-строка)
3. Из `MARKET_REGISTRY_PATH` (путь к файлу)

### Валидация

При запуске система проверяет:
- Все ли обязательные ключи присутствуют (`polymarket`, `opinion`, `question`)
- Нет ли дублирующихся ID бирж
- Нет ли неизвестных ключей

**Важно:** Для работы WebSocket-режимов (`paper`, `live`) нужны реальные CLOB token IDs от Polymarket. Если registry не указан или в нём нет `pm_yes_token` — система выдаст предупреждение `"No market registry configured — cannot resolve CLOB token IDs"` и не сможет подписаться на обновления цен.

---

## 6. Режимы запуска

У системы 5 режимов. Выбираются через `--mode` в `main.py`.

### 6.1 Backtest — историческое тестирование

**Назначение:** Проверить стратегию на синтетических или исторических данных. Не требует никаких credentials.

**Как работает:**
Генерируются синтетические тики цен для каждого рынка. На каждом тике система оценивает арбитражные возможности, применяет риск-менеджмент и "исполняет" сделки. Результат — P&L, Sharpe ratio, max drawdown.

```bash
# Быстрый тест (200 тиков — несколько секунд)
python main.py --mode backtest --ticks 200 --capital 10000

# Полноценный тест
python main.py --mode backtest --ticks 5000 --capital 10000

# С детальным логированием
python main.py --mode backtest --ticks 200 --log-level DEBUG --verbose
```

**Что смотреть в выводе:**
```
=== Backtest Results ===
Total Return: 1.57%          # прибыль за период
Total PnL: $157.32           # в долларах
Sharpe Ratio: 0.89           # > 1.0 — хорошо, > 2.0 — отлично
Max Drawdown: -3.21%         # максимальная просадка
Fill Rate: 78.5%             # процент исполнения ордеров
Total Proposals: 143         # сколько арбитражных возможностей найдено
Approved: 38                 # сколько пропущено риск-менеджментом
```

**Типичные проблемы:**
- Risk Engine отклоняет 60-70% предложений из-за `delta_limit` (50 max delta, projected часто 100-400)
- SOL-Q2 даёт ~20 bps спреда между биржами
- BTC-Q4 — самые узкие спреды

### 6.2 Sweep — подбор гиперпараметров

**Назначение:** Автоматически перебрать комбинации параметров стратегии и найти оптимальные.

**Как работает:**
Перебирает все комбинации `min_net_edge`, `max_order_usdc`, `drawdown_kill_pct`, `arb_budget_usdc` и для каждой запускает backtest. Выводит таблицу результатов и лучшую конфигурацию.

```bash
python main.py --mode sweep --ticks 500 --capital 10000 \
  --sweep-min-edge 0.003 0.006 0.01 \
  --sweep-max-order 100 200 400 \
  --sweep-dd-kill 0.15 0.20 0.25 \
  --sweep-arb-budget 1000 2000 4000
```

**Пояснение параметров:**
- `--sweep-min-edge` — какие значения минимального спреда перебирать
- `--sweep-max-order` — максимальный размер ордера
- `--sweep-dd-kill` — порог просадки для Kill Switch
- `--sweep-arb-budget` — бюджет на арбитраж

**Результат:**
```
Best params: min_net_edge=0.006, max_order_usdc=200, dd_kill=0.20, arb_budget=2000
Return: 2.15%  Sharpe: 1.23
```

### 6.3 Paper-Offline — бумажная торговля без сети

**Назначение:** Проверить систему в "боевом" режиме, но без подключения к биржам. Все данные — синтетические.

**Как работает:**
- Создаются синтетические потоки цен (аналогично backtest)
- Запускаются все компоненты: Orchestrator, RiskEngine, PortfolioManager, ExecutionEngine (с PaperTradingClient), ObservabilityServer
- Ордера "исполняются" с вероятностью `--paper-fill-prob` (по умолчанию 85%)
- Работает Kill Switch, резервации капитала, все метрики

```bash
python main.py --mode paper-offline --paper-fill-prob 0.85
```

**Когда использовать:**
- Для отладки перед live
- Для интеграционного тестирования всех компонентов
- Для оценки работы риск-менеджмента в реальном времени

**Проверка работы:**
```bash
# В другом окне терминала:
curl http://localhost:8080/health     # статус системы
curl http://localhost:8080/metrics    # метрики
```

### 6.4 Paper — бумажная торговля с реальными данными

**Назначение:** Получать реальные цены через WebSocket, но не выставлять реальные ордера.

**Как работает:**
- Подключается к Polymarket WS и Opinion WS
- Использует PaperTradingClient для симуляции исполнения
- Требует `market_registry.json` с реальными CLOB token ID
- Требует подключения к интернету

```bash
python main.py --mode paper --paper-fill-prob 0.85
```

**Когда использовать:** Перед live, чтобы убедиться, что WS-каналы работают и цены приходят корректно.

**Важно:** Если в логах видите:
```
WARNING  No market registry configured — cannot resolve CLOB token IDs for WS subscription
```
— значит `market_registry.json` отсутствует или не содержит `pm_yes_token`. Без него WS-подписка не работает.

### 6.5 Live — реальная торговля

**Назначение:** Боевой режим. Выставляет реальные ордера на биржах.

**Требования:**
- Все API-ключи и wallet keys в `.env`
- `ENABLE_TRADING=true`
- Достаточный баланс USDC на обеих биржах
- `market_registry.json` с корректными ID

```bash
python main.py --mode live
```

**Перед запуском (обязательно):**
1. Проверьте тесты: `python -m pytest tests/ -v`
2. Запустите paper-offline: `python main.py --mode paper-offline`
3. Запустите paper с реальными данными: `python main.py --mode paper`
4. Проверьте, что Kill Switch настроен и работает
5. Убедитесь, что `ENABLE_TRADING=false` в `.env` перед live — осознанно переключите на `true`

---

## 7. Observability — мониторинг и управление

Система запускает HTTP-сервер на порту **8080** (настраивается через `OBSERVABILITY_PORT` и `OBSERVABILITY_BIND_HOST`).

### Health Check

```bash
curl http://localhost:8080/ready
# {"status": "ok", "uptime": 12345, "mode": "paper"}
```

Используется Docker'ом для HEALTHCHECK.

### Kill Switch — аварийный выключатель

Kill Switch — механизм экстренной остановки всех торговых операций.

**Как работает:**
- При активации: все открытые ордера отменяются, новые не создаются, позиции закрываются
- Может активироваться автоматически при просадке (`DRAWDOWN_KILL_PCT`)
- Может активироваться вручную через API с подтверждением токеном
- Состояние сохраняется в SQLite/PostgreSQL — после перезапуска системы Kill Switch остаётся активным

**API:**
```bash
# Проверить статус
curl http://localhost:8080/kill-switch/status
# {"active": false, "reason": null}

# Активировать (аварийная остановка)
curl -X POST http://localhost:8080/kill-switch/activate \
  -H "Content-Type: application/json" \
  -d '{"token": "MySecureToken1234!@#$", "reason": "manual"}'

# Сбросить (только с токеном)
curl -X POST http://localhost:8080/kill-switch/reset \
  -H "Content-Type: application/json" \
  -d '{"token": "MySecureToken1234!@#$", "operator_id": "admin"}'
```

### Метрики

```bash
curl http://localhost:8080/metrics
```

Возвращает JSON с ключевыми метриками:
- **orchestrator:** количество предложений (evaluated/approved/rejected)
- **risk:** статус Kill Switch, текущая просадка
- **portfolio:** общая стоимость портфеля, свободный USDC, зарезервированный капитал
- **market_data:** количество полученных снепшотов, устаревших данных, дедуплицированных сообщений

### Prometheus + Grafana (Docker Compose)

В `docker-compose.yml` настроены:
- **Prometheus** (порт 9090) — сбор метрик
- **Grafana** (порт 3000) — визуализация с готовым дашбордом

Настройки:
- Конфигурация Prometheus: `docs/prometheus.yml`
- Дашборд Grafana: `docs/grafana-dashboard.json`
- Data source provision: `docs/grafana-provisioning/`

---

## 8. Docker + Docker Compose

### Dockerfile

```dockerfile
FROM python:3.11-slim

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libssl-dev curl && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN useradd -m pmts && chown -R pmts:pmts /app
USER pmts

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8080/ready || exit 1

ENTRYPOINT ["python", "main.py"]
CMD ["--mode", "live"]
```

**Пояснения:**
- `python:3.11-slim` — минимальный образ
- `gcc libssl-dev` — нужны для сборки eth-account
- `curl` — для HEALTHCHECK
- `pmts` — непривилегированный пользователь (безопасность)
- `PYTHONUNBUFFERED=1` — логи сразу видны в docker logs

### Docker Compose (полный стек)

```yaml
services:
  pmts:
    build: .
    env_file: .env
    ports: ["8080:8080"]
    volumes: ["./logs:/app/logs"]
    restart: unless-stopped
    command: ["--mode", "paper"]  # или live
    deploy:
      resources:
        limits: { memory: 256M }
        reservations: { memory: 128M }

  prometheus:    # опционально
  grafana:       # опционально
  pmts-backtest: # разовый запуск: docker compose --profile backtest up pmts-backtest
```

**Запуск:**
```powershell
# Paper-режим (для начала)
docker compose up -d pmts

# Проверка
curl http://localhost:8080/health

# Логи
docker compose logs -f pmts

# Остановка
docker compose down

# Полный стек с мониторингом
docker compose up -d pmts prometheus grafana

# Разовый backtest
docker compose --profile backtest up pmts-backtest
```

---

## 9. Systemd — запуск как служба (Linux)

Для продакшн-сервера (Ubuntu/Debian):

**Файл:** `/etc/systemd/system/pmts.service`

```ini
[Unit]
Description=Polymarket Arbitrage Trading System
After=network.target postgresql.service redis.service
Wants=postgresql.service redis.service

[Service]
Type=exec
User=pmts
WorkingDirectory=/opt/pmts
EnvironmentFile=/opt/pmts/.env
ExecStart=/opt/pmts/.venv/bin/python main.py --mode live
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
```

**Установка:**
```bash
sudo useradd -m pmts -s /bin/bash
sudo mkdir -p /opt/pmts
sudo cp -r . /opt/pmts/
sudo chown -R pmts:pmts /opt/pmts

# Активация службы
sudo systemctl daemon-reload
sudo systemctl enable pmts
sudo systemctl start pmts
sudo systemctl status pmts

# Просмотр логов
sudo journalctl -u pmts -f
```

---

## 10. PostgreSQL — настройка БД

### Когда нужен

SQLite подходит для одного инстанса. PostgreSQL нужен, если:
- Запускаете несколько инстансов (с одним портфелем)
- Нужна репликация и бэкапы на уровне БД
- Ожидаете высокую нагрузку (много транзакций)

### Настройка

```bash
# Создание пользователя и БД
sudo -u postgres psql <<EOF
CREATE DATABASE pmts;
CREATE USER pmts WITH ENCRYPTED PASSWORD 'secure_password';
GRANT ALL PRIVILEGES ON DATABASE pmts TO pmts;
EOF

# В .env
DATABASE_URL=postgresql://pmts:secure_password@localhost:5432/pmts
```

**Важно:** Таблицы создаются автоматически при первом запуске.

### Миграция с SQLite на PostgreSQL

При смене БД портфель и резервации будут пустыми. Kill Switch состояние тоже сбросится (будет неактивен).

---

## 11. Pre-launch checklist

Перед запуском live убедитесь, что все пункты выполнены:

| # | Шаг | Команда/проверка | Пояснение |
|---|-----|------------------|-----------|
| 1 | Тесты | `python -m pytest tests/ -v` | Все 313 тестов должны проходить |
| 2 | Backtest | `python main.py --mode backtest --ticks 200` | Система находит и исполняет сделки |
| 3 | Paper-offline | `python main.py --mode paper-offline` | Запускается без ошибок, идёт обработка |
| 4 | Kill Switch токен | Проверьте длину (>=16) и сложность (>=2 типа символов) | `KILL_SWITCH_TOKEN` в `.env` |
| 5 | Market Registry | Все ли рынки имеют `polymarket` + `opinion` ID | Проверьте `market_registry.json` |
| 6 | Credentials | `python -c "from config.settings import get_settings; s=get_settings(); print(s.trading.markets)"` | Загружаются ли рынки |
| 7 | WS connectivity | Запустите `--mode paper` и проверьте логи | Должно быть "WSAdapter started" |
| 8 | Observability | `curl localhost:8080/health` | Сервер отвечает |
| 9 | Paper test (real data) | Запустите на час `--mode paper` | Нет ошибок в логах |
| 10 | Документация | Прочитайте PRODUCTION_READINESS.md | P0 все PASSED |

---

## 12. Troubleshooting — частые проблемы

### Проблема: `MARKETS list cannot be empty`
**Причина:** Не задана переменная `MARKETS` в `.env`.
**Решение:** Добавьте `MARKETS=BTC-Q4,ETH-Q1,SOL-Q2` в `.env`.

### Проблема: `KILL_SWITCH_TOKEN not set correctly`
**Причина:** Токен отсутствует, слишком короткий или недостаточно сложный.
**Решение:** Сгенерируйте токен:
```bash
# Linux/macOS
openssl rand -base64 32

# PowerShell
[Convert]::ToBase64String([System.Security.Cryptography.RandomNumberGenerator]::GetBytes(32))
```

### Проблема: `ModuleNotFoundError: aiohttp`
**Причина:** Не установлены зависимости.
**Решение:** `pip install -r requirements.txt`

### Проблема: `WebSocket getaddrinfo failed`
**Причина:** Нет подключения к интернету или блокируется firewall.
**Решение:** Используйте `--mode paper-offline` — он не требует сети.

### Проблема: `No market registry configured`
**Причина:** Отсутствует `market_registry.json` или `MARKET_REGISTRY_JSON`.
**Решение:** Создайте `market_registry.json` с корректными ID.

### Проблема: `Duplicate polymarket venue ID`
**Причина:** В `market_registry.json` два рынка имеют одинаковый `polymarket` ID.
**Решение:** Проверьте уникальность venue ID.

### Проблема: WS не подписывается
**Причина:** В registry нет `pm_yes_token` / `pm_no_token` для рынков.
**Решение:** Добавьте CLOB token IDs в `market_registry.json`.

### Проблема: Backtest ничего не предлагает
**Причина:** Risk Engine отклоняет всё из-за лимитов.
**Решение:** Проверьте `MAX_NET_DELTA` (50 по умолчанию). В `BACKTEST_RISK_LIMITS` в `main.py` стоит `max_net_delta_per_market: 10_000.0` — это нормально.

### Проблема: Система не запускается после клонирования
**Причина:** Не создано виртуальное окружение.
**Решение:**
```bash
python -m venv .venv
.venv\Scripts\Activate.ps1  # Windows
source .venv/bin/activate    # Linux
pip install -r requirements.txt
```

---

## 13. Логирование и отладка

### Формат логов

```ini
# .env
LOG_LEVEL=DEBUG
LOG_FORMAT=json    # или "text" для читаемого вида
LOG_FILE=logs/pmts.log
```

**JSON-формат** — для отправки в ELK/Graylog:
```json
{"timestamp": "2026-06-26T10:30:00Z", "level": "INFO", "logger": "orchestrator", "message": "Processing tick", ...}
```

**Text-формат** — для локальной разработки:
```
2026-06-26 10:30:00 INFO [orchestrator] Processing tick BTC-Q4
```

### Режим --verbose

```bash
python main.py --mode backtest --ticks 100 --log-level DEBUG --verbose
```

`--verbose` переключает уровень на DEBUG (даже если указан INFO).

### Ключевые логи для мониторинга

| Сообщение в логе | Что означает |
|-----------------|--------------|
| `SYSTEM LIVE TRADING mode.` | Система запущена и работает |
| `SYSTEM OFFLINE PAPER TRADING mode.` | Бумажный режим без сети |
| `Proposal APPROVED` | Арбитражная сделка прошла риск-менеджмент |
| `Proposal REJECTED` | Сделка отклонена (см. причину рядом) |
| `Kill switch ACTIVATED` | Аварийная остановка (проверьте причину) |
| `Drawdown warning: {pct}%` | Просадка превысила порог предупреждения |
| `Shutdown complete.` | Система корректно остановлена |
| `WSAdapter started` | WebSocket-подключение установлено |

---

## 14. Бэкапы и восстановление

### SQLite

```powershell
# Ручной бэкап
Copy-Item portfolio_paper.db "portfolio_paper.db.backup.$(Get-Date -Format 'yyyy-MM-dd')"

# Linux
cp portfolio_paper.db portfolio_paper.db.backup.$(date +%F)
```

### PostgreSQL

```bash
pg_dump pmts > pmts_backup_$(date +%F).sql
```

### Kill Switch

Состояние Kill Switch хранится в БД (SQLite или PostgreSQL). После перезапуска системы:
- Если Kill Switch был активен — останется активным
- Ордера не будут выставляться, пока вы явно не сбросите Kill Switch через API

Это защита: если система упала из-за аварийной ситуации, после перезапуска она продолжит быть в безопасном режиме.

### Восстановление после сбоя

```bash
# 1. Восстановить БД
# 2. Запустить в paper-offline для проверки
python main.py --mode paper-offline

# 3. Запустить paper с реальными данными
python main.py --mode paper

# 4. Сбросить Kill Switch (если активен)
curl -X POST http://localhost:8080/kill-switch/reset \
  -H "Content-Type: application/json" \
  -d '{"token": "ваш-токен", "operator_id": "admin"}'

# 5. Запустить live
python main.py --mode live
```

---

## Приложение: Быстрый старт за 5 минут

```powershell
# 1. Клонируем
git clone <url> && cd polymarket-arbitrage

# 2. Виртуальное окружение
python -m venv .venv
.venv\Scripts\Activate.ps1

# 3. Зависимости
pip install -r requirements.txt

# 4. .env
cp .env.example .env
# Редактируем .env: MARKETS, KILL_SWITCH_TOKEN

# 5. Тесты
python -m pytest tests/ -v

# 6. Backtest
python main.py --mode backtest --ticks 200 --capital 10000

# 7. Paper-offline
python main.py --mode paper-offline
```
