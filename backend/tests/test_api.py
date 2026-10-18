import csv
import time

import pytest
from fastapi.testclient import TestClient

from app.config import load_settings
from app.main import create_app

M = 60_000
H = 3_600_000
T0 = 1_700_000_000_000 // M * M


def _write_history(path, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["id", "date_utc", "deep_global", "deep_local", "deep_scalp", "liq_long_k", "liq_short_k", "text"])
        for i, (ts, loc, sc, ll, ls) in enumerate(rows, 1):
            from datetime import datetime, timezone
            w.writerow([i, datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat(), 1.0, loc, sc, ll, ls, ""])


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # отчёты каждые 30 минут: сначала лонговая серия, потом шортовая, потом нейтральные
    rows = []
    t = T0
    for loc, sc, n in ((2.0, 2.5, 6), (1.0, 0.5, 6), (1.3, 1.0, 6)):
        for _ in range(n):
            rows.append((t, loc, sc, 10.0, 20.0))
            t += H // 2
    _write_history(tmp_path / "history.csv", rows)
    # свечи по минуте: сначала рост (лонг дойдёт до TP), потом падение (шорт дойдёт до TP)
    candles, price = [], 100.0
    for i in range(int((t - T0) / M) + 600):
        price *= 1.0004 if i < 200 else 0.9996 if i < 400 else 1.0
        candles.append((T0 + i * M, price, price * 1.0002, price * 0.9998, price))
    cache = tmp_path / "cache"
    cache.mkdir()
    with open(cache / "binanceusdm_BTCUSDT_USDT_1m.csv", "w", newline="") as f:
        csv.writer(f).writerows(candles)
    env = dict(HISTORY_FILE=str(tmp_path / "history.csv"), LIQ_FILE="", CACHE_DIR=str(cache),
               LOCAL_MIN="1.65", SCALP_MIN="2.0", SHORT_LOCAL_MAX="1.05", SHORT_SCALP_MAX="0.65",
               CONFIRM="2", TP_PCT="3", SL_PCT="1", ORDER_SIZE="0.1", FEE="0", SLIPPAGE="0",
               SIDE="both", ONLY="", GLOBAL_MIN="", SHORT_GLOBAL_MAX="", COOL_DROP="", COOL_DECLINES="", LIQ_HOURS="", COOLDOWN_H="0",
               CORS_ORIGINS="http://localhost:5173",
               SOURCES="", SOURCE_DEFAULT="", LIQ_SOURCE="", SYMBOLS="", SYMBOL="", SYMBOL_DEFAULT="",
               TP_LEVELS="", BE_AFTER_TP1="", STATE_FILE=str(tmp_path / "state.json"))      # пустые значения не дают реальному backend/.env подмешаться в тесты
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return TestClient(create_app(load_settings()))


def test_config_and_data_info(client):
    j = client.get("/api/config").json()
    assert j["defaults"]["confirm"] == 2 and j["defaults"]["only"] is None and j["defaults"]["cool_drop"] is None
    src = j["data"]["sources"][0]
    assert src["id"] == "default" and src["count"] == 18 and src["has_liq"] and j["data"]["assets"][0]["count"] > 1000
    assert j["defaults"]["source"] == "default" and j["defaults"]["liq_source"] is None
    assert j["symbol"] == "BTC/USDT:USDT" and j["symbols"] == ["BTC/USDT:USDT"]
    assert j["data"]["assets"][0]["symbol"] == "BTC/USDT:USDT" and j["defaults"]["symbol"] == "BTC/USDT:USDT"


def test_backtest_defaults_and_overrides(client):
    r = client.post("/api/backtest", json={})
    assert r.status_code == 200, r.text
    j = r.json()
    sides = [t["side"] for t in j["trades"]]
    assert "long" in sides and "short" in sides
    first = j["trades"][0]
    assert first["tp"] > first["entry"] > first["sl"] and first["local"] == 2.0 and first["liq_long_k"] == 10.0
    assert j["stats"]["total"]["trades"] == len(j["trades"])
    assert j["stats"]["long"]["trades"] + j["stats"]["short"]["trades"] == len(j["trades"])
    assert j["equity"][0][1] == 0.0 and abs(j["equity"][-1][1] - j["stats"]["total"]["total_usd"]) < 1e-6
    assert j["signals"] and {"ts", "side", "taken"} <= set(j["signals"][0])
    # переопределение: только лонги
    j2 = client.post("/api/backtest", json={"side": "long"}).json()
    assert {t["side"] for t in j2["trades"]} == {"long"}
    # явный null отключает необязательный параметр, неуказанный — берётся из env
    j3 = client.post("/api/backtest", json={"side": "short", "only": "local", "cool_drop": None}).json()
    assert {t["side"] for t in j3["trades"]} == {"short"} and j3["params"]["only"] == "local"


