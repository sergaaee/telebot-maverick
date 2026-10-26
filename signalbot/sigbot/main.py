import asyncio
import logging
import os

from app.config import ROOT, load_settings
from app.data import DataStore
from app.db import Database
from app.history_sync import HistorySync
from app.markets import MarketsCache, ccxt_swap_symbols

from aiogram.types import BotCommand

from .bot import SignalService
from .config import load_bot_settings
from .engine import Engine, make_price_fn
from .transport import AiogramTransport, build_dispatcher, make_bot

log = logging.getLogger("signalbot")
LIVE_SEC = 20
COMMANDS = [("monitor", "мониторинг: стратегии, позиции, автомод, live"), ("positions", "открытые позиции"), ("digest", "сводка в чат: daily / weekly / off / now"), ("export", "выгрузка CSV: trades / log / fills"), ("panic", "СТОП ВСЁ: закрыть позиции и выключить автомод"), ("list", "ваши конфиги"), ("check", "проверить сигналы сейчас"),
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
    if not b.token:
        raise SystemExit("Не задан SIGNALBOT_TOKEN (токен бота от @BotFather) в signalbot/.env")
    s = load_settings()
    db = Database(s.db_file)
    if db.user_count() == 0:
        raise SystemExit("В базе нет пользователей. Создайте первого: python backend/cli.py user add ЛОГИН (данные прежней версии перенесутся автоматически)")
    store = DataStore(s)
    markets = MarketsCache(lambda: ccxt_swap_symbols(s.exchange))
    engine = Engine(s, store, markets, make_price_fn(s.exchange) if b.live_price else None)
    sync = None
    if b.sync:
        sync = HistorySync(list(s.sources.values()), b.sync_min, enabled=True)
        sync.start()
        log.info("подгрузка сообщений ботов каждые %g мин", b.sync_min)
    service = SignalService(s, None, db, engine, b, sync)
    bot = make_bot(b.token)
    dp = build_dispatcher(service)
    log.info("запущен; пользователей: %d, привязано к Telegram: %d", db.user_count(), sum(1 for u in db.list_users() if u["telegram_id"]))
    try:
        asyncio.run(run(service, bot, dp, b.poll_sec))
    except KeyboardInterrupt:
        service.stop()
