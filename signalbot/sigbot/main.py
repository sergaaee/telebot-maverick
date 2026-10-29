import asyncio
import logging
import os
import time

from app.config import ROOT, load_settings
from app.data import DataStore
from app.db import Database
from app.history_sync import HistorySync
from app.markets import MarketsCache, ccxt_swap_symbols

from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.types import BotCommand

from .bot import SignalService
from .config import load_bot_settings
from .engine import Engine, make_price_fn
from .transport import AiogramTransport, build_dispatcher, make_bot

log = logging.getLogger("signalbot")
LIVE_SEC = 20
COMMANDS = [("monitor", "мониторинг: стратегии, позиции, автомод, live"), ("positions", "открытые позиции"), ("catchup", "догнать свежий сигнал: /catchup № on|off [мин] [%]"), ("size", "размер входа конфига: /size № % плечо"), ("digest", "сводка в чат: daily / weekly / off / now"), ("export", "выгрузка CSV: trades / log / fills"), ("panic", "СТОП ВСЁ: закрыть позиции и выключить автомод"), ("list", "ваши конфиги"), ("check", "проверить сигналы сейчас"),
            ("status", "состояние данных"), ("help", "справка")]


async def run(service, bot, dp, poll_sec):
    loop = asyncio.get_running_loop()
    service.tg = AiogramTransport(bot, loop)

    async def cycles():
        while not service.stopped:
            try:
                await asyncio.to_thread(service.cycle)
            except Exception:
                log.exception("цикл")
            await asyncio.to_thread(service.wait, poll_sec)

    async def live():
        """«Живые» экраны мониторинга: раз в ~20 с перерисовываем подписанные сообщения (правим на месте, только если что-то изменилось)."""
        while not service.stopped:
            await asyncio.sleep(LIVE_SEC)
            try:
                await asyncio.to_thread(service.refresh_live)
            except Exception:
                log.exception("live-обновление")

    async def alive():
        """Пульс процесса для сторожа: пока жив цикл событий, раз в 15 с пишем время в базу."""
        while not service.stopped:
            await asyncio.to_thread(service._beat, "bot_alive")
            await asyncio.sleep(15)

    task0 = asyncio.create_task(alive())
    task = asyncio.create_task(cycles())
    task2 = asyncio.create_task(live())
    try:
        try:
            await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in COMMANDS])
        except Exception:
            log.warning("не удалось задать меню команд")
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"], handle_signals=True)
    finally:
        service.stop()
        task.cancel()
        task2.cancel()
        task0.cancel()
        await bot.session.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    os.chdir(ROOT)
    b = load_bot_settings()
    interactive = os.isatty(0)                       # в контейнере (сервер) вместо выхода ждём настройки, чтобы не уходить в цикл перезапусков
    if not b.token:
        msg = "Не задан SIGNALBOT_TOKEN (токен бота от @BotFather) в signalbot/.env"
        if interactive:
            raise SystemExit(msg)
        log.error("%s — бот ждёт; задайте токен и перезапустите контейнер", msg)
        while True:
            time.sleep(3600)
    s = load_settings()
    db = Database(s.db_file)
    if db.user_count() == 0:
        msg = "В базе нет пользователей. Создайте первого: python backend/cli.py user add ЛОГИН (данные прежней версии перенесутся автоматически)"
        if interactive:
            raise SystemExit(msg)
        log.warning("%s — жду появления пользователя", msg)
        while db.user_count() == 0:
            time.sleep(30)
    store = DataStore(s)
    markets = MarketsCache(lambda: ccxt_swap_symbols(s.exchange))
    engine = Engine(s, store, markets, make_price_fn(s.exchange) if b.live_price else None)
    sync = None
    if b.sync:
        sync = HistorySync(list(s.sources.values()), b.sync_min, enabled=True, at_minutes=list(b.sync_at))
        sync.start()
        log.info("подгрузка сообщений ботов %s", ("в минуты :" + ", :".join(f"{m:02d}" for m in b.sync_at) + " каждого часа (UTC)") if b.sync_at else f"каждые {b.sync_min:g} мин")
    service = SignalService(s, None, db, engine, b, sync)
    log.info("запущен; пользователей: %d, привязано к Telegram: %d", db.user_count(), sum(1 for u in db.list_users() if u["telegram_id"]))
    while True:
        bot = make_bot(b.token)
        dp = build_dispatcher(service)
        try:
            asyncio.run(run(service, bot, dp, b.poll_sec))
            return
        except KeyboardInterrupt:
            service.stop()
            return
        except TelegramUnauthorizedError:
            raise SystemExit("Telegram не принял SIGNALBOT_TOKEN — проверьте токен у @BotFather")
        except TelegramNetworkError as e:              # нет связи с api.telegram.org: пробуем снова, а не падаем
            log.error("Telegram недоступен (%s) — повтор через 15 с", str(e)[:150])
            time.sleep(15)
