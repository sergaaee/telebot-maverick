"""Доборы пирамидинга тоже можно вносить лимиткой (entry_limit["adds"]): ордер на X % от цены сигнала добора, срок жизни, отмена обратным сигналом или закрытием позиции,
исполнение — отдельной ногой или добором в усреднённую позицию по цене лимитки."""
import pytest

from app.core.backtest import LIMIT_ADD_PENDING, normalize_tps, run_events

M = 60_000
LV = normalize_tps([(5.0, 100)])                 # тейк +5%
SL = 3.0
PX = 99.9


def bars(n, px=PX, **kw):
    """n минут цены px (open = high = low); kw: m5=(open, high, low) — подмена минуты 5."""
    rows = [(px, px, px)] * n
    for k, v in kw.items():
        rows[int(k[1:])] = v
    return rows


def cs(rows):
    return [(i * M, o, h, l, o) for i, (o, h, l) in enumerate(rows)]


def run(rows, events, pct=0.5, mode="pullback", ttl_h=1.0, adds=True, pyramid=None, **kw):
    sk = []
    pyr = pyramid if pyramid is not None else {"max": 3, "gap_h": 0}
    ev = [(m * M, d) for m, d in events]
    tr = run_events(ev, cs(rows), LV, SL, fee=0.0, slip=0.0, skipped=sk, pyramid=pyr, entry_limit={"pct": pct, "dir": mode, "ttl_h": ttl_h, "adds": adds}, **kw)
    return tr, sk


# Раскладка по минутам. Сигнал первой ноги — минута 0 (цена 100): лимитка 0.5% ниже = 99.5, исполняется на минуте 1 (low 99.4); дальше цена 99.9.
# Добор — сигнал на минуте 9 (open 99.9): лимитка 99.9 × 0.995 = 99.4005 исполняется на минуте 11 (low 99.2).
FIRST = {"m0": (100.0, 100.0, 100.0), "m1": (99.8, 99.8, 99.4)}
ADD_LEVEL = PX * 0.995


def base_rows(n=16, **kw):
    return bars(n, **FIRST, **{"m11": (99.7, 99.9, 99.2), **kw})


def test_add_fills_at_the_limit_level_as_a_separate_leg_with_its_own_stop_and_takes():
    tr, sk = run(base_rows(), [(0, 1), (9, 1)])
    first, add = tr
    assert (first["leg"], first["entry"], first["entry_ts"]) == (1, 99.5, 1 * M) and not sk
    assert add["pyramid"] and add["leg"] == 2 and add["signal_ts"] == 9 * M and add["entry_ts"] == 11 * M
    assert add["entry"] == pytest.approx(ADD_LEVEL) and add["limit"] == {"level": pytest.approx(ADD_LEVEL), "ref": PX, "dir": "pullback", "wait_h": pytest.approx(2 / 60), "add": True}
    assert add["tp"] == pytest.approx(ADD_LEVEL * 1.05) and add["sl"] == pytest.approx(ADD_LEVEL * 0.97) and first["reason"] == add["reason"] == "OPEN"


def test_without_the_flag_adds_stay_at_market_on_the_signal_candle():
    tr, _ = run(base_rows(), [(0, 1), (9, 1)], adds=False)
    assert len(tr) == 2 and tr[1]["entry_ts"] == 9 * M and tr[1]["entry"] == PX and "limit" not in tr[1]


def test_unfilled_add_is_skipped_after_ttl_and_repeated_signals_wait_while_it_stands():
    rows = bars(30, **FIRST)                                                                                   # откат за 0.5% после первой ноги не случается
    tr, sk = run(rows, [(0, 1), (3, 1), (4, 1), (12, 1)], ttl_h=5 / 60)                                        # лимитка добора живёт 5 минут: с 3-й до 8-й
    assert len(tr) == 1
    assert [x[2].startswith("лимитка добора не исполнилась за 0.0833") for x in sk] == [True, False, True] and sk[1][2] == "лимитка добора уже стоит"      # сигнал минуты 12 — прошлый ордер снят, новый тоже не исполняется
    assert [x[0] for x in sk] == [3 * M, 4 * M, 12 * M]


