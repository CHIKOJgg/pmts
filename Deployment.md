# PMTS — Deployment Guide

## Деплой через Docker (единственный поддерживаемый способ)

```
  docker compose --profile backtest up pmts-backtest   # бэктест
  docker compose up pmts                                # paper-торговля
  docker compose --profile monitoring up -d pmts        # live-торговля с мониторингом
```

---

## 1. Требования

### 1.1 Технические

| Компонент | Минимум | Рекомендация |
|-----------|---------|--------------|
| Docker | 24+ | 27+ |
| Docker Compose | 2.24+ | 2.30+ |
| ОЗУ | 512 MB | 2 GB (4 GB с мониторингом) |
| Диск | 2 GB | 10 GB (для логов + история) |
| VPN | открытые 80/443 порты | выделенный VPS |

### 1.2 VPN (обязательно для live)

Контейнеры не имеют собственного VPN. Подключайте VPN **на хосте**.

**Провайдеры:**
- Mullvad, NordVPN, ExpressVPN, ProtonVPN

**Регионы (минимальная задержка к биржам):**
- Япония (Токио), Сингапур, Гонконг
- США (Нью-Йорк, Сан-Франциско)
- Европа (Франкфурт, Лондон)

**Проверка задержки:**
```bash
# до запуска контейнеров
ping gamma-api.polymarket.com
ping api.opinion.markets

# из контейнера
docker compose exec pmts curl -so /dev/null -w '%{time_total}s\n' https://gamma-api.polymarket.com
```

---

## 2. Быстрый старт

### 2.1 Клонирование и настройка

```bash
git clone <repo-url> pmts
cd pmts

# скопировать переменные окружения
cp .env.example .env
```

### 2.2 Базовый запуск (paper-торговля — без API-ключей)

```bash
docker compose build --no-cache
docker compose up pmts
```

**Ожидаемый вывод:**
```
pmts-1  | 2026-07-06 14:43:52 [INFO] Orchestrator starting...
pmts-1  | 2026-07-06 14:43:52 [INFO] Portfolio manager started
pmts-1  | 2026-07-06 14:43:52 [INFO] PM paper connector: UP
pmts-1  | 2026-07-06 14:43:52 [INFO] OP paper connector: UP
pmts-1  | 2026-07-06 14:43:52 [INFO] Orchestrator started: 3 markets, trading=false
```

### 2.3 Бэктест

```bash
docker compose --profile backtest up pmts-backtest
```

Логи сразу в консоль (текстовый формат). После завершения контейнер останавливается.

### 2.4 Live

```bash
# заполнить .env реальными ключами, потом:
docker compose --profile monitoring up -d pmts
```

---

## 3. Переменные окружения (.env)

**Скопировать и заполнить:**
```bash
cp .env.example .env
nano .env
```

### 3.1 Обязательные для live

```bash
# ── Polymarket ──
PM_API_KEY=ваш-polymarket-api-key
PM_API_SECRET=ваш-polymarket-api-secret
PM_PASSPHRASE=ваша-polymarket-passphrase
PM_WALLET_KEY=64-hex-символа-без-0x

# ── Opinion Markets ──
OP_API_KEY=ваш-opinion-api-key
OP_WALLET_KEY=тот-же-или-отдельный-кошелек
OP_CTF_EXCHANGE_ADDR=адрес-контракта-ctf-биржи

# ── Рынки ──
MARKETS=ID-рынка-1,ID-рынка-2

# ── Kill switch ──
KILL_SWITCH_TOKEN=$(openssl rand -hex 32)   # сгенерировать! сохранить в 2 местах

# ── Капитал ──
INITIAL_CASH_USDC=600         # ваш реальный баланс USDC на обеих сетях
ARB_BUDGET_USDC=120           # бюджет для арбитража
MM_BUDGET_USDC=180            # бюджет для маркет-мейкинга
```

### 3.2 Полный список

