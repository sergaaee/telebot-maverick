import csv
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.config import load_settings
from app.core import backtest
from app.main import create_app
from app.markets import MarketsCache, MarketsError, normalize_symbol
from test_api import H, M, T0, _write_history

EXCHANGE_SYMBOLS = {"BTC/USDT:USDT", "ETH/USDT:USDT", "ETHW/USDT:USDT", "SOL/USDT:USDT"}


def _write_candles(cache_dir, symbol, n=600, base=100.0):
    path = backtest.cache_path("binanceusdm", symbol, "1m", str(cache_dir))
    price, rows = base, []
    for i in range(n):
        price *= 1.0004
        rows.append((T0 + i * M, price, price * 1.0002, price * 0.9998, price))
    with open(path, "w", newline="") as f:
        csv.writer(f).writerows(rows)


class Env:
    """Приложение с подменённой биржей и загрузчиком свечей; gate позволяет «подержать» загрузку, чтобы увидеть прогресс."""

    def __init__(self, tmp_path, monkeypatch):
        self.tmp, self.mp = tmp_path, monkeypatch
        _write_history(tmp_path / "history.csv", [(T0 + i * H // 2, 2.0, 2.5, 1.0, 1.0) for i in range(8)])
        self.cache = tmp_path / "cache"
        self.cache.mkdir()
        _write_candles(self.cache, "BTC/USDT:USDT")
        for k, v in dict(HISTORY_FILE=str(tmp_path / "history.csv"), CACHE_DIR=str(self.cache), SOURCES="", LIQ_FILE="", LIQ_SOURCE="",
                         SYMBOLS="BTC/USDT:USDT", SYMBOL_DEFAULT="BTC/USDT:USDT", SYMBOL="", TP_LEVELS="", BE_AFTER_TP1="", CONFIRM="2",
                         TP_PCT="3", SL_PCT="1", STATE_FILE=str(tmp_path / "s.json"), ASSETS_FILE=str(tmp_path / "assets.json")).items():
            monkeypatch.setenv(k, v)
        self.gate = threading.Event()
        self.gate.set()
        self.fail = None
        self.calls = []
        self.markets_error = None

    def provider(self):
        if self.markets_error:
            raise MarketsError(self.markets_error)
        return EXCHANGE_SYMBOLS

    def runner(self, symbol, progress):
        self.calls.append(symbol)
        progress(3, T0)
        assert self.gate.wait(10), "загрузка не была разрешена в тесте"
        if self.fail:
            raise self.fail
        _write_candles(self.cache, symbol, base=10.0)
        progress(600, T0 + 599 * M)

    def client(self):
        return TestClient(create_app(load_settings(), markets=MarketsCache(self.provider), runner=self.runner))

    def wait(self, c, job_id, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            j = next(x for x in c.get("/api/jobs").json()["jobs"] if x["id"] == job_id)
            if j["status"] in ("done", "error"):
                return j
            time.sleep(0.05)
        raise AssertionError("задача не завершилась")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


def test_normalize_symbol():
    cases = {"eth": "ETH/USDT:USDT", "ETHUSDT": "ETH/USDT:USDT", "eth/usdt": "ETH/USDT:USDT", " ETH/USDT:USDT ": "ETH/USDT:USDT",
             "1000pepe": "1000PEPE/USDT:USDT", "BTCUSDC": "BTC/USDC:USDC", "usdt": "USDT/USDT:USDT"}
    for raw, want in cases.items():
        assert normalize_symbol(raw) == want, raw
    for bad in ("", "   "):
        with pytest.raises(ValueError):
            normalize_symbol(bad)


def test_add_asset_downloads_in_background_and_notifies(env):
    c = env.client()
    env.gate.clear()                                      # загрузка «идёт»: видим прогресс до завершения
    r = c.post("/api/assets", json={"symbol": "eth"})
    assert r.status_code == 200
    body = r.json()
    assert body["symbol"] == "ETH/USDT:USDT" and body["added"] is True and body["job"]["symbol"] == "ETH/USDT:USDT"
    cfg = c.get("/api/config").json()
    assert "ETH/USDT:USDT" in cfg["symbols"] and cfg["user_symbols"] == ["ETH/USDT:USDT"]
    row = next(a for a in cfg["data"]["assets"] if a["symbol"] == "ETH/USDT:USDT")
    assert row["user"] is True and row["count"] == 0 and row["error"]            # свечей ещё нет — но актив уже в списке
    end = time.time() + 5                                  # прогресс виден, пока загрузка не закончена
    while time.time() < end and next(j for j in c.get("/api/jobs").json()["jobs"])["count"] != 3:
        time.sleep(0.05)
    job = next(j for j in c.get("/api/jobs").json()["jobs"])
    assert job["status"] == "running" and job["count"] == 3 and job["acked"] is False
    env.gate.set()
    done = env.wait(c, body["job"]["id"])
    assert done["status"] == "done" and done["count"] == 600 and done["finished_at"] >= done["started_at"]
    cfg = c.get("/api/config").json()
    row = next(a for a in cfg["data"]["assets"] if a["symbol"] == "ETH/USDT:USDT")
    assert row["count"] == 600 and row["error"] is None and cfg["timeframes"]["ETH/USDT:USDT"][0] == 60   # данные подхвачены без перезапуска
    j = c.post("/api/backtest", json={"symbol": "ETH/USDT:USDT"}).json()
    assert j["symbol"] == "ETH/USDT:USDT" and j["trades"][0]["entry"] < 20      # бэктест на ценах нового актива
    # уведомление о завершении подтверждается и не показывается повторно
    assert c.post(f"/api/jobs/{done['id']}/ack").json()["acked"] is True
    assert next(x for x in c.get("/api/jobs").json()["jobs"] if x["id"] == done["id"])["acked"] is True
    assert c.post("/api/jobs/999/ack").status_code == 404


def test_added_assets_survive_restart(env):
    c = env.client()
    env.wait(c, c.post("/api/assets", json={"symbol": "SOL"}).json()["job"]["id"])
    c2 = env.client()                                     # «перезапуск» бэкенда
    cfg = c2.get("/api/config").json()
    assert "SOL/USDT:USDT" in cfg["symbols"] and cfg["user_symbols"] == ["SOL/USDT:USDT"]
    assert next(a for a in cfg["data"]["assets"] if a["symbol"] == "SOL/USDT:USDT")["count"] == 600


def test_unknown_symbol_is_rejected_with_suggestions(env):
    c = env.client()
    r = c.post("/api/assets", json={"symbol": "ETHX"})
    assert r.status_code == 422 and "нет на binanceusdm" in r.json()["detail"]
    r = c.post("/api/assets", json={"symbol": "ETHZ"})
    assert "ETH/USDT:USDT" in r.json()["detail"]                              # подсказка с похожими символами
    assert c.get("/api/config").json()["user_symbols"] == [] and env.calls == []     # ничего не добавлено и не качалось
    assert c.post("/api/assets", json={"symbol": "  "}).status_code == 422


def test_exchange_unavailable_gives_clear_error(env):
    env.markets_error = "Не удалось получить список рынков binanceusdm: NetworkError"
    c = env.client()
    r = c.post("/api/assets", json={"symbol": "ETH"})
    assert r.status_code == 503 and "позже" in r.json()["detail"]
    assert c.get("/api/config").json()["user_symbols"] == []


def test_existing_asset_with_candles_is_not_downloaded_again(env):
    c = env.client()
    r = c.post("/api/assets", json={"symbol": "BTC/USDT:USDT"}).json()
    assert r["added"] is False and r["job"] is None and env.calls == []


def test_existing_asset_without_candles_starts_download(env, monkeypatch):
    monkeypatch.setenv("SYMBOLS", "BTC/USDT:USDT,ETH/USDT:USDT")
    c = env.client()
    r = c.post("/api/assets", json={"symbol": "ETH"}).json()
    assert r["added"] is False and r["job"]["symbol"] == "ETH/USDT:USDT"
    assert env.wait(c, r["job"]["id"])["status"] == "done"


def test_refresh_is_a_background_job_and_deduplicated(env):
    c = env.client()
    env.gate.clear()
    a = c.post("/api/candles/refresh", params={"symbol": "BTC/USDT:USDT"}).json()
    b = c.post("/api/candles/refresh", params={"symbol": "BTC/USDT:USDT"}).json()
    assert a["created"] is True and b["created"] is False and a["job"]["id"] == b["job"]["id"]      # одна задача на актив
    assert c.post("/api/candles/refresh", params={"symbol": "DOGE/USDT:USDT"}).status_code == 422
    env.gate.set()
    assert env.wait(c, a["job"]["id"])["status"] == "done"
    assert c.post("/api/candles/refresh").json()["created"] is True               # после завершения можно запустить снова


def test_failed_download_is_reported(env):
    c = env.client()
    env.fail = SystemExit("Биржа не отвечает; прогресс сохранён")
    j = env.wait(c, c.post("/api/assets", json={"symbol": "ETH"}).json()["job"]["id"])
    assert j["status"] == "error" and "Биржа не отвечает" in j["error"]
    env.fail = ValueError("что-то пошло не так")
    j = env.wait(c, c.post("/api/candles/refresh", params={"symbol": "ETH/USDT:USDT"}).json()["job"]["id"])
    assert j["status"] == "error" and "что-то" in j["error"]
    env.fail = None
    j = env.wait(c, c.post("/api/candles/refresh", params={"symbol": "ETH/USDT:USDT"}).json()["job"]["id"])   # и повторная попытка работает
    assert j["status"] == "done"


def test_remove_user_asset(env):
    c = env.client()
    env.wait(c, c.post("/api/assets", json={"symbol": "ETH"}).json()["job"]["id"])
    assert c.delete("/api/assets", params={"symbol": "BTC/USDT:USDT"}).status_code == 422       # актив по умолчанию
    assert c.delete("/api/assets", params={"symbol": "SOL/USDT:USDT"}).status_code == 404        # такого нет
    assert c.delete("/api/assets", params={"symbol": "ETH/USDT:USDT"}).status_code == 200
    assert "ETH/USDT:USDT" not in c.get("/api/config").json()["symbols"]
    assert "ETH/USDT:USDT" not in env.client().get("/api/config").json()["symbols"]               # и после перезапуска
    env.gate.clear()
    jid = c.post("/api/assets", json={"symbol": "SOL"}).json()["job"]["id"]
    assert c.delete("/api/assets", params={"symbol": "SOL/USDT:USDT"}).status_code == 409          # пока качается — нельзя
    env.gate.set()
    env.wait(c, jid)
