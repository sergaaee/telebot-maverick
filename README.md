# telebot-maverick

Слушает закрытый Telegram-канал (от вашего аккаунта, через Telethon) и при
`Deep Local >= 1.65` и `Deep Scalp >= 2.0` открывает лонг 0.1 BTC, если такой позиции ещё нет.

    pip install -r requirements.txt
    cp .env.example .env   # заполнить
    python bot.py          # первый запуск спросит телефон и код из Telegram

По умолчанию `DRY_RUN=true` — ордера только логируются. Поставьте `DRY_RUN=false` для реальной торговли.
После входа выставляются стоп (−1%) и тейк (+3%) от цены входа (`SL_PCT`, `TP_PCT`), reduce-only. Если стоп выставить не удалось — позиция сразу закрывается.

## Выгрузка истории канала

    python export_history.py history.csv

Все сообщения от старых к новым: id, дата (UTC), Deep Global/Local/Scalp и полный текст.
Бот при этом можно не останавливать, но для двух процессов лучше отдельная сессия: `TG_SESSION=export python export_history.py`.
