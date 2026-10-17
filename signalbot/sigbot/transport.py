"""Связь с Telegram через aiogram.

Ядро бота (bot.py) синхронное и ничего не знает про aiogram: оно получает обычные словари сообщений и отправляет ответы через
`AiogramTransport`, который из рабочего потока ставит корутины aiogram в основной event loop и ждёт результата."""
import asyncio
import io
import logging

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BufferedInputFile, Message

log = logging.getLogger("signalbot")
MAX_LEN = 4000                       # лимит Telegram — 4096 символов
MAX_FILE = 1_000_000


class TelegramError(RuntimeError):
    pass


def split_text(text, limit=MAX_LEN):
    """Режет длинное сообщение по границам строк."""
    if len(text) <= limit:
        return [text]
    parts, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            parts.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        parts.append(cur)
    return parts


def to_incoming(m: Message) -> dict:
    """Сообщение aiogram -> простой словарь (формат Bot API), с которым работает ядро."""
    d = {"chat": {"id": m.chat.id}, "from": {"id": m.from_user.id if m.from_user else m.chat.id, "username": m.from_user.username if m.from_user else None}}
    if m.text:
        d["text"] = m.text
    if m.caption:
        d["caption"] = m.caption
    if m.document:
        d["document"] = {"file_id": m.document.file_id, "file_name": m.document.file_name, "mime_type": m.document.mime_type, "file_size": m.document.file_size}
    return d


class AiogramTransport:
    """Синхронный фасад над aiogram.Bot для вызова из рабочих потоков."""

    def __init__(self, bot: Bot, loop: asyncio.AbstractEventLoop, timeout=60):
        self.bot, self.loop, self.timeout = bot, loop, timeout

    def _run(self, coro):
        try:
            return asyncio.run_coroutine_threadsafe(coro, self.loop).result(self.timeout)
        except Exception as e:                          # TelegramAPIError, сеть, таймаут
            raise TelegramError(f"{type(e).__name__}: {e}"[:200]) from None

    def send_message(self, chat_id, text, html=True):
        for part in split_text(text):
            self._run(self.bot.send_message(chat_id, part, parse_mode=ParseMode.HTML if html else None, disable_web_page_preview=True))

    def send_document(self, chat_id, filename, content: bytes, caption=None):
        self._run(self.bot.send_document(chat_id, BufferedInputFile(content, filename=filename), caption=caption))

    def download(self, file_id, max_bytes=MAX_FILE):
        async def go():
            info = await self.bot.get_file(file_id)
            if (info.file_size or 0) > max_bytes:
                raise ValueError("файл слишком большой для конфига")
            buf = io.BytesIO()
            await self.bot.download_file(info.file_path, destination=buf)
            return buf.getvalue()
        return self._run(go())


def build_dispatcher(service, run_in_thread=None):
    """Dispatcher aiogram: любое сообщение превращается в словарь и обрабатывается ядром в потоке (расчёты блокирующие)."""
    router = Router()
    run = run_in_thread or asyncio.to_thread

    @router.message()
    async def on_message(message: Message):
        await run(service.handle_update, {"message": to_incoming(message)})

    dp = Dispatcher()
    dp.include_router(router)
    return dp


def make_bot(token):
    return Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
