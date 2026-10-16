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
               SIDE="both", ONLY="", COOL_DROP="", COOL_DECLINES="", LIQ_HOURS="", COOLDOWN_H="0",
               CORS_ORIGINS="http://localhost:5173")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return TestClient(create_app(load_settings()))


def test_config_and_data_info(client):
    j = client.get("/api/config").json()
    assert j["defaults"]["confirm"] == 2 and j["defaults"]["only"] is None and j["defaults"]["cool_drop"] is None
    assert j["data"]["signals"]["count"] == 18 and j["data"]["candles"]["count"] > 1000
    assert j["symbol"] == "BTC/USDT:USDT"


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
    assert cfg["timeframes"][0] == 60 and 900 in cfg["timeframes"]
    j = client.get("/api/candles", params={"start": T0, "end": T0 + 600 * M, "tf": 900}).json()
    assert j["tf"] == 900 and not j["truncated"]
    assert all(b[0] % 900 == 0 for b in j["bars"]) and len(j["bars"]) <= 601 // 15 + 2
    # окно шире, чем влезает в max_bars на выбранном ТФ: отдаём последнюю часть и сообщаем об этом
    j = client.get("/api/candles", params={"tf": 60, "max_bars": 500}).json()
    assert j["tf"] == 60 and j["truncated"] and len(j["bars"]) <= 501
    assert j["bars"][-1][0] == max(b[0] for b in j["bars"])
    # ТФ меньше родного поднимается до родного
    assert client.get("/api/candles", params={"tf": 5, "start": T0, "end": T0 + 10 * M}).json()["tf"] == 60