def test_add_waiting_at_the_end_of_data_is_pending_not_failed():
    tr, sk = run(bars(8, **FIRST), [(0, 1), (3, 1)], ttl_h=1.0)
    assert len(tr) == 1 and [x[2] for x in sk] == [LIMIT_ADD_PENDING]


def test_opposite_signal_cancels_the_standing_add_and_reverses():
    rows = bars(14, **FIRST, m7=(99.9, 100.5, 99.9), m8=(100.0, 100.0, 99.0))                                  # лимитка добора (с минуты 3) исполнилась бы на 8-й, но на 5-й приходит разворот
    tr, sk = run(rows, [(0, 1), (3, 1), (5, -1)])
    assert [(t["side"], t["reason"], t["exit_ts"] if t["side"] == "long" else t["entry_ts"]) for t in tr] == [("long", "REV", 5 * M), ("short", "OPEN", 7 * M)]
    assert [x[2] for x in sk] == ["лимитка добора отменена: пришёл обратный сигнал"] and not any(t.get("pyramid") for t in tr)


def test_position_closed_by_take_before_the_add_fills_cancels_the_add():
    rows = bars(14, **FIRST, m4=(99.9, 105.0, 99.9), m8=(99.9, 99.9, 99.0))                                    # тейк первой ноги на 4-й минуте; лимитка добора (сигнал 3) заполнилась бы на 8-й
    tr, sk = run(rows, [(0, 1), (3, 1)])
    assert [t["reason"] for t in tr] == ["TP"] and tr[0]["exit_ts"] == 4 * M
    assert [x[2] for x in sk] == ["позиция закрыта до исполнения лимитки добора"]


def test_old_add_order_never_leaks_into_the_next_position():
    rows = bars(16, **FIRST, m4=(99.9, 105.0, 99.9), m5=(100.0, 100.0, 100.0), m6=(100.0, 100.0, 100.0), m8=(100.0, 100.0, 99.0))
    tr, sk = run(rows, [(0, 1), (3, 1), (6, 1)])                                                               # сигнал 6 после тейка открывает новую позицию (лимиткой), а не добор
    assert [(t["group"], t["leg"], t["reason"]) for t in tr] == [(1, 1, "TP"), (2, 1, "OPEN")] and not any(t.get("pyramid") for t in tr)
    assert "позиция закрыта до исполнения лимитки добора" in [x[2] for x in sk]


def test_breakout_add_waits_for_the_price_to_run_in_the_direction_of_the_position():
    rows = bars(14, px=100.4, m0=(100.0, 100.0, 100.0), m1=(100.0, 100.6, 100.0), m7=(100.6, 101.2, 100.5))     # первая нога: по ходу, 100.5 (минута 1); добор (сигнал 4, open 100.4): 100.4 × 1.005 = 100.902 — на 7-й
    tr, sk = run(rows, [(0, 1), (4, 1)], mode="breakout")
    add = tr[1]
    assert tr[0]["entry"] == pytest.approx(100.5) and add["pyramid"] and add["entry_ts"] == 7 * M and add["entry"] == pytest.approx(100.4 * 1.005) and add["limit"]["dir"] == "breakout"


def test_takes_are_not_counted_in_the_add_fill_candle():
    tr, _ = run(base_rows(m11=(99.7, 106.0, 99.2)), [(0, 1), (9, 1)])                                           # в свече исполнения добора цена касается и уровня, и далёкого тейка
    first, add = tr
    assert (first["reason"], first["exit_ts"]) == ("TP", 11 * M)                                               # у первой ноги тейк в этой свече засчитан
    assert add["reason"] == "OPEN" and add["tp_hits"] == 0 and add["entry_ts"] == 11 * M                       # у добора — нет: порядок внутри минуты неизвестен


