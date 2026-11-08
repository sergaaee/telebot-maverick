"""Две независимые опции выходных вместо одной галочки «только будни» («не открывать позиции по сигналам в выходные» и «закрывать позиции перед выходными»)
и закрытие позиции по DS нового отчёта с собственными порогами выхода (отдельно для лонга и шорта)."""
from datetime import datetime, timezone

import pytest

from app.core import tradingweek
from app.core.backtest import ds_exit_checker, normalize_tps, run_events, stats
from app.schemas import BacktestParams
from app.service import effective_params
from test_api import client  # noqa: F401
from test_pyramid import post

M = 60_000
LEVELS = normalize_tps([(3.0, 100)])


def wk(*a):
    return int(datetime(*a, tzinfo=timezone.utc).timestamp() * 1000)


def candles(start, minutes, price=100.0):
    return [(start + i * M, price, price, price, price) for i in range(minutes)]


def run(events, c, week=None, **kw):
    return run_events(events, c, LEVELS, 2.0, fee=0.0, slip=kw.pop("slip", 0.0), skipped=kw.pop("skipped", None), week=week, **kw)


# ---------------------------------------------------------------- выходные: две опции

def test_options_follow_the_legacy_checkbox_only_when_new_ones_are_missing():
    assert tradingweek.options({}) == (False, False, 0.0)
    assert tradingweek.options({"weekdays_only": True, "friday_close_before_min": 15}) == (True, True, 15.0)          # прежняя галочка = обе опции
    assert tradingweek.options({"weekdays_only": True, "weekend_close": False}) == (True, False, 0.0)
    assert tradingweek.options({"weekdays_only": False, "weekend_no_entry": True, "weekend_close": None}) == (True, False, 0.0)
    assert tradingweek.options({"weekdays_only": True, "weekend_no_entry": False, "weekend_close": False}) == (False, False, 0.0)       # явные новые значения важнее старой галочки


def test_next_close_is_the_closest_friday_close_after_the_moment():
    assert tradingweek.next_close(wk(2026, 11, 5, 20, 30)) == wk(2026, 11, 6, 22, 0)            # четверг -> эта пятница (зимнее время: 22:00 UTC)
    assert tradingweek.next_close(wk(2026, 11, 6, 21, 59)) == wk(2026, 11, 6, 22, 0)
    assert tradingweek.next_close(wk(2026, 11, 6, 22, 0)) == wk(2026, 11, 13, 22, 0)            # ровно в закрытие — уже следующее
    assert tradingweek.next_close(wk(2026, 11, 7, 11, 0)) == wk(2026, 11, 13, 22, 0)            # суббота -> следующая пятница
    assert tradingweek.next_close(wk(2026, 11, 8, 23, 59)) == wk(2026, 11, 13, 22, 0)
    assert tradingweek.next_close(wk(2026, 11, 6, 21, 30), before_min=60) == wk(2026, 11, 13, 21, 0)      # после сдвинутого закрытия — следующая неделя


def test_no_entry_only_skips_weekend_signals_but_keeps_positions_over_the_weekend():
    c = candles(wk(2026, 11, 5, 0, 0), 6 * 1440)                                        # четверг 00:00 … вторник 00:00
    sk = []
    trades = run([(wk(2026, 11, 5, 12, 0), 1), (wk(2026, 11, 7, 12, 0), -1)], c, {"before_min": 0, "no_entry": True, "close": False}, skipped=sk)
    assert len(trades) == 1 and trades[0]["reason"] == "OPEN"                           # пятничное закрытие позицию НЕ закрыло
    assert [x[2] for x in sk] == ["выходные: вне торгового окна"]                       # субботний разворот пропущен целиком


def test_close_only_closes_before_the_weekend_but_allows_weekend_entries():
    c = candles(wk(2026, 11, 5, 0, 0), 12 * 1440)                                       # до понедельника 17-го
    sk = []
    trades = run([(wk(2026, 11, 5, 12, 0), 1), (wk(2026, 11, 7, 12, 0), 1), (wk(2026, 11, 8, 12, 0), -1)], c, {"before_min": 0, "no_entry": False, "close": True}, skipped=sk)
    thu, sat, sun = trades
    assert (thu["reason"], thu["exit_ts"]) == ("WKND", wk(2026, 11, 6, 22, 0))                      # четверговый лонг закрыт перед выходными
    assert (sat["reason"], sat["entry_ts"], sat["exit_ts"]) == ("REV", wk(2026, 11, 7, 12, 0), wk(2026, 11, 8, 12, 0))      # субботний сигнал открыл позицию (вход в выходные разрешён) — её закрыл воскресный разворот
    assert (sun["side"], sun["reason"], sun["exit_ts"]) == ("short", "WKND", wk(2026, 11, 13, 22, 0))       # воскресный шорт доживает до ближайшей пятницы, а не закрывается сразу
    assert not [x for x in sk if "выходные" in x[2]]


