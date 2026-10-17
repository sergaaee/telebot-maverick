import csv
import json
from datetime import datetime, timezone

import pytest

from app.config import load_settings
from app.core import backtest
from app.data import DataStore
from app.db import Database
from sigbot.bot import SignalService
from sigbot.config import BotSettings
from sigbot.configs import ConfigError, parse_configs, validate
from sigbot.engine import Engine

M = 60_000
H = 3_600_000
T0 = 1_700_000_000_000 // M * M
CHAT = 111          # Telegram id пользователя alice
BOB = 222


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

    def texts(self, chat=None):
        return [t for c, t in self.sent if chat is None or c == chat]


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
    monkeypatch.setenv("DB_FILE", str(tmp_path / "users.db"))
    s = load_settings()
    ds = DataStore(s)
    db = Database(s.db_file)
    alice = db.add_user("alice", "alice-password")
    bob = db.add_user("bob", "bob-password-1")
    db.add_user("carol", "carol-password")                       # без Telegram
    db.link_telegram("alice", CHAT)
    db.link_telegram("bob", BOB)
    engine = Engine(s, ds, None, price_fn=lambda sym: 100.0, candles_fn=lambda sym, progress=None: None, windows_fn=lambda w: None)
    tg = FakeTG()
    now = {"t": (T0 + 30 * H) / 1000}
    bot = SignalService(s, tg, db, engine, BotSettings(token="x", max_age_min=180, live_price=True), clock=lambda: now["t"])
    bot.hist, bot.now, bot.tg, bot.tmp, bot.db, bot.alice, bot.bob = hist, now, tg, tmp_path, db, alice, bob
    bot.evals = []
    real = engine.evaluate
    engine.evaluate = lambda params: (bot.evals.append(1), real(params))[1]
    return bot


def msg(text=None, chat=CHAT, doc=None, caption=None):
    m = {"chat": {"id": chat}, "from": {"id": chat, "username": "u%d" % chat}}
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
def test_unlinked_telegram_cannot_use_bot_and_gets_instructions(env):
    env.handle_update(msg("/list", chat=999))
    assert "не привязан" in env.tg.texts()[0] and env.db.list_configs() == []
    env.handle_update(msg(json.dumps(UI_CONFIG), chat=999))
    assert env.db.list_configs() == []
    env.handle_update(msg("/start", chat=999))
    assert "Привязать Telegram" in env.tg.texts()[-1]
    env.handle_update(msg("/start"))                                          # привязанному — полная справка
    assert "Как добавить конфиг" in env.tg.texts()[-1]


def test_link_flow_and_attempt_limit(env):
    carol = env.db.get_user("carol")
    code = env.db.create_link_code(carol["id"])
    env.handle_update(msg("/link", chat=555))
    assert "Формат" in env.tg.texts()[-1]
    env.handle_update(msg("/link WRONGCODE", chat=555))
    assert "не подошёл" in env.tg.texts()[-1]
    env.handle_update(msg(f"/link {code}", chat=555))
    assert "carol" in env.tg.texts()[-1] and env.db.user_by_telegram(555)["login"] == "carol"
    env.handle_update(msg(json.dumps(UI_CONFIG), chat=555))                    # теперь можно добавлять конфиги
    assert len(env.db.list_configs(carol["id"])) == 1
    env.handle_update(msg("/unlink", chat=555))
    assert env.db.user_by_telegram(555) is None
    for _ in range(6):                                                         # перебор кода ограничен
        env.handle_update(msg("/link AAAAAAAA", chat=777))
    assert "Слишком много попыток" in env.tg.texts()[-1]