| Переменная | Обязательно | Описание |
|------------|-------------|----------|
| `PM_API_KEY` | live | Polymarket CLOB API Key |
| `PM_API_SECRET` | live | Polymarket CLOB API Secret |
| `PM_PASSPHRASE` | live | Polymarket CLOB Passphrase |
| `PM_WALLET_KEY` | live | Приватный ключ EVM-кошелька (без `0x`) |
| `PM_CLOB_URL` | — | `https://clob.polymarket.com` |
| `PM_WS_URL` | — | `wss://ws-subscriptions-clob.polymarket.com/ws/market` |
| `PM_TAKER_FEE_BPS` | — | `20` (комиссия Polymarket в bps) |
| `OP_API_KEY` | live | Opinion Markets API Key |
| `OP_WALLET_KEY` | live | Приватный ключ (тот же или отдельный) |
| `OP_CTF_EXCHANGE_ADDR` | live | Адрес контракта CTF Exchange |
| `OP_REST_URL` | — | `https://api.opinion.markets/v1` |
| `OP_WS_URL` | — | `wss://ws.opinion.markets` |
| `OP_TAKER_FEE_BPS` | — | `25` |
| `MARKETS` | live | Список ID рынков через запятую |
| `INITIAL_CASH_USDC` | live | Начальный баланс USDC |
| `KILL_SWITCH_TOKEN` | live | Токен для сброса kill switch (32+ hex) |
| `DRAWDOWN_KILL_PCT` | — | `0.20` (остановка при просадке 20%) |
| `DRAWDOWN_WARN_PCT` | — | `0.15` (предупреждение при 15%) |
| `MAX_ORDER_USDC` | — | `200` макс. размер одного ордера |
| `MIN_ORDER_USDC` | — | `1.0` мин. размер ордера |
| `MAX_MARKET_EXP_USDC` | — | `500` макс. экспозиция на рынок |
| `MAX_NET_DELTA` | — | `50` макс. нет-дельта на рынок |
| `ARB_BUDGET_USDC` | — | `2000` бюджет для арбитража |
| `MM_BUDGET_USDC` | — | `3000` бюджет для MM |
| `ENABLE_TRADING` | — | `false` (выключает всю торговлю) |
| `ENABLE_ARB` | — | `true` |
| `ENABLE_MM` | — | `true` |
| `ENABLE_HEDGE` | — | `true` |
| `AI_ENABLED` | — | `false` (включить AI-модуль) |
| `ANTHROPIC_API_KEY` | — | API-ключ Claude (если AI_ENABLED=true) |
| `LOG_LEVEL` | — | `INFO` |
| `LOG_FORMAT` | — | `json` или `text` |
| `API_TOKEN` | — | Токен для FastAPI эндпоинтов |
| `REDIS_ENABLED` | — | `false` (включить Redis для снапшотов) |
| `POSTGRES_ENABLED` | — | `false` (включить PostgreSQL для истории) |

### 3.3 НОВЫЕ переменные (добавлены в последних обновлениях)

| Переменная | Обязательно | Описание | Дефолт |
|------------|-------------|----------|--------|
| `PM_SANDBOX` | — | `true` = sandbox-среда Polymarket | `false` |
| `OP_SANDBOX` | — | `true` = sandbox-среда Opinion Markets | `false` |
| `MIN_NET_EDGE` | — | Мин. чистый edge для сделки | `0.006` |
| `HEDGE_THRESHOLD` | — | Порог хеджа (USDC) | `10.0` |
| `MM_QUOTE_SIZE_USDC` | — | Размер котировки маркет-мейкера | `25.0` |
| `SESSION_LOSS_LIMIT_USDC` | — | Лимит убытка сессии | `500.0` |
| `KILL_SWITCH_GRACE_S` | — | Грейс перед авто-kill-switch (с) | `5.0` |
| `MAX_CONCURRENT_ORDERS` | — | Макс. параллельных ордеров | `5` |
| `SUBMIT_RETRY_COUNT` | — | Число попыток отправки | `3` |
| `SUBMIT_BASE_DELAY_S` | — | Базовая задержка повтора (с) | `0.2` |
| `POLL_NORMAL_S` | — | Интервал опроса (норм.) | `2.0` |
| `POLL_FAST_S` | — | Интервал опроса (быстр.) | `0.5` |
| `STALE_THRESHOLD_MS` | — | Порог устаревания данных (мс) | `2000` |
| `MARKET_REGISTRY_JSON` | — | Альтернатива файлу `market_registry.json` (JSON-строка) | — |
| `ENABLE_COPY_TRADING` | — | Копитрейдинг чужих кошельков | `false` |
| `COPY_TRADING_BUDGET_USDC` | — | Бюджет копитрейдинга | `75.0` |
| `COPY_TRADING_MAX_PER_TRADE_USDC` | — | Макс. на сделку | `25.0` |
| `COPY_TRADING_DELAY_MS` | — | Задержка повтора (мс) | `7000` |
| `COPY_TRADING_FOLLOW_MODE` | — | `proportional`/`fixed`/`exact` | `proportional` |
| `COPY_TRADING_POLL_INTERVAL_S` | — | Интервал опроса (с) | `10.0` |
| `COPY_TRADING_MAX_PRICE_DEVIATION_PCT` | — | Макс. отклонение цены | `0.30` |
| `COPY_TRADING_DATA_URL` | — | Источник данных | `https://clob.polymarket.com` |
| `COPY_TRADING_TRACKED_WALLETS` | live | Адреса кошельков через запятую | — |
| `COPY_TRADING_EXCLUDE_MARKETS` | — | ID рынков для исключения | — |