def test_backtest_validation_and_ranges(client):
    assert client.post("/api/backtest", json={"tp_pct": -1}).status_code == 422
    assert client.post("/api/backtest", json={"confirm": 0}).status_code == 422
    assert client.post("/api/backtest", json={"date_from": "2001-01-01", "date_to": "2001-01-02"}).status_code == 422
    assert client.post("/api/backtest", json={"liq_hours": 6}).status_code == 200      # ликвидации в файле есть
    day = time.strftime("%Y-%m-%d", time.gmtime(T0 / 1000))
    assert client.post("/api/backtest", json={"date_from": day, "date_to": day}).status_code == 200


def test_candles_window_resample(client):
    j = client.get("/api/candles", params={"max_bars": 200}).json()
    assert j["tf"] > 60 and 0 < len(j["bars"]) <= 200
    t, o, h, l, c = j["bars"][0]
    assert h >= max(o, c) and l <= min(o, c)
    # узкое окно -> минутные свечи
    j = client.get("/api/candles", params={"start": T0, "end": T0 + 30 * M}).json()
    assert j["tf"] == 60 and len(j["bars"]) == 31 and j["bars"][0][0] == T0 // 1000


def test_missing_data_returns_404(tmp_path, monkeypatch):
    monkeypatch.setenv("HISTORY_FILE", str(tmp_path / "nope.csv"))
    monkeypatch.setenv("CACHE_DIR", str(tmp_path))
    c = TestClient(create_app(load_settings()))
    assert c.get("/api/candles").status_code == 404
    assert c.post("/api/backtest", json={}).status_code == 404
    assert c.get("/api/config").status_code == 200       # конфиг отдаётся даже без данных


def test_candles_fixed_timeframe_and_truncation(client):
    cfg = client.get("/api/config").json()
    tfs = cfg["timeframes"]["BTC/USDT:USDT"]
    assert tfs[0] == 60 and 900 in tfs
    j = client.get("/api/candles", params={"start": T0, "end": T0 + 600 * M, "tf": 900}).json()
    assert j["tf"] == 900 and not j["truncated"]
    assert all(b[0] % 900 == 0 for b in j["bars"]) and len(j["bars"]) <= 601 // 15 + 2
    # окно шире, чем влезает в max_bars на выбранном ТФ: отдаём последнюю часть и сообщаем об этом
    j = client.get("/api/candles", params={"tf": 60, "max_bars": 500}).json()
    assert j["tf"] == 60 and j["truncated"] and len(j["bars"]) <= 501
    assert j["bars"][-1][0] == max(b[0] for b in j["bars"])
    # ТФ меньше родного поднимается до родного
    assert client.get("/api/candles", params={"tf": 5, "start": T0, "end": T0 + 10 * M}).json()["tf"] == 60


