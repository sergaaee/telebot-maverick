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


def _tps(prefix="", default_pct=6.0):
    """TP_LEVELS="3:50,6:50" (расстояние % : доля позиции %) или один тейк из TP_PCT на 100%. prefix="TOP_" — для топ-коинов."""
    name = f"{prefix}TP_LEVELS"
    raw = _opt(name)
    if not raw:
        return [{"pct": _num(f"{prefix}TP_PCT", default_pct), "share": 100.0}]
    try:
        levels = [{"pct": float(a), "share": float(b)} for a, b in (x.split(":") for x in raw.split(","))]
    except ValueError:
        raise SystemExit(f"{name}='{raw}': ожидается 'расстояние%:доля%,...', например 3:50,6:50")
    from app.core.backtest import normalize_tps       # одна и та же проверка для env, API и консоли
    try:
        normalize_tps([(l["pct"], l["share"]) for l in levels])
    except ValueError as e:
        raise SystemExit(f"{name}='{raw}': {e}")
    return levels


def _bool(name, default=False):
    v = _opt(name)
    return default if v is None else v.lower() in ("1", "true", "yes", "on")


def _path(name, default):
    p = Path(os.getenv(name, default).strip() or default)
    return p if p.is_absolute() else (BACKEND_DIR / p).resolve()


def _source_chat(source_id):
    """SOURCE_<ID>_CHAT; запасные значения для привычных источников: coinbase — основной канал (TG_CHANNEL_ID), binance — бот maverickBinance (2119980662)."""
    v = _opt(f"SOURCE_{source_id.upper()}_CHAT")
    if v:
        return v
    return {"coinbase": _opt("TG_CHANNEL_ID") or "3217240841", "binance": "2119980662"}.get(source_id)


@dataclass(frozen=True)
class Source:
    """Источник метрик: файл истории одного Telegram-бота/канала."""
    id: str
    label: str
    file: Path
    chat: "str | None" = None          # чат Telegram (id или имя) для автоподгрузки сообщений на сервере


