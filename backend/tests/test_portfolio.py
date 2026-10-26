from app.portfolio import build_portfolio

DAY = 86_400_000


def tr(day, pnl, reason="TP", side="long", notional=None, margin=None):
    return {"exit_ts": day * DAY, "entry_ts": day * DAY - 3600_000, "pnl_usd": pnl, "reason": reason, "side": side, "notional": notional, "margin": margin}


def row(i, name, trades, symbol="BTC/USDT:USDT", paused=False, deposit=1000.0):
    return {"id": i, "name": name, "symbol": symbol, "paused": paused, "deposit": deposit, "trades": trades, "stats": {"trades": len(trades), "total_usd": sum(t["pnl_usd"] for t in trades if t["reason"] != "OPEN"), "winrate": 50.0}, "error": None}


def test_portfolio_curves_correlation_and_exposure():
    a_days = [10 + i for i in range(20)]
    a = row(1, "A", [tr(d, 10 if d % 2 else -5) for d in a_days] + [tr(40, 0, "OPEN", "long", 500, 100)])
    b = row(2, "B", [tr(d, 20 if d % 2 else -10) for d in a_days] + [tr(40, 0, "OPEN", "long", 300, 60)])          # движется синхронно с A
    c = row(3, "C", [tr(d, -10 if d % 2 else 5) for d in a_days] + [tr(40, 0, "OPEN", "short", 200, 40)])         # зеркально A
    p = row(4, "Пауза", [tr(11, 999)], paused=True)
    bad = {"id": 5, "name": "Сломан", "symbol": None, "paused": False, "error": "нет данных"}
    r = build_portfolio([a, b, c, p, bad])
    assert [i["name"] for i in r["items"]] == ["A", "B", "C", "Пауза", "Сломан"] and r["items"][4]["error"] == "нет данных"
    assert r["portfolio"]["strategies"] == 3 and r["portfolio"]["deposit"] == 3000.0                            # пауза в портфель не входит
    assert abs(r["portfolio"]["total_usd"] - (sum(i["total_usd"] for i in r["items"][:3]))) < 1e-9
    ids = r["correlation"]["ids"]
    m = r["correlation"]["matrix"]
    assert ids == [1, 2, 3] and m[0][0] == 1.0
    assert m[0][1] > 0.99 and m[0][2] < -0.99                                                                   # A и B вместе, C против них
    ex = r["exposure"]
    btc = ex["symbols"][0]
    assert btc["asset"] == "BTC" and btc["long"] == 800 and btc["short"] == 200 and btc["net"] == 600 and ex["gross"] == 1000
    text = " ".join(ex["warnings"])
    assert "одновременно в лонге" in text and "противоположные" in text


def test_correlation_needs_history_and_real_exchange_block():
    a = row(1, "A", [tr(1, 5), tr(2, -3)])
    b = row(2, "B", [tr(1, 2), tr(2, 1)])
    r = build_portfolio([a, b], real={"equity": 50.0, "positions": [{"symbol": "BTC/USDC:USDC", "notional": 400.0}]})
    assert r["correlation"]["matrix"][0][1] is None and "10 дней" in r["correlation"]["note"]
    assert r["exposure"]["real"] == {"equity": 50.0, "gross": 400.0, "leverage": 8.0}
    assert build_portfolio([])["portfolio"]["strategies"] == 0


def test_infinite_profit_factor_is_json_safe():
    import json
    r = row(1, "A", [tr(1, 5)])
    r["stats"]["profit_factor"] = float("inf")
    out = build_portfolio([r])
    json.dumps(out, allow_nan=False)                                       # inf/nan не должны попадать в ответ API
    assert out["items"][0]["profit_factor"] is None


def test_strategies_without_closed_trades_do_not_break_portfolio():
    r = build_portfolio([row(1, "A", [tr(5, 0, "OPEN", "long", 100, 10)]), row(2, "B", [])])
    assert r["correlation"]["days"] is None and r["correlation"]["matrix"][0][1] is None and r["exposure"]["gross"] == 100
