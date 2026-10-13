import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from entries import attach_liq, entries

H = 3_600_000


def rows(scalps, liqs=None):
    return [(i * H, 2.0, s, *( (liqs[i], 0.0) if liqs else ())) for i, s in enumerate(scalps)]


def idx(ents):
    return [t // H for t, _ in ents]


def test_baseline_matches_confirm():
    r = rows([2.5, 2.6, 1.0, 2.5, 2.5, 2.5])
    assert idx(entries(r, 1.65, 2.0, confirm=2)) == [1, 4, 5]


def test_cool_drop_waits_for_pullback():
    r = rows([2.5, 2.6, 2.6, 2.3, 2.2])
    assert idx(entries(r, 1.65, 2.0, confirm=2, cool=dict(metric="scalp", drop=0.1))) == [3, 4]
    assert idx(entries(r, 1.65, 2.0, confirm=2, cool=dict(metric="scalp", drop=0.5))) == []


def test_cool_declines():
    r = rows([2.8, 2.7, 2.6, 2.7, 2.6])
    assert idx(entries(r, 1.65, 2.0, confirm=1, cool=dict(metric="scalp", declines=2))) == [2]


def test_cool_short_mirrored():
    r = [(i * H, 1.0, s) for i, s in enumerate([0.5, 0.4, 0.6])]
    e = entries(r, 1.65, 2.0, confirm=1, short=(1.05, 0.65), cool=dict(metric="scalp", drop=0.1))
    assert e == [(2 * H, -1)]


def test_liq_ended_filter():
    r = rows([2.5] * 5, liqs=[0, 100, 100, 20, 5])
    f = dict(hours=24, frac=0.25, pct=0)
    assert idx(entries(r, 1.65, 2.0, confirm=1, liq=f)) == [3, 4]
    assert entries(r, 1.65, 2.0, confirm=1, liq=dict(hours=24, frac=0.25, pct=90)) == []  # мало истории для перцентиля
    r2 = rows([2.5] * 5, liqs=[0, 100, 100, 20, 5])
    assert entries(r2, 1.65, 2.0, confirm=1, liq=dict(hours=0.5, frac=0.25, pct=0)) == []  # окно без прошлого: всплеска не видно


def test_liq_missing_blocks_and_attach():
    assert entries(rows([2.5, 2.5]), 1.65, 2.0, liq=dict(hours=6, frac=0.5, pct=0)) == []
    main = [(10 * H, 2, 2.5), (20 * H, 2, 2.5)]
    aux = [(9 * H, 0, 0, 7.0, 1.0)]
    out, hit = attach_liq(main, aux, max_age_h=3)
    assert hit == 1 and out[0][3:] == (7.0, 1.0) and out[1][3:] == (None, None)
