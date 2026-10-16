import csv
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import load_settings
from app.core import backtest, topcoins
from app.core.signal_parser import parse_top_coin
from app.main import create_app
from app.markets import MarketsCache
from test_api import H, M, T0

DAY = 86_400_000
AVAILABLE = {"BTC/USDT:USDT", "SOL/USDT:USDT", "TIA/USDT:USDT", "1000PEPE/USDT:USDT"}


def test_parse_top_coin():
    t = "Deep Local: 1\nTop Coin\n————————————————\nTIA-USD      | 10.12\nPAXG-USD | 6.42\n"
    assert parse_top_coin(t) == {"coin": "TIA-USD", "value": 10.12}
    assert parse_top_coin("Top Coin\nWBETHUSDT | 12.40\nBTCUSDT | 3") == {"coin": "WBETHUSDT", "value": 12.4}
    assert parse_top_coin("нет такого блока\nTIA-USD | 3") is None
    assert parse_top_coin("") is None


def test_resolve_symbol():
    assert topcoins.split_coin("TIA-USD") == "TIA"
    assert topcoins.split_coin("wbethusdt") == "WBETH"
    assert topcoins.resolve_symbol("TIA-USD", AVAILABLE) == "TIA/USDT:USDT"
    assert topcoins.resolve_symbol("PEPEUSDT", AVAILABLE) == "1000PEPE/USDT:USDT"       # контракт «пачкой»
    assert topcoins.resolve_symbol("WBETHUSDT", AVAILABLE) is None
    assert topcoins.resolve_symbol("", AVAILABLE) is None


def _p(**kw):
    p = dict(top_tps=[{"pct": 5.0, "share": 100.0}], top_side="long", top_sl_pct=3.0, top_size=100.0, top_fee=0.0, top_slip=0.0,
             top_days=3.0, top_min_value=0.0, top_be_after_tp1=False)
    p.update(kw)
    return p


def _candles(start, n, step=0.0004, base=100.0):
    price, rows = base, []
    for i in range(n):
        price *= 1 + step
        rows.append((start + i * M, price, price * 1.0002, price * 0.9998, price))
    return rows


def _build(triggers, covered, candles, p=None, now_ms=T0 + 30 * DAY, available=AVAILABLE):
    return topcoins.build_top(triggers, p or _p(), available, lambda s: covered.get(s, []), lambda s: candles[s], M, now_ms)


