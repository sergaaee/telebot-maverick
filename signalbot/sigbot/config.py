"""Настройки бота из переменных окружения (signalbot/.env; общие данные и ключи Telegram-клиента — из backend/.env)."""
import os
from dataclasses import dataclass

def _opt(name):
    v = os.getenv(name, "").strip()
    return v or None


def _num(name, default, cast=float):
    v = _opt(name)
    return cast(v) if v is not None else default


def _bool(name, default):
    v = _opt(name)
    return default if v is None else v.lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class BotSettings:
    token: "str | None"
    poll_sec: float = 60.0                          # как часто проверять, не пришли ли новые отчёты
    max_age_min: float = 180.0                      # сигналы старше (например, пока бот был выключен) не присылаются
    sync: bool = False                              # подгружать сообщения ботов из Telegram самим (если не делает backend)
    sync_min: float = 5.0
    live_price: bool = True                         # показывать цену входа/уровни от текущей цены биржи
    top_windows: int = 10                           # сколько окон свечей топ-коинов докачивать за один цикл


def load_bot_settings() -> BotSettings:
    has_tg = bool(_opt("TG_API_ID") and _opt("TG_API_HASH"))
    return BotSettings(
        token=_opt("SIGNALBOT_TOKEN"),
        poll_sec=_num("SIGNALBOT_POLL_SEC", 60.0), max_age_min=_num("SIGNALBOT_MAX_AGE_MIN", 180.0),
        sync=_bool("SIGNALBOT_SYNC", has_tg) and has_tg, sync_min=_num("SIGNALBOT_SYNC_MIN", 5.0),
        live_price=_bool("SIGNALBOT_LIVE_PRICE", True), top_windows=_num("SIGNALBOT_TOP_WINDOWS", 10, int),
    )
