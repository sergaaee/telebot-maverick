import asyncio
import csv
import sys
import time
import types
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import load_settings
from app.candle_sync import CandleSync
from app.history_sync import HistorySync
from app.main import create_app
from test_api import H, T0, _write_history


# ---------------- подгрузка по расписанию ----------------
class Src:
    def __init__(self, id, chat, file="x.csv"):
        self.id, self.label, self.chat, self.file = id, id.upper(), chat, file


def test_sync_runs_per_source_and_survives_errors():
    calls, updated = [], []

    def exporter(path, chat):
        calls.append(chat)
        if chat == "bad":
            raise SystemExit("Чат не найден")
        return 3

    s = HistorySync([Src("a", "1"), Src("b", "bad"), Src("c", None)], 32, exporter=exporter, on_updated=lambda src: updated.append(src.id))
    assert [x["id"] for x in s.snapshot()["sources"]] == ["a", "b"]                 # источник без чата не участвует
    s.run_once()
    snap = {x["id"]: x for x in s.snapshot()["sources"]}
    assert calls == ["1", "bad"] and updated == ["a"]
    assert snap["a"]["status"] == "ok" and snap["a"]["added"] == 3 and snap["a"]["last_ok"]
    assert snap["b"]["status"] == "error" and "Чат не найден" in snap["b"]["error"] and snap["b"]["last_ok"] is None
    s.run_once()
    assert {x["id"]: x for x in s.snapshot()["sources"]}["a"]["added_total"] == 6


def test_sync_loop_runs_at_start_and_on_interval_and_trigger():
    n = []
    s = HistorySync([Src("a", "1")], 0.05, exporter=lambda p, c: n.append(1) or 0)          # 0.05 мин = 3 с
    s.start()
    end = time.time() + 5
    while not n and time.time() < end:
        time.sleep(0.02)
    assert len(n) == 1                                                                         # сразу при запуске
    time.sleep(0.3)
    assert s.trigger() and (time.sleep(0.5) or len(n) == 2)                                    # внеочередной запуск
    s.stop()


def test_sync_disabled():
    s = HistorySync([Src("a", "1")], 32, enabled=False, disabled_reason="нет ключей")
    s.start()
    assert not s.enabled and not s.trigger() and s.snapshot()["reason"] == "нет ключей"
    assert not HistorySync([Src("a", None)], 32).enabled