> **Все переменные считываются из `config/settings.py`.** Актуальный полный список — в `.env.example`.

---

## 4. Market Registry

Для работы системы нужна маппинг логических названий рынков на технические ID бирж.

**Файл:** `market_registry.json` (в корне проекта)

```json
{
  "BTC-Q4": {
    "pm_yes_token": "1234567890",
    "pm_no_token": "0987654321",
    "opinion": "opinion-btc-q4-id",
    "description": "Bitcoin Q4 2024"
  },
  "ETH-Q1": {
    "pm_yes_token": "2345678901",
    "pm_no_token": "1098765432",
    "opinion": "opinion-eth-q1-id",
    "description": "Ethereum Q1 2024"
  }
}
```

**Где взять ID:**
- Polymarket: `https://gamma-api.polymarket.com/markets?limit=20`
- Opinion: `https://api.opinion.markets/v1/markets` (с API-ключом)

---

## 5. Docker

### 5.1 Основные команды

```bash
# Сборка образа
docker compose build --no-cache

# Запуск в фоне (paper-режим)
docker compose up -d

# Запуск в консоли (логи сразу видны)
docker compose up pmts

# Просмотр логов
docker compose logs pmts
docker compose logs -f pmts

# Остановка
docker compose down          # graceful stop
docker compose stop          # emergency stop

# Перезапуск
docker compose restart

# Полная пересборка + запуск
docker compose down -v && docker compose up --build
```

### 5.2 Режимы (profiles)

| Команда | Режим | Описание |
|---------|-------|----------|
| `docker compose up pmts` | **paper** | Без ключей, симуляция биржи |
| `docker compose --profile backtest up pmts-backtest` | **backtest** | Разовый запуск бэктеста |
| `docker compose --profile monitoring up -d pmts` | **live** | Реальная торговля с мониторингом |
| `docker compose --profile monitoring up -d pmts prometheus grafana` | **live+metrics** | Торговля + метрики + дашборды |
| `docker compose --profile full up` | **all** | Полный стек (с Redis/PostgreSQL) |

**Изменить параметры бэктеста (docker-compose.yml, строка 85):**
```yaml
command: ["--mode", "backtest", "--ticks", "2000", "--capital", "10000", "--verbose"]
```

**Изменить режим на live (docker-compose.yml, строка 26):**
```yaml
command: ["--mode", "live"]
```

### 5.3 Проброс портов

| Порт | Сервис | Назначение |
|------|--------|------------|
| `8080:8080` | pmts | FastAPI health + metrics |
| `9090:9090` | prometheus | Сбор метрик |
| `3000:3000` | grafana | Визуализация |

### 5.4 Healthcheck

Dockerfile содержит встроенный healthcheck:
```dockerfile
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8080/ready || exit 1
```

Проверка вручную:
```bash
docker compose ps            # статус всех контейнеров
curl http://localhost:8080/ready   # готовность приложения
```

### 5.5 Volumes (постоянные данные)

