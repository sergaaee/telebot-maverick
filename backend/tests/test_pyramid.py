import pytest

from app.core import backtest
from app.core.backtest import normalize_tps, run_events, strong_checker

M = 60_000
H = 3_600_000
T0 = 1_700_000_000_000 // M * M
LEVELS = normalize_tps([(3.0, 100)])


def flat(n=600, price=100.0):
    return [(T0 + i * M, price, price, price, price) for i in range(n)]


def move(c, start, end, price):
    """Свечи [start, end) целиком на другом уровне цены."""
    for i in range(start, end):
        c[i] = (T0 + i * M, price, price, price, price)
    return c


def ev(*minutes, d=1):
    return [(T0 + m * M, d) for m in minutes]


def run(events, candles, **kw):
    skipped = []
    trades = run_events(events, candles, LEVELS, 2.0, fee=0.0, slip=0.0, skipped=skipped, **kw)
    return trades, skipped


# ---------------- пирамидинг ----------------
def test_without_pyramid_one_position_at_a_time():
    trades, skipped = run(ev(10, 20, 30), flat())
    assert len(trades) == 1 and trades[0]["leg"] == 1 and not trades[0]["pyramid"]
    assert [r for _, _, r in skipped] == ["позиция уже открыта"] * 2


def test_pyramid_adds_limited_by_max_and_group_resets_after_close():
    c = move(flat(), 100, 600, 104)                      # на 100-й минуте цена +4%: все ноги берут TP 3% по open свечи
    trades, skipped = run(ev(10, 20, 30, 40, 50, 150, 160), c, pyramid={"max": 2, "gap_h": 0})
    assert [(t["group"], t["leg"], t["pyramid"]) for t in trades] == [(1, 1, False), (1, 2, True), (1, 3, True), (2, 1, False), (2, 2, True)]
    assert [r for _, _, r in skipped] == ["достигнут максимум доборов"] * 2          # сигналы 40 и 50 мин
    assert [t["reason"] for t in trades[:3]] == ["TP"] * 3                                # вторая позиция открыта по 104 и до тейка не дошла