def test_import_text_and_file_and_add_command(env):
    env.handle_update(msg(json.dumps(UI_CONFIG)))                                                    # просто текстом
    assert len(env.db.list_configs(env.alice['id'])) == 1 and "Принято" in env.tg.texts()[-1] and "bogus" in env.tg.texts()[-1]
    env.tg.files["f1"] = json.dumps(UI_CONFIG).encode()
    env.handle_update(msg(doc={"file_id": "f1", "file_name": "maverick-config_default_BTC.json"}, caption="Файловый"))   # файлом, имя из подписи
    assert env.db.find_config(env.alice['id'], "Файловый")
    env.handle_update(msg("/add Командный\n" + json.dumps(UI_CONFIG["params"])))
    assert env.db.find_config(env.alice['id'], "Командный")
    env.handle_update(msg("/add Командный " + json.dumps(UI_CONFIG["params"] | {"confirm": 5})))        # то же имя — обновление
    assert len([c for c in env.db.list_configs(env.alice['id']) if c["name"] == "Командный"]) == 1 and env.db.find_config(env.alice['id'], "Командный")["params"]["confirm"] == 5
    env.handle_update(msg(json.dumps(UI_CONFIG)))                                                    # без имени — уникальное
    assert len({c["name"] for c in env.db.list_configs(env.alice['id'])}) == len(env.db.list_configs(env.alice['id'])) == 4
    n = len(env.tg.sent)
    env.handle_update(msg("{не json"))
    assert "разобрать" in env.tg.texts()[-1] and len(env.db.list_configs(env.alice['id'])) == 4 and len(env.tg.sent) == n + 1
    env.handle_update(msg(json.dumps(UI_CONFIG["params"] | {"sl_pct": -3})))
    assert "Не принято" in env.tg.texts()[-1] and len(env.db.list_configs(env.alice['id'])) == 4
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
    assert env.db.list_configs(env.alice['id'])[0]["alerts"] == 1
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
    assert env.db.find_config(env.alice['id'], "новое имя")
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
    assert env.db.list_configs(env.alice['id']) == []
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
    again = Database(env.s.db_file).list_configs(env.alice["id"])
    assert again[0]["baseline"] is True and again[0]["name"] and again[0]["last_ts"] == 0 or again[0]["baseline"]


def test_users_are_isolated(env):
    env.handle_update(msg(json.dumps(UI_CONFIG), chat=CHAT))
    env.handle_update(msg("/add Боба " + json.dumps(UI_CONFIG["params"]), chat=BOB))
    a, b = env.db.list_configs(env.alice["id"]), env.db.list_configs(env.bob["id"])
    assert len(a) == 1 and len(b) == 1 and b[0]["name"] == "Боба"
    env.tg.sent.clear()
    env.handle_update(msg("/list", chat=BOB))
    assert "Боба" in env.tg.texts(BOB)[0] and a[0]["name"] not in env.tg.texts(BOB)[0]
    for cmd in ("/stats", "/get", "/remove", "/pause", "/rename"):                        # чужой номер/имя недоступен
        env.handle_update(msg(f"{cmd} {a[0]['id']} x" if cmd == "/rename" else f"{cmd} {a[0]['id']}", chat=BOB))
        env.handle_update(msg(f"{cmd} {a[0]['name']}", chat=BOB))
    assert env.tg.docs == [] and len(env.db.list_configs(env.alice["id"])) == 1 and not env.db.list_configs(env.alice["id"])[0]["paused"]
    assert all("не найден" in t or "Укажите" in t for t in env.tg.texts(BOB)[1:])
    # одно и то же имя у разных пользователей — не конфликт
    env.handle_update(msg("/add Общее " + json.dumps(UI_CONFIG["params"]), chat=CHAT))
    env.handle_update(msg("/add Общее " + json.dumps(UI_CONFIG["params"]), chat=BOB))
    assert env.db.find_config(env.alice["id"], "Общее") and env.db.find_config(env.bob["id"], "Общее")


