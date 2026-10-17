import asyncio
import logging
import os

from telethon import TelegramClient, events
from telethon.tl.types import PeerChannel

from app.config import ROOT, load_settings
from app.core.backtest import resolve_entry
from app.core.signal_parser import Confirmer, parse_signal, should_long, should_short
from tools.trader import Trader

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

# Параметры стратегии — те же, что в интерфейсе бэктеста (backend/.env), чтобы бот и бэктест не расходились.
# Фильтры «остывание» и «ликвидации» в боте пока не реализованы — только пороги, подтверждение, SIDE и ONLY.
S = load_settings()
D = S.defaults
CONFIRM = D["confirm"]
LOCAL_MIN, SCALP_MIN, GLOBAL_MIN, SHORT, SHORT_GLOBAL = resolve_entry(
    D["local_min"], D["scalp_min"], D.get("global_min"), (D["short_local_max"], D["short_scalp_max"]), D.get("short_global_max"), D["only"])
LONG_ON, SHORT_ON = D["side"] in ("both", "long"), D["side"] in ("both", "short")


async def main():
    if len(D["tps"]) > 1 or D["be_after_tp1"]:
        # в бэктесте и интерфейсе такая стратегия считается иначе, чем бот её исполнил бы, поэтому молча упрощать нельзя
        raise SystemExit("Бот пока умеет один тейк на всю позицию без переноса в безубыток. Задайте в backend/.env один уровень "
                         "(TP_LEVELS пустой или '6:100') и BE_AFTER_TP1=false. Тейки частями и БУ сейчас работают в бэктесте и интерфейсе.")
    trader = Trader(
        S.exchange,
        os.getenv("API_KEY"),
        os.getenv("API_SECRET"),
        os.getenv("API_PASSWORD"),
        S.symbol,
        D["size"],
        os.getenv("DRY_RUN", "true").lower() != "false",
        D["tps"][0]["pct"],
        D["sl_pct"],
    )
    client = TelegramClient(os.path.join(ROOT, os.getenv("TG_SESSION", "maverick")), int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"])
    lock = asyncio.Lock()  # чтобы два быстрых сообщения не открыли две позы
    conf_long = Confirmer(lambda s: LONG_ON and should_long(s, LOCAL_MIN, SCALP_MIN, GLOBAL_MIN), CONFIRM)
    conf_short = Confirmer(lambda s: SHORT_ON and should_short(s, *SHORT, SHORT_GLOBAL), CONFIRM)
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
