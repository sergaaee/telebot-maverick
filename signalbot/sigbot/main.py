import asyncio
import logging
import os

from app.config import ROOT, load_settings
from app.data import DataStore
from app.db import Database
from app.history_sync import HistorySync
from app.markets import MarketsCache, ccxt_swap_symbols

from .bot import SignalService
from .config import load_bot_settings
from .engine import Engine, make_price_fn
from .transport import AiogramTransport, build_dispatcher, make_bot

log = logging.getLogger("signalbot")


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

    task = asyncio.create_task(cycles())
    try:
        await dp.start_polling(bot, allowed_updates=["message"], handle_signals=True)
    finally:
        service.stop()
        task.cancel()
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
