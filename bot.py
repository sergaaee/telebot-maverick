import asyncio
import logging
import os

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.tl.types import PeerChannel

from signal_parser import Confirmer, parse_signal, should_long, should_short
from trader import Trader

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

LOCAL_MIN = float(os.getenv("DEEP_LOCAL_MIN", "1.65"))
SCALP_MIN = float(os.getenv("DEEP_SCALP_MIN", "2.0"))
SHORT_LOCAL_MAX = float(os.getenv("SHORT_LOCAL_MAX", "1.05"))
SHORT_SCALP_MAX = float(os.getenv("SHORT_SCALP_MAX", "0.65"))
CONFIRM = int(os.getenv("CONFIRM_REPORTS", "2"))


async def main():
    trader = Trader(
        os.environ["EXCHANGE"],
        os.getenv("API_KEY"),
        os.getenv("API_SECRET"),
        os.getenv("API_PASSWORD"),
        os.environ["SYMBOL"],
        float(os.getenv("ORDER_SIZE", "0.1")),
        os.getenv("DRY_RUN", "true").lower() != "false",
        float(os.getenv("TP_PCT", "3")),
        float(os.getenv("SL_PCT", "1")),
    )
    client = TelegramClient(os.getenv("TG_SESSION", "maverick"), int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"])
    lock = asyncio.Lock()  # чтобы два быстрых сообщения не открыли две позы
    conf_long = Confirmer(lambda s: should_long(s, LOCAL_MIN, SCALP_MIN), CONFIRM)
    conf_short = Confirmer(lambda s: should_short(s, SHORT_LOCAL_MAX, SHORT_SCALP_MAX), CONFIRM)
    channel = PeerChannel(int(os.getenv("TG_CHANNEL_ID", "3217240841")))

    @client.on(events.NewMessage(chats=channel))
    async def handler(event):
        sig = parse_signal(event.raw_text or "")
        if not sig:
            return
        log.info("Сигнал: %s", sig)
        long_ok, short_ok = conf_long.update(sig), conf_short.update(sig)
        log.info("Серии подряд: long %s, short %s (нужно %s)", conf_long.streak, conf_short.streak, CONFIRM)
        side = "long" if long_ok else "short" if short_ok else None
        if not side:
            return
        async with lock:
            try:
                await trader.open_position(side)
            except Exception:
                log.exception("Не удалось открыть %s", side)

    await client.start()  # при первом запуске спросит телефон и код
    await client.get_dialogs()  # прогрев кеша, иначе закрытый канал не резолвится
    # после рестарта восстанавливаем серию по последним отчётам (ордер по ним не открываем)
    recent = [m async for m in client.iter_messages(channel, limit=50)]
    for m in reversed(recent):
        s = parse_signal(m.raw_text or "")
        if s:
            conf_long.update(s)
            conf_short.update(s)
    log.info("Серии на старте: long %s, short %s", conf_long.streak, conf_short.streak)
    log.info("Слушаю канал %s (DRY_RUN=%s)", channel.channel_id, trader.dry_run)
    try:
        await client.run_until_disconnected()
    finally:
        await trader.close()


if __name__ == "__main__":
    asyncio.run(main())
