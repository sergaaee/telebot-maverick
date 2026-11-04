"""«Стоп-лосс по маркету»: стоп-ордера нет, касание цены им ничего не закрывает. Решает только отчёт: пришёл новый отчёт, метрики ниже порогов входа, а цена всё ещё за стопом —
закрываем по рынку (по open свечи отчёта); метрики сильны или цена вернулась — позиция остаётся и ждёт следующего отчёта."""
import pytest

from app.core.backtest import normalize_tps, run_events, strong_checker

M = 60_000
T0 = 1_700_000_000_000 // M * M
LEVELS = normalize_tps([(3.0, 100)])
TWO = normalize_tps([(1.0, 50), (3.0, 50)])


def flat(n=200, price=100.0):
    return [(T0 + i * M, price, price, price, price) for i in range(n)]


def set_px(c, start, end, price):
    for i in range(start, end):
        c[i] = (T0 + i * M, price, price, price, price)
    return c


def wick(c, minute, low=None, high=None, close=None):
    """Свеча с проколом: open остаётся прежним, low/high уходят, close — как задано (по умолчанию возвращается к open)."""
    t, o, h, l, cl = c[minute]
    c[minute] = (t, o, max(h, high) if high else h, min(l, low) if low else l, close if close is not None else cl)
    return c


def rep(minute, strong):
    return (T0 + minute * M, 2.0 if strong else 0.5, 2.0 if strong else 0.5)


def chk(*reports):
    return strong_checker(list(reports), 1.0, 1.0, (0.4, 0.4))


def ev(minute, d=1):
    return [(T0 + minute * M, d)]


def run(events, candles, reports=None, levels=LEVELS, sl=2.0, market=True, **kw):
    mstop = chk(*reports) if (market and reports is not None) else None
    return run_events(events, candles, levels, sl, fee=0.0, slip=kw.pop("slip", 0.0), skipped=[], mstop=mstop, **kw)


def test_plain_stop_closes_on_touch_but_market_stop_waits_for_a_weak_report():
    c = wick(flat(), 20, low=97.0)
    set_px(c, 21, 200, 97.0)                                                     # цена ушла за стоп (98) на 20-й минуте и осталась там
    plain = run(ev(10), c, market=False)[0]
    assert plain["reason"] == "SL" and plain["exit_ts"] == T0 + 20 * M            # обычный стоп — сразу
    t = run(ev(10), c, [rep(10, True), rep(30, False)])[0]
    assert t["reason"] == "SL" and t["exit_ts"] == T0 + 30 * M and t["exit"] == 97.0 and t["sl_market"] is True      # по рынку на свече отчёта, по её open (хуже стопа 98)
    assert t["pnl_pct"] == pytest.approx(-3.0)


def test_strong_report_below_the_stop_does_not_close_and_the_next_weak_one_does():
    c = set_px(flat(), 20, 200, 97.0)
    t = run(ev(10), c, [rep(10, True), rep(30, True), rep(40, False)])[0]
    assert t["reason"] == "SL" and t["exit_ts"] == T0 + 40 * M                    # на сильном отчёте (30) — ждём, на следующем слабом (40) — выходим
    t = run(ev(10), c, [rep(10, True), rep(30, True), rep(40, True), rep(60, True)])[0]
    assert t["reason"] == "OPEN"                                                  # пока отчёты сильные, убыток ничем не ограничен


def test_price_back_above_the_stop_at_the_report_means_nothing_happens_then_tp():
    c = wick(flat(), 20, low=96.0)                                                # прокол вниз и возврат
    set_px(c, 50, 200, 104.0)                                                     # дальше рост до тейка (103)
    t = run(ev(10), c, [rep(10, True), rep(30, False)])[0]
    assert t["reason"] == "TP" and t["exit_ts"] == T0 + 50 * M and "sl_market" in t


def test_no_new_report_after_the_touch_means_no_exit():
    c = set_px(flat(), 20, 200, 97.0)
    t = run(ev(10), c, [rep(10, True), rep(15, False)])[0]                        # слабый отчёт был ДО касания, цена тогда была выше стопа
    assert t["reason"] == "OPEN"


def test_report_on_the_entry_candle_is_ignored_and_reports_before_entry_too():
    c = set_px(flat(), 0, 200, 97.0)
    c[10] = (T0 + 10 * M, 100.0, 100.0, 100.0, 100.0)
    set_px(c, 11, 200, 99.0)                                                      # выше стопа
    t = run(ev(10), c, [rep(5, False), rep(10, False)])[0]
    assert t["reason"] == "OPEN"


