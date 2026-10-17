"""Минимальный клиент Telegram Bot API (httpx): получение обновлений, отправка сообщений и файлов, скачивание файлов."""
import time

import httpx

API = "https://api.telegram.org"
MAX_LEN = 4000                       # лимит Telegram — 4096 символов


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


class TelegramAPI:
    def __init__(self, token, client=None):
        self.token = token
        self.http = client or httpx.Client(timeout=httpx.Timeout(70.0, connect=15.0))

    def call(self, method, **params):
        try:
            r = self.http.post(f"{API}/bot{self.token}/{method}", json=params)
            data = r.json()
        except httpx.HTTPError as e:
            raise TelegramError(f"{method}: {type(e).__name__}") from None       # без URL: в нём токен
        if not data.get("ok"):
            raise TelegramError(f"{method}: {data.get('description') or r.status_code}")
        return data["result"]

    def get_updates(self, offset=None, timeout=30):
        params = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset is not None:
            params["offset"] = offset
        return self.call("getUpdates", **params)

    def send_message(self, chat_id, text, html=True):
        last = None
        for part in split_text(text):
            last = self.call("sendMessage", chat_id=chat_id, text=part, **({"parse_mode": "HTML"} if html else {}),
                             disable_web_page_preview=True)
        return last

    def send_document(self, chat_id, filename, content: bytes, caption=None):
        try:
            r = self.http.post(f"{API}/bot{self.token}/sendDocument", data={"chat_id": chat_id, **({"caption": caption} if caption else {})},
                               files={"document": (filename, content, "application/json")})
            data = r.json()
        except httpx.HTTPError as e:
            raise TelegramError(f"sendDocument: {type(e).__name__}") from None
        if not data.get("ok"):
            raise TelegramError(f"sendDocument: {data.get('description') or r.status_code}")
        return data["result"]

    def download(self, file_id, max_bytes=1_000_000):
        info = self.call("getFile", file_id=file_id)
        if info.get("file_size", 0) > max_bytes:
            raise TelegramError("файл слишком большой для конфига")
        try:
            r = self.http.get(f"{API}/file/bot{self.token}/{info['file_path']}")
        except httpx.HTTPError as e:
            raise TelegramError(f"download: {type(e).__name__}") from None
        if r.status_code != 200:
            raise TelegramError(f"download: HTTP {r.status_code}")
        return r.content[:max_bytes + 1]

    def get_me(self):
        return self.call("getMe")


def retry_sleep(attempt):
    time.sleep(min(60, 2 ** attempt))
