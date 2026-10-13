# telebot-maverick

Слушает закрытый Telegram-канал (от вашего аккаунта, через Telethon) и при
`Deep Local >= 1.65` и `Deep Scalp >= 2.0` в двух отчётах подряд (`CONFIRM_REPORTS`) открывает лонг 0.1 BTC, если такой позиции ещё нет.

    pip install -r requirements.txt
    cp .env.example .env   # заполнить
    python bot.py          # первый запуск спросит телефон и код из Telegram

По умолчанию `DRY_RUN=true` — ордера только логируются. Поставьте `DRY_RUN=false` для реальной торговли.
После входа выставляются стоп (−1%) и тейк (+3%) от цены входа (`SL_PCT`, `TP_PCT`), reduce-only. Если стоп выставить не удалось — позиция сразу закрывается.

## Выгрузка истории канала

    python export_history.py history.csv

Все сообщения от старых к новым: id, дата (UTC), Deep Global/Local/Scalp и полный текст.
Бот при этом можно не останавливать, но для двух процессов лучше отдельная сессия: `TG_SESSION=export python export_history.py`.

## Бэктест

    python backtest.py history.csv --exchange binanceusdm --symbol BTC/USDT:USDT
    python backtest.py history.csv --sweep      # сетка порогов Deep Local / Scalp

Скачивает минутные свечи с публичного API биржи (кеш в `data/`), прогоняет те же правила, что и бот,
печатает статистику и пишет сделки в `trades.csv`. Опции: `--tp --sl --fee --slip --local-min --scalp-min`.

## Binance

`EXCHANGE=binanceusdm`, `SYMBOL=BTC/USDT:USDT`. Ключ API: Futures включён, вывод выключен, лучше с привязкой к IP.
Аккаунт должен быть в **One-way** режиме (не Hedge) — иначе `reduceOnly` отклоняется. Обновляйте ccxt (`pip install -U ccxt`):
Binance меняет API условных ордеров, старые версии могут не ставить SL/TP.

## Шорт

`Deep Local <= 1.05` и `Deep Scalp <= 0.65` (в тех же `CONFIRM_REPORTS` отчётах подряд) → шорт `ORDER_SIZE`,
SL +1% / TP −3% от входа. Одновременно открыта максимум одна позиция по символу. Сигнал в ту же сторону пропускается;
в противоположную — **разворот**: отменяются SL/TP старой позиции, она закрывается по рынку и открывается новая со своими SL/TP. В бэктесте шорты отключаются флагом `--no-short`.