| Volume | Путь в контейнере | Назначение |
|--------|-------------------|------------|
| `./logs` | `/app/logs` | Логи приложения |
| `pmts-data` | `/app/data` | SQLite-база портфеля |
| `prometheus-data` | `/prometheus` | Метрики Prometheus |
| `grafana-data` | `/var/lib/grafana` | Данные Grafana |

---

## 6. Рабочий процесс деплоя

### 6.1 Этап 1 — Подготовка

```bash
# 1. Клонировать
git clone <repo-url> pmts && cd pmts

# 2. Настроить .env
cp .env.example .env
nano .env       # заполнить MIN_ORDER_USDC, DRAWDOWN_KILL_PCT и т.д.
# API-ключи пока не нужны — оставить пустыми

# 3. Собрать образ
docker compose build --no-cache
```

### 6.2 Этап 2 — Бэктест (проверка системы)

```bash
docker compose --profile backtest up pmts-backtest
```

Ожидаемый результат:
```
═══ BACKTEST RESULTS ═══
P&L: $+34.21 (+0.34%)
Fill rate: 52.3%
Proposals: 412 eval | 231 approved | 181 rejected
```

Не переходите к следующему шагу, если P&L отрицательный или fill rate < 30%.

### 6.3 Этап 3 — Paper-торговля (проверка live-пайплайна)

```bash
docker compose up pmts
```

Что проверяется:
- система запускается без ошибок
- коннекторы помечены как UP
- порт 8080 отвечает
- ордера создаются, заполняются, отслеживаются
- kill switch не срабатывает ложно

Остановка:
```
Ctrl+C
```

### 6.4 Этап 4 — Live (реальная торговля)

**Перед запуском:**

```bash
# 1. Заполнить .env
nano .env
# PM_API_KEY, PM_API_SECRET, PM_PASSPHRASE, PM_WALLET_KEY
# OP_API_KEY, OP_WALLET_KEY, OP_CTF_EXCHANGE_ADDR
# MARKETS, INITIAL_CASH_USDC, KILL_SWITCH_TOKEN

# 2. Включить торговлю
nano .env   # ENABLE_TRADING=true

# 3. Открыть порты в фаерволе (если есть)
# 8080/tcp — должен быть доступен только изнутри

# 4. Подключить VPN (обязательно!)
```

```bash
# Запуск
docker compose --profile monitoring up -d pmts

# Мониторинг логов
docker compose logs -f pmts | grep -E "(connector: DOWN|ERROR|CRITICAL)"

# Проверка здоровья
watch -n 5 "docker compose ps && curl -s http://localhost:8080/ready"
```

### 6.5 Этап 5 — Мониторинг (опционально)

```bash
# Запустить полный стек мониторинга
docker compose --profile monitoring up -d

# Grafana: http://localhost:3000  (admin/admin)
# Prometheus: http://localhost:9090
```

---

## 7. Мониторинг

### 7.1 Логи

Формат JSON (по умолчанию):
```bash
docker compose logs -f pmts | python3 -c "
import sys, json
for line in sys.stdin:
    try:
        log = json.loads(line)
        level = log.get('level', 'INFO')
        if level in ('ERROR', 'CRITICAL'):
            print(f'[{level}] {log.get(\"message\")}')
    except:
        pass
"
```

Фильтр по категориям:
```bash
docker compose logs -f pmts | grep '"logger":"risk.engine"'
docker compose logs -f pmts | grep '"logger":"strategies.arbitrage"'
docker compose logs -f pmts | grep '"logger":"execution.engine"'
```

Ключевые сообщения:
```
ARB ACCEPTED          — сделка прошла
REJECT ... reason=    — причина отказа
KILL SWITCH ACTIVATED — стоп-лосс сработал
connector: DOWN       — биржа недоступна
```

### 7.2 Метрики (Prometheus)

Доступны на `http://localhost:9090` при включённом профиле `monitoring`.

Основные метрики:
- `FILL_USDC_TOTAL` — объём заполненных ордеров
- `FILLS_TOTAL` — количество заполнений
- `ACTIVE_ORDERS_COUNT` — активные ордера
- `API_ERRORS_TOTAL` — ошибки API
- `ORDER_LATENCY` — задержка исполнения

