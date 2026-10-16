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
