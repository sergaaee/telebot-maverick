"""Выгрузка всей истории чата/канала/бота в CSV.

    python export_history.py [out.csv] [--chat ID_или_имя]

--chat: числовой id (канал, бот, группа) или часть названия/username из ваших диалогов, например maverickBinance.
Без --chat берётся TG_CHANNEL_ID из .env.
"""
import argparse
import asyncio
import csv
import os

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.types import PeerChannel

from signal_parser import parse_liquidation, parse_signal

load_dotenv()


async def resolve(client, ident):
    """Ищет чат среди диалогов по id или по имени/username (в диалогах есть access_hash, нужный для закрытых чатов)."""
    dialogs = await client.get_dialogs()
    if ident.lstrip("-").isdigit():
        want = abs(int(ident))
        # id канала в Telegram бывает как 3217240841, так и -1003217240841
        want_ids = {want, int(str(want)[3:]) if str(want).startswith("100") else want}
        for d in dialogs:
            if d.entity.id in want_ids:
                return d.entity
    else:
        low = ident.lower().lstrip("@")
        for d in dialogs:
            e = d.entity
            names = [d.name or "", getattr(e, "username", "") or ""]
            if any(low in n.lower() for n in names):
                return e
    if ident.lstrip("-").isdigit():
        return PeerChannel(int(ident))  # запасной вариант: вдруг есть в кеше сессии
    raise SystemExit(f"Чат '{ident}' не найден среди диалогов этого аккаунта. Откройте его в Telegram и повторите.")


async def main(out_path, chat):
    client = TelegramClient(os.getenv("TG_SESSION", "maverick"), int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"])
    await client.start()
    entity = await resolve(client, chat)
    print(f"Источник: {getattr(entity, 'title', None) or getattr(entity, 'first_name', None) or entity} (id {getattr(entity, 'id', '?')})")
    n = 0
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["id", "date_utc", "deep_global", "deep_local", "deep_scalp", "liq_long_k", "liq_short_k", "text"])
        # reverse=True — от старых к новым; limit=None — вся история
        async for m in client.iter_messages(entity, limit=None, reverse=True):
            text = m.raw_text or ""
            sig = parse_signal(text) or {}
            liq = parse_liquidation(text) or {}
            w.writerow([m.id, m.date.isoformat(), sig.get("global"), sig.get("local"), sig.get("scalp"),
                        liq.get("long"), liq.get("short"), text])
            n += 1
            if n % 1000 == 0:
                print(f"{n}...", flush=True)
    print(f"Готово: {n} сообщений -> {out_path}")
    await client.disconnect()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("out", nargs="?", default="history.csv")
    ap.add_argument("--chat", default=os.getenv("TG_CHANNEL_ID", "3217240841"))
    a = ap.parse_args()
    asyncio.run(main(a.out, a.chat))
