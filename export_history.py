"""Выгрузка всей истории канала в CSV: python export_history.py [out.csv]"""
import asyncio
import csv
import os
import sys

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.types import PeerChannel

from signal_parser import parse_signal

load_dotenv()


async def main(out_path):
    client = TelegramClient(os.getenv("TG_SESSION", "maverick"), int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"])
    await client.start()
    await client.get_dialogs()  # прогрев кеша для закрытого канала
    channel = PeerChannel(int(os.getenv("TG_CHANNEL_ID", "3217240841")))
    n = 0
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["id", "date_utc", "deep_global", "deep_local", "deep_scalp", "text"])
        # reverse=True — от старых к новым; limit=None — вся история
        async for m in client.iter_messages(channel, limit=None, reverse=True):
            sig = parse_signal(m.raw_text or "") or {}
            w.writerow([m.id, m.date.isoformat(), sig.get("global"), sig.get("local"), sig.get("scalp"), m.raw_text or ""])
            n += 1
            if n % 1000 == 0:
                print(f"{n}...", flush=True)
    print(f"Готово: {n} сообщений -> {out_path}")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "history.csv"))