def test_weekend_entry_is_closed_on_the_next_friday_not_instantly():
    c = candles(wk(2026, 11, 7, 0, 0), 9 * 1440)
    t = run([(wk(2026, 11, 7, 12, 0), 1)], c, {"before_min": 0, "no_entry": False, "close": True})[0]
    assert t["reason"] == "WKND" and t["entry_ts"] == wk(2026, 11, 7, 12, 0) and t["exit_ts"] == wk(2026, 11, 13, 22, 0)


def test_close_option_uses_friday_shift_and_both_options_equal_the_old_mode():
    c = candles(wk(2026, 1, 5, 0, 0), 5 * 1440)
    ev = [(wk(2026, 1, 5, 1, 0), 1)]
    t = run(ev, c, {"before_min": 45, "no_entry": False, "close": True})[0]
    assert t["reason"] == "WKND" and t["exit_ts"] == wk(2026, 1, 9, 21, 15)
    both = run(ev, c, {"before_min": 45, "no_entry": True, "close": True})
    old = run(ev, c, {"before_min": 45})                                                  # прежний вызов без ключей: обе опции включены
    assert [(x["reason"], x["exit_ts"], x["entry_ts"]) for x in both] == [(x["reason"], x["exit_ts"], x["entry_ts"]) for x in old]
    off = run(ev, c, {"before_min": 45, "no_entry": False, "close": False})
    assert off[0]["reason"] == "OPEN" and run(ev, c)[0]["reason"] == "OPEN"


def test_no_entry_only_expires_a_limit_order_at_the_weekend_close_but_close_only_does_not():
    c = candles(wk(2026, 11, 6, 20, 0), 3 * 1440)
    for i in range(2 * 60, 3 * 1440):
        c[i] = (c[i][0], 94.0, 94.0, 94.0, 94.0)                                          # цена откатывается на 6% в 22:00 пятницы — после закрытия недели (22:00 UTC)
    lim = {"pct": 5.0, "dir": "pullback", "ttl_h": 48.0}
    sk = []
    assert run([(wk(2026, 11, 6, 20, 30), 1)], c, {"before_min": 0, "no_entry": True, "close": False}, skipped=sk, entry_limit=lim) == []
    assert any("не исполнилась" in x[2] for x in sk)                                      # лимитка живёт только до закрытия пятницы и не успела исполниться
    t = run([(wk(2026, 11, 6, 20, 30), 1)], c, {"before_min": 0, "no_entry": False, "close": False}, entry_limit=lim)
    assert len(t) == 1 and t[0]["entry_ts"] == wk(2026, 11, 6, 22, 0)                     # без запрета на вход лимитка живёт весь срок и исполняется в выходные


# ---------------------------------------------------------------- параметры и обратная совместимость

def test_legacy_checkbox_in_api_params_maps_to_both_new_options():
    d = {"weekend_no_entry": False, "weekend_close": False, "weekdays_only": False, "tps": [{"pct": 1, "share": 100}], "top_tps": [{"pct": 1, "share": 100}]}
    assert effective_params(d, BacktestParams(weekdays_only=True))["weekend_no_entry"] is True and effective_params(d, BacktestParams(weekdays_only=True))["weekend_close"] is True
    eff = effective_params(d, BacktestParams(weekdays_only=True, weekend_close=False))
    assert eff["weekend_no_entry"] is True and eff["weekend_close"] is False                 # явная новая опция не перетирается
    eff = effective_params(d | {"weekend_close": True}, BacktestParams())
    assert eff["weekend_close"] is True and eff["weekend_no_entry"] is False                 # умолчания .env тоже раздельные
    from app.legacy_off import LEGACY_OFF
    old = effective_params(d | {"weekend_no_entry": True, "weekend_close": True}, BacktestParams(**{**LEGACY_OFF, "weekdays_only": False}))
    assert old["weekend_no_entry"] is False and old["weekend_close"] is False               # старый конфиг без выходных не подхватывает умолчания .env
    old = effective_params(d, BacktestParams(**{**LEGACY_OFF, "weekdays_only": True}))
    assert old["weekend_no_entry"] is True and old["weekend_close"] is True and old["exit_long_ds"] is None


