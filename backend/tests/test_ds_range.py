"""Диапазон DS для входа («от 2.0 до 2.5») и «экстремум» для выхода (при DS ≥ 2.5 лонг закрывается, при DS ≤ нижней границы — шорт)."""
import json

import pytest

from app.core.backtest import ds_exit_checker, normalize_tps, run_events, strong_checker
from app.core.entries import entries
from app.core.signal_parser import should_long, should_short
from app.runcache import make_key
from test_api import client  # noqa: F401
from test_pyramid import post
from test_weekend_ds import START, M, candles, rep, run

LEVELS = normalize_tps([(3.0, 100)])
NEW = ("long_scalp_max", "short_scalp_min", "exit_long_ds_max", "exit_short_ds_min")


def sig(ds, local=2.0, glob=None):
    return {"local": local, "scalp": ds, "global": glob}


def test_predicates_apply_the_range_only_when_a_bound_is_given():
    assert should_long(sig(2.2), 1.0, 2.0) and should_long(sig(9.0), 1.0, 2.0)                                      # без границы — как раньше
    assert should_long(sig(2.2), 1.0, 2.0, None, 2.5) and should_long(sig(2.5), 1.0, 2.0, None, 2.5) and should_long(sig(2.0), 1.0, 2.0, None, 2.5)
    assert not should_long(sig(2.51), 1.0, 2.0, None, 2.5) and not should_long(sig(1.9), 1.0, 2.0, None, 2.5)       # выше «экстремума» и ниже нижней границы
    assert should_short(sig(0.5, local=0.5), 1.0, 1.2) and should_short(sig(-3.0, local=0.5), 1.0, 1.2)             # без границы — как раньше
    assert should_short(sig(0.5, local=0.5), 1.0, 1.2, None, 0.5) and not should_short(sig(0.49, local=0.5), 1.0, 1.2, None, 0.5)
    assert not should_short(sig(1.3, local=0.5), 1.0, 1.2, None, 0.5)                                                # верхний порог шорта по-прежнему действует


def reps(*ds, short_local=False):
    return [(START + i * 30 * 60_000, 0.5 if short_local else 2.0, v, 10.0, 20.0) for i, v in enumerate(ds)]


def test_entries_skip_reports_above_the_long_cap_and_below_the_short_floor():
    sigs = reps(1.5, 2.2, 2.4, 2.7, 3.5, 2.3, 2.6)                                                                  # DS по отчётам
    base = entries(sigs, 1.0, 2.0, confirm=1)
    assert [s[0] for s in base] == [sigs[i][0] for i in (1, 2, 3, 4, 5, 6)]                                         # от 2.0 до бесконечности
    capped = entries(sigs, 1.0, 2.0, confirm=1, ds_range=(2.5, None))
    assert [s[0] for s in capped] == [sigs[i][0] for i in (1, 2, 5)] and all(d == 1 for _, d in capped)             # диапазон 2.0…2.5
    assert entries(sigs, 1.0, 2.0, confirm=1, ds_range=(None, None)) == base and entries(sigs, 1.0, 2.0, confirm=1, ds_range=None) == base
    shorts = reps(1.1, 0.9, 0.4, 0.8, -0.5, 0.3, short_local=True)
    sh = entries(shorts, 9.0, 9.0, confirm=1, short=(1.0, 1.0))
    assert [s[0] for s in sh] == [shorts[i][0] for i in (1, 2, 3, 4, 5)]                                            # шорт при DS ≤ 1.0 — без нижней границы
    floored = entries(shorts, 9.0, 9.0, confirm=1, short=(1.0, 1.0), ds_range=(None, 0.5))
    assert [d for _, d in floored] == [-1, -1] and [s[0] for s in floored] == [shorts[i][0] for i in (1, 3)]         # DS от 0.5 до 1.0: отчёты 0.9 и 0.8


def test_confirmation_series_is_broken_by_an_extreme_report():
    sigs = reps(2.2, 2.8, 2.3, 2.4)                                                                                 # серия лонга из двух отчётов подряд: 2.2 → «экстремум» → …
    assert [s[0] for s in entries(sigs, 1.0, 2.0, confirm=2, ds_range=(2.5, None))] == [sigs[3][0]]                 # подтверждение заново набирается после экстремума
    assert [s[0] for s in entries(sigs, 1.0, 2.0, confirm=2)] == [sigs[1][0], sigs[2][0], sigs[3][0]]


def test_strong_checker_treats_an_extreme_report_as_not_strong():
    sigs = reps(2.2, 2.8)
    plain, ranged = strong_checker(sigs, 1.0, 2.0, None), strong_checker(sigs, 1.0, 2.0, None, ds_range=(2.5, None))
    t = sigs[1][0]
    assert plain(1, t) is True and ranged(1, t) is False and ranged(1, sigs[0][0]) is True


def test_exit_checker_closes_on_the_extreme_side_independently():
    sigs = [rep(0, 2.2, START), rep(60, 2.6, START), rep(120, 1.0, START), rep(180, 0.2, START), rep(240, 1.5, START)]
    t = lambda m: START + m * M
    ex = ds_exit_checker(sigs, long_hi=2.5)
    assert [ex(1, t(m)) for m in (0, 60, 120)] == [False, True, False] and ex(-1, t(60)) is False                    # лонг: DS ≥ 2.5; шорт без порогов не закрывается
    both = ds_exit_checker(sigs, long_ds=1.05, long_hi=2.5)
    assert [both(1, t(m)) for m in (0, 60, 120, 180, 240)] == [False, True, True, True, False]                     # и «упал», и «экстремум»
    sh = ds_exit_checker(sigs, short_ds=2.0, short_lo=0.5)
    assert [sh(-1, t(m)) for m in (0, 60, 120, 180, 240)] == [True, True, False, True, False]                       # шорт: DS ≥ 2.0 («вырос») или DS ≤ 0.5 («экстремум» снизу)
    assert ds_exit_checker(sigs, long_hi=2.5).rep_ts == [s[0] for s in sigs] and ds_exit_checker(sigs).rep_ts == []
    assert ds_exit_checker([(START, 1.0, None, None, None)], long_hi=0.0, short_lo=9.0)(1, START) is False          # нет DS в отчёте — не закрывать