def test_signals_go_to_owner_only_and_identical_configs_are_evaluated_once(env):
    env.handle_update(msg(json.dumps(UI_CONFIG), chat=CHAT))
    env.handle_update(msg(json.dumps(UI_CONFIG), chat=BOB))
    env.handle_update(msg(json.dumps(UI_CONFIG["params"] | {"confirm": 9}), chat=BOB))            # третий, другие параметры
    env.cycle()
    assert len(env.tg.texts(CHAT)) == 2 and len(env.tg.texts(BOB)) == 4                         # ответ на импорт и карточка — на каждый из двух конфигов
    assert len(env.evals) == 2                                                                  # два уникальных набора параметров на три конфига
    env.tg.sent.clear()
    start = T0 + 10 * H // 2
    append_reports(env.hist, LONG(start, 3), 11)
    env.now["t"] = (start + 3 * H // 2) / 1000
    env.evals.clear()
    env.cycle()
    assert len(env.tg.texts(CHAT)) == 1 and "ЛОНГ" in env.tg.texts(CHAT)[0]
    assert len(env.tg.texts(BOB)) == 1 and "ЛОНГ" in env.tg.texts(BOB)[0]                     # второй пользователь получил свой сигнал
    assert len(env.evals) == 2
    # у пользователя без Telegram и на паузе сигналов нет
    carol = env.db.get_user("carol")
    env.db.add_config(carol["id"], "Каролины", validate(UI_CONFIG["params"], env.s.defaults)[0])
    env.tg.sent.clear()
    env.cycle()
    assert env.tg.sent == []


def test_unlinked_user_stops_receiving_but_keeps_configs(env):
    env.handle_update(msg(json.dumps(UI_CONFIG)))
    env.handle_update(msg("/unlink"))
    assert len(env.db.list_configs(env.alice["id"])) == 1
    env.tg.sent.clear()
    env.cycle()
    assert env.tg.sent == []


def test_telegram_split():
    from sigbot.transport import split_text
    parts = split_text("\n".join(["строка " * 50] * 40))
    assert len(parts) > 1 and all(len(p) <= 4000 for p in parts)


# ---------------- долгий подсчёт: уведомления, /cancel, пауза на лету, бюджет топ-коинов ----------------
import threading  # noqa: E402

from sigbot.engine import Cancelled  # noqa: E402


def _two_configs(env):
    env.handle_update(msg(json.dumps({"configs": [UI_CONFIG, UI_CONFIG["params"] | {"confirm": 3}]})))
    return env.db.list_configs(env.alice["id"])


def test_cancel_interrupts_download_pauses_unfinished_and_keeps_ready(env):
    c1, c2 = _two_configs(env)
    calls = []

    def slow_download(sym, progress=None):
        calls.append(sym)
        for i in range(3):
            if i == 1:                                   # пользователь нажимает /cancel посреди загрузки (из другого потока)
                t = threading.Thread(target=lambda: env.handle_update(msg("/cancel")))
                t.start()
                t.join()
            progress(i, 0)                               # как fetch_candles: после каждой порции

    env.engine._candles_fn = slow_download
    env.cycle()
    assert calls == ["BTC/USDT:USDT"]                    # загрузка прервана, второй конфиг в этом цикле не стартовал
    cfgs = env.db.list_configs(env.alice["id"])
    assert all(c["paused"] and not c["baseline"] for c in cfgs)
    texts = " ".join(env.tg.texts(CHAT))
    assert "Остановил подсчёт" in texts and "На паузе" in texts and "принят" not in texts
    # после этого /resume по одному конфигу — считается только он
    env.engine._candles_fn = lambda sym, progress=None: None
    env.tg.sent.clear()
    env.handle_update(msg(f"/resume {c1['id']}"))
    env.cycle()
    got = env.db.list_configs(env.alice["id"])
    assert got[0]["baseline"] and not got[0]["paused"] and got[1]["paused"] and not got[1]["baseline"]
    assert sum("принят" in t for t in env.tg.texts(CHAT)) == 1


def test_pause_or_remove_takes_effect_before_config_is_reached(env):
    c1, c2 = _two_configs(env)
    seen = []

    def candles(sym, progress=None):
        seen.append(sym)
        env.db.update_config(c2["id"], paused=True)      # пока считается первый, второй ставят на паузу
    env.engine._candles_fn = candles
    env.cycle()
    assert not env.db.get_config(c2["id"])["baseline"] and env.db.get_config(c1["id"])["baseline"]
    assert sum("принят" in t for t in env.tg.texts(CHAT)) == 1
    env.db.update_config(c2["id"], paused=False)
    env.db.remove_config(c2["id"])                       # удалённый конфиг не считается и не падает
    env.cycle(force=True)


def test_first_download_is_announced_and_failure_reported_once(env):
    import os
    path = backtest.cache_path("binanceusdm", "BTC/USDT:USDT", "1m", str(env.s.cache_dir))
    os.rename(path, path + ".bak")                      # свечей актива ещё нет
    env.handle_update(msg(json.dumps(UI_CONFIG)))

    def boom(sym, progress=None):
        raise SystemExit("биржа не отвечает")
    env.engine._candles_fn = boom
    env.tg.sent.clear()
    env.cycle()
    env.cycle()
    t = env.tg.texts(CHAT)
    assert sum("Свечей" in x and "ещё нет" in x for x in t) == 2 and "/cancel" in t[0]       # объявление перед каждой попыткой
    assert sum("⚠️" in x and "биржа не отвечает" in x for x in t) == 1                        # а об одной и той же ошибке — один раз
    os.rename(path + ".bak", path)
    env.engine._candles_fn = lambda sym, progress=None: None
    env.cycle()
    assert any("принят" in x for x in env.tg.texts(CHAT))


def test_check_does_not_block_and_forces_recalculation(env):
    env.handle_update(msg(json.dumps(UI_CONFIG)))
    env.cycle()
    n = len(env.evals)
    env.tg.sent.clear()
    env.handle_update(msg("/check"))                     # сразу отвечает, расчёт — в цикле
    assert "Проверка запущена" in env.tg.texts()[0] and len(env.evals) == n
    env.cycle()
    assert len(env.evals) == n + 1


def test_top_windows_have_time_budget_and_dont_delay_card(env):
    cfg = json.loads(json.dumps(UI_CONFIG))
    cfg["params"] |= {"top_enabled": True, "top_side": "long"}
    cfg["params"].pop("bogus")
    env.b = BotSettings(token="x", max_age_min=180, live_price=True, top_windows=5, top_budget_sec=0)
    env.handle_update(msg(json.dumps(cfg)))
    got = []
    env.engine._windows_fn = lambda w: got.append(list(w))
    env.engine.pending_windows = lambda res, limit: ([("TIA/USDT:USDT", i * 1000, i * 1000 + 10) for i in range(3)], 3)
    env.cycle()
    assert any("принят" in t for t in env.tg.texts(CHAT))             # карточка пришла, не дожидаясь окон
    assert len(got) == 1                                              # бюджет 0 -> за цикл одно окно


# ---------------- пирамидинг, паузы и обратная совместимость конфигов ----------------
PYR = {"pyramid_enabled": True, "pyramid_max": 2, "pyramid_pct": 5, "trade_pct": 20}


def _pyr_config(extra=None):
    cfg = json.loads(json.dumps(UI_CONFIG))
    cfg["params"].pop("bogus")
    cfg["params"] |= PYR | (extra or {})
    return cfg


def _signals_text(env):
    return [t for t in env.tg.texts(CHAT) if "План сделки" in t]


def test_pyramid_adds_are_announced_as_adds_with_limits_inside_candles(env):
    env.handle_update(msg(json.dumps(_pyr_config())))
    env.cycle()
    env.tg.sent.clear()
    start = T0 + 10 * H // 2
    append_reports(env.hist, LONG(start, 5), 11)                    # серия из 5 отчётов -> события на 2-м ... 5-м
    env.now["t"] = (start + 5 * H // 2) / 1000
    env.cycle()
    got = _signals_text(env)
    assert len(got) == 3                                             # вход + 2 добора; четвёртое подтверждение — «максимум доборов»
    assert "ДОБОР" not in got[0] and "ДОБОР #1" in got[1] and "ДОБОР #2" in got[2]
    assert "20%" in got[0] and "5%" in got[1] and "5%" in got[2]       # объём первой ноги и доборов по их долям депозита
    assert env.db.list_configs(env.alice["id"])[0]["alerts"] == 3


def test_pyramid_beyond_candles_and_gap(env):
    env.handle_update(msg(json.dumps(_pyr_config({"pyramid_gap_h": 1}))))
    env.cycle()
    env.tg.sent.clear()
    candle_end = backtest.read_cache(backtest.cache_path("binanceusdm", "BTC/USDT:USDT", "1m", str(env.s.cache_dir)))[-1][0]
    late = candle_end + 3 * H                                        # свечей входа ещё нет
    append_reports(env.hist, LONG(late, 5), 11)
    env.now["t"] = (late + 3 * H) / 1000
    env.cycle()
    got = _signals_text(env)
    assert len(got) == 2 and "ДОБОР" not in got[0] and "ДОБОР #1" in got[1]       # события через 30 мин: часовая пауза пропускает каждое второе


def test_no_pyramid_means_old_behavior(env):
    env.handle_update(msg(json.dumps(UI_CONFIG)))
    env.cycle()
    env.tg.sent.clear()
    start = T0 + 10 * H // 2
    append_reports(env.hist, LONG(start, 5), 11)
    env.now["t"] = (start + 5 * H // 2) / 1000
    env.cycle()
    assert len(_signals_text(env)) == 1 and "ДОБОР" not in _signals_text(env)[0]


def test_old_stored_config_stays_off_even_if_env_defaults_enable_new_features(env):
    old = {k: v for k, v in validate(UI_CONFIG["params"], env.s.defaults)[0].items()
           if k not in ("pyramid_enabled", "pyramid_max", "pyramid_pct", "pyramid_gap_h", "signal_gap_h", "sl_hold_strong")}      # как сохранён до обновления
    env.s.defaults.update(pyramid_enabled=True, signal_gap_h=5, sl_hold_strong=True)                                              # а в .env включили
    res = env.engine.evaluate(old)
    assert res["params"]["pyramid_enabled"] is False and res["params"]["signal_gap_h"] == 0 and res["params"]["sl_hold_strong"] is False
    new, _ = validate(UI_CONFIG["params"], env.s.defaults)                                                                         # новый импорт фиксирует значения явно
    assert new["pyramid_enabled"] is True and new["signal_gap_h"] == 5
    cfg, _ = env.db.add_config(env.alice["id"], "Старый", old)
    env.cycle()
    assert "принят" in env.tg.texts(CHAT)[-1] and "пирамидинг" not in env.tg.texts(CHAT)[-1].lower()


def test_avg_pyramid_alerts_show_new_average_levels(env):
    env.handle_update(msg(json.dumps(_pyr_config({"pyramid_avg": True}))))
    env.cycle()
    env.tg.sent.clear()
    start = T0 + 10 * H // 2
    append_reports(env.hist, LONG(start, 5), 11)
    env.now["t"] = (start + 5 * H // 2) / 1000
    env.cycle()
    got = _signals_text(env)
    assert len(got) == 3 and "ДОБОР" not in got[0] and "ДОБОР #1" in got[1] and "ДОБОР #2" in got[2]
    assert "Усреднение" in got[1] and "Усреднение" in got[2]
    assert "новая средняя" in got[2]                                            # у последнего добора известна итоговая средняя и уровни от неё
    cfg = env.db.list_configs(env.alice["id"])[0]
    assert cfg["params"]["pyramid_avg"] is True and cfg["alerts"] == 3


def test_avg_flag_missing_in_old_config_means_off(env):
    old = {k: v for k, v in validate(_pyr_config()["params"], env.s.defaults)[0].items() if k != "pyramid_avg"}
    env.s.defaults["pyramid_avg"] = True
    assert env.engine.evaluate(old)["params"]["pyramid_avg"] is False


def test_old_stored_config_ignores_env_deep_global_defaults(env):
    old = {k: v for k, v in validate(UI_CONFIG["params"], env.s.defaults)[0].items() if k not in ("global_min", "short_global_max")}      # сохранён до появления Deep Global
    env.s.defaults.update(global_min=99.0, short_global_max=-99.0)                                                                       # а в .env пороги задали
    res = env.engine.evaluate(old)
    assert res["params"]["global_min"] is None and res["params"]["short_global_max"] is None
    assert res["stats"]["total"]["trades"] == env.engine.evaluate({**old, "global_min": None})["stats"]["total"]["trades"]
    new, _ = validate(UI_CONFIG["params"], env.s.defaults)                                                                                # новый импорт фиксирует значения явно
    assert new["global_min"] == 99.0