def test_pyramid_gap_between_adds():
    trades, skipped = run(ev(10, 20, 30, 40, 50), flat(), pyramid={"max": 5, "gap_h": 0.25})      # 15 минут
    assert [t["entry_ts"] // M - T0 // M for t in trades] == [10, 30, 50]            # 20 и 40 — слишком рано после предыдущей ноги
    assert [r for _, _, r in skipped] == ["пауза между доборами"] * 2


def test_each_leg_has_own_entry_and_levels_and_exit():
    c = flat()
    move(c, 15, 600, 101)
    trades, _ = run(ev(10, 20), c, pyramid={"max": 3, "gap_h": 0})
    a, b = trades
    assert a["entry"] == 100 and b["entry"] == 101 and b["tps"][0] == pytest.approx(101 * 1.03) and b["sl"] == pytest.approx(101 * 0.98)
    assert a["tps"][0] == pytest.approx(103) and a["reason"] == b["reason"] == "OPEN"                 # данные кончились раньше уровней


def test_opposite_signal_closes_all_legs_and_starts_new_group():
    c = move(flat(), 35, 600, 101)
    events = ev(10, 20) + [(T0 + 35 * M, -1)]
    trades, _ = run(events, c, pyramid={"max": 3, "gap_h": 0})
    long_legs = [t for t in trades if t["side"] == "long"]
    assert len(long_legs) == 2 and all(t["reason"] == "REV" and t["exit_ts"] == T0 + 35 * M for t in long_legs)
    short = trades[-1]
    assert short["side"] == "short" and short["leg"] == 1 and short["group"] == 2


def test_stop_of_one_leg_does_not_close_others():
    c = flat()
    move(c, 25, 600, 97.5)                               # падение на 2.5%: нога 1 (вход 100, стоп 98) и нога 2 (вход 100) стопятся оба
    trades, _ = run(ev(10, 20), c, pyramid={"max": 3, "gap_h": 0})
    assert [t["reason"] for t in trades] == ["SL", "SL"]
    c2 = flat()
    move(c2, 15, 25, 101)                                # нога 2 откроется по 101, нога 1 (100) переживёт отскок
    move(c2, 25, 600, 98.5)                              # 98.5 < стоп ноги 2 (101×0.98=98.98), но > стопа ноги 1 (98)
    trades, _ = run(ev(10, 20), c2, pyramid={"max": 3, "gap_h": 0})
    assert trades[0]["reason"] == "OPEN" and trades[1]["reason"] == "SL"


# ---------------- паузы ----------------
def test_signal_gap_between_positions_and_reversals():
    c = move(flat(), 15, 600, 104)                       # нога 1 берёт TP на 15-й минуте
    trades, skipped = run(ev(10, 20, 40, 200), c, signal_gap_h=1.0)
    assert [t["entry_ts"] // M - T0 // M for t in trades] == [10, 200]                # 20 и 40 минут — внутри часа от открытия на 10-й
    assert [r for _, _, r in skipped] == ["пауза между сигналами"] * 2
    # разворот тоже считается «новым сигналом»
    c = move(flat(), 5, 600, 101)
    trades, skipped = run([(T0 + 10 * M, 1), (T0 + 30 * M, -1)], c, signal_gap_h=1.0)
    assert len(trades) == 1 and skipped[0][2] == "пауза между сигналами" and trades[0]["reason"] == "OPEN"


def test_cooldown_after_stop_still_works_with_legs():
    c = move(flat(), 12, 600, 97)
    trades, skipped = run(ev(10, 30, 200), c, cooldown_h=2.0)
    assert [t["reason"] for t in trades] == ["SL", "SL"] or len(trades) == 2
    assert skipped and skipped[0][2] == "пауза после стопа" and skipped[0][0] == T0 + 30 * M


def test_signal_after_last_candle_is_reported():
    trades, skipped = run(ev(10, 9999), flat(100))
    assert len(trades) == 1 and skipped == [(T0 + 9999 * M, "long", "нет свечи входа")]


# ---------------- «стоп не ставить, пока метрики сильны» ----------------
def hold_until(minute):
    return lambda d, t: t < T0 + minute * M


def test_sl_is_ignored_while_metrics_strong_and_applies_after():
    c = move(flat(), 21, 600, 90)                        # цена падает на 10%
    c[20] = (T0 + 20 * M, 100.0, 100.0, 97.0, 97.0)       # на 20-й минуте проходит стоп 2% (98)
    plain, _ = run(ev(10), c)
    held, _ = run(ev(10), c, hold=hold_until(300))
    assert plain[0]["reason"] == "SL" and plain[0]["exit_ts"] == T0 + 20 * M and plain[0]["exit"] == pytest.approx(98.0)
    assert held[0]["reason"] == "SL" and held[0]["exit_ts"] == T0 + 300 * M            # стоп заработал, как только метрики остыли
    assert held[0]["exit"] == pytest.approx(90.0)                                      # цена уже ниже стопа: выход по open свечи
    assert held[0]["pnl_pct"] < plain[0]["pnl_pct"]
    never, _ = run(ev(10), c, hold=lambda d, t: True)
    assert never[0]["reason"] == "OPEN"                                                # метрики всё время сильны — стопа нет


def test_hold_is_not_applied_when_price_comes_back_before_metrics_cool():
    c = flat()
    move(c, 20, 40, 95)
    move(c, 40, 600, 104)                                # отскок до тейка при сильных метриках
    held, _ = run(ev(10), c, hold=hold_until(300))
    assert held[0]["reason"] == "TP" and held[0]["exit_ts"] == T0 + 40 * M


def test_break_even_stop_stays_active_under_hold():
    levels = normalize_tps([(3, 50), (6, 50)])
    c = flat()
    move(c, 20, 30, 103.5)                               # TP1
    move(c, 30, 600, 99)                                 # возврат ниже входа
    trades = run_events(ev(10), c, levels, 2.0, fee=0.0, be_after_tp1=True, hold=lambda d, t: True)
    assert trades[0]["reason"] == "BE" and trades[0]["tp_hits"] == 1                  # БУ защищает прибыль и при «сильных» метриках


def test_strong_checker_uses_entry_thresholds_and_only_mode():
    sigs = [(T0, 1.0, 1.0), (T0 + H, 2.0, 2.5), (T0 + 2 * H, 2.0, 1.0), (T0 + 3 * H, 0.5, 0.5)]
    both = strong_checker(sigs, 1.65, 2.0, (1.05, 0.65))
    assert not both(1, T0 - 1) and not both(1, T0 + 10)                               # нет отчёта / метрики слабые
    assert both(1, T0 + H) and both(1, T0 + H + 59 * M)                               # DL и DS выше порогов лонга
    assert not both(1, T0 + 2 * H)                                                    # DS упал ниже порога
    assert both(-1, T0 + 3 * H) and not both(-1, T0 + H)                              # шорт: оба ниже порогов шорта
    only_local = strong_checker(sigs, *backtest.apply_only(1.65, 2.0, (1.05, 0.65), "local")[:2], backtest.apply_only(1.65, 2.0, (1.05, 0.65), "local")[2])
    assert only_local(1, T0 + 2 * H)                                                  # учитывается один DL: DS не мешает
    only_scalp = strong_checker(sigs, *backtest.apply_only(1.65, 2.0, (1.05, 0.65), "scalp")[:2], backtest.apply_only(1.65, 2.0, (1.05, 0.65), "scalp")[2])
    assert not only_scalp(1, T0 + 2 * H) and only_scalp(1, T0 + H)


# ---------------- через API: размеры, причины пропусков, обратная совместимость ----------------
import json  # noqa: E402

from test_api import client  # noqa: E402,F401

OFF = dict(pyramid_enabled=False, signal_gap_h=0, sl_hold_strong=False)


def post(c, **kw):
    r = c.post("/api/backtest", json=kw)
    return r.status_code, r.json()


def test_old_configs_without_new_fields_give_identical_results(client):  # noqa: F811
    s0, base = post(client)                                                   # как присылали раньше: ни одного нового поля
    s1, explicit = post(client, **OFF, pyramid_max=3, pyramid_pct=5, pyramid_gap_h=0)
    assert s0 == s1 == 200
    for key in ("trades", "stats", "equity", "monthly"):
        assert json.dumps(base[key], sort_keys=True) == json.dumps(explicit[key], sort_keys=True), key
    assert all(t["leg"] == 1 and t["pyramid"] is False for t in base["trades"])
    assert base["stats"]["total"]["adds"] == 0 and base["stats"]["total"]["positions"] == len(base["trades"])
    # сигналы по-прежнему делятся на открытые и пропущенные, но теперь у пропущенных есть причина
    skipped = [x for x in base["signals"] if not x["taken"]]
    assert skipped and {x["reason"] for x in skipped} <= {"позиция уже открыта", "нет свечи входа", "пауза после стопа"}
    assert sum(base["skipped_reasons"].values()) == len(skipped)


def test_pyramid_sizing_stats_and_signal_reasons(client):  # noqa: F811
    st, plain = post(client, deposit=10000, trade_pct=10, leverage=2, compound=False)
    st, pyr = post(client, deposit=10000, trade_pct=10, leverage=2, compound=False, pyramid_enabled=True, pyramid_max=2, pyramid_pct=5)
    assert st == 200
    legs = pyr["trades"]
    first = next(t for t in legs if t["leg"] == 1)
    adds = [t for t in legs if t["leg"] > 1 and t["group"] == first["group"]]
    assert adds and len(adds) <= 2
    assert first["notional"] == pytest.approx(2000) and all(a["notional"] == pytest.approx(1000) and a["margin"] == pytest.approx(500) for a in adds)
    assert all(a["pyramid"] for a in adds) and all(a["entry_ts"] > first["entry_ts"] for a in adds)
    tot = pyr["stats"]["total"]
    assert tot["adds"] == sum(1 for t in legs if t["leg"] > 1) and tot["positions"] == len({t["group"] for t in legs}) < len(legs)
    assert tot["exposure_pct"] <= 100.0
    # доборы взяты из тех сигналов, которые раньше пропускались как «позиция уже открыта»
    assert "достигнут максимум доборов" in pyr["skipped_reasons"] or pyr["skipped_reasons"].get("позиция уже открыта", 0) < plain["skipped_reasons"]["позиция уже открыта"]
    assert sum(1 for x in pyr["signals"] if x["taken"]) == len(legs)
    # итог в $ складывается из ног; кривая капитала упорядочена по времени закрытия
    assert tot["total_usd"] == pytest.approx(sum(t["pnl_usd"] for t in legs))
    assert [p[0] for p in pyr["equity"][1:]] == sorted(p[0] for p in pyr["equity"][1:])


def test_pyramid_gap_and_limits(client):  # noqa: F811
    _, free = post(client, pyramid_enabled=True, pyramid_max=5, pyramid_pct=5)
    _, gapped = post(client, pyramid_enabled=True, pyramid_max=5, pyramid_pct=5, pyramid_gap_h=10)
    assert gapped["stats"]["total"]["adds"] < free["stats"]["total"]["adds"]
    assert "пауза между доборами" in gapped["skipped_reasons"]
    _, capped = post(client, pyramid_enabled=True, trade_pct=80, pyramid_max=5, pyramid_pct=10)
    assert any("доборов не больше 2" in w for w in capped["warnings"]) and capped["stats"]["total"]["adds"] <= 2 * capped["stats"]["total"]["positions"]
    code, body = post(client, pyramid_enabled=True, trade_pct=98, pyramid_pct=5)
    assert code == 422 and "места не остаётся" in body["detail"]
    for bad in (dict(pyramid_max=0), dict(pyramid_max=21), dict(pyramid_pct=0), dict(pyramid_pct=101), dict(pyramid_gap_h=-1), dict(signal_gap_h=-1)):
        assert post(client, **bad)[0] == 422


def test_signal_gap_via_api(client):  # noqa: F811
    _, base = post(client, side="both")
    _, gap = post(client, side="both", signal_gap_h=48)
    assert len(gap["trades"]) < len(base["trades"]) and gap["skipped_reasons"].get("пауза между сигналами", 0) > 0


def test_sl_hold_strong_via_api(client):  # noqa: F811
    loose = dict(local_min=0, scalp_min=0, sl_pct=1, side="long")                       # метрики теста всегда «сильные», а цена падает
    _, stopped = post(client, **loose)
    _, held = post(client, **loose, sl_hold_strong=True)
    assert any(t["reason"] == "SL" for t in stopped["trades"])
    assert not any(t["reason"] == "SL" for t in held["trades"])
    # выше порогов только один показатель: Scalp тут слабый, но режим «только Local» его не учитывает
    strict = dict(local_min=0.5, scalp_min=5, sl_pct=1, side="long")
    _, none = post(client, **strict)
    assert none["trades"] == []
    _, only_local = post(client, **{**strict, "only": "local", "sl_hold_strong": True})
    assert only_local["trades"] and not any(t["reason"] == "SL" for t in only_local["trades"])