### 7.3 Дашборд (Grafana)

Доступен на `http://localhost:3000`. Логин: `admin`, пароль: `admin` (изменить при первом входе).

Дашборды находятся в `docs/grafana-dashboard.json`.

---

## 8. Безопасность

### 8.1 Kill Switch Token

Генерировать на локальной машине:
```bash
openssl rand -hex 32
# или
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Требования (проверяются в `RiskEngine`):
- минимум 32 hex-символа
- не может быть пустым в live-режиме
- не может быть `CHANGE-ME`

**Сохранить в 2 местах:**
1. `.env` → `KILL_SWITCH_TOKEN=...`
2. Менеджер паролей (Bitwarden, 1Password, KeePass)

Без этого токена **невозможно сбросить kill switch** после срабатывания.

### 8.2 Приватные ключи

- `PM_WALLET_KEY` и `OP_WALLET_KEY` — приватные ключи EVM-кошельков
- Никогда не сохранять в репозиторий (`.gitignore` уже настроен)
- Использовать отдельные кошельки для торговли (не личный)
- Если ключ скомпрометирован — средства на кошельке могут быть украдены

### 8.3 API-токен (для эндпоинтов)

```bash
API_TOKEN=ваш-секретный-токен
```

Если задан, все запросы к FastAPI-эндпоинтам требуют заголовок:
```
X-API-Key: ваш-секретный-токен
```

### 8.4 Файлы

```bash
# Права доступа
chmod 600 .env
chmod 600 market_registry.json
chmod 600 .env.example   # если содержит черновики ключей
```

---

## 9. Режимы работы

### 9.1 Backtest (тестирование стратегий)

```
docker compose --profile backtest up pmts-backtest
```

- Симуляция рынка без подключения к биржам
- P&L, drawdown, fill rate, slippage
- Детерминированный результат (одинаковые параметры = одинаковый результат)
- Не требует API-ключей
- Контейнер завершается после расчёта

### 9.2 Paper (симуляция live-пайплайна)

```
docker compose up pmts
```

- Полный live-пайплайн, но ордера симулируются
- Реальные данные с бирж (через SyntheticMarketFeed)
- Fill rate ~80% на агрессивных ордерах
- Проверка всей цепочки: стратегия → риск → исполнение → портфель
- Не требует API-ключей

### 9.3 Live (реальная торговля)

```
docker compose --profile monitoring up -d pmts
```

- Реальные ордера на Polymarket и Opinion Markets
- Требует заполненные API-ключи в `.env`
- Требует VPN для доступа к биржам
- Требует настроенный `market_registry.json`

---

## 10. Мониторинг здоровья

### 10.1 Docker healthcheck

```bash
# Статус контейнера
docker compose ps

# Пример вывода:
NAME                IMAGE               COMMAND                  SERVICE             STATUS              PORTS
pmts-pmts-1         pmts:latest         "python main.py --mo…"   pmts                Up 2 minutes (healthy)   0.0.0.0:8080->8080/tcp
```

### 10.2 FastAPI эндпоинты (на порту 8080)

| Эндпоинт | Метод | Описание |
|----------|-------|----------|
| `/ready` | GET | Healthcheck (200 = OK) |
| `/metrics` | GET | Prometheus-метрики |
| `/positions` | GET | Текущие позиции |
| `/portfolio` | GET | Снапшот портфеля |
| `/kill-switch` | POST | Сбросить kill switch (требует `api_token`) |
| `/cancel-order` | POST | Отменить ордер (требует `api_token`) |
| `/reload-config` | POST | Перезагрузить конфиг (требует `api_token`) |

---

## 11. Kill Switch

### 11.1 Автоматическое срабатывание

- Просадка портфеля достигает `DRAWDOWN_KILL_PCT` (по умолчанию 20%)
- Все новые ордера отклоняются
- Открытые ордера отменяются
- В логах: `KILL SWITCH ACTIVATED`

### 11.2 Ручная активация

```bash
curl -X POST http://localhost:8080/kill-switch \
  -H "X-API-Key: ваш-api-token" \
  -H "Content-Type: application/json" \
  -d '{"reason": "manual_halt"}'
