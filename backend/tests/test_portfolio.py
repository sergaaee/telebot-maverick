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


H = 3_600_000
SZ = {"trade_pct": 10.0, "leverage": 2.0, "pyramid_pct": None, "pyramid_leverage": None, "compound": True}


def leg(entry_h, exit_h, pnl_pct, reason="TP", side="long", leg_no=1):
    return {"entry_ts": entry_h * H, "exit_ts": exit_h * H, "pnl_pct": pnl_pct, "pnl_usd": 0.0, "reason": reason, "side": side, "leg": leg_no, "notional": 0.0, "margin": 0.0}


def srow(i, name, trades, deposit=500.0, **sz):
    return {"id": i, "name": name, "symbol": "BTC/USDT:USDT", "paused": False, "deposit": deposit, "trades": trades, "stats": {"trades": len(trades), "total_usd": 0.0}, "error": None,
            "sizing": {**SZ, **sz}}


def test_common_deposit_sizes_every_trade_from_the_shared_balance():
    a = srow(1, "A", [leg(0, 10, 5.0), leg(30, 40, -2.0)])             # у каждой «свой» депозит 500, но торгуют они с общего 1000
    b = srow(2, "B", [leg(5, 20, 10.0)], deposit=99999.0, trade_pct=20.0, leverage=3.0)
    r = build_portfolio([a, b], total_deposit=1000.0)
    assert r["portfolio"]["joint"] and r["portfolio"]["deposit"] == 1000.0 and r["settings"]["total_deposit"] == 1000.0
    ia, ib = r["items"][0], r["items"][1]
    # A#1: 10% от 1000 × 2 = 200 $ объёма, +5% -> +10 $ (закрылась в час 10)
    # B#1 открывается в час 5, ДО закрытия A#1 -> база тоже 1000: 20% × 3 = 600 $ объёма, +10% -> +60 $ (закрылась в час 20)
    # A#2 в час 30: баланс 1000 + 10 + 60 = 1070 -> 10% × 2 = 214 $, -2% -> -4.28 $
    assert abs(ia["total_usd"] - (10 - 4.28)) < 1e-9 and abs(ib["total_usd"] - 60) < 1e-9
    assert abs(r["portfolio"]["total_usd"] - (10 - 4.28 + 60)) < 1e-9 and abs(r["portfolio"]["return_pct"] - 6.572) < 1e-9
    assert [round(p[1], 2) for p in ia["curve"]] == [10.0, 5.72] and r["portfolio"]["curve"][-1][1] == r["portfolio"]["total_usd"]
    # пик маржи: в часы 5–10 открыты обе -> 100 + 200 = 300 $ = 30% общего депозита
    assert abs(r["portfolio"]["peak_margin"] - 300.0) < 1e-9 and abs(r["portfolio"]["peak_margin_pct"] - 30.0) < 1e-9
    # без общего депозита каждая стратегия считается от своего (тут stats подставлены как есть)
    sep = build_portfolio([a, b])
    assert not sep["portfolio"]["joint"] and sep["portfolio"]["deposit"] == 500.0 + 99999.0 and sep["settings"]["total_deposit"] is None


def test_common_deposit_warns_when_margin_does_not_fit():
    big = [srow(i, f"S{i}", [leg(0, 10, 1.0)], trade_pct=60.0, leverage=2.0) for i in (1, 2)]           # две стратегии по 60% маржи на один депозит
    r = build_portfolio(big, total_deposit=1000.0)
    assert r["portfolio"]["peak_margin_pct"] == 120.0 and any("пик одновременно занятой маржи" in w for w in r["exposure"]["warnings"])


def test_excluded_strategies_are_left_out_of_every_calculation():
    days = list(range(10, 30))
    mk = lambda i, name, sign: row(i, name, [tr(d, sign * (10 if d % 2 else -5)) for d in days] + [tr(40, 0, "OPEN", "long", 100, 10)])
    r = build_portfolio([mk(1, "A", 1), mk(2, "B", 1), mk(3, "C", -1)], exclude=[3])
    assert [i["included"] for i in r["items"]] == [True, True, False] and r["items"][2]["curve"]               # её цифры видны, чтобы можно было вернуть
    assert r["portfolio"]["strategies"] == 2 and r["portfolio"]["deposit"] == 2000.0 and r["correlation"]["ids"] == [1, 2]
    assert r["exposure"]["gross"] == 200 and r["settings"]["exclude"] == [3]
    assert build_portfolio([mk(1, "A", 1)], exclude=[1])["portfolio"]["strategies"] == 0
