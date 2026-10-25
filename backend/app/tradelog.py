"""Отдельный лог всей автоторговли: каждое решение, запрос к бирже, ответ и ошибка — одной JSON-строкой в файл (по умолчанию data/trading.log рядом с базой).

Зачем: если автомод не сработал, биржа вернула ошибку или упал API — можно открыть файл и по порядку увидеть, что и почему произошло (кто, какой конфиг, какой запрос, какой ответ).
Секреты в лог не попадают: ключи и 64-символьные hex-строки вырезаются фильтром, адреса кошельков маскируются. Файл ротируется (5 МБ × 5).

Поля записи: ts (UTC), level, event, user, config и произвольные детали. События (event): auto_decision, order_request/order_ok/order_fail, stop_moved, close_*, exchange_request/exchange_fail,
verify_start/verify_ok/verify_fail, account_connected/account_disconnected, auto_toggle, pause/resume, test_position/test_close, error."""
import json
import logging
import os
import re
import threading
import time
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path

HEX64 = re.compile(r"(?i)\b(0x)?[0-9a-f]{64}\b")
ADDR = re.compile(r"\b0x[0-9a-fA-F]{40}\b")
SECRET_KEYS = ("key", "secret", "private", "password", "token")
_lock = threading.Lock()
_logger = None
_path = None


def log_path(db_path=None):
    env = os.getenv("TRADE_LOG_FILE", "").strip()
    if env:
        return Path(env)
    return Path(db_path or os.getenv("DB_FILE") or "data/users.db").resolve().parent / "trading.log"


def _mask_addr(m):
    a = m.group(0)
    return f"{a[:6]}…{a[-4:]}"


def scrub(v, depth=0):
    """Рекурсивно вырезает секреты: поля с «key/secret/private/password/token» в имени, hex-строки на 64 символа; адреса кошельков маскирует."""
    if depth > 6:
        return "…"
    if isinstance(v, dict):
        return {k: ("***" if any(w in str(k).lower() for w in SECRET_KEYS) and not str(k).lower().endswith(("_ts", "_id")) else scrub(x, depth + 1)) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [scrub(x, depth + 1) for x in v][:50]
    if isinstance(v, str):
        return ADDR.sub(_mask_addr, HEX64.sub("***", v))[:2000]
    if isinstance(v, float):
        return round(v, 8)
    return v if isinstance(v, (int, bool)) or v is None else scrub(str(v), depth + 1)


def _get(db_path=None):
    global _logger, _path
    p = log_path(db_path)
    with _lock:
        if _logger is None or _path != p:
            p.parent.mkdir(parents=True, exist_ok=True)
            lg = logging.getLogger("maverick.trading")
            for h in list(lg.handlers):
                lg.removeHandler(h)
                h.close()
            h = RotatingFileHandler(p, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
            h.setFormatter(logging.Formatter("%(message)s"))
            lg.addHandler(h)
            lg.setLevel(logging.INFO)
            lg.propagate = False
            _logger, _path = lg, p
    return _logger


def trade_log(event, level="info", user=None, config=None, db_path=None, **fields):
    """Одна запись в лог автоторговли. Никогда не бросает исключений: сбой логирования не должен ломать торговлю."""
    try:
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int(time.time() * 1000) % 1000:03d}Z", "level": level, "event": event, "user": user, "config": config}
        rec.update(scrub(fields))
        _get(db_path).log(logging.ERROR if level == "error" else logging.WARNING if level == "warn" else logging.INFO, json.dumps(rec, ensure_ascii=False, default=str))
    except Exception:                                                   # noqa: BLE001
        logging.getLogger("signalbot").exception("не удалось записать лог автоторговли")


def trade_error(event, exc, user=None, config=None, db_path=None, **fields):
    """Ошибка со стеком (стек урезается и тоже чистится от секретов)."""
    full = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    tb = full if len(full) <= 1800 else full[:300] + "\n…\n" + full[-1500:]
    trade_log(event, "error", user, config, db_path, error=str(exc), error_type=type(exc).__name__, traceback=tb, **fields)


def read_tail(user=None, limit=50, db_path=None):
    """Последние записи лога (новые снизу); user — только записи этого пользователя. Для команды /tradelog и отладки."""
    p = log_path(db_path)
    if not p.exists():
        return []
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    out = []
    for ln in reversed(lines):
        try:
            rec = json.loads(ln)
        except ValueError:
            continue
        if user is not None and rec.get("user") != user:
            continue
        out.append(ln)
        if len(out) >= limit:
            break
    return out[::-1]