def test_long_is_closed_at_the_open_of_the_extreme_report():
    c = candles(START, 400)
    for i in range(60, 400):
        c[i] = (c[i][0], 102.0, 102.0, 102.0, 102.0)                                                                # после отчёта в 60 цена 102
    sigs = [rep(0, 2.2, START), rep(30, 2.4, START), rep(60, 2.7, START), rep(90, 2.1, START)]
    trades = run([(START + 5 * M, 1)], c, dsx=ds_exit_checker(sigs, long_hi=2.5))
    assert [t["reason"] for t in trades] == ["DS"] and trades[0]["exit_ts"] == START + 60 * M and trades[0]["exit"] == 102.0
    assert run([(START + 5 * M, 1)], c, dsx=ds_exit_checker(sigs, long_hi=3.0))[0]["reason"] == "OPEN"                # порог выше любого DS — позиция остаётся
    shorts = [rep(0, 0.6, START), rep(60, 0.3, START)]
    ts = run([(START + 5 * M, -1)], c, dsx=ds_exit_checker(shorts, short_lo=0.4))[0]
    assert ts["reason"] == "DS" and ts["side"] == "short" and ts["exit_ts"] == START + 60 * M


# ---------------------------------------------------------------- API / сервис

def test_api_range_removes_extreme_entries_and_old_requests_are_unchanged(client):  # noqa: F811
    s0, base = post(client)
    s1, same = post(client, long_scalp_max=None, short_scalp_min=None, exit_long_ds_max=None, exit_short_ds_min=None)
    assert s0 == s1 == 200 and json.dumps(base["trades"], sort_keys=True) == json.dumps(same["trades"], sort_keys=True)          # как раньше, пока границы не заданы
    assert all(base["params"][k] is None for k in NEW)
    longs = [t for t in base["trades"] if t["side"] == "long"]
    shorts = [t for t in base["trades"] if t["side"] == "short"]
    assert longs and shorts
    s, wide = post(client, long_scalp_max=3.0, short_scalp_min=0.0)                                                    # границы шире данных — те же сделки
    assert s == 200 and json.dumps(wide["trades"], sort_keys=True) == json.dumps(base["trades"], sort_keys=True)
    s, cap = post(client, long_scalp_max=2.4)                                                                          # лонговые отчёты имеют DS 2.5 — выше границы
    assert s == 200 and not [t for t in cap["trades"] if t["side"] == "long"] and len([t for t in cap["trades"] if t["side"] == "short"]) == len(shorts)
    s, floor = post(client, short_scalp_min=0.6)                                                                       # шортовые отчёты имеют DS 0.5 — ниже границы
    assert s == 200 and not [t for t in floor["trades"] if t["side"] == "short"] and len([t for t in floor["trades"] if t["side"] == "long"]) == len(longs)


def test_api_extreme_exit_and_validation(client):  # noqa: F811
    s, base = post(client, exit_long_ds_max=2.5)                                                                       # лонг входит при DS 2.5 и закрывается на следующем отчёте с DS ≥ 2.5
    assert s == 200 and base["params"]["exit_long_ds_max"] == 2.5 and any("DS ≥ 2.5" in w for w in base["warnings"])  # вход разрешён и выше — предупреждение
    s, ok = post(client, exit_long_ds_max=2.5, long_scalp_max=2.5)
    assert s == 200 and not any("Выход лонга по DS ≥" in w for w in ok["warnings"])                                    # вход «до 2.5», выход «от 2.5» — согласованно, без предупреждения
    for bad in ({"long_scalp_max": 50}, {"short_scalp_min": -50}, {"exit_long_ds_max": "много"}, {"exit_short_ds_min": 11}):
        assert post(client, **bad)[0] == 422, bad
    s, err = post(client, scalp_min=2.0, long_scalp_max=1.5)
    assert s == 422 and "пустой" in err["detail"]                                                                      # диапазон от 2.0 до 1.5
    s, err = post(client, short_scalp_max=0.65, short_scalp_min=0.9)
    assert s == 422 and "пустой" in err["detail"]
    s, err = post(client, exit_long_ds=2.6, exit_long_ds_max=2.5)
    assert s == 422 and "перекрываются" in err["detail"]
    assert post(client, side="short", long_scalp_max=1.0, scalp_min=2.0)[0] == 200                                      # лонг выключен стороной — его диапазон не проверяется
    assert post(client, only="local", long_scalp_max=1.0, scalp_min=2.0)[0] == 200                                      # при «только Local» DS в условии входа не участвует


def test_cache_key_is_unchanged_until_a_new_bound_is_set():
    eff = {"symbol": "BTC/USDT:USDT", "scalp_min": 2.0, "sl_pct": 2.0}
    key = make_key(eff, "src", None, "BTC")
    assert make_key({**eff, **{k: None for k in NEW}}, "src", None, "BTC") == key                                         # старые снимки остаются годными
    assert make_key({**eff, "long_scalp_max": 2.5}, "src", None, "BTC") != key and make_key({**eff, "exit_short_ds_min": 0.4}, "src", None, "BTC") != key


def test_old_configs_keep_the_bounds_off_even_if_env_defaults_set_them():
    from app.legacy_off import LEGACY_OFF
    assert all(LEGACY_OFF[k] is None for k in NEW)
