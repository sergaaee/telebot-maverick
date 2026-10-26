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


def _candles(prices, t0=0, step=60_000):
    """prices: [(open, high, low, close)] по минутам."""
    return [(t0 + i * step, o, h, l, c) for i, (o, h, l, c) in enumerate(prices)]


def _trade(side, entry, notional, entry_i, exit_i, exit_px, pnl, reason="TP"):
    return {"side": side, "entry": entry, "notional": notional, "entry_ts": entry_i * 60_000, "exit_ts": exit_i * 60_000, "exit": exit_px, "pnl_usd": pnl, "reason": reason,
            "fills": [{"ts": exit_i * 60_000, "price": exit_px, "share": 1.0}]}


def test_mtm_drawdown_sees_the_dip_inside_a_trade_with_leverage():
    from app.core.equity import mtm_drawdown_multi
    c = _candles([(100, 101, 99, 100), (100, 100, 90, 95), (95, 111, 94, 110), (110, 110, 110, 110)])
    t = _trade("long", 100.0, 1000.0, 0, 3, 110.0, 100.0)                       # объём 1000 $ (плечо уже внутри), по дороге цена падала до 90, закрылась +10%
    r = mtm_drawdown_multi([([t], c)], 1000.0)
    assert abs(r["max_dd_real_usd"] - 100.0) < 1e-9 and abs(r["max_dd_real_pct"] - 10.0) < 1e-9 and abs(r["min_equity_usd"] - 900.0) < 1e-9
    assert r["max_dd_real_ts"] is not None
    # по закрытым сделкам просадки не было совсем — именно это и занижало показатель
    assert build_portfolio([{**srow(1, "A", [{**leg(0, 3, 10.0), "pnl_usd": 100.0}]), "candles": c}])["portfolio"]["max_dd_usd"] == 0.0


def test_mtm_drawdown_of_two_strategies_on_one_account_adds_up():
    from app.core.equity import mtm_drawdown_multi
    btc = _candles([(100, 100, 100, 100), (100, 100, 90, 95), (95, 100, 95, 100), (100, 100, 100, 100)])
    eth = _candles([(50, 50, 50, 50), (50, 52, 45, 48), (48, 50, 48, 50), (50, 50, 50, 50)])
    a = _trade("long", 100.0, 1000.0, 0, 3, 100.0, 0.0, "BE")
    b = _trade("long", 50.0, 500.0, 0, 3, 50.0, 0.0, "BE")
    r = mtm_drawdown_multi([([a], btc), ([b], eth)], 2000.0)
    assert abs(r["max_dd_real_usd"] - (100 + 50)) < 1e-9                         # обе просели в одном блоке: 10% от 1000 и 10% от 500
    p = build_portfolio([{**srow(1, "A", [{**leg(0, 3, 0.0), "entry": 100.0, "side": "long", "fills": [], "notional": 1000.0}]), "candles": btc}])
    assert p["portfolio"]["max_dd_real_pct"] is not None


def test_what_if_sizing_rescales_trades_in_both_modes():
    a = srow(1, "A", [leg(0, 10, 5.0)], deposit=1000.0)                         # в конфиге 10% × 2 = 20% депозита
    b = srow(2, "B", [leg(0, 10, 5.0)], deposit=1000.0)
    sep = build_portfolio([a, b], sizing={1: {"trade_pct": 30.0}})              # у A доля 30% вместо 10%, плечо прежнее
    ia, ib = sep["items"]
    assert ia["overridden"] and ia["trade_pct"] == 30 and ia["cfg_trade_pct"] == 10 and not ib["overridden"]
    assert abs(ia["total_usd"] - 1000 * 0.30 * 2 * 0.05) < 1e-9                   # 30 $ маржи... 1000 × 30% × 2× × 5% = 30 $
    assert sep["settings"]["sizing"] == {"1": {"trade_pct": 30.0}}
    j = build_portfolio([a, b], total_deposit=2000.0, sizing={"2": {"leverage": 4.0}})
    assert abs(j["items"][0]["total_usd"] - 2000 * 0.10 * 2 * 0.05) < 1e-9        # A: от общих 2000
    assert abs(j["items"][1]["total_usd"] - 2000 * 0.10 * 4 * 0.05) < 1e-9        # B: плечо 4× вместо 2×
    same = build_portfolio([a, b], sizing={1: {"trade_pct": 10.0, "leverage": 2.0}})
    assert not same["items"][0]["overridden"] and same["settings"]["sizing"] == {}  # равно конфигу — не «что если»


def test_compounding_switch_for_the_whole_portfolio():
    # две сделки одной стратегии подряд: первая +10% на объём, вторая -10% на объём; 50% депозита в объёме (25% × 2×)
    t = [leg(0, 10, 10.0), leg(20, 30, -10.0)]
    a = srow(1, "A", t, deposit=1000.0, trade_pct=25.0, leverage=2.0)
    off = build_portfolio([a], compound=False)["items"][0]
    # без сложного процента обе сделки считаются от начальных 1000: +50 и -50
    assert abs(off["total_usd"]) < 1e-9 and off["compound"] is False and off["cfg_compound"] is True
    # со сложным: первая +50 -> баланс 1050, вторая 1050 × 50% × -10% = -52.5
    cfg_off = srow(1, "A", t, deposit=1000.0, trade_pct=25.0, leverage=2.0, compound=False)
    forced = build_portfolio([cfg_off], compound=True)
    assert abs(forced["items"][0]["total_usd"] - (50 - 52.5)) < 1e-9 and forced["settings"]["compound"] is True      # «для всех» важнее конфига
    # общий депозит: тот же переключатель
    j_on = build_portfolio([a], total_deposit=2000.0, compound=True)["portfolio"]["total_usd"]
    j_off = build_portfolio([a], total_deposit=2000.0, compound=False)["portfolio"]["total_usd"]
    assert abs(j_off) < 1e-9 and abs(j_on - (100 - 105)) < 1e-9
    assert build_portfolio([a])["settings"]["compound"] is None
