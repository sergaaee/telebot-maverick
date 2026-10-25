"""Связь с Telegram через aiogram.

Ядро бота (bot.py) синхронное и ничего не знает про aiogram: оно получает обычные словари сообщений и отправляет ответы через
`AiogramTransport`, который из рабочего потока ставит корутины aiogram в основной event loop и ждёт результата."""
import asyncio
import io
import logging

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

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
    d = {"message_id": m.message_id, "chat": {"id": m.chat.id}, "from": {"id": m.from_user.id if m.from_user else m.chat.id, "username": m.from_user.username if m.from_user else None}}
    if m.text:
        d["text"] = m.text
    if m.caption:
        d["caption"] = m.caption
    if m.document:
        d["document"] = {"file_id": m.document.file_id, "file_name": m.document.file_name, "mime_type": m.document.mime_type, "file_size": m.document.file_size}
    return d


def to_incoming_callback(c: CallbackQuery) -> dict:
    """Нажатие инлайн-кнопки -> словарь (формат Bot API) для ядра."""
    m = c.message
    return {"id": c.id, "from": {"id": c.from_user.id, "username": c.from_user.username}, "data": c.data or "",
            "message": {"chat": {"id": m.chat.id}, "message_id": m.message_id} if m else None}


def markup(keyboard):
    """[[(подпись, callback_data), …], …] -> InlineKeyboardMarkup (None, если клавиатуры нет)."""
    if not keyboard:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in keyboard])


class AiogramTransport:
    """Синхронный фасад над aiogram.Bot для вызова из рабочих потоков."""

    def __init__(self, bot: Bot, loop: asyncio.AbstractEventLoop, timeout=60):
        self.bot, self.loop, self.timeout = bot, loop, timeout

    def _run(self, coro):
        try:
            return asyncio.run_coroutine_threadsafe(coro, self.loop).result(self.timeout)
        except Exception as e:                          # TelegramAPIError, сеть, таймаут
            raise TelegramError(f"{type(e).__name__}: {e}"[:200]) from None

    def send_message(self, chat_id, text, html=True, keyboard=None):
        """Отправляет сообщение (длинное режется по строкам; клавиатура — на последней части). Возвращает id последнего сообщения."""
        parts = split_text(text)
        mid = None
        for i, part in enumerate(parts):
            m = self._run(self.bot.send_message(chat_id, part, parse_mode=ParseMode.HTML if html else None, disable_web_page_preview=True,
                                                reply_markup=markup(keyboard) if i == len(parts) - 1 else None))
            mid = getattr(m, "message_id", None)
        return mid

    def edit_message(self, chat_id, message_id, text, keyboard=None, html=True):
        """Правит сообщение на месте (для «живых» экранов). False — если менять нечего (Telegram: «message is not modified»)."""
        try:
            self._run(self.bot.edit_message_text(text[:MAX_LEN], chat_id=chat_id, message_id=message_id, parse_mode=ParseMode.HTML if html else None,
                                                 disable_web_page_preview=True, reply_markup=markup(keyboard)))
            return True
        except TelegramError as e:
            if "not modified" in str(e):
                return False
            raise

    def delete_message(self, chat_id, message_id):
        """Удаляет сообщение (в личном чате бот может удалять сообщения пользователя не старше 48 ч). True — удалено."""
        try:
            self._run(self.bot.delete_message(chat_id, message_id))
            return True
        except TelegramError:
            return False

    def answer_callback(self, callback_id, text=None, alert=False):
        try:
            self._run(self.bot.answer_callback_query(callback_id, text=text, show_alert=alert))
        except TelegramError:
            pass                                          # старая кнопка / таймаут ответа — не критично

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

    @router.callback_query()
    async def on_callback(cb: CallbackQuery):
        await run(service.handle_update, {"callback_query": to_incoming_callback(cb)})

    dp = Dispatcher()
    dp.include_router(router)
    return dp


def make_bot(token):
    return Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