def test_build_top_trade_and_skips():
    sym = "TIA/USDT:USDT"
    candles = {sym: _candles(T0, 3 * 1440 + 10)}
    covered = {sym: [(T0, T0 + (3 * 1440 + 9) * M)]}
    trig = [
        {"ts": T0 + 5 * M, "d": 1, "coin": "TIA-USD", "value": 10.0},
        {"ts": T0 + 6 * H // 60, "d": 1, "coin": "TIA-USD", "value": 9.0},     # позиция по монете ещё открыта или уже закрыта - зависит от выхода
        {"ts": T0 + 10 * M, "d": 1, "coin": None, "value": None},
        {"ts": T0 + 11 * M, "d": 1, "coin": "WBETHUSDT", "value": 12.0},
        {"ts": T0 + 12 * M, "d": -1, "coin": "TIA-USD", "value": 8.0},          # сторона не включена -> не пропуск
    ]
    res = _build(trig, covered, candles)
    t0 = res["trades"][0]
    assert t0["symbol"] == sym and t0["side"] == "long" and t0["entry_ts"] == T0 + 5 * M
    assert t0["reason"] == "TP" and t0["partial"] is False and t0["pnl_usd"] == pytest.approx(5.0, rel=0.05)    # 5% от 100$
    reasons = sorted(s["reason"] for s in res["skipped"])
    assert "в отчёте нет топ-коина" in reasons
    assert "нет бессрочного USDT-контракта на бирже" in reasons
    assert len(res["skipped"]) - len([s for s in res["skipped"] if s["symbol"]]) == 2
    assert not res["pending"]

    both = _build(trig, covered, candles, _p(top_side="both"))
    assert any(s["side"] == "short" for s in both["skipped"] + both["trades"]) or any(t["side"] == "short" for t in both["trades"])
    assert _build(trig, covered, candles, _p(top_min_value=9.5))["trades"][0]["top_value"] == 10.0
    low = _build(trig, covered, candles, _p(top_min_value=11.0))
    assert not low["trades"] and sum("ниже порога" in s["reason"] for s in low["skipped"]) == 2


def test_build_top_position_busy_and_time_exit():
    sym = "TIA/USDT:USDT"
    flat = _candles(T0, 3 * 1440 + 10, step=0.0)         # цена не движется: ни TP, ни SL
    candles, covered = {sym: flat}, {sym: [(T0, T0 + (3 * 1440 + 9) * M)]}
    trig = [{"ts": T0 + M, "d": 1, "coin": "TIA", "value": 5.0}, {"ts": T0 + 2 * H, "d": 1, "coin": "TIA", "value": 6.0}]
    res = _build(trig, covered, candles)
    assert len(res["trades"]) == 1 and res["trades"][0]["reason"] == "TIME" and res["trades"][0]["fills"][-1]["label"] == "TIME"
    assert res["trades"][0]["exit_ts"] == T0 + 3 * DAY + M or res["trades"][0]["exit_ts"] <= T0 + M + 3 * DAY
    assert [s["reason"] for s in res["skipped"]] == ["по этой монете уже открыта позиция"]


def test_build_top_pending_and_partial_and_young_coin():
    sym = "TIA/USDT:USDT"
    trig = [{"ts": T0 + M, "d": 1, "coin": "TIA-USD", "value": 5.0}]
    res = _build(trig, {}, {})
    assert res["trades"] == [] and res["pending"][sym] == [(T0 + M, T0 + M + 3 * DAY)]
    assert res["skipped"][0]["pending"] is True
    assert topcoins.estimate_requests(res["pending"], M) == 5                    # 4321 свеча по 1000 за запрос

    # часть окна покрыта: докачать нужно только недостающее
    cov = {sym: [(T0, T0 + 1000 * M)]}
    res = _build(trig, cov, {})
    assert res["pending"][sym] == [(T0 + 1001 * M, T0 + M + 3 * DAY)]

    # окно ещё не закончилось (сигнал свежий): считаем до последней закрытой свечи, сделка помечена partial
    now = T0 + M + 600 * M
    cov = {sym: [(T0, backtest.last_closed_start(M, now))]}
    res = _build(trig, cov, {sym: _candles(T0, 700, step=0.0)}, now_ms=now)
    assert res["trades"][0]["partial"] is True and res["trades"][0]["reason"] == "OPEN"

    # монеты ещё не было на бирже: данные пусты, окно закрыто как «пустое»
    cov = {sym: [(T0, T0 + 3 * DAY + M)]}
    res = _build(trig, cov, {sym: _candles(T0 + 2 * DAY, 100)})
    assert res["trades"] == [] and res["skipped"][0]["reason"] == "монеты ещё не было на бирже"


# ---------------- API ----------------
def _history(path):
    text = "Top Coin\n————————————————\nSOL-USD      | 10.12\nBTC-USD | 6.42\n"
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["id", "date_utc", "deep_global", "deep_local", "deep_scalp", "liq_long_k", "liq_short_k", "text"])
        for i in range(8):
            ts = T0 + i * H // 2
            w.writerow([i + 1, datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat(), 1.0, 2.0, 2.5, 10.0, 20.0, text])


@pytest.fixture()
def api(tmp_path, monkeypatch):
    _history(tmp_path / "history.csv")
    cache = tmp_path / "cache"
    cache.mkdir()
    sym = "BTC/USDT:USDT"
    with open(backtest.cache_path("binanceusdm", sym, "1m", str(cache)), "w", newline="") as f:
        csv.writer(f).writerows(_candles(T0, 1200))
    for k, v in dict(HISTORY_FILE=str(tmp_path / "history.csv"), CACHE_DIR=str(cache), SOURCES="", LIQ_FILE="", LIQ_SOURCE="",
                     SYMBOLS=sym, SYMBOL_DEFAULT=sym, SYMBOL="", TP_LEVELS="", BE_AFTER_TP1="", CONFIRM="2", TP_PCT="3", SL_PCT="1",
                     STATE_FILE=str(tmp_path / "s.json"), ASSETS_FILE=str(tmp_path / "a.json"), COOL_DROP="", COOL_DECLINES="",
                     LIQ_HOURS="", COOLDOWN_H="0", SIDE="long", ONLY="", TOP_ENABLED="true", TOP_SIDES="long", TOP_TP_LEVELS="",
                     TOP_TP_PCT="5", TOP_SL_PCT="3", TOP_SIZE_USD="100", TOP_FEE="0", TOP_SLIPPAGE="0", TOP_DAYS="3",
                     TOP_MIN_VALUE="0", TOP_BE_AFTER_TP1="").items():
        monkeypatch.setenv(k, v)
    calls = []

    def top_runner(job, progress):
        calls.append(job.payload["windows"])
        job.total = len(job.payload["windows"])
        for s, ws in job.payload["windows"].items():
            with open(backtest.cache_path("binanceusdm", s, "1m", str(cache)), "w", newline="") as f:
                csv.writer(f).writerows(_candles(T0, 3 * 1440 + 600))
            job.done += 1
        progress(1, T0)

    c = TestClient(create_app(load_settings(), markets=MarketsCache(lambda: {"BTC/USDT:USDT", "SOL/USDT:USDT"}), top_runner=top_runner))
    c.calls = calls
    return c