```

### 11.3 Сброс

```bash
curl -X POST http://localhost:8080/kill-switch \
  -H "X-API-Key: ваш-api-token" \
  -H "Content-Type: application/json" \
  -d '{
    "action": "reset",
    "token": "ваш-kill-switch-token-из-.env",
    "operator_id": "ваше-имя"
  }'
```

**Важно:** без `token` из `.env` сброс невозможен.

---

## 12. Поиск и устранение неисправностей

### 12.1 Контейнер не стартует

```bash
docker compose logs pmts      # смотрим реальную ошибку
```

| Симптом | Причина | Решение |
|---------|---------|---------|
| `ModuleNotFoundError` | нет зависимостей | `docker compose build --no-cache` |
| `ValueError` | неверный `.env` | проверить `DRAWDOWN_WARN_PCT < DRAWDOWN_KILL_PCT` |
| `connector: DOWN` | биржа недоступна | проверить VPN, `ping gamma-api.polymarket.com` |
| `KILL SWITCH ACTIVATED` сразу | завышен `INITIAL_CASH_USDC` | установить реальный баланс |

### 12.2 Нет сделок

```bash
docker compose logs -f pmts | grep "REJECT"
```

| Причина отказа | Решение |
|---------------|---------|
| `liquidity_buffer` | уменьшить `MAX_ORDER_USDC` |
| `insufficient_capital` | увеличить бюджет или ждать расчёта ордеров |
| `duplicate_proposal` | нормально — дедупликация за 60с |
| `drawdown_limit` | просадка достигла порога — проверить позиции |

### 12.3 API-ошибки

```
Polymarket HTTP 401 → неверные API-ключи или ключ привязан к другому кошельку
Opinion HTTP 403 → ключ истёк или не для той среды (testnet/live)
Polymarket 422 → неверный формат ордера
```

### 12.4 Проблемы с VPN

```bash
# Проверить из контейнера
docker compose exec pmts curl -s -o /dev/null -w "%{http_code}" https://gamma-api.polymarket.com

# Если 000 — сеть недоступна
# Если 200 — VPN работает
```

---

## 13. Резервное копирование

### 13.1 База данных (SQLite)

```bash
# Ручной бэкап
docker compose exec pmts cp /app/data/portfolio.db /app/data/portfolio.db.$(date +%Y%m%d)

# Скопировать на хост
docker compose cp pmts:/app/data/portfolio.db ./backups/pmts_portfolio_$(date +%Y%m%d).db

# Восстановление
docker compose cp ./backups/pmts_portfolio_20260706.db pmts:/app/data/portfolio.db
docker compose restart pmts
```

### 13.2 Логи

```bash
# Архивация старых логов
tar -czf logs_$(date +%Y%m%d).tar.gz logs/

# Очистка логов старше 30 дней
find logs/ -name "*.log" -mtime +30 -delete
```

### 13.3 .env

```bash
cp .env .env.backup.$(date +%Y%m%d)
chmod 600 .env.backup.*
```

---

## 14. Полный чек-лист перед запуском

### Перед paper-режимом
- [ ] `docker compose build --no-cache` выполнен без ошибок
- [ ] `.env` содержит корректные значения
- [ ] `docker compose --profile backtest up pmts-backtest` показывает положительный P&L
- [ ] система запускается в paper и все коннекторы UP

### Перед live
- [ ] VPN подключён, задержка <100ms до бирж
- [ ] `.env` заполнен:
  - `PM_API_KEY`, `PM_API_SECRET`, `PM_PASSPHRASE`, `PM_WALLET_KEY`
  - `OP_API_KEY`, `OP_WALLET_KEY`, `OP_CTF_EXCHANGE_ADDR`
  - `MARKETS`, `INITIAL_CASH_USDC`, `KILL_SWITCH_TOKEN`
- [ ] `KILL_SWITCH_TOKEN` сохранён в менеджере паролей
- [ ] `ENABLE_TRADING=true`
- [ ] `market_registry.json` настроен для всех рынков
- [ ] `API_TOKEN` задан, если нужна защита эндпоинтов
- [ ] порт 8080 не открыт наружу (только localhost или внутренняя сеть)
- [ ] `chmod 600 .env`

### После запуска live
- [ ] `docker compose ps` — статус `healthy`
- [ ] `curl http://localhost:8080/ready` — `200 OK`
- [ ] в логах нет `connector: DOWN`
- [ ] kill switch не сработал ложно
- [ ] мониторинг активен (если настроен)