def test_api_weekend_options_are_separate_and_validated(client):  # noqa: F811
    s, base = post(client)
    assert s == 200 and base["params"]["weekend_no_entry"] is False and base["params"]["weekend_close"] is False and base["params"]["exit_long_ds"] is None
    s, old = post(client, weekdays_only=True)
    assert old["params"]["weekend_no_entry"] is True and old["params"]["weekend_close"] is True
    s, one = post(client, weekend_close=True, friday_close_before_min=15)
    assert s == 200 and one["params"]["weekend_close"] is True and one["params"]["weekend_no_entry"] is False
    assert post(client, exit_long_ds=50)[0] == 422 and post(client, friday_close_before_min=-1)[0] == 422


# ---------------------------------------------------------------- закрытие по DS

def rep(minute, ds, start):
    """Отчёт: (ts, local, scalp, liq_long, liq_short) — DS это третий элемент."""
    return (start + minute * M, 2.0, ds, 10.0, 20.0)


START = wk(2026, 1, 5, 0, 0)


def test_ds_checker_uses_separate_thresholds_and_the_latest_report():
    sigs = [rep(0, 2.0, START), rep(60, 1.0, START), rep(120, 1.07, START), rep(180, 1.95, START), rep(240, 2.4, START)]
    ex = ds_exit_checker(sigs, long_ds=1.05, short_ds=1.95)
    t = lambda m: START + m * M
    assert [ex(1, t(m)) for m in (0, 59, 60, 119, 120, 179)] == [False, False, True, True, False, False]       # лонг: DS <= 1.05
    assert [ex(-1, t(m)) for m in (0, 60, 179, 180, 239, 240)] == [True, False, False, True, True, True]       # шорт: DS >= 1.95
    assert ex(1, START - M) is False and ex.rep_ts == [s[0] for s in sigs]
    assert ds_exit_checker(sigs, long_ds=1.05)(-1, t(300)) is False and ds_exit_checker(sigs, short_ds=1.95)(1, t(60)) is False       # порог не задан — не закрывать
    assert ds_exit_checker(sigs).rep_ts == [] and ds_exit_checker([(START, 1.0, None, None, None)], 5.0, 5.0)(1, START) is False       # нет порогов / нет DS в отчёте


def test_long_closes_on_a_report_with_low_ds_at_its_open_and_short_on_high_ds():
    c = candles(START, 600)
    for i in range(120, 600):
        c[i] = (c[i][0], 101.0, 101.0, 101.0, 101.0)                                                   # цена после отчёта выросла: выход по open 101
    sigs = [rep(0, 2.0, START), rep(60, 1.5, START), rep(120, 1.0, START), rep(180, 0.9, START)]
    ex = ds_exit_checker(sigs, long_ds=1.05, short_ds=1.95)
    t = run([(START + 5 * M, 1)], c, dsx=ex)[0]
    assert t["reason"] == "DS" and t["exit_ts"] == START + 120 * M and t["exit"] == 101.0 and t["tp_hits"] == 0      # отчёт в 60 (DS 1.5) не закрывает, отчёт в 120 (DS 1.0) — закрывает
    sigs_s = [rep(0, 1.0, START), rep(60, 1.5, START), rep(120, 2.0, START)]
    t = run([(START + 5 * M, -1)], c, dsx=ds_exit_checker(sigs_s, long_ds=1.05, short_ds=1.95))[0]
    assert t["reason"] == "DS" and t["exit_ts"] == START + 120 * M and t["side"] == "short"
    t = run([(START + 5 * M, 1)], c, dsx=ds_exit_checker(sigs, long_ds=None, short_ds=1.95))[0]
    assert t["reason"] == "OPEN"                                                                          # для лонга порог не задан — как раньше


def test_report_on_the_entry_candle_does_not_close_and_slippage_applies():
    c = candles(START, 300)
    sigs = [rep(10, 0.5, START), rep(100, 3.0, START)]                                                    # отчёт входа (DS 0.5 ≤ порога) и следующий (DS 3.0 — выше)
    assert run([(START + 10 * M, 1)], c, dsx=ds_exit_checker(sigs, long_ds=1.0))[0]["reason"] == "OPEN"
    sigs = [rep(10, 2.0, START), rep(100, 0.5, START)]
    t = run([(START + 10 * M, 1)], c, dsx=ds_exit_checker(sigs, long_ds=1.0), slip=0.001)[0]
    assert t["reason"] == "DS" and t["exit"] == pytest.approx(100.0 * 0.999) and t["pnl_pct"] < 0
    ts = run([(START + 10 * M, -1)], c, dsx=ds_exit_checker([rep(10, 0.5, START), rep(100, 2.5, START)], short_ds=2.0), slip=0.001)[0]
    assert ts["reason"] == "DS" and ts["exit"] == pytest.approx(100.0 * 1.001)


