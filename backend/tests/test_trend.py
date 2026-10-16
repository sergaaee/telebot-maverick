import pytest

from app.core.trend import build_trend
from test_api import H, M, T0, client  # noqa: F401  (client — фикстура из test_api)


def _c(prices, step=H):
    return [(T0 + i * step, p, p, p, p) for i, p in enumerate(prices)]


def test_trend_up_and_down_no_lookahead():
    up = _c([100 + i for i in range(60)])                       # устойчивый рост: цена выше EMA
    allow = build_trend(up, "1h", [20, 50])
    assert allow(T0 + 55 * H, 1) and not allow(T0 + 55 * H, -1)
    assert not allow(T0 + 30 * H, 1)                            # EMA50 ещё не прогрета: тренд неизвестен
    down = _c([200 - i for i in range(60)])
    allow = build_trend(down, "1h", [20])
    assert allow(T0 + 55 * H, -1) and not allow(T0 + 55 * H, 1)
    # берётся последняя ЗАКРЫТАЯ свеча: внутри часа 55 цена ещё не учтена
    spike = _c([100] * 59 + [500])
    a = build_trend(spike, "1h", [20])
    assert not a(T0 + 59 * H + 30 * M, 1)                       # свеча с 500 ещё не закрылась
    assert a(T0 + 60 * H, 1)                                    # закрылась


def test_trend_aggregates_minutes():
    m = [(T0 + i * M, 100 + i / 60, 0, 0, 100 + i / 60) for i in range(60 * 30)]     # 30 часов минуток, рост
    allow = build_trend(m, "1h", [20])
    assert allow(T0 + 29 * H, 1)


def test_api_trend_filter(client):  # noqa: F811
    base = client.post("/api/backtest", json={}).json()
    assert base["trend"] == {"enabled": False, "blocked": 0, "confirmed": len(base["signals"])}
    # на данных теста свечи — минутки; EMA20 на 5m в начале не прогрета -> часть сигналов отсечена
    r = client.post("/api/backtest", json={"trend_enabled": True, "trend_emas": [20], "trend_tf": "5m"})
    j = r.json()
    assert r.status_code == 200 and j["trend"]["enabled"] and j["trend"]["blocked"] >= 0
    assert len(j["trades"]) <= len(base["trades"])
    assert all(not s["taken"] for s in j["signals"] if s["trend_blocked"])
    assert sum(s["trend_blocked"] for s in j["signals"]) == j["trend"]["blocked"]
    assert client.post("/api/backtest", json={"trend_enabled": True, "trend_emas": [30]}).status_code == 422
    assert client.post("/api/backtest", json={"trend_enabled": True, "trend_emas": []}).status_code == 422
    assert client.post("/api/backtest", json={"trend_enabled": True, "trend_tf": "2h"}).status_code == 422


def test_sizing_deposit_pct_leverage_and_sharpe(client):  # noqa: F811
    a = client.post("/api/backtest", json={"deposit": 1000, "trade_pct": 10, "leverage": 1}).json()
    b = client.post("/api/backtest", json={"deposit": 1000, "trade_pct": 10, "leverage": 5}).json()
    t1, t5 = a["trades"][0], b["trades"][0]
    assert t1["notional"] == pytest.approx(100) and t1["margin"] == pytest.approx(100)
    assert t5["notional"] == pytest.approx(500) and t5["margin"] == pytest.approx(100)
    assert t5["pnl_usd"] == pytest.approx(5 * t1["pnl_usd"]) and t5["pnl_pct"] == pytest.approx(t1["pnl_pct"])      # доходность на объём от плеча не зависит
    if len(a["trades"]) > 1:                                                                              # сложный процент
        assert a["trades"][1]["notional"] == pytest.approx((1000 + t1["pnl_usd"]) * 0.1)
    tot = a["stats"]["total"]
    assert tot["return_pct"] == pytest.approx(100 * tot["total_usd"] / 1000)
    assert tot["max_dd_pct"] >= 0 and "sharpe" in tot
    assert not any("ликвидации" in w for w in a["warnings"])
    w = client.post("/api/backtest", json={"leverage": 100, "sl_pct": 1}).json()["warnings"]
    assert any("ликвидации" in x for x in w)
    for bad in ({"deposit": 0}, {"trade_pct": 0}, {"trade_pct": 101}, {"leverage": 0.5}, {"leverage": 126}):
        assert client.post("/api/backtest", json=bad).status_code == 422


def test_sharpe_math():
    from app.service import sharpe_ratio
    day = 86_400_000
    mk = lambda d, p: {"entry_ts": d * day, "exit_ts": d * day + 1000, "pnl_usd": p}
    assert sharpe_ratio([], 1000) is None
    assert sharpe_ratio([mk(0, 10)], 1000) is None                       # один день
    flat = [mk(d, 10) for d in range(10)]
    assert sharpe_ratio(flat, 1000) is not None and sharpe_ratio(flat, 1000) > 10          # почти без разброса
    mixed = [mk(d, 10 if d % 2 else -5) for d in range(40)]
    s = sharpe_ratio(mixed, 1000)
    assert s > 0
    assert sharpe_ratio([mk(d, -x) for d, x in enumerate([5, 10] * 20)], 1000) < 0


def test_simple_percent_fixed_amount(client):  # noqa: F811
    j = client.post("/api/backtest", json={"deposit": 1000, "trade_pct": 10, "leverage": 2, "compound": False}).json()
    assert len(j["trades"]) > 1
    assert all(t["notional"] == pytest.approx(200) for t in j["trades"])               # всегда 1000 × 10% × 2
    c = client.post("/api/backtest", json={"deposit": 1000, "trade_pct": 10, "leverage": 2, "compound": True}).json()
    assert c["trades"][0]["notional"] == pytest.approx(200)
    assert c["trades"][1]["notional"] != pytest.approx(200)
    assert j["stats"]["total"]["total_usd"] == pytest.approx(sum(t["pnl_pct"] / 100 * 200 for t in j["trades"]))
