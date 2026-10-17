"""Выгрузка всей истории чата/канала/бота в CSV.

    python export_history.py [out.csv] [--chat ID_или_имя]

Если out.csv уже существует, дописываются только сообщения новее сохранённых (по максимальному id в файле);
--full выгружает всю историю заново.

--chat: числовой id (канал, бот, группа) или часть названия/username из ваших диалогов, например maverickBinance.
Без --chat берётся TG_CHANNEL_ID из .env.
"""
import argparse
import asyncio
import csv
import os
import shutil

from app.config import ROOT
from app.core.signal_parser import parse_liquidation, parse_signal



async def resolve(client, ident):
    """Ищет чат среди диалогов по id или по имени/username (в диалогах есть access_hash, нужный для закрытых чатов)."""
    from telethon.tl.types import PeerChannel

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


COLUMNS = ["id", "date_utc", "deep_global", "deep_local", "deep_scalp", "liq_long_k", "liq_short_k", "text"]


def read_state(path):
    """(заголовок, максимальный id) уже сохранённого файла; (None, 0), если файла нет или он пуст."""
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return None, 0
    with open(path, newline="", encoding="utf-8-sig") as f:
        r = csv.DictReader(f)
        last = max((int(row["id"]) for row in r if row.get("id")), default=0)
        return r.fieldnames, last


class RowWriter:
    """Пишет сообщения в CSV в колонках, которые уже есть в файле (файл старого формата не ломается)."""

    def __init__(self, path, header, append):
        self.header = header or COLUMNS
        # BOM нужен только в начале нового файла; при дозаписи в середину его быть не должно
        self.f = open(path, "a" if append else "w", newline="", encoding="utf-8" if append else "utf-8-sig")
        self.w = csv.DictWriter(self.f, fieldnames=self.header, extrasaction="ignore", restval="")
        if not append:
            self.w.writeheader()
        self.count = 0

    def write(self, m):
        text = m.raw_text or ""
        sig = parse_signal(text) or {}
        liq = parse_liquidation(text) or {}
        self.w.writerow({"id": m.id, "date_utc": m.date.isoformat(), "deep_global": sig.get("global"),
                         "deep_local": sig.get("local"), "deep_scalp": sig.get("scalp"),
                         "liq_long_k": liq.get("long"), "liq_short_k": liq.get("short"), "text": text})
        self.count += 1
        if self.count % 500 == 0:
            self.f.flush()

    def close(self):
        self.f.close()


async def main(out_path, chat, full=False, interactive=True):
    """Выгрузка чата в CSV; возвращает число добавленных сообщений. interactive=False (сервер) — без запроса кода входа:
    если сессия не авторизована, бросает RuntimeError. Запись идёт во временную копию и подменяет файл целиком, поэтому читатели
    никогда не видят недописанный файл."""
    from telethon import TelegramClient
    header, last_id = (None, 0) if full else read_state(out_path)
    client = TelegramClient(os.path.join(ROOT, os.getenv("TG_SESSION", "maverick")), int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"])
    if interactive:
        await client.start()
    else:
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise RuntimeError("Сессия Telegram не авторизована: один раз выполните в консоли python cli.py export … и введите код")
    try:
        entity = await resolve(client, chat)
    except BaseException:
        await client.disconnect()
        raise
    print(f"Источник: {getattr(entity, 'title', None) or getattr(entity, 'first_name', None) or entity} (id {getattr(entity, 'id', '?')})")
    if last_id:
        print(f"В {out_path} уже есть сообщения до id {last_id} — докачиваю только новые (--full: заново)")
    tmp = out_path + ".tmp"
    if last_id:
        shutil.copyfile(out_path, tmp)
    writer = RowWriter(tmp, header, append=bool(last_id))
    ok = False
    try:
        # reverse=True — от старых к новым; min_id — только сообщения новее сохранённых; limit=None — без ограничения
        async for m in client.iter_messages(entity, limit=None, reverse=True, min_id=last_id):
            if m.id <= last_id:
                continue
            writer.write(m)
            if writer.count % 1000 == 0:
                print(f"{writer.count}...", flush=True)
        ok = True
    finally:
        writer.close()
        await client.disconnect()
        if ok or writer.count:               # при обрыве сохраняем то, что успели (докачается в следующий раз)
            os.replace(tmp, out_path)
        elif os.path.exists(tmp):
            os.remove(tmp)
    total = f", всего в файле больше {last_id}" if last_id else ""
    print(f"Готово: добавлено {writer.count} сообщений -> {out_path}{total}")
    return writer.count


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("out", nargs="?", default="history.csv")
    ap.add_argument("--chat", default=os.getenv("TG_CHANNEL_ID", "3217240841"))
    ap.add_argument("--full", action="store_true", help="игнорировать существующий файл и выгрузить всю историю заново")
    a = ap.parse_args()
    asyncio.run(main(a.out, a.chat, a.full))
