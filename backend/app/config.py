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


def _tps():
    """TP_LEVELS="3:50,6:50" (расстояние % : доля позиции %) или один тейк из TP_PCT на 100%."""
    raw = _opt("TP_LEVELS")
    if not raw:
        return [{"pct": _num("TP_PCT", 6.0), "share": 100.0}]
    try:
        levels = [{"pct": float(a), "share": float(b)} for a, b in (x.split(":") for x in raw.split(","))]
    except ValueError:
        raise SystemExit(f"TP_LEVELS='{raw}': ожидается 'расстояние%:доля%,...', например 3:50,6:50")
    from app.core.backtest import normalize_tps       # одна и та же проверка для env, API и консоли
    try:
        normalize_tps([(l["pct"], l["share"]) for l in levels])
    except ValueError as e:
        raise SystemExit(f"TP_LEVELS='{raw}': {e}")
    return levels


def _bool(name, default=False):
    v = _opt(name)
    return default if v is None else v.lower() in ("1", "true", "yes", "on")


def _path(name, default):
    p = Path(os.getenv(name, default).strip() or default)
    return p if p.is_absolute() else (BACKEND_DIR / p).resolve()


@dataclass(frozen=True)
class Source:
    """Источник метрик: файл истории одного Telegram-бота/канала."""
    id: str
    label: str
    file: Path


def _load_sources():
    """SOURCES=coinbase,binance + SOURCE_<ID>_FILE / SOURCE_<ID>_LABEL. Для совместимости: HISTORY_FILE (+ LIQ_FILE)."""
    ids = [x.strip().lower() for x in (_opt("SOURCES") or "").split(",") if x.strip()]
    out = {}
    for i in ids:
        f = _opt(f"SOURCE_{i.upper()}_FILE")
        if f:
            p = Path(f)
            out[i] = Source(i, _opt(f"SOURCE_{i.upper()}_LABEL") or i, p if p.is_absolute() else (BACKEND_DIR / p).resolve())
    legacy_liq = None
    if not out:                                   # старый формат: один файл (+ отдельный файл ликвидаций)
        out["default"] = Source("default", "История", _path("HISTORY_FILE", "../history.csv"))
        if _opt("LIQ_FILE"):
            out["liq"] = Source("liq", "Ликвидации", _path("LIQ_FILE", _opt("LIQ_FILE")))
            legacy_liq = "liq"
    return out, legacy_liq


@dataclass(frozen=True)
class Settings:
    sources: dict
    default_source: str
    liq_max_age_h: float
    exchange: str
    symbol: str                 # актив по умолчанию
    symbols: list               # доступные активы (SYMBOLS)
    candle_tf: str
    cache_dir: Path
    state_file: Path
    assets_file: Path           # активы, добавленные из интерфейса (рядом с SYMBOLS из .env)
    max_bars: int
    host: str
    port: int
    cors_origins: list = field(default_factory=list)
    defaults: dict = field(default_factory=dict)


def load_settings() -> Settings:
    sources, legacy_liq = _load_sources()
    default_source = (_opt("SOURCE_DEFAULT") or "").lower()
    if default_source not in sources:
        default_source = next(iter(sources))
    liq_source = (_opt("LIQ_SOURCE") or legacy_liq or "").lower() or None
    if liq_source not in sources:
        liq_source = None
    symbols = [x.strip() for x in (_opt("SYMBOLS") or _opt("SYMBOL") or "BTC/USDT:USDT").split(",") if x.strip()]
    symbol = _opt("SYMBOL_DEFAULT") or _opt("SYMBOL") or symbols[0]
    if symbol not in symbols:
        symbols.insert(0, symbol)
    side = (_opt("SIDE") or "both").lower()
    only = (_opt("ONLY") or "").lower() or None
    defaults = dict(
        source=default_source, liq_source=liq_source, symbol=symbol,
        side=side, only=only,
        local_min=_num("LOCAL_MIN", 1.85), scalp_min=_num("SCALP_MIN", 1.25),
        short_local_max=_num("SHORT_LOCAL_MAX", 0.7), short_scalp_max=_num("SHORT_SCALP_MAX", 1.2),
        confirm=_num("CONFIRM", 4, int), tps=_tps(), be_after_tp1=_bool("BE_AFTER_TP1"), sl_pct=_num("SL_PCT", 2.0),
        size=_num("ORDER_SIZE", 0.1), fee=_num("FEE", 0.0005), slip=_num("SLIPPAGE", 0.0),
        cool_metric=(_opt("COOL_METRIC") or "scalp").lower(),
        cool_drop=_num("COOL_DROP", None), cool_declines=_num("COOL_DECLINES", None, int),
        liq_hours=_num("LIQ_HOURS", None), liq_frac=_num("LIQ_FRAC", 0.25), liq_pct=_num("LIQ_PCT", 0.0),
        cooldown_h=_num("COOLDOWN_H", 0.0),
    )
    return Settings(
        sources=sources,
        default_source=default_source,
        liq_max_age_h=_num("LIQ_MAX_AGE_H", 3.0),
        exchange=_opt("EXCHANGE") or "binanceusdm",
        symbol=symbol,
        symbols=symbols,
        candle_tf=_opt("CANDLE_TF") or "1m",
        cache_dir=_path("CACHE_DIR", "../data"),
        state_file=_path("STATE_FILE", "../data/ui_state.json"),
        assets_file=_path("ASSETS_FILE", "../data/assets.json"),
        max_bars=_num("MAX_BARS", 3000, int),
        host=_opt("HOST") or "127.0.0.1",
        port=_num("PORT", 8000, int),
        cors_origins=[o.strip() for o in (_opt("CORS_ORIGINS") or "").split(",") if o.strip()],
        defaults=defaults,
    )
