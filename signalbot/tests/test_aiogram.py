"""Сквозная проверка слоя aiogram: настоящие Dispatcher/Bot, но вместо сети — поддельная сессия, записывающая вызовы Bot API."""
import asyncio
import json
from datetime import datetime

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import GetFile, GetMe, SendDocument, SendMessage
from aiogram.types import Chat, File, Message, Update, User

from sigbot.transport import AiogramTransport, build_dispatcher
from test_signalbot import CHAT, UI_CONFIG, env  # noqa: F401


class FakeSession(BaseSession):
    def __init__(self, files=None):
        super().__init__()
        self.calls, self.files = [], files or {}

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, SendMessage):
            return Message(message_id=len(self.calls), date=datetime.now(), chat=Chat(id=method.chat_id, type="private"), text=method.text)
        if isinstance(method, GetFile):
            return File(file_id=method.file_id, file_unique_id="u", file_size=len(self.files[method.file_id]), file_path=f"docs/{method.file_id}")
        if isinstance(method, SendDocument):
            return Message(message_id=len(self.calls), date=datetime.now(), chat=Chat(id=method.chat_id, type="private"))
        raise AssertionError(f"неожиданный вызов {method}")

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True):
        yield self.files[url.rsplit("/", 1)[-1]]


def update(text=None, uid=CHAT, document=None, caption=None, upd_id=1):
    m = {"message_id": upd_id, "date": 1700000000, "chat": {"id": uid, "type": "private"}, "from": {"id": uid, "is_bot": False, "first_name": "T", "username": f"u{uid}"}}
    if text is not None:
        m["text"] = text
    if document:
        m["document"] = document
    if caption:
        m["caption"] = caption
    return Update.model_validate({"update_id": upd_id, "message": m})


def run(env, updates, files=None):
    session = FakeSession(files)
    bot = Bot("123456:TESTTOKEN", session=session)
    dp = build_dispatcher(env)

    async def go():
        env.tg = AiogramTransport(bot, asyncio.get_running_loop())
        for u in updates:
            await dp.feed_update(bot, u)

    asyncio.run(go())
    return session


def sent_texts(session):
    return [c.text for c in session.calls if isinstance(c, SendMessage)]


def test_text_command_goes_through_aiogram(env):  # noqa: F811
    s = run(env, [update("/list")])
    assert any("Конфигов пока нет" in t for t in sent_texts(s))
    assert [c.parse_mode for c in s.calls if isinstance(c, SendMessage)] == ["HTML"]                       # разметка включена


def test_config_as_message_and_as_file(env):  # noqa: F811
    s = run(env, [update(json.dumps(UI_CONFIG), upd_id=1)])
    assert len(env.db.list_configs(env.alice["id"])) == 1 and any("Принято" in t for t in sent_texts(s))
    body = json.dumps(UI_CONFIG).encode()
    s = run(env, [update(document={"file_id": "f1", "file_unique_id": "u", "file_name": "maverick-config.json", "mime_type": "application/json", "file_size": len(body)},
                         caption="Из файла", upd_id=2)], files={"f1": body})
    assert env.db.find_config(env.alice["id"], "Из файла") and any("Принято" in t for t in sent_texts(s))


def test_get_sends_document_and_unlinked_user_is_refused(env):  # noqa: F811
    run(env, [update(json.dumps(UI_CONFIG))])
    s = run(env, [update("/get 1")])
    docs = [c for c in s.calls if isinstance(c, SendDocument)]
    assert len(docs) == 1 and docs[0].chat_id == CHAT
    s = run(env, [update("/list", uid=4242)])
    assert any("не привязан" in t for t in sent_texts(s))


def test_signal_notification_is_sent_to_owner_via_aiogram(env):  # noqa: F811
    from test_signalbot import H, LONG, T0, append_reports
    env.handle_update({"message": {"chat": {"id": CHAT}, "from": {"id": CHAT}, "text": json.dumps(UI_CONFIG)}})
    session = FakeSession()
    bot = Bot("123456:TESTTOKEN", session=session)

    async def go():
        env.tg = AiogramTransport(bot, asyncio.get_running_loop())
        await asyncio.to_thread(env.cycle)                                    # так цикл вызывает main.py
        start = T0 + 10 * H // 2
        append_reports(env.hist, LONG(start, 3), 11)
        env.now["t"] = (start + 3 * H // 2) / 1000
        await asyncio.to_thread(env.cycle)

    asyncio.run(go())
    msgs = [c for c in session.calls if isinstance(c, SendMessage) and "исполнение —" not in c.text]
    assert len(msgs) == 2 and all(m.chat_id == CHAT for m in msgs)
    assert "принят" in msgs[0].text and "ЛОНГ" in msgs[1].text and "Статистика конфига" in msgs[1].text