def test_averaged_position_adds_at_the_limit_price_and_keeps_the_signal_list():
    pyr = {"max": 2, "gap_h": 0, "avg": True, "w0": 10.0, "w": 10.0}
    tr, sk = run(base_rows(), [(0, 1), (9, 1)], pyramid=pyr)
    t = tr[0]
    assert len(tr) == 1 and t["avg"] and t["adds_n"] == 1 and t["add_signals"] == [9 * M] and not sk
    assert t["adds"][1]["price"] == pytest.approx(ADD_LEVEL) and t["adds"][1]["ts"] == 11 * M
    assert t["entry"] == pytest.approx(20.0 / (10.0 / 99.5 + 10.0 / ADD_LEVEL))                                # средняя по объёму: равные веса -> гармоническое среднее цен


def test_averaged_position_closed_before_the_add_fills_drops_the_add():
    pyr = {"max": 2, "gap_h": 0, "avg": True, "w0": 10.0, "w": 10.0}
    tr, sk = run(bars(14, **FIRST, m4=(99.9, 105.0, 99.9), m8=(99.9, 99.9, 99.0)), [(0, 1), (3, 1)], pyramid=pyr)
    assert len(tr) == 1 and tr[0]["reason"] == "TP" and tr[0]["adds_n"] == 0 and [x[2] for x in sk] == ["позиция закрыта до исполнения лимитки добора"]


def test_max_adds_and_pause_between_adds_apply_before_the_limit_is_placed():
    tr, sk = run(base_rows(m8=(99.7, 99.9, 99.2), m11=PX_BAR()), [(0, 1), (3, 1), (12, 1)], pyramid={"max": 1, "gap_h": 0})          # добор (сигнал 3) исполнился на 8-й; сигнал 12 — больше доборов нельзя
    assert len(tr) == 2 and tr[1]["entry_ts"] == 8 * M and [x[2] for x in sk] == ["достигнут максимум доборов"]
    tr, sk = run(base_rows(), [(0, 1), (9, 1)], pyramid={"max": 3, "gap_h": 5.0})                              # пауза между доборами считается от открытия последней ноги
    assert len(tr) == 1 and [x[2] for x in sk] == ["пауза между доборами"]


def PX_BAR():
    return (PX, PX, PX)


# ---------------------------------------------------------------- API

from test_api import client  # noqa: E402,F401
from test_pyramid import post  # noqa: E402


def test_api_entry_limit_adds_param_warning_and_effect(client):  # noqa: F811
    loose = dict(local_min=0, scalp_min=0, short_scalp_max=10, short_local_max=10, side="long", sl_pct=50, tps=[{"pct": 90, "share": 100}], confirm=1)
    s, none = post(client, **loose, entry_limit_adds=True)
    assert s == 200 and none["params"]["entry_limit_adds"] is True and any("Доборы лимиткой" in w for w in none["warnings"])            # пирамидинга и входа лимиткой нет — опция ни на что не влияет
    pyr = dict(pyramid_enabled=True, pyramid_max=3, pyramid_pct=5, trade_pct=10, entry_limit_pct=0.05, entry_limit_dir="breakout", entry_limit_ttl_h=2)
    s, market = post(client, **loose, **pyr)
    assert s == 200 and market["params"]["entry_limit_adds"] is False and not any(t.get("pyramid") and "limit" in t for t in market["trades"])
    s, limit = post(client, **loose, **pyr, entry_limit_adds=True)
    adds = [t for t in limit["trades"] if t.get("pyramid")]
    assert s == 200 and adds and all(t["limit"]["add"] is True and t["entry_ts"] >= t["signal_ts"] for t in adds) and not any("Доборы лимиткой" in w for w in limit["warnings"])
    assert any(t["entry_ts"] > t["signal_ts"] for t in adds)                                                                              # лимитка исполняется позже сигнала (цена должна подняться на 0.05%)
    assert post(client, **loose, **pyr, entry_limit_adds="да")[0] == 422