def test_tp_and_stop_before_the_report_still_win_and_ds_wins_over_a_later_stop():
    c = candles(START, 600)
    for i in range(30, 60):
        c[i] = (c[i][0], 104.0, 104.0, 104.0, 104.0)                                                      # тейк (3%) до отчёта в 60
    sigs = [rep(0, 2.0, START), rep(60, 0.5, START)]
    t = run([(START + 5 * M, 1)], c, dsx=ds_exit_checker(sigs, long_ds=1.0))[0]
    assert t["reason"] == "TP"
    c2 = candles(START, 600)
    for i in range(70, 600):
        c2[i] = (c2[i][0], 90.0, 90.0, 90.0, 90.0)                                                        # стоп (2%) был бы позже отчёта
    t = run([(START + 5 * M, 1)], c2, dsx=ds_exit_checker(sigs, long_ds=1.0))[0]
    assert t["reason"] == "DS" and t["exit_ts"] == START + 60 * M and t["exit"] == 100.0


def test_ds_exit_closes_every_leg_and_averaged_positions_and_a_new_entry_follows():
    c = candles(START, 600)
    sigs = [rep(0, 2.0, START), rep(30, 2.0, START), rep(120, 1.0, START), rep(200, 2.0, START)]
    ex = ds_exit_checker(sigs, long_ds=1.05)
    ev = [(START + 5 * M, 1), (START + 31 * M, 1), (START + 201 * M, 1)]
    legs = run(ev, c, dsx=ex, pyramid={"max": 2, "gap_h": 0})
    assert [t["reason"] for t in legs] == ["DS", "DS", "OPEN"] and legs[0]["exit_ts"] == legs[1]["exit_ts"] == START + 120 * M
    avg = run(ev, c, dsx=ex, pyramid={"max": 2, "gap_h": 0, "avg": True, "w0": 10.0, "w": 10.0})
    assert [t["reason"] for t in avg] == ["DS", "OPEN"] and avg[0]["adds_n"] == 1 and avg[0]["exit_ts"] == START + 120 * M
    assert stats(legs)["ds"] == 2 and stats(avg)["ds"] == 1 and stats([])["ds"] == 0


def test_ds_exit_and_weekend_close_on_the_same_candle_prefers_the_weekend_label():
    c = candles(wk(2026, 1, 8, 0, 0), 3 * 1440)
    close_ts = wk(2026, 1, 9, 22, 0)
    sigs = [(wk(2026, 1, 8, 1, 0), 2.0, 2.0, 1.0, 1.0), (close_ts, 2.0, 0.1, 1.0, 1.0)]
    t = run([(wk(2026, 1, 8, 1, 0), 1)], c, {"before_min": 0}, dsx=ds_exit_checker(sigs, long_ds=1.0))[0]
    assert t["reason"] == "WKND" and t["exit_ts"] == close_ts


# ---------------------------------------------------------------- API

def test_api_ds_exit_closes_positions_and_warns_about_overlap(client):  # noqa: F811
    loose = dict(local_min=0, scalp_min=0, short_scalp_max=10, short_local_max=10, sl_pct=50, tps=[{"pct": 90, "share": 100}])
    s, base = post(client, **loose)
    assert s == 200 and base["stats"]["total"]["ds"] == 0
    s, ex = post(client, **loose, exit_long_ds=10, exit_short_ds=0)
    assert s == 200 and ex["stats"]["total"]["ds"] > 0 and any(t["reason"] == "DS" for t in ex["trades"])
    assert ex["params"]["exit_long_ds"] == 10 and any("Выход лонга по DS" in w for w in ex["warnings"]) and any("Выход шорта по DS" in w for w in ex["warnings"])
    s, narrow = post(client, **loose, exit_long_ds=-5)
    assert s == 200 and narrow["stats"]["total"]["ds"] == 0                                              # DS в тесте всегда выше -5: лонг не закрывается
    ds = next(t for t in ex["trades"] if t["reason"] == "DS")
    assert ds["fills"][-1]["label"] == "DS" and ds["exit_ts"] > ds["entry_ts"]
    assert ex["weekday_split"]["weekday"]["reasons"].get("DS", 0) + ex["weekday_split"]["weekend"]["reasons"].get("DS", 0) == ex["stats"]["total"]["ds"]
