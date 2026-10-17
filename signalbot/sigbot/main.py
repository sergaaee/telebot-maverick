import logging
import os
import threading

from app.config import ROOT, load_settings
from app.data import DataStore
from app.history_sync import HistorySync
from app.markets import MarketsCache, ccxt_swap_symbols

from .bot import SignalBot
from .config import load_bot_settings
from .engine import Engine, make_price_fn
from .store import ConfigStore
from .telegram import TelegramAPI, TelegramError


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("signalbot")
    os.chdir(ROOT)
    b = load_bot_settings()
    if not b.token:
        raise SystemExit("Не задан SIGNALBOT_TOKEN (токен бота от @BotFather) в signalbot/.env")
    if not b.chats:
        log.warning("SIGNALBOT_CHATS пуст: бот никого не слушает. Напишите ему — он ответит вашим chat id, впишите его в signalbot/.env и перезапустите.")
    s = load_settings()
    store = DataStore(s)
    markets = MarketsCache(lambda: ccxt_swap_symbols(s.exchange))
    engine = Engine(s, store, markets, make_price_fn(s.exchange) if b.live_price else None)
    tg = TelegramAPI(b.token)
    try:
        me = tg.get_me()
    except TelegramError as e:
        raise SystemExit(f"Telegram не принял токен или недоступен: {e}")
    sync = None
    if b.sync:
        sync = HistorySync(list(s.sources.values()), b.sync_min, enabled=True)
        sync.start()
        log.info("подгрузка сообщений ботов каждые %g мин", b.sync_min)
    bot = SignalBot(s, tg, ConfigStore(b.state_file), engine, b, sync)
    log.info("запущен @%s, конфигов: %d, получатели: %s", me.get("username"), len(bot.cfgs.configs), b.chats or "—")
    threading.Thread(target=bot.cycle_loop, name="signals", daemon=True).start()
    try:
        bot.poll_loop()
    except KeyboardInterrupt:
        bot.stop()
