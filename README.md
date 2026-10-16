# telebot-maverick

Торговый бот по сигналам Telegram-ботов Maverick (метрики Deep Global / Local / Scalp), бэктест и интерактивный интерфейс
для просмотра сделок на графике.

```
backend/     Python: FastAPI-API для интерфейса, движок бэктеста, Telegram-бот и экспорт истории
  app/core/    движок: симуляция сделок (backtest.py), точки входа и фильтры (entries.py), разбор сообщений (signal_parser.py)
  app/         API: config.py (env), data.py (источники и свечи), service.py (запуск бэктеста), state.py, main.py
  tools/       bot.py, trader.py (биржа), export_history.py (выгрузка истории из Telegram)
  tests/       pytest: движок, API, экспорт
  cli.py       консольная точка входа: serve | backtest | export | bot
frontend/    React + TypeScript + Vite + lightweight-charts: график, сигналы, TP/SL, статистика
e2e/         проверка интерфейса в настоящем браузере на синтетических данных
```

Пользовательские данные лежат в **корне репозитория** и не коммитятся: `history*.csv` (выгрузки), `data/` (кеш свечей и
`ui_state.json` с вашим выбором), `*.session` (сессии Telegram), `trades.csv`.

## Быстрый старт

```bash
# 1. Бэкенд
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env              # заполнить: источники, активы, параметры стратегии (+ Telegram/биржа для бота)
python cli.py serve               # API на http://127.0.0.1:8000  (python cli.py serve --reload — с автоперезагрузкой)

# 2. Интерфейс (в другом терминале)
cd frontend
cp .env.example .env              # необязательно
npm install
npm run dev                       # http://localhost:5173, /api проксируется на бэкенд
```

Свечи: в интерфейсе кнопка **«Обновить свечи»** (или `python cli.py backtest …`) скачивает их с биржи в `data/`.

## Конфигурация (`backend/.env`)

Все важные значения — в одном файле `backend/.env` (шаблон — `backend/.env.example`). Им пользуются и API, и бэктест, и бот,
поэтому интерфейс и бот не расходятся в параметрах.

| Группа | Переменные |
|---|---|
| Источники метрик | `SOURCES=coinbase,binance`, `SOURCE_<ID>_FILE`, `SOURCE_<ID>_LABEL`, `SOURCE_DEFAULT`, `LIQ_SOURCE`, `LIQ_MAX_AGE_H` |
| Активы и свечи | `EXCHANGE`, `SYMBOLS`, `SYMBOL_DEFAULT`, `CANDLE_TF`, `CACHE_DIR`, `MAX_BARS` |
| Сервер | `HOST`, `PORT`, `CORS_ORIGINS`, `STATE_FILE` |
| Стратегия (умолчания) | `SIDE`, `ONLY`, `LOCAL_MIN`, `SCALP_MIN`, `SHORT_LOCAL_MAX`, `SHORT_SCALP_MAX`, `CONFIRM`, `TP_PCT`, `SL_PCT`, `ORDER_SIZE`, `FEE`, `SLIPPAGE` |
| Фильтры (умолчания) | `COOL_METRIC`, `COOL_DROP`, `COOL_DECLINES`, `LIQ_HOURS`, `LIQ_FRAC`, `LIQ_PCT`, `COOLDOWN_H` |
| Telegram и биржа (бот, экспорт) | `TG_API_ID`, `TG_API_HASH`, `TG_SESSION`, `TG_CHANNEL_ID`, `API_KEY`, `API_SECRET`, `API_PASSWORD`, `DRY_RUN` |

Старый формат (`HISTORY_FILE`, `LIQ_FILE`, `SYMBOL`) тоже понимается. Корневой `.env` читается как запасной.

## Интерфейс

- **График** свечей с маркерами входа (▲ лонг / ▼ шорт) и выхода (TP / SL / REV / OPEN), линиями входа, тейка и стопа на время
  сделки и, по желанию, пропущенными сигналами. Клик по сделке на графике или в таблице приближает к ней.