def _load_sources():
    """SOURCES=coinbase,binance + SOURCE_<ID>_FILE / SOURCE_<ID>_LABEL. Для совместимости: HISTORY_FILE (+ LIQ_FILE)."""
    ids = [x.strip().lower() for x in (_opt("SOURCES") or "").split(",") if x.strip()]
    out = {}
    for i in ids:
        f = _opt(f"SOURCE_{i.upper()}_FILE")
        if f:
            p = Path(f)
            out[i] = Source(i, _opt(f"SOURCE_{i.upper()}_LABEL") or i, p if p.is_absolute() else (BACKEND_DIR / p).resolve(), _source_chat(i))
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
    sync_enabled: bool = False       # подгрузка сообщений Telegram на сервере (нужны TG_API_ID/TG_API_HASH)
    sync_min: float = 32.0           # период подгрузки, минут
    auth_enabled: bool = True        # вход на сайт по логину и паролю (пользователей заводят через `python cli.py user add`)
    db_file: Path = Path("maverick.db")   # SQLite: пользователи, сессии, состояние интерфейса, конфиги бота
    session_days: float = 30.0
    cookie_secure: bool = False      # true — если сайт открыт по HTTPS
    bot_username: "str | None" = None   # @имя бота сигналов (для подсказки на сайте)
    candles_sync: bool = True        # подгрузка свечей при запуске и по расписанию
    candles_sync_min: float = 5.0
    candles_sync_symbols: list = field(default_factory=list)   # какие активы (пусто — все из SYMBOLS)
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
        global_min=_num("GLOBAL_MIN", None), short_global_max=_num("SHORT_GLOBAL_MAX", None),
        confirm=_num("CONFIRM", 4, int), tps=_tps(), be_after_tp1=_bool("BE_AFTER_TP1"), sl_pct=_num("SL_PCT", 2.0),
        size=_num("ORDER_SIZE", 0.1),
        deposit=_num("DEPOSIT", 10000.0), trade_pct=_num("TRADE_PCT", 10.0), leverage=_num("LEVERAGE", 1.0), compound=_bool("COMPOUND", True), fee=_num("FEE", 0.0005), slip=_num("SLIPPAGE", 0.0),
        # вход лимитным ордером: % движения цены от цены сигнала (0 — по рынку), сторона (pullback — откат, breakout — по ходу), срок жизни ордера, часов
        entry_limit_pct=_num("ENTRY_LIMIT_PCT", 0.0), entry_limit_dir=(_opt("ENTRY_LIMIT_DIR") or "pullback").lower(), entry_limit_ttl_h=_num("ENTRY_LIMIT_TTL_H", 4.0),
        # фандинг: годовые %, период списания (часы), режим signed (по знаку) / cost (платят обе стороны); 0 — не учитывается
        funding_apr=_num("FUNDING_APR", 0.0), funding_period_h=_num("FUNDING_PERIOD_H", 8, int), funding_mode=(_opt("FUNDING_MODE") or "signed").lower(),
        cool_metric=(_opt("COOL_METRIC") or "scalp").lower(),
        cool_drop=_num("COOL_DROP", None), cool_declines=_num("COOL_DECLINES", None, int),
        liq_hours=_num("LIQ_HOURS", None), liq_frac=_num("LIQ_FRAC", 0.25), liq_pct=_num("LIQ_PCT", 0.0),
        cooldown_h=_num("COOLDOWN_H", 0.0),
        # пирамидинг, паузы между сигналами и «стоп не ставить, пока метрики сильны» (по умолчанию выключено — как раньше)
        pyramid_enabled=_bool("PYRAMID_ENABLED"), pyramid_max=_num("PYRAMID_MAX", 3, int), pyramid_pct=_num("PYRAMID_PCT", 5.0),
        pyramid_gap_h=_num("PYRAMID_GAP_H", 0.0), pyramid_avg=_bool("PYRAMID_AVG"), weekdays_only=_bool("WEEKDAYS_ONLY"), friday_close_before_min=_num("FRIDAY_CLOSE_BEFORE_MIN", 0.0), pyramid_leverage=_num("PYRAMID_LEVERAGE", None), liquidation_sim=_bool("LIQUIDATION_SIM"), maint_margin_pct=_num("MAINT_MARGIN_PCT", 0.5), pyramid_move_pct=_num("PYRAMID_MOVE_PCT", 0.0), pyramid_move_dir=(_opt("PYRAMID_MOVE_DIR") or "any").lower(), signal_gap_h=_num("SIGNAL_GAP_H", 0.0), sl_hold_strong=_bool("SL_HOLD_STRONG"),
        trend_enabled=_bool("TREND_ENABLED"), trend_tf=(_opt("TREND_TF") or "1h").lower(),
        trend_emas=[int(x) for x in (_opt("TREND_EMAS") or "50,200").split(",") if x.strip()],
        # топ-коины: торговля монетой из блока Top Coin отчёта по сигналу основной стратегии
        top_enabled=_bool("TOP_ENABLED", True), top_side=(_opt("TOP_SIDES") or "long").lower(),
        top_tps=_tps("TOP_", 5.0), top_be_after_tp1=_bool("TOP_BE_AFTER_TP1"), top_sl_pct=_num("TOP_SL_PCT", 3.0),
        top_size=_num("TOP_SIZE_USD", 100.0), top_fee=_num("TOP_FEE", 0.0005), top_slip=_num("TOP_SLIPPAGE", 0.0),
        top_days=_num("TOP_DAYS", 3.0), top_min_value=_num("TOP_MIN_VALUE", 0.0),
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
        sync_enabled=_bool("HISTORY_SYNC", bool(_opt("TG_API_ID") and _opt("TG_API_HASH"))), sync_min=_num("HISTORY_SYNC_MIN", 32.0),
        auth_enabled=_bool("AUTH_ENABLED", True), db_file=_path("DB_FILE", "../data/maverick.db"), session_days=_num("SESSION_DAYS", 30.0),
        cookie_secure=_bool("COOKIE_SECURE", False), bot_username=(_opt("SIGNALBOT_USERNAME") or "").lstrip("@") or None,
        candles_sync=_bool("CANDLES_SYNC", True), candles_sync_min=_num("CANDLES_SYNC_MIN", 5.0),
        candles_sync_symbols=[x.strip() for x in (_opt("CANDLES_SYNC_SYMBOLS") or "").split(",") if x.strip()],
        cors_origins=[o.strip() for o in (_opt("CORS_ORIGINS") or "").split(",") if o.strip()],
        defaults=defaults,
    )