def test_slippage_is_applied_to_the_market_exit_and_short_is_mirrored():
    c = set_px(flat(), 20, 200, 97.0)
    t = run(ev(10), c, [rep(10, True), rep(30, False)], slip=0.001)[0]
    assert t["exit"] == pytest.approx(97.0 * 0.999)
    cs = set_px(flat(), 20, 200, 103.0)                                           # шорт: стоп 102, цена выше
    ts = run(ev(10, d=-1), cs, [rep(10, True), rep(30, False)], slip=0.001)[0]
    assert ts["side"] == "short" and ts["reason"] == "SL" and ts["exit_ts"] == T0 + 30 * M and ts["exit"] == pytest.approx(103.0 * 1.001)
    shorts = strong_checker([rep(10, True), rep(30, False)], 1.0, 1.0, (0.4, 0.4))
    assert shorts(-1, T0 + 30 * M) is False                                       # для шорта «сильны» — метрики ниже порогов шорта: 0.5 > 0.4 -> слабы


def test_breakeven_stop_after_tp1_is_a_normal_stop_not_gated_by_reports():
    c = flat()
    c[20] = (T0 + 20 * M, 100.6, 101.5, 100.5, 101.2)                           # TP1 (101) взят, стоп переехал в безубыток, к входу цена в этой свече не вернулась
    set_px(c, 21, 200, 101.2)
    c[25] = (T0 + 25 * M, 101.2, 101.2, 99.9, 99.9)                               # возврат ниже входа
    set_px(c, 26, 200, 99.9)
    t = run(ev(10), c, [rep(10, True), rep(100, True)], levels=TWO, be_after_tp1=True)[0]
    assert t["reason"] == "BE" and t["exit_ts"] == T0 + 25 * M                    # безубыток сработал сразу, отчёт не нужен


def test_averaged_position_uses_the_same_rule():
    c = set_px(flat(), 30, 200, 97.0)                                             # средняя 100, стоп 98
    pyr = {"max": 2, "gap_h": 0, "avg": True, "w0": 10.0, "w": 10.0}
    t = run(ev(10) + ev(20), c, [rep(10, True), rep(20, True), rep(40, False)], pyramid=pyr)[0]
    assert t["avg"] and t["sl_market"] and t["reason"] == "SL" and t["exit_ts"] == T0 + 40 * M and t["exit"] == 97.0
    plain = run(ev(10) + ev(20), wick(set_px(flat(), 30, 200, 97.0), 30, low=96.0), market=False, pyramid=pyr)[0]
    assert plain["reason"] == "SL" and plain["exit_ts"] == T0 + 30 * M


def test_pyramid_legs_each_wait_for_the_report_on_their_own_stop():
    c = set_px(flat(), 20, 200, 97.9)                                             # стоп первой ноги 98, второй ноги (вход 100 -> тот же)
    c = set_px(c, 15, 20, 101.0)                                                  # вторая нога входит дороже: стоп 98.98
    trades = run(ev(10) + ev(16), c, [rep(10, True), rep(16, True), rep(30, False)], pyramid={"max": 2, "gap_h": 0})
    assert [t["reason"] for t in trades] == ["SL", "SL"] and all(t["exit_ts"] == T0 + 30 * M for t in trades)


def test_options_off_leave_results_unchanged():
    c = wick(set_px(flat(), 20, 200, 97.0), 20, low=96.0)
    a = run_events(ev(10), c, LEVELS, 2.0, fee=0.0, slip=0.0, skipped=[])
    b = run_events(ev(10), c, LEVELS, 2.0, fee=0.0, slip=0.0, skipped=[], mstop=None)
    assert [(t["reason"], t["exit_ts"], t["exit"]) for t in a] == [(t["reason"], t["exit_ts"], t["exit"]) for t in b] and "sl_market" not in a[0]


def test_market_stop_replaces_hold_strong():
    """Если включены обе опции, решает отчёт: свечной «стоп после остывания метрик» не срабатывает между отчётами."""
    c = set_px(flat(), 20, 200, 97.0)
    hold = chk(rep(10, True), rep(15, False), rep(60, False))
    t = run_events(ev(10), c, LEVELS, 2.0, fee=0.0, slip=0.0, skipped=[], hold=hold, mstop=hold)[0]
    assert t["exit_ts"] == T0 + 60 * M                                            # отчёт 15 был до касания (цена тогда 100), следующий слабый — 60
    h = run_events(ev(10), c, LEVELS, 2.0, fee=0.0, slip=0.0, skipped=[], hold=hold)[0]
    assert h["exit_ts"] == T0 + 20 * M                                            # одна «hold»: метрики уже остыли, стоп действует на свече касания


def test_sl_market_via_backtest_api(client):  # noqa: F811
    from test_pyramid import post
    loose = dict(local_min=0, scalp_min=0, sl_pct=1, side="long")                # метрики теста всегда «сильные», а цена падает
    _, stopped = post(client, **loose)
    _, market = post(client, **loose, sl_market=True)
    assert any(t["reason"] == "SL" for t in stopped["trades"])
    assert not any(t["reason"] == "SL" for t in market["trades"])                 # отчёты сильные — по маркету не закрываем
    assert market["hold_now"]["long"] is True and stopped["hold_now"] is None


from test_pyramid import client  # noqa: E402,F401