def _wait(c, job_id, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        j = next(x for x in c.get("/api/jobs").json()["jobs"] if x["id"] == job_id)
        if j["status"] in ("done", "error"):
            return j
        time.sleep(0.05)
    raise AssertionError("задача не завершилась")


def test_api_top_flow(api):
    j = api.post("/api/backtest", json={}).json()
    top = j["top"]
    assert top["enabled"] and top["trades"] == [] and top["pending"]["symbols"] == 1
    sig_n = len(j["signals"])
    assert top["pending"]["items"][0]["symbol"] == "SOL/USDT:USDT" and top["skipped_total"] == sig_n

    d = api.post("/api/top/download", json={}).json()
    assert d["job"]["kind"] == "top" and d["created"] and d["windows"] >= 1
    job = _wait(api, d["job"]["id"])
    assert job["status"] == "done" and job["warning"] is None and job["done"] == 1 and job["label"].startswith("Топ-коины")
    assert "payload" not in job
    assert list(api.calls[0]) == ["SOL/USDT:USDT"]

    top = api.post("/api/backtest", json={}).json()["top"]
    assert top["pending"]["windows"] == 0
    t = top["trades"][0]
    assert all(x["entry_ts"] >= prev["exit_ts"] for prev, x in zip(top["trades"], top["trades"][1:]))      # по монете одна позиция
    assert t["symbol"] == "SOL/USDT:USDT" and t["coin"] == "SOL-USD" and t["top_value"] == 10.12 and t["side"] == "long"
    n = len(top["trades"])
    assert top["stats"]["trades"] == n and top["by_coin"][0]["symbol"] == "SOL/USDT:USDT" and top["by_coin"][0]["trades"] == n
    assert top["skipped_total"] + n == sig_n                                    # остальные сигналы: позиция по монете уже открыта

    # график монеты, которой нет в списке активов, но свечи скачаны
    r = api.get("/api/candles", params={"symbol": "SOL/USDT:USDT", "start": t["entry_ts"], "end": t["exit_ts"]})
    assert r.status_code == 200 and r.json()["bars"]
    assert api.get("/api/candles", params={"symbol": "XXX/USDT:USDT"}).status_code == 422
    assert api.post("/api/top/download", json={}).json()["job"] is None          # скачивать больше нечего

    # параметры топ-коинов влияют на результат, можно выключить
    off = api.post("/api/backtest", json={"top_enabled": False}).json()["top"]
    assert off == {"enabled": False}
    big = api.post("/api/backtest", json={"top_size": 1000}).json()["top"]
    assert big["trades"][0]["pnl_usd"] == pytest.approx(top["trades"][0]["pnl_usd"] * 10)
    assert api.post("/api/backtest", json={"top_tps": [{"pct": 2, "share": 50}]}).status_code == 422
    assert api.post("/api/backtest", json={"top_days": 0}).status_code == 422
    two = api.post("/api/backtest", json={"top_tps": [{"pct": 1, "share": 50}, {"pct": 2, "share": 50}], "top_be_after_tp1": True}).json()["top"]
    assert two["trades"][0]["tp_hits"] == 2 and two["trades"][0]["be"] is True


def test_api_top_markets_unavailable(api, monkeypatch):
    def boom():
        from app.markets import MarketsError
        raise MarketsError("нет сети")
    api.app.state.store  # приложение создано
    c = TestClient(create_app(load_settings(), markets=MarketsCache(boom)))
    j = c.post("/api/backtest", json={}).json()
    assert any("список рынков биржи недоступен" in w for w in j["warnings"])
    assert j["top"]["trades"] == [] and j["top"]["skipped"][0]["reason"] == "нет бессрочного USDT-контракта на бирже"
