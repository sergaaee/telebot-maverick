import csv
import json
from datetime import datetime, timezone

import pytest

from app.config import load_settings
from app.core import backtest
from app.data import DataStore
from sigbot.bot import SignalBot
from sigbot.config import BotSettings
from sigbot.configs import ConfigError, parse_configs, validate
from sigbot.engine import Engine
from sigbot.store import ConfigStore

M = 60_000
H = 3_600_000
T0 = 1_700_000_000_000 // M * M
CHAT = 111


def _iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def append_reports(path, rows, start_id):
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        for i, (ts, dg, dl, ds) in enumerate(rows):
            w.writerow([start_id + i, _iso(ts), dg, dl, ds, 10.0, 20.0, "Top Coin\n———\nTIA-USD | 9.5\n"])


NEUTRAL = lambda n, t0=T0: [(t0 + i * H // 2, 1.0, 1.3, 1.0) for i in range(n)]
LONG = lambda t0, n: [(t0 + i * H // 2, 1.2, 2.0, 2.5) for i in range(n)]


class FakeTG:
    def __init__(self):
        self.sent, self.docs, self.files = [], [], {}

    def send_message(self, chat, text, html=True):
        self.sent.append((chat, text))

    def send_document(self, chat, name, content, caption=None):
        self.docs.append((chat, name, content, caption))

    def download(self, file_id):
        return self.files[file_id]

    def texts(self):
        return [t for _, t in self.sent]


def candles(n, step=0.0004, start=T0):
    price, rows = 100.0, []
    for i in range(n):
        price *= 1 + step
        rows.append((start + i * M, price, price * 1.0002, price * 0.9998, price))
    return rows


@pytest.fixture()
def env(tmp_path, monkeypatch):
    hist = tmp_path / "history.csv"
    with open(hist, "w", newline="", encoding="utf-8-sig") as f:
        csv.writer(f).writerow(["id", "date_utc", "deep_global", "deep_local", "deep_scalp", "liq_long_k", "liq_short_k", "text"])
    append_reports(hist, NEUTRAL(10), 1)
    cache = tmp_path / "cache"
    cache.mkdir()
    with open(backtest.cache_path("binanceusdm", "BTC/USDT:USDT", "1m", str(cache)), "w", newline="") as f:
        csv.writer(f).writerows(candles(60 * 40))
    for k, v in dict(HISTORY_FILE=str(hist), CACHE_DIR=str(cache), SOURCES="", LIQ_FILE="", LIQ_SOURCE="", SYMBOLS="BTC/USDT:USDT",
                     SYMBOL_DEFAULT="BTC/USDT:USDT", SYMBOL="", STATE_FILE=str(tmp_path / "s.json"), ASSETS_FILE=str(tmp_path / "a.json"),
                     CONFIRM="2", LOCAL_MIN="1.65", SCALP_MIN="2.0", SIDE="long", TP_PCT="3", SL_PCT="1", TP_LEVELS="", BE_AFTER_TP1="",
                     COOL_DROP="", COOL_DECLINES="", LIQ_HOURS="", COOLDOWN_H="0", TOP_ENABLED="false", FEE="0", SLIPPAGE="0").items():
        monkeypatch.setenv(k, v)
    s = load_settings()
    ds = DataStore(s)
    engine = Engine(s, ds, None, price_fn=lambda sym: 100.0, candles_fn=lambda sym: None, windows_fn=lambda w: None)
    tg = FakeTG()
    now = {"t": (T0 + 30 * H) / 1000}
    bot = SignalBot(s, tg, ConfigStore(tmp_path / "bot.json"), engine, BotSettings(token="x", chats=[CHAT], max_age_min=180, live_price=True),
                    clock=lambda: now["t"])
    bot.hist, bot.now, bot.tg, bot.tmp = hist, now, tg, tmp_path
    return bot


def msg(text=None, chat=CHAT, doc=None, caption=None):
    m = {"chat": {"id": chat}}
    if text is not None:
        m["text"] = text
    if doc:
        m["document"] = doc
    if caption:
        m["caption"] = caption
    return {"update_id": 1, "message": m}


UI_CONFIG = {"format": "maverick-config", "version": 1, "tf": "auto", "params": {"symbol": "BTC/USDT:USDT", "side": "long", "confirm": 2, "deposit": 5000, "trade_pct": 20, "leverage": 2,
                                                                                  "tps": [{"pct": 3, "share": 50}, {"pct": 6, "share": 50}], "sl_pct": 1, "be_after_tp1": True, "bogus": 1}}


# ---------------- разбор ----------------
def test_parse_variants():
    wrapped = json.dumps(UI_CONFIG)
    assert parse_configs(wrapped)[0]["params"]["confirm"] == 2
    assert parse_configs(json.dumps(UI_CONFIG["params"]))[0]["params"]["side"] == "long"                      # «голый» объект
    assert parse_configs("```json\n" + wrapped + "\n```")[0]["tf"] == "auto"                                  # блок кода
    assert len(parse_configs(json.dumps({"configs": [UI_CONFIG, UI_CONFIG]}))) == 2
    assert len(parse_configs(json.dumps([UI_CONFIG, UI_CONFIG, UI_CONFIG]))) == 3
    assert parse_configs(wrapped, "Имя")[0]["name"] == "Имя"
    for bad, msg_ in (("привет", "нет JSON"), ("{не json", "разобрать"), (json.dumps({"format": "x"}), "не конфиг"), ("[]", "Пустой")):
        with pytest.raises(ConfigError, match=msg_):
            parse_configs(bad)


def test_validate(env):
    eff, ignored = validate(UI_CONFIG["params"], env.s.defaults)
    assert ignored == ["bogus"] and eff["deposit"] == 5000 and eff["source"] == "default" and eff["local_min"] == 1.65      # недостающее — из умолчаний
    with pytest.raises(ConfigError, match="sl_pct"):
        validate({"sl_pct": -1}, env.s.defaults)
    with pytest.raises(ConfigError, match="leverage"):
        validate({"leverage": 500}, env.s.defaults)


# ---------------- доступ и импорт ----------------
def test_access_denied_shows_chat_id(env):
    env.handle_update(msg("/list", chat=999))
    assert "999" in env.tg.texts()[0] and "SIGNALBOT_CHATS" in env.tg.texts()[0]
    assert env.cfgs.configs == []
    env.handle_update(msg(json.dumps(UI_CONFIG), chat=999))
    assert env.cfgs.configs == []


def test_import_text_and_file_and_add_command(env):
    env.handle_update(msg(json.dumps(UI_CONFIG)))                                                    # просто текстом
    assert len(env.cfgs.configs) == 1 and "Принято" in env.tg.texts()[-1] and "bogus" in env.tg.texts()[-1]
    env.tg.files["f1"] = json.dumps(UI_CONFIG).encode()
    env.handle_update(msg(doc={"file_id": "f1", "file_name": "maverick-config_default_BTC.json"}, caption="Файловый"))   # файлом, имя из подписи
    assert env.cfgs.find("Файловый")
    env.handle_update(msg("/add Командный\n" + json.dumps(UI_CONFIG["params"])))
    assert env.cfgs.find("Командный")
    env.handle_update(msg("/add Командный " + json.dumps(UI_CONFIG["params"] | {"confirm": 5})))        # то же имя — обновление
    assert len([c for c in env.cfgs.configs if c["name"] == "Командный"]) == 1 and env.cfgs.find("Командный")["params"]["confirm"] == 5
    env.handle_update(msg(json.dumps(UI_CONFIG)))                                                    # без имени — уникальное
    assert len({c["name"] for c in env.cfgs.configs}) == len(env.cfgs.configs) == 4
    n = len(env.tg.sent)
    env.handle_update(msg("{не json"))
    assert "разобрать" in env.tg.texts()[-1] and len(env.cfgs.configs) == 4 and len(env.tg.sent) == n + 1
    env.handle_update(msg(json.dumps(UI_CONFIG["params"] | {"sl_pct": -3})))
    assert "Не принято" in env.tg.texts()[-1] and len(env.cfgs.configs) == 4
    env.handle_update(msg(json.dumps(UI_CONFIG["params"] | {"source": "nope"})))
    assert "источник" in env.tg.texts()[-1]
    env.tg.files["f2"] = b"x"
    env.handle_update(msg(doc={"file_id": "f2", "file_name": "photo.png", "mime_type": "image/png"}))
    assert "Пришлите конфиг файлом" in env.tg.texts()[-1]


# ---------------- сигналы ----------------
def test_baseline_then_new_signal_once_with_stats(env):
    env.handle_update(msg(json.dumps(UI_CONFIG), ))
    env.tg.sent.clear()
    env.cycle()
    assert len(env.tg.sent) == 1 and "принят" in env.tg.texts()[0] and "Статистика" in env.tg.texts()[0]            # карточка, без старых сигналов
    env.tg.sent.clear()
    env.cycle()
    assert env.tg.sent == []                                                                                      # ничего нового
    # две подряд лонговые метрики -> подтверждение (confirm=2) -> сигнал
    start = T0 + 10 * H // 2
    append_reports(env.hist, LONG(start, 3), 11)                                                                  # 3 отчёта подряд: события на 2-м и 3-м
    env.now["t"] = (start + 3 * H // 2) / 1000
    env.cycle()
    assert len(env.tg.sent) == 1, env.tg.texts()                                                                  # серия — это ОДИН сигнал
    t = env.tg.texts()[0]
    for need in ("ЛОНГ", "BTC/USDT:USDT", "DG", "DL", "DS", "2.00", "План сделки", "TP1", "TP2", "SL", "Статистика конфига", "Sharpe", "Winrate", "Объём", "Топ-коин"):
        assert need in t, (need, t)
    assert env.cfgs.configs[0]["alerts"] == 1
    env.cycle()
    assert len(env.tg.sent) == 1                                                                                  # повтор не отправляется
    # серия оборвалась и началась новая -> второй сигнал
    nxt = start + 3 * H // 2
    append_reports(env.hist, NEUTRAL(2, nxt), 14)
    append_reports(env.hist, LONG(nxt + 2 * H // 2, 2), 16)
    env.now["t"] = (nxt + 4 * H // 2) / 1000
    env.cycle()
    assert len(env.tg.sent) == 2


def test_signal_beyond_candles_and_old_signal_skipped(env):
    env.handle_update(msg(json.dumps(UI_CONFIG)))
    env.cycle()
    env.tg.sent.clear()
    start = T0 + 10 * H // 2
    candle_end = backtest.read_cache(backtest.cache_path("binanceusdm", "BTC/USDT:USDT", "1m", str(env.s.cache_dir)))[-1][0]
    late = candle_end + 3 * H                                                                                     # отчёты позже последней свечи
    append_reports(env.hist, LONG(late, 2), 11)
    env.now["t"] = (late + H) / 1000
    env.cycle()
    assert len(env.tg.sent) == 1 and "ЛОНГ" in env.tg.texts()[0]                                                  # сигнал без свечи входа тоже виден
    # устаревший сигнал (бот был выключен) не присылается, но запоминается
    env.tg.sent.clear()
    later = late + 10 * H
    append_reports(env.hist, NEUTRAL(1, later - H // 2), 13)
    append_reports(env.hist, LONG(later, 2), 14)
    env.now["t"] = (later + 20 * H) / 1000
    env.cycle()
    assert env.tg.sent == []
    env.cycle(force=True)
    assert env.tg.sent == []


def test_commands(env):
    env.handle_update(msg(json.dumps(UI_CONFIG)))
    env.cycle()
    env.tg.sent.clear()
    env.handle_update(msg("/list"))
    assert "#1" in env.tg.texts()[0] and "активен" in env.tg.texts()[0]
    env.handle_update(msg("/stats 1"))
    assert "Статистика" in env.tg.texts()[-1] and "Объём" in env.tg.texts()[-1]
    env.handle_update(msg("/get 1"))
    chat, name, content, cap = env.tg.docs[0]
    got = json.loads(content)
    assert name.endswith(".json") and got["format"] == "maverick-config" and got["params"]["deposit"] == 5000
    env.handle_update(msg("/rename 1 Новое имя"))
    assert env.cfgs.find("новое имя")
    env.handle_update(msg("/pause 1"))
    start = T0 + 10 * H // 2
    append_reports(env.hist, LONG(start, 3), 11)
    env.now["t"] = (start + 3 * H // 2) / 1000
    env.tg.sent.clear()
    env.cycle()
    assert env.tg.sent == []                                                                                      # на паузе тишина
    env.handle_update(msg("/resume 1"))
    env.cycle()
    assert not any("ЛОНГ" in t and "План сделки" in t for t in env.tg.texts())                                    # пропущенное на паузе не присылается
    env.handle_update(msg("/status"))
    assert "Состояние" in env.tg.texts()[-1]
    env.handle_update(msg("/remove Новое имя"))
    assert env.cfgs.configs == []
    env.handle_update(msg("/stats 7"))
    assert "не найден" in env.tg.texts()[-1]
    env.handle_update(msg("/help"))
    assert "Как добавить конфиг" in env.tg.texts()[-1]


def test_trend_blocked_signal_is_not_sent(env):
    cfg = json.loads(json.dumps(UI_CONFIG))
    cfg["params"] |= {"trend_enabled": True, "trend_emas": [20], "trend_tf": "5m"}
    cfg["params"].pop("bogus")
    env.handle_update(msg(json.dumps(cfg)))
    env.cycle()
    env.tg.sent.clear()
    # свечи растут -> лонг по тренду разрешён после прогрева; делаем сигнал в самом начале (EMA не прогрета) через новый конфиг
    start = T0 + 10 * H // 2
    append_reports(env.hist, LONG(start, 3), 11)
    env.now["t"] = (start + 3 * H // 2) / 1000
    env.cycle()
    assert len(env.tg.sent) == 1 and "ЛОНГ" in env.tg.texts()[0]                       # растущий рынок: тренд за лонг


def test_state_persists_between_restarts(env):
    env.handle_update(msg(json.dumps(UI_CONFIG)))
    env.cycle()
    again = ConfigStore(env.tmp / "bot.json")
    assert again.configs[0]["baseline"] is True and again.configs[0]["name"] and again.data["next_id"] == 2


def test_telegram_split_and_token_not_leaked():
    from sigbot.telegram import TelegramAPI, TelegramError, split_text
    parts = split_text("\n".join(["строка " * 50] * 40))
    assert len(parts) > 1 and all(len(p) <= 4000 for p in parts)
    import httpx

    def boom(request):
        raise httpx.ConnectError("secret-url-with-TOKEN123", request=request)

    api = TelegramAPI("TOKEN123", httpx.Client(transport=httpx.MockTransport(boom)))
    with pytest.raises(TelegramError) as e:
        api.send_message(1, "x")
    assert "TOKEN123" not in str(e.value)
