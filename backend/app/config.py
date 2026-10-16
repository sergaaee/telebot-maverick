"""Настройки бэкенда из переменных окружения (см. backend/.env.example)."""
import os
from dataclasses import dataclass, field
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
ROOT = BACKEND_DIR.parent              # корень репозитория: рядом лежат history*.csv и data/ (кеш свечей)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(BACKEND_DIR / ".env")      # уже заданные переменные окружения не перезаписываются
load_dotenv(ROOT / ".env")             # совместимость: старый корневой .env тоже читается


def _opt(name):
    v = os.getenv(name, "").strip()
    return v or None


def _num(name, default, cast=float):
    v = _opt(name)
    return cast(v) if v is not None else default


def _path(name, default):
    p = Path(os.getenv(name, default).strip() or default)
    return p if p.is_absolute() else (BACKEND_DIR / p).resolve()


@dataclass(frozen=True)
class Settings:
    history_file: Path
    liq_file: "Path | None"
    liq_max_age_h: float
    exchange: str
    symbol: str
    candle_tf: str
    cache_dir: Path
    max_bars: int
    host: str
    port: int
    cors_origins: list = field(default_factory=list)
    defaults: dict = field(default_factory=dict)


def load_settings() -> Settings:
    liq = _opt("LIQ_FILE")
    side = (_opt("SIDE") or "both").lower()
    only = (_opt("ONLY") or "").lower() or None
    defaults = dict(
        side=side, only=only,
        local_min=_num("LOCAL_MIN", 1.85), scalp_min=_num("SCALP_MIN", 1.25),
        short_local_max=_num("SHORT_LOCAL_MAX", 0.7), short_scalp_max=_num("SHORT_SCALP_MAX", 1.2),
        confirm=_num("CONFIRM", 4, int), tp_pct=_num("TP_PCT", 6.0), sl_pct=_num("SL_PCT", 2.0),
        size=_num("ORDER_SIZE", 0.1), fee=_num("FEE", 0.0005), slip=_num("SLIPPAGE", 0.0),
        cool_metric=(_opt("COOL_METRIC") or "scalp").lower(),
        cool_drop=_num("COOL_DROP", None), cool_declines=_num("COOL_DECLINES", None, int),
        liq_hours=_num("LIQ_HOURS", None), liq_frac=_num("LIQ_FRAC", 0.25), liq_pct=_num("LIQ_PCT", 0.0),
        cooldown_h=_num("COOLDOWN_H", 0.0),
    )
    return Settings(
        history_file=_path("HISTORY_FILE", "../history.csv"),
        liq_file=_path("LIQ_FILE", liq) if liq else None,
        liq_max_age_h=_num("LIQ_MAX_AGE_H", 3.0),
        exchange=_opt("EXCHANGE") or "binanceusdm",
        symbol=_opt("SYMBOL") or "BTC/USDT:USDT",
        candle_tf=_opt("CANDLE_TF") or "1m",
        cache_dir=_path("CACHE_DIR", "../data"),
        max_bars=_num("MAX_BARS", 3000, int),
        host=_opt("HOST") or "127.0.0.1",
        port=_num("PORT", 8000, int),
        cors_origins=[o.strip() for o in (_opt("CORS_ORIGINS") or "").split(",") if o.strip()],
        defaults=defaults,
    )