- **Таймфрейм**: «Авто» подбирает под масштаб, либо фиксированный (1m … 1d). Листание и зум подгружают свечи; смена ТФ
  сохраняет вид и выбранную сделку; ← / → — предыдущая/следующая сделка.
- **Параметры**: актив, источник метрик (Coinbase / Binance Maverick) и отдельно источник ликвидаций, сторона, режим «только Local /
  только Scalp», пороги, подтверждение, TP/SL, комиссия, проскальзывание, пауза после стопа, фильтры «остывание метрик» и
  «ликвидации закончились», период. У каждого параметра значок **«?»** с описанием, как он работает (тексты — `frontend/src/helpTexts.ts`).
- **Статистика**: итог, число сделок, winrate, profit factor, просадка, средние, серия убытков, время в позиции; разрезы по
  сторонам и половинам истории; результаты по месяцам; кривая капитала; таблица сделок.
- **Запоминание выбора**: последний источник и актив, а также все параметры и таймфрейм **по каждой связке «источник + актив»**
  хранятся на сервере в `STATE_FILE` (по умолчанию `data/ui_state.json`). Переживают перезапуск бэкенда, браузера и работают из
  любого браузера. «Сбросить к env» возвращает значения из `.env` для текущей связки.

## Консоль

Запускать из папки `backend/`: `python cli.py <команда>`. Рабочая папка при этом переключается в корень репозитория,
поэтому `history.csv`, `trades.csv`, `data/` и сессии Telegram лежат там же, где и раньше.

```bash
python cli.py backtest history.csv --exchange binanceusdm --symbol BTC/USDT:USDT      # бэктест (флаги как раньше, --help)
python cli.py backtest history.csv --sweep-long          # сетка порогов лонга; --sweep-short — шорта
python cli.py backtest history.csv --sweep-cool --side short
python cli.py backtest history.csv --liq-file history_binance.csv --sweep-liq
python cli.py export history.csv                         # выгрузка канала из TG_CHANNEL_ID (дописывает только новые сообщения)
python cli.py export history_binance.csv --chat 2119980662   # или --chat maverickBinance; --full — заново
python cli.py bot                                        # Telegram-бот (DRY_RUN=true по умолчанию — только лог)
```

Подробности по правилам и флагам бэктеста: `python cli.py backtest --help` и `backend/app/core/entries.py`.

### Правила симуляции (одни и те же в бэктесте, интерфейсе и боте)

- **Лонг**: `Deep Local ≥ LOCAL_MIN` и `Deep Scalp ≥ SCALP_MIN` в `CONFIRM` отчётах подряд. **Шорт**: `Local ≤ SHORT_LOCAL_MAX`
  и `Scalp ≤ SHORT_SCALP_MAX`. Режим `ONLY` оставляет одну метрику.
- Вход по open первой свечи после последнего подтверждающего отчёта; SL / TP — проценты от цены входа; если в одной свече
  достигнуты оба, считается стоп. Комиссия берётся с обеих сторон.
- Одна позиция одновременно: сигнал в ту же сторону игнорируется, в противоположную — **разворот** (закрытие + новая позиция).
- Фильтры входа (только бэктест/интерфейс, бот их пока не использует): остывание метрик, «ликвидации закончились», пауза после стопа.

### Бот и Binance

`EXCHANGE=binanceusdm`, `SYMBOL=BTC/USDT:USDT`. Ключ API: Futures включён, вывод выключен, лучше с привязкой к IP.
Аккаунт должен быть в **One-way** режиме (не Hedge) — иначе `reduceOnly` отклоняется. Обновляйте ccxt (`pip install -U ccxt`):
Binance меняет API условных ордеров. Если SL/TP выставить не удалось — позиция сразу закрывается. `DRY_RUN=false` — реальная торговля.

## Тесты

```bash
cd backend && python -m pytest -q          # движок, API, экспорт
cd frontend && npm run typecheck && npm run build
python e2e/run.py                          # браузерная проверка интерфейса (нужны playwright и собранный фронтенд, см. шапку файла)
```
