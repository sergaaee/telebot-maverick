"""Вход на сайт: сессии в SQLite (cookie HttpOnly), защита от перебора пароля, проверка заголовка против CSRF."""
import secrets
import time
from collections import defaultdict

from fastapi import HTTPException, Request

COOKIE = "mav_session"
CSRF_HEADER = "x-maverick"
PUBLIC = {"/api/health", "/api/auth/login", "/api/auth/status", "/api/auth/me", "/api/auth/logout"}
MAX_FAILS, WINDOW, LOCK = 5, 300.0, 60.0


class LoginLimiter:
    """5 неудачных попыток за 5 минут на пару (ip, логин) -> блокировка на минуту."""

    def __init__(self):
        self.fails = defaultdict(list)
        self.locked = {}

    def check(self, key):
        now = time.time()
        if self.locked.get(key, 0) > now:
            raise HTTPException(429, f"Слишком много неудачных попыток. Подождите {int(self.locked[key] - now) + 1} с.")

    def fail(self, key):
        now = time.time()
        self.fails[key] = [t for t in self.fails[key] if now - t < WINDOW] + [now]
        if len(self.fails[key]) >= MAX_FAILS:
            self.locked[key] = now + LOCK
            self.fails[key] = []

    def ok(self, key):
        self.fails.pop(key, None)
        self.locked.pop(key, None)


def make_dependency(db, settings):
    local = {}

    def require_user(request: Request):
        if not settings.auth_enabled:
            if "u" not in local:                              # режим без входа (разработка/тесты): единственный локальный пользователь
                local["u"] = db.get_user("local") or db.add_user("local", "local-" + secrets.token_urlsafe(12))
            request.state.user = local["u"]
            return
        # защита от CSRF: изменяющие запросы должны нести заголовок, которого межсайтовая форма отправить не может
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get(CSRF_HEADER) != "1":
            raise HTTPException(403, "Нет служебного заголовка запроса")
        if request.url.path in PUBLIC:
            return
        user = db.session_user(request.cookies.get(COOKIE))
        if not user:
            raise HTTPException(401, "Требуется вход")
        request.state.user = user

    return require_user
