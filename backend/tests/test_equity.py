from app.core.equity import mtm_drawdown

M = 60_000
T0 = 1_700_000_000_000 // M * M


def candles(prices):
    """prices: [(low, high, close)] по минутам; open = предыдущий close."""
    out, prev = [], prices[0][2]
    for i, (lo, hi, c) in enumerate(prices):
        out.append((T0 + i * M, prev, max(hi, prev, c), min(lo, prev, c), c))
        prev = c
    return out


def leg(entry_i, exit_i, entry, exit_px, notional, pnl_usd, reason="TP", fills=None, side="long"):
    return {"side": side, "entry_ts": T0 + entry_i * M, "exit_ts": T0 + exit_i * M, "entry": entry, "exit": exit_px, "notional": notional, "pnl_usd": pnl_usd,
            "reason": reason, "fills": fills or [{"ts": T0 + exit_i * M, "price": exit_px, "share": 1.0, "label": reason}]}


def test_drawdown_inside_a_winning_trade_is_visible():
    # лонг 1000$ от 100: внутри сделки цена проседает до 90 (−10% → −100$), потом тейк на 103 (+30$). По закрытым сделкам просадки нет, по рынку — 100$ (1%)
    c = candles([(100, 100, 100)] * 5 + [(90, 100, 95)] * 5 + [(95, 103, 103)] * 3)
    t = leg(0, 11, 100.0, 103.0, 1000.0, 30.0)
    r = mtm_drawdown([t], c, 10000.0, max_blocks=15000)
    assert abs(r["max_dd_real_usd"] - 100.0) < 1e-6 and abs(r["max_dd_real_pct"] - 1.0) < 1e-6 and abs(r["min_equity_usd"] - 9900.0) < 1e-6


def test_leverage_and_overlapping_legs_add_up():
    c = candles([(100, 100, 100)] * 3 + [(95, 100, 96)] * 3 + [(96, 100, 100)] * 3)
    a = leg(0, 8, 100.0, 100.0, 5000.0, 0.0, reason="OPEN")             # 5× от 1000$ маржи: −5% → −250$
    b = leg(2, 8, 100.0, 100.0, 2000.0, 0.0, reason="OPEN")             # добор: −5% → −100$
    r = mtm_drawdown([a, b], c, 10000.0)
    assert abs(r["max_dd_real_usd"] - 350.0) < 1e-6
    short = leg(0, 8, 100.0, 100.0, 1000.0, 0.0, reason="OPEN", side="short")
    r = mtm_drawdown([short], candles([(100, 100, 100)] * 3 + [(100, 110, 108)] * 3 + [(100, 100, 100)] * 3), 10000.0)
    assert abs(r["max_dd_real_usd"] - 100.0) < 1e-6                     # шорт страдает от high


def test_closed_profit_raises_peak_and_stop_exit_is_not_worse_than_exit_price():
    c = candles([(100, 100, 100)] * 2 + [(100, 120, 120)] * 2 + [(110, 120, 115), (60, 120, 100)])
    win = leg(0, 2, 100.0, 120.0, 1000.0, 200.0)                         # +200$: тейк на 120 достигнут в 2-й минуте
    loss = leg(4, 5, 120.0, 100.0, 1000.0, -166.67, reason="SL")        # вход 120, стоп 100: low свечи 60, но выход не хуже 100
    r = mtm_drawdown([win, loss], c, 10000.0)
    assert abs(r["max_dd_real_usd"] - 166.67) < 0.5 and r["max_dd_real_pct"] < 2.0


def test_liquidated_leg_loses_margin_and_empty_inputs():
    c = candles([(100, 100, 100)] * 2 + [(80, 100, 80)] * 2)
    liq = leg(0, 2, 100.0, 90.0, 5000.0, -500.0, reason="LIQ")           # 10× от 500$ маржи: теряется вся маржа
    r = mtm_drawdown([liq], c, 10000.0)
    assert abs(r["max_dd_real_usd"] - 500.0) < 1e-6 and abs(r["max_dd_real_pct"] - 5.0) < 1e-6
    assert mtm_drawdown([], c, 10000.0) == {"max_dd_real_usd": 0.0, "max_dd_real_pct": 0.0, "min_equity_usd": 10000.0}


def test_block_aggregation_keeps_result_close():
    prices = [(100, 100, 100)] * 100 + [(90, 100, 95)] * 100 + [(95, 103, 103)] * 100
    c = candles(prices)
    t = leg(0, 299, 100.0, 103.0, 1000.0, 30.0)
    exact = mtm_drawdown([t], c, 10000.0, max_blocks=100000)
    coarse = mtm_drawdown([t], c, 10000.0, max_blocks=20)
    assert abs(exact["max_dd_real_usd"] - coarse["max_dd_real_usd"]) < 1e-6