@pytest.fixture()
def two_sources(tmp_path, monkeypatch):
    """Два источника: A — основной (без ликвидаций, сигналы раньше), B — «binance» (с ликвидациями)."""
    rows_a, rows_b = [], []
    t = T0
    for loc, sc, n in ((2.0, 2.5, 6), (1.0, 0.5, 6), (1.3, 1.0, 6)):
        for _ in range(n):
            rows_a.append((t, loc, sc, None, None))
            rows_b.append((t + 60_000, loc, sc, 111.0, 222.0))      # у B метки времени на минуту позже и есть ликвидации
            t += H // 2
    rows_a.insert(0, (T0 - 3 * H, 2.0, 2.5, None, None))            # отчёт раньше первой свечи
    _write_history(tmp_path / "a.csv", rows_a)
    _write_history(tmp_path / "b.csv", rows_b)
    candles, price = [], 100.0
    for i in range(int((t - T0) / M) + 600):
        price *= 1.0004 if i < 200 else 0.9996 if i < 400 else 1.0
        candles.append((T0 + i * M, price, price * 1.0002, price * 0.9998, price))
    cache = tmp_path / "cache"
    cache.mkdir()
    with open(cache / "binanceusdm_BTCUSDT_USDT_1m.csv", "w", newline="") as f:
        csv.writer(f).writerows(candles)
    env = dict(SOURCES="coinbase,binance", SOURCE_DEFAULT="binance",
               SOURCE_COINBASE_LABEL="Coinbase Maverick", SOURCE_COINBASE_FILE=str(tmp_path / "a.csv"),
               SOURCE_BINANCE_LABEL="Binance Maverick", SOURCE_BINANCE_FILE=str(tmp_path / "b.csv"),
               LIQ_SOURCE="", CACHE_DIR=str(cache), HISTORY_FILE="", LIQ_FILE="",
               LOCAL_MIN="1.65", SCALP_MIN="2.0", SHORT_LOCAL_MAX="1.05", SHORT_SCALP_MAX="0.65",
               CONFIRM="2", TP_PCT="3", SL_PCT="1", FEE="0", SLIPPAGE="0", SIDE="both", ONLY="", GLOBAL_MIN="", SHORT_GLOBAL_MAX="",
               COOL_DROP="", COOL_DECLINES="", LIQ_HOURS="", COOLDOWN_H="0")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return TestClient(create_app(load_settings()))


def test_sources_in_config(two_sources):
    j = two_sources.get("/api/config").json()
    assert j["defaults"]["source"] == "binance"
    byid = {s["id"]: s for s in j["data"]["sources"]}
    assert set(byid) == {"coinbase", "binance"} and byid["binance"]["label"] == "Binance Maverick"
    assert byid["coinbase"]["count"] == 19 and not byid["coinbase"]["has_liq"] and byid["binance"]["has_liq"]


def test_backtest_source_selection(two_sources):
    j = two_sources.post("/api/backtest", json={}).json()
    assert j["source"]["id"] == "binance" and j["trades"][0]["liq_long_k"] == 111.0
    a = two_sources.post("/api/backtest", json={"source": "coinbase"}).json()
    assert a["source"] == {"id": "coinbase", "label": "Coinbase Maverick", "liq_source": None}
    assert a["trades"][0]["liq_long_k"] is None
    assert any("раньше начала свечей" in w for w in a["warnings"])       # отчёт до первой свечи отброшен
    assert j["warnings"] == []
    # сигналы из coinbase, ликвидации из binance
    m = two_sources.post("/api/backtest", json={"source": "coinbase", "liq_source": "binance"}).json()
    assert m["source"]["liq_source"] == "binance" and m["trades"][0]["liq_long_k"] == 111.0
    assert m["trades"][0]["local"] == 2.0
    # фильтр ликвидаций без данных о них -> понятная ошибка; с источником ликвидаций -> работает
    assert two_sources.post("/api/backtest", json={"source": "coinbase", "liq_hours": 6}).status_code == 422
    assert two_sources.post("/api/backtest", json={"source": "coinbase", "liq_source": "binance", "liq_hours": 6}).status_code == 200
    # неизвестный источник
    r = two_sources.post("/api/backtest", json={"source": "kraken"})
    assert r.status_code == 422 and "kraken" in r.json()["detail"]
    assert two_sources.post("/api/backtest", json={"liq_source": "nope"}).status_code == 422


def test_ui_state_roundtrip(client, tmp_path):
    assert client.get("/api/ui-state").json() == {}
    state = {"last": {"source": "binance", "symbol": "ETH/USDT:USDT"}, "profiles": {"binance|ETH/USDT:USDT": {"confirm": 3}}}
    assert client.put("/api/ui-state", json=state).json() == {"ok": True}
    assert client.get("/api/ui-state").json() == state
    assert client.put("/api/ui-state", json=["не", "объект"]).status_code == 422


