import asyncio
import logging
import os

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.tl.types import PeerChannel

from signal_parser import Confirmer, parse_signal
from trader import Trader

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

LOCAL_MIN = float(os.getenv("DEEP_LOCAL_MIN", "1.65"))
SCALP_MIN = float(os.getenv("DEEP_SCALP_MIN", "2.0"))
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
    conf = Confirmer(LOCAL_MIN, SCALP_MIN, CONFIRM)
    channel = PeerChannel(int(os.getenv("TG_CHANNEL_ID", "3217240841")))

    @client.on(events.NewMessage(chats=channel))
    async def handler(event):
        sig = parse_signal(event.raw_text or "")
        if not sig:
            return
        log.info("Сигнал: %s", sig)
        confirmed = conf.update(sig)
        log.info("Серия подряд: %s из %s", conf.streak, CONFIRM)
        if not confirmed:
            return
        async with lock:
            try:
                await trader.open_long()
            except Exception:
                log.exception("Не удалось открыть лонг")

    await client.start()  # при первом запуске спросит телефон и код
    await client.get_dialogs()  # прогрев кеша, иначе закрытый канал не резолвится
    # после рестарта восстанавливаем серию по последним отчётам (ордер по ним не открываем)
    recent = [m async for m in client.iter_messages(channel, limit=50)]
    for m in reversed(recent):
        s = parse_signal(m.raw_text or "")
        if s:
            conf.update(s)
    log.info("Серия на старте: %s из %s", conf.streak, CONFIRM)
    log.info("Слушаю канал %s (DRY_RUN=%s)", channel.channel_id, trader.dry_run)
    try:
        await client.run_until_disconnected()
    finally:
        await trader.close()


if __name__ == "__main__":
    asyncio.run(main())
