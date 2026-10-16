# telebot-maverick

Слушает закрытый Telegram-канал (от вашего аккаунта, через Telethon) и при
`Deep Local >= 1.65` и `Deep Scalp >= 2.0` в двух отчётах подряд (`CONFIRM_REPORTS`) открывает лонг 0.1 BTC, если такой позиции ещё нет.

    pip install -r requirements.txt
    cp .env.example .env   # заполнить
    python bot.py          # первый запуск спросит телефон и код из Telegram

По умолчанию `DRY_RUN=true` — ордера только логируются. Поставьте `DRY_RUN=false` для реальной торговли.
После входа выставляются стоп (−1%) и тейк (+3%) от цены входа (`SL_PCT`, `TP_PCT`), reduce-only. Если стоп выставить не удалось — позиция сразу закрывается.

## Выгрузка истории

    python export_history.py history.csv                                     # канал из TG_CHANNEL_ID
    TG_SESSION=export python export_history.py history_binance.csv --chat 2119980662   # или --chat maverickBinance

Источник — id (канал, бот, группа) или часть названия/username из ваших диалогов. Все сообщения от старых к новым:
id, дата (UTC), Deep Global/Local/Scalp, ликвидации Long/Short (тыс. $) и полный текст.

**Повторный запуск с тем же файлом дописывает только новые сообщения** (id больше максимального в файле), `--full` — выгрузить всё заново.
Файл старого формата (без колонок ликвидаций) дописывается в его же колонках, ничего не ломая.

## Бэктест

    python backtest.py history.csv --exchange binanceusdm --symbol BTC/USDT:USDT
    python backtest.py history.csv --sweep-long    # сетка порогов лонга (Local/Scalp >=), шорты выключены
    python backtest.py history.csv --sweep-short   # сетка порогов шорта (Local/Scalp <=), лонги выключены

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

## Фильтры входа (свипы)

Проверка гипотез «заходить после окончания ликвидаций» и «заходить, когда метрики начали остывать».
Описание фильтров — в `entries.py`.

    # ликвидации берём из maverickBinance, сигналы — из основного файла
    python backtest.py history.csv --liq-file history_binance.csv --sweep-liq
    python backtest.py history.csv --sweep-cool

Свип печатает базу и все конфигурации с разбивкой на две половины истории и топ по нижней границе
доверительного интервала winrate. Выбранную конфигурацию можно прогнать отдельно:

    python backtest.py history.csv --liq-file history_binance.csv --liq-hours 6 --liq-frac 0.25 --liq-pct 75
    python backtest.py history.csv --cool-metric scalp --cool-drop 0.2      # или --cool-declines 2

Флаг `--explain` к одиночному прогону показывает, какие сделки базы фильтр убрал (дата, сторона, результат, ликвидации).

Фильтры пока только в бэктесте; в бот их стоит переносить только после подтверждения на данных.

**Контроль для фильтров:** `--sweep-cooldown` — та же база, но с простой паузой после стопа (1–48 ч) и без ликвидаций.
Если пауза даёт тот же прирост, значит фильтр ликвидаций ничего своего не добавляет.

## Режим одной метрики

    python backtest.py history.csv --only local     # сигнал только по Deep Local (Scalp игнорируется)
    python backtest.py history.csv --only scalp     # сигнал только по Deep Scalp
    python backtest.py history.csv --only local --sweep-long --sweep-short

Действует на лонг и шорт, на все свипы (пороги перебираются только по выбранной метрике), фильтры и `--explain`.
Остальные пороги (`--local-min`, `--scalp-min`, `--short-*`) работают как раньше для выбранной метрики.

## Лонг и шорт отдельно в фильтр-свипах

    python backtest.py history.csv --sweep-cool --side long
    python backtest.py history.csv --sweep-cool --side short
    python backtest.py history.csv --sweep-liq --liq-file history_binance.csv --side short

`--side long|short|both` (по умолчанию `both` — как раньше) работает для `--sweep-liq`, `--sweep-cool`, `--sweep-cooldown`,
одиночных прогонов с фильтрами и `--explain`. Сочетается с `--only local|scalp`.