def test_multiple_assets(tmp_path, monkeypatch):
    rows = [(T0 + i * H // 2, 2.0, 2.5, 1.0, 1.0) for i in range(8)]
    _write_history(tmp_path / "history.csv", rows)
    cache = tmp_path / "cache"
    cache.mkdir()
    for name, base in (("BTCUSDT_USDT", 100.0), ("ETHUSDT_USDT", 10.0)):
        price, candles = base, []
        for i in range(600):
            price *= 1.0004
            candles.append((T0 + i * M, price, price * 1.0002, price * 0.9998, price))
        with open(cache / f"binanceusdm_{name}_1m.csv", "w", newline="") as f:
            csv.writer(f).writerows(candles)
    for k, v in dict(HISTORY_FILE=str(tmp_path / "history.csv"), CACHE_DIR=str(cache), SOURCES="", LIQ_FILE="", LIQ_SOURCE="",
                     SYMBOLS="BTC/USDT:USDT,ETH/USDT:USDT", SYMBOL_DEFAULT="ETH/USDT:USDT", SYMBOL="", CONFIRM="2",
                     TP_PCT="3", SL_PCT="1", STATE_FILE=str(tmp_path / "s.json")).items():
        monkeypatch.setenv(k, v)
    c = TestClient(create_app(load_settings()))
    cfg = c.get("/api/config").json()
    assert cfg["symbols"] == ["BTC/USDT:USDT", "ETH/USDT:USDT"] and cfg["defaults"]["symbol"] == "ETH/USDT:USDT"
    assert {a["symbol"] for a in cfg["data"]["assets"]} == {"BTC/USDT:USDT", "ETH/USDT:USDT"}
    eth = c.post("/api/backtest", json={}).json()
    btc = c.post("/api/backtest", json={"symbol": "BTC/USDT:USDT"}).json()
    assert eth["symbol"] == "ETH/USDT:USDT" and btc["symbol"] == "BTC/USDT:USDT"
    assert eth["trades"][0]["entry"] < 20 < btc["trades"][0]["entry"]            # цены разных активов
    pe = c.get("/api/candles", params={"symbol": "ETH/USDT:USDT", "start": T0, "end": T0 + 5 * M}).json()["bars"][0][1]
    pb = c.get("/api/candles", params={"symbol": "BTC/USDT:USDT", "start": T0, "end": T0 + 5 * M}).json()["bars"][0][1]
    assert pe < 20 < pb
    assert c.get("/api/candles", params={"symbol": "DOGE/USDT:USDT"}).status_code == 422
    assert c.post("/api/backtest", json={"symbol": "DOGE/USDT:USDT"}).status_code == 422


def test_multi_tp_in_api(client):
    d = client.get("/api/config").json()["defaults"]
    assert d["tps"] == [{"pct": 3.0, "share": 100.0}] and d["be_after_tp1"] is False and "tp_pct" not in d
    two = [{"pct": 1.5, "share": 50}, {"pct": 3, "share": 50}]
    j = client.post("/api/backtest", json={"tps": two, "be_after_tp1": True, "side": "long"}).json()
    t = j["trades"][0]
    assert j["params"]["tps"] == two and j["params"]["be_after_tp1"] is True and "tp_pct" not in j["params"]
    assert t["tps"] == pytest.approx([t["entry"] * 1.015, t["entry"] * 1.03]) and t["tp_shares"] == [0.5, 0.5] and t["be"] is True
    assert [f["label"] for f in t["fills"]][:2] == ["TP1", "TP2"] and t["tp_hits"] == 2 and t["reason"] == "TP"
    assert abs(sum(f["share"] for f in t["fills"]) - 1.0) < 1e-9 and t["fills"][0]["ts"] <= t["fills"][1]["ts"]
    assert t["exit_avg"] == pytest.approx((t["fills"][0]["price"] + t["fills"][1]["price"]) / 2)
    tot = j["stats"]["total"]
    assert tot["tp_reach"][0] >= tot["tp_reach"][1] >= tot["tp_reach"][2] == 0 and tot["tp_reach"][1] >= 1 and "be" in tot
    # старое поле tp_pct без tps работает как один тейк на 100%
    j = client.post("/api/backtest", json={"tp_pct": 2, "side": "long"}).json()
    assert j["params"]["tps"] == [{"pct": 2.0, "share": 100.0}]


def test_multi_tp_validation(client):
    bad = {
        "доли не 100%": [{"pct": 1, "share": 60}, {"pct": 2, "share": 60}],
        "тейки не растут": [{"pct": 2, "share": 50}, {"pct": 1, "share": 50}],
    }
    for name, tps in bad.items():
        r = client.post("/api/backtest", json={"tps": tps})
        assert r.status_code == 422, name
    assert "100%" in client.post("/api/backtest", json={"tps": bad["доли не 100%"]}).json()["detail"]
    four = [{"pct": i + 1, "share": 25} for i in range(4)]
    assert client.post("/api/backtest", json={"tps": four}).status_code == 422          # не больше трёх
    assert client.post("/api/backtest", json={"tps": []}).status_code == 422


def test_tp_levels_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TP_LEVELS", "3:50,6:30,10:20")
    monkeypatch.setenv("BE_AFTER_TP1", "true")
    d = load_settings().defaults
    assert d["tps"] == [{"pct": 3.0, "share": 50.0}, {"pct": 6.0, "share": 30.0}, {"pct": 10.0, "share": 20.0}] and d["be_after_tp1"] is True
    monkeypatch.setenv("TP_LEVELS", "3:50,6:40")
    with pytest.raises(SystemExit, match="100%"):
        load_settings()


def test_deep_global_in_api(client):
    base = client.post("/api/backtest", json={}).json()
    assert base["stats"]["total"]["trades"] > 0 and base["params"]["global_min"] is None
    same = client.post("/api/backtest", json={"global_min": 0.5, "short_global_max": 1.5}).json()          # во всех отчётах DG = 1.0
    assert same["stats"]["total"]["trades"] == base["stats"]["total"]["trades"]
    assert all(t["global"] == 1.0 for t in same["trades"])
    strict = client.post("/api/backtest", json={"global_min": 1.5, "short_global_max": 0.5}).json()
    assert strict["stats"]["total"]["trades"] == 0
    only_g = client.post("/api/backtest", json={"only": "global", "global_min": 0.5, "short_global_max": 1.5}).json()
    assert only_g["stats"]["total"]["trades"] > 0
    cool_g = client.post("/api/backtest", json={"cool_drop": 0.1, "cool_metric": "global"})
    assert cool_g.status_code == 200
    assert client.post("/api/backtest", json={"only": "global"}).status_code == 422            # режим «только Global» без порогов
    assert client.post("/api/backtest", json={"only": "global", "side": "long", "global_min": 0.5}).status_code == 200
    assert client.post("/api/backtest", json={"global_min": 99}).status_code == 422


def test_deep_global_env_default_and_explicit_off(tmp_path, monkeypatch, client):
    monkeypatch.setenv("GLOBAL_MIN", "1.5")
    c2 = TestClient(create_app(load_settings()))
    assert c2.get("/api/config").json()["defaults"]["global_min"] == 1.5
    assert c2.post("/api/backtest", json={"side": "long"}).json()["stats"]["total"]["trades"] == 0     # умолчание из env применено
    off = c2.post("/api/backtest", json={"global_min": None, "side": "long"}).json()                                # явный null (старый конфиг) — выключено
    assert off["stats"]["total"]["trades"] == client.post("/api/backtest", json={"side": "long"}).json()["stats"]["total"]["trades"] > 0


def test_pyramid_move_pct_in_api(client):
    kw = dict(pyramid_enabled=True, pyramid_max=3, pyramid_pct=5, trade_pct=10)
    base = client.post("/api/backtest", json=kw).json()
    assert base["params"]["pyramid_move_pct"] == 0 and base["stats"]["total"]["adds"] > 0
    far = client.post("/api/backtest", json={**kw, "pyramid_move_pct": 50}).json()
    assert far["stats"]["total"]["adds"] == 0 and far["skipped_reasons"].get("мало движения от последнего входа", 0) > 0
    assert client.post("/api/backtest", json={**kw, "pyramid_move_dir": "up"}).status_code == 422
    assert client.post("/api/backtest", json={**kw, "pyramid_move_pct": -1}).status_code == 422