---

## 15. Архитектура деплоя

```
┌──────────────────────────────────────────────────────────┐
│                      Host Machine (VPS)                   │
│  ┌────────────────────────────────────────────────────┐  │
│  │                   Docker Network                    │  │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────────────┐ │  │
│  │  │  pmts    │  │prometheus│  │    grafana       │ │  │
│  │  │ main.py  │  │ metrics  │  │  dashboards      │ │  │
│  │  │ :8080    │  │ :9090    │  │  :3000           │ │  │
│  │  └────┬─────┘  └──────────┘  └──────────────────┘ │  │
│  │       │                                            │  │
│  │  ┌────▼────────────────────────────────────────┐   │  │
│  │  │          Volumes (persistent)                │   │  │
│  │  │  ./logs/  →  /app/logs                      │   │  │
│  │  │  pmts-data → /app/data (SQLite)             │   │  │
│  │  └─────────────────────────────────────────────┘   │  │
│  └────────────────────────────────────────────────────┘  │
│                                                          │
│  ┌────────────────────────────────────────────────────┐  │
│  │              VPN tunnel (на хосте)                   │  │
│  │   Polymarket API: clob.polymarket.com :443          │  │
│  │   Polymarket WS:  ws-subscriptions-clob... :443     │  │
│  │   Opinion API:    api.opinion.markets :443          │  │
│  │   Opinion WS:     ws.opinion.markets :443           │  │
│  └────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────┘
```

---

## 16. Ссылки

- `README.md` — пользовательская документация
- `PRODUCTION_READINESS.md` — оценка готовности к production
- `architectural_audit.md` — аудит архитектуры
- `docs/prometheus.yml` — конфигурация Prometheus
- `docs/grafana-dashboard.json` — дашборд Grafana

---

## 17. Приложение: docker-compose.yml (эталон)

```yaml
services:
  pmts:
    build: .
    env_file: .env
    environment:
      - PYTHONPATH=/app
      - LOG_FORMAT=json
      - OBSERVABILITY_BIND_HOST=0.0.0.0
      - DB_PATH=/app/data/portfolio.db
    volumes:
      - ./logs:/app/logs
      - pmts-data:/app/data
    ports:
      - "8080:8080"
    restart: unless-stopped
    command: ["--mode", "paper"]
    deploy:
      resources:
        limits:
          memory: 256M
        reservations:
          memory: 128M

  pmts-backtest:
    build: .
    env_file: .env
    environment:
      - PYTHONPATH=/app
      - LOG_FORMAT=text
    command: ["--mode", "backtest", "--ticks", "5000", "--capital", "10000", "--verbose"]
    profiles:
      - backtest

  prometheus:
    image: prom/prometheus:v2.54.1
    volumes:
      - ./docs/prometheus.yml:/etc/prometheus/prometheus.yml:ro
      - prometheus-data:/prometheus
    ports:
      - "9090:9090"
    profiles:
      - monitoring

  grafana:
    image: grafana/grafana:11.1.0
    environment:
      GF_SECURITY_ADMIN_USER: admin
      GF_SECURITY_ADMIN_PASSWORD: ${GF_SECURITY_ADMIN_PASSWORD:-admin}
    volumes:
      - grafana-data:/var/lib/grafana
      - ./docs/grafana-dashboard.json:/var/lib/grafana/dashboards/pmts-dashboard.json:ro
    ports:
      - "3000:3000"
    profiles:
      - monitoring

volumes:
  pmts-data:
  prometheus-data:
  grafana-data:
```

> **Примечание:** Для live-торговли измените `command: ["--mode", "live"]` в сервисе `pmts`.
Для бэктеста: `docker compose --profile backtest up pmts-backtest`.
Для полного мониторинга: `docker compose --profile monitoring up -d`.
