# telebot-maverick

Слушает закрытый Telegram-канал (от вашего аккаунта, через Telethon) и при
`Deep Local >= 1.65` и `Deep Scalp >= 2.0` открывает лонг 0.1 BTC, если такой позиции ещё нет.

    pip install -r requirements.txt
    cp .env.example .env   # заполнить
    python bot.py          # первый запуск спросит телефон и код из Telegram

По умолчанию `DRY_RUN=true` — ордера только логируются. Поставьте `DRY_RUN=false` для реальной торговли.
Стопов/тейков нет — лонг остаётся открытым, пока вы не закроете его сами.

## Выгрузка истории канала

    python export_history.py history.csv

Все сообщения от старых к новым: id, дата (UTC), Deep Global/Local/Scalp и полный текст.
Бот при этом можно не останавливать, но для двух процессов лучше отдельная сессия: `TG_SESSION=export python export_history.py`.