# ---------------- API: текущие метрики и статус ----------------
@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    rows = [(T0 + i * H // 2, 2.0, 2.5, 10.0, 20.0) for i in range(4)]
    _write_history(tmp_path / "history.csv", rows)
    for k, v in dict(HISTORY_FILE=str(tmp_path / "history.csv"), CACHE_DIR=str(tmp_path / "c"), SOURCES="", LIQ_FILE="", LIQ_SOURCE="",
                     SYMBOLS="BTC/USDT:USDT", SYMBOL_DEFAULT="BTC/USDT:USDT", SYMBOL="", STATE_FILE=str(tmp_path / "s.json"),
                     ASSETS_FILE=str(tmp_path / "a.json")).items():
        monkeypatch.setenv(k, v)
    return tmp_path


def test_api_latest_metrics(app_env):
    c = TestClient(create_app(load_settings()))
    j = c.get("/api/latest").json()["sources"]
    r = j[0]["report"]
    assert r["dl"] == 2.0 and r["ds"] == 2.5 and r["dg"] == 1.0 and r["liq_long_k"] == 10.0 and r["ts"] == (T0 + 3 * H // 2)
    # файл дополнили — метрики обновились без перезапуска
    with open(app_env / "history.csv", "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([99, (datetime.fromtimestamp((T0 + 9 * H) / 1000, timezone.utc)).isoformat(), 0.7, 1.1, 0.4, 1, 2, "Top Coin\n———\nTIA-USD | 9.5\n"])
    r2 = c.get("/api/latest").json()["sources"][0]["report"]
    assert (r2["dg"], r2["dl"], r2["ds"], r2["top_coin"]) == (0.7, 1.1, 0.4, "TIA-USD")


def test_api_sync_status_and_run(app_env):
    got = []
    sync = HistorySync([Src("default", "chat1", app_env / "history.csv")], 32, exporter=lambda p, c: got.append(c) or 2)
    app = create_app(load_settings(), sync=sync, candle_sync=CandleSync(lambda: [], lambda s: None, enabled=False, disabled_reason="тест"))
    with TestClient(app) as c:                                                  # lifespan: подгрузка стартует вместе с сервером
        end = time.time() + 5
        while not got and time.time() < end:
            time.sleep(0.02)
        assert got == ["chat1"]
        st = c.get("/api/sync").json()
        assert st["enabled"] and st["sources"][0]["added"] == 2 and st["interval_min"] == 32
        time.sleep(0.2)
        assert c.post("/api/sync/run").json()["started"] is True
    off = HistorySync([Src("default", "x")], 32, enabled=False, disabled_reason="нет ключей")
    c2 = TestClient(create_app(load_settings(), sync=off))
    r = c2.post("/api/sync/run")
    assert r.status_code == 409 and "нет ключей" in r.json()["detail"]


def test_config_default_chats(monkeypatch):
    for k, v in dict(SOURCES="coinbase,binance,other", SOURCE_COINBASE_FILE="a.csv", SOURCE_BINANCE_FILE="b.csv", SOURCE_OTHER_FILE="c.csv",
                     SOURCE_OTHER_CHAT="maverickX", TG_CHANNEL_ID="", SOURCE_COINBASE_CHAT="", SOURCE_BINANCE_CHAT="",
                     TG_API_ID="", TG_API_HASH="", HISTORY_SYNC="").items():
        monkeypatch.setenv(k, v)
    s = load_settings()
    assert [x.chat for x in s.sources.values()] == ["3217240841", "2119980662", "maverickX"] and not s.sync_enabled
    monkeypatch.setenv("TG_API_ID", "1"); monkeypatch.setenv("TG_API_HASH", "h")
    assert load_settings().sync_enabled
    monkeypatch.setenv("HISTORY_SYNC", "false")
    assert not load_settings().sync_enabled


# ---------------- выгрузка: инкремент, атомарная подмена, неавторизованная сессия ----------------
class FakeMsg:
    def __init__(self, i, text):
        self.id, self.raw_text, self.date = i, text, datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=30 * i)


def _fake_telethon(monkeypatch, msgs, authorized=True):
    class Ent:
        id = 3217240841
        title = "chan"

    class Dialog:
        entity, name = Ent(), "chan"

    class Client:
        def __init__(self, *a):
            pass

        async def start(self):
            pass

        async def connect(self):
            pass

        async def is_user_authorized(self):
            return authorized

        async def disconnect(self):
            pass

        async def get_dialogs(self):
            return [Dialog()]

        async def iter_messages(self, entity, limit=None, reverse=True, min_id=0):
            for m in msgs:
                if m.id > min_id:
                    yield m

    mod = types.ModuleType("telethon")
    mod.TelegramClient = Client
    tl, types_mod = types.ModuleType("telethon.tl"), types.ModuleType("telethon.tl.types")
    types_mod.PeerChannel = lambda i: ("peer", i)
    mod.tl, tl.types = tl, types_mod
    for name, m in (("telethon", mod), ("telethon.tl", tl), ("telethon.tl.types", types_mod)):
        monkeypatch.setitem(sys.modules, name, m)
    monkeypatch.setenv("TG_API_ID", "1")
    monkeypatch.setenv("TG_API_HASH", "h")


SIGNAL = "Deep Global: 1.1\nDeep Local: {l}\nDeep Scalp: {s}\n"


def test_export_incremental_and_atomic(tmp_path, monkeypatch):
    from tools import export_history

    sig = lambda i: FakeMsg(i, f"Deep Global 1.1\nDeep Local 1.{i}\nDeep Scalp 1.{i}\n")
    _fake_telethon(monkeypatch, [sig(1), sig(2)])
    out = str(tmp_path / "h.csv")
    assert asyncio.run(export_history.main(out, "3217240841", interactive=False)) == 2
    _fake_telethon(monkeypatch, [sig(1), sig(2), sig(3)])
    assert asyncio.run(export_history.main(out, "3217240841", interactive=False)) == 1                  # только новое
    assert asyncio.run(export_history.main(out, "3217240841", interactive=False)) == 0
    rows = list(csv.DictReader(open(out, encoding="utf-8-sig")))
    assert [r["id"] for r in rows] == ["1", "2", "3"]
    assert not (tmp_path / "h.csv.tmp").exists()                                                          # временный файл не остаётся
    _fake_telethon(monkeypatch, [sig(4)], authorized=False)
    with pytest.raises(RuntimeError, match="не авторизована"):
        asyncio.run(export_history.main(out, "3217240841", interactive=False))
    assert len(list(csv.DictReader(open(out, encoding="utf-8-sig")))) == 3                               # файл не тронут


# ---------------- подгрузка свечей ----------------
def test_candle_sync_runs_at_start_and_by_schedule_and_survives_errors():
    calls, updated = [], []

    def fetch(sym):
        calls.append(sym)
        if sym == "BAD":
            raise SystemExit("биржа не отвечает")

    cs = CandleSync(lambda: ["BTC", "BAD", "ETH"], fetch, 0.02, skip_fn=lambda s: s == "ETH", on_updated=updated.append)      # 0.02 мин ≈ 1,2 с
    cs.start()
    end = time.time() + 6
    while calls.count("BTC") < 2 and time.time() < end:
        time.sleep(0.05)
    cs.stop()
    assert calls.count("BTC") >= 2                                     # сразу при запуске и затем по расписанию
    snap = {x["symbol"]: x for x in cs.snapshot()["symbols"]}
    assert snap["BTC"]["status"] == "ok" and snap["BAD"]["status"] == "error" and "не отвечает" in snap["BAD"]["error"]
    assert "ETH" not in calls and updated.count("BTC") >= 2             # актив с ручной загрузкой пропущен, ошибка не остановила цикл
    off = CandleSync(lambda: ["BTC"], fetch, enabled=False, disabled_reason="выкл")
    off.start()
    assert not off.trigger() and off.snapshot()["reason"] == "выкл"


def test_api_candle_sync_default_symbols_and_status(app_env, monkeypatch):
    fetched = []
    monkeypatch.setattr("app.main.backtest.fetch_candles", lambda ex, sym, tf, since, cache_dir, progress=None: fetched.append(sym))
    monkeypatch.setenv("SYMBOLS", "BTC/USDT:USDT,ETH/USDT:USDT")
    monkeypatch.setenv("CANDLES_SYNC_SYMBOLS", "BTC/USDT:USDT")
    off = HistorySync([], 32, enabled=False)
    with TestClient(create_app(load_settings(), sync=off)) as c:
        end = time.time() + 5
        while not fetched and time.time() < end:
            time.sleep(0.02)
        assert fetched == ["BTC/USDT:USDT"]                                # только выбранный актив, при запуске сервера
        j = c.get("/api/sync").json()["candles"]
        assert j["enabled"] and j["interval_min"] == 5 and j["symbols"][0]["symbol"] == "BTC/USDT:USDT"


def test_config_candle_sync_defaults(monkeypatch):
    for k in ("CANDLES_SYNC", "CANDLES_SYNC_MIN", "CANDLES_SYNC_SYMBOLS"):
        monkeypatch.setenv(k, "")
    s = load_settings()
    assert s.candles_sync and s.candles_sync_min == 5 and s.candles_sync_symbols == []
    monkeypatch.setenv("CANDLES_SYNC", "false")
    assert not load_settings().candles_sync


def test_schedule_runs_at_minutes_02_and_32_of_each_hour_utc():
    from datetime import datetime, timezone
    from app.history_sync import next_slot, parse_minutes

    def ts(h, m, s=0, day=5):
        return datetime(2026, 11, day, h, m, s, tzinfo=timezone.utc).timestamp()

    mins = [2, 32]
    assert next_slot(ts(10, 0), mins) == ts(10, 2) and next_slot(ts(10, 2), mins) == ts(10, 32)          # ровно в слот — следующий, не тот же самый
    assert next_slot(ts(10, 2, 1), mins) == ts(10, 32) and next_slot(ts(10, 31, 59), mins) == ts(10, 32)
    assert next_slot(ts(10, 32), mins) == ts(11, 2) and next_slot(ts(10, 45), mins) == ts(11, 2)
    assert next_slot(ts(23, 40), mins) == ts(0, 2, day=6)                                                  # через полночь
    assert next_slot(ts(10, 10), [5]) == ts(11, 5)                                                         # одна минута в часе
    assert parse_minutes("2,32") == [2, 32] and parse_minutes(" :32 ; 2,2,70,x") == [2, 32] and parse_minutes("") == [] and parse_minutes(None) == []


def test_scheduled_sync_waits_for_the_slot_and_reports_it():
    n = []
    s = HistorySync([Src("a", "1")], 0.05, exporter=lambda p, c: n.append(1) or 0, at_minutes=[2, 32])
    s.start()
    end = time.time() + 5
    while not n and time.time() < end:
        time.sleep(0.02)
    time.sleep(0.2)
    assert len(n) == 1                                                                                     # при запуске — сразу; следующий запуск — в ближайшую :02/:32, а не через период
    assert s.next_run and s.next_run > time.time() and int(s.next_run) % 60 == 0 and int(s.next_run // 60) % 60 in (2, 32)
    snap = s.snapshot()
    assert snap["schedule"] == "в :02 и :32 каждого часа (UTC)" and snap["interval_min"] == 30.0
    assert s.trigger() and (time.sleep(0.5) or len(n) == 2)                                                # «Обновить сейчас» работает и с расписанием
    s.stop()
    assert HistorySync([Src("a", "1")], 32).snapshot()["schedule"] is None                                 # без расписания — прежнее поведение по периоду


def test_settings_default_schedule_and_env_override(monkeypatch):
    from app.config import load_settings
    monkeypatch.delenv("HISTORY_SYNC_AT", raising=False)
    assert load_settings().sync_at == [2, 32]
    monkeypatch.setenv("HISTORY_SYNC_AT", "5,35")
    assert load_settings().sync_at == [5, 35]
    monkeypatch.setenv("HISTORY_SYNC_AT", "")
    assert load_settings().sync_at == []                                                                   # пусто — по периоду HISTORY_SYNC_MIN
