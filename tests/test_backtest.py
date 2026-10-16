import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from backtest import simulate, stats

M = 60_000
def c(i, o, h, l, cl): return (i * M, o, h, l, cl)
KW = dict(tp_pct=3, sl_pct=1, fee=0, size=0.1)


def run(candles, sigs):
    return simulate(sigs, candles, 1.65, 2.0, **KW)


def test_tp_hit():
    cs = [c(0, 100, 100, 100, 100), c(1, 100, 104, 99.5, 103), c(2, 103, 103, 103, 103)]
    t = run(cs, [(0, 2, 2.5)])
    assert t[0]["reason"] == "TP" and round(t[0]["exit"], 6) == 103 and round(t[0]["pnl_usd"], 6) == 0.3


def test_sl_hit_and_sl_wins_tie():
    cs = [c(0, 100, 100, 100, 100), c(1, 100, 104, 98.9, 100)]  # и TP, и SL в одной свече
    t = run(cs, [(0, 2, 2.5)])
    assert t[0]["reason"] == "SL" and round(t[0]["exit"], 6) == 99


def test_no_signal_below_threshold():
    cs = [c(0, 100, 100, 100, 100), c(1, 100, 110, 100, 110)]
    assert run(cs, [(0, 1.6, 2.5), (0, 2, 1.9)]) == []


def test_ignores_signals_while_open_then_reenters():
    cs = [c(0, 100, 100, 100, 100), c(1, 100, 101, 100, 101), c(2, 101, 101, 101, 101),
          c(3, 101, 105, 101, 105), c(4, 105, 105, 105, 105)]
    t = run(cs, [(0, 2, 2.5), (M * 2, 2, 2.5), (M * 4, 2, 2.5)])
    assert [x["reason"] for x in t] == ["TP", "OPEN"]
    assert t[0]["exit_ts"] == 3 * M and t[1]["entry_ts"] == 4 * M


def test_stats():
    s = stats([{"pnl_usd": 3, "pnl_pct": 3, "reason": "TP"}, {"pnl_usd": -1, "pnl_pct": -1, "reason": "SL"}])
    assert s["trades"] == 2 and s["total_usd"] == 2 and s["max_dd_usd"] == 1 and s["profit_factor"] == 3


def test_confirm_two_reports_in_a_row():
    cs = [c(i, 100, 100, 100, 100) for i in range(10)] + [c(10, 100, 104, 100, 104)]
    ok, bad = (2, 2.5), (1, 1)
    sigs = [(0, *ok), (M, *bad), (2 * M, *ok), (3 * M, *ok)]
    t = simulate(sigs, cs, 1.65, 2.0, confirm=2, **KW)
    assert len(t) == 1 and t[0]["signal_ts"] == 3 * M and t[0]["entry_ts"] == 3 * M
    assert len(simulate(sigs, cs, 1.65, 2.0, confirm=1, **KW)) >= 1


def test_short_tp_and_sl():
    sh = (1.05, 0.65)
    low, hi = (1.0, 0.5), (1.0, 0.5)
    cs = [c(0, 100, 100, 100, 100), c(1, 100, 100.5, 96.5, 97)]
    t = simulate([(0, *low)], cs, 1.65, 2.0, short=sh, **KW)
    assert t[0]["side"] == "short" and t[0]["reason"] == "TP" and round(t[0]["exit"], 6) == 97
    assert round(t[0]["pnl_usd"], 6) == 0.3
    cs = [c(0, 100, 100, 100, 100), c(1, 100, 101.5, 96, 97)]  # оба уровня в свече -> SL
    t = simulate([(0, *low)], cs, 1.65, 2.0, short=sh, **KW)
    assert t[0]["reason"] == "SL" and round(t[0]["exit"], 6) == 101 and round(t[0]["pnl_usd"], 6) == -0.1


def test_short_disabled_by_default():
    cs = [c(0, 100, 100, 100, 100), c(1, 100, 100, 90, 90)]
    assert simulate([(0, 1.0, 0.5)], cs, 1.65, 2.0, **KW) == []


def test_reversal_long_to_short():
    sh = (1.05, 0.65)
    cs = [c(0, 100, 100, 100, 100), c(1, 100, 101, 100, 101), c(2, 102, 102, 102, 102), c(3, 102, 102, 98, 98)]
    sigs = [(0, 2, 2.5), (2 * M, 1.0, 0.5)]
    t = simulate(sigs, cs, 1.65, 2.0, short=sh, **KW)
    assert [x["side"] for x in t] == ["long", "short"]
    assert t[0]["reason"] == "REV" and t[0]["exit"] == 102 and t[0]["exit_ts"] == 2 * M
    assert round(t[0]["pnl_usd"], 6) == 0.2
    assert t[1]["entry"] == 102 and t[1]["reason"] == "TP" and round(t[1]["exit"], 6) == 98.94
    assert round(t[1]["pnl_usd"], 6) == 0.306


def test_same_side_signal_ignored_while_open():
    cs = [c(0, 100, 100, 100, 100), c(1, 100, 101, 100, 101), c(2, 101, 101, 101, 101)]
    t = simulate([(0, 2, 2.5), (M, 2, 2.5)], cs, 1.65, 2.0, short=(1.05, 0.65), **KW)
    assert len(t) == 1 and t[0]["reason"] == "OPEN"


def test_cooldown_after_stop():
    H = 60  # свечи по минуте; пауза 1 час = 60 свечей
    cs = [c(0, 100, 100, 100, 100), c(1, 100, 100, 98, 98)] + [c(i, 98, 98, 98, 98) for i in range(2, 200)]
    sigs = [(0, 2, 2.5), (10 * M, 2, 2.5), (100 * M, 2, 2.5)]
    kw = dict(tp_pct=3, sl_pct=1, fee=0, size=0.1)
    assert [x["signal_ts"] for x in simulate(sigs, cs, 1.65, 2.0, **kw)] == [0, 10 * M]  # 3-й сигнал: позиция уже открыта
    t = simulate(sigs, cs, 1.65, 2.0, cooldown_h=1, **kw)
    assert [x["signal_ts"] for x in t] == [0, 100 * M]


def test_only_local_and_only_scalp():
    cs = [c(0, 100, 100, 100, 100), c(1, 100, 104, 100, 104)] + [c(i, 104, 104, 104, 104) for i in range(2, 20)]
    kw = dict(tp_pct=3, sl_pct=1, fee=0, size=0.1)
    hi_local_low_scalp = [(0, 2.0, 0.5)]    # Local проходит, Scalp нет
    hi_scalp_low_local = [(0, 1.0, 2.5)]    # Scalp проходит, Local нет
    assert simulate(hi_local_low_scalp, cs, 1.65, 2.0, **kw) == []
    assert len(simulate(hi_local_low_scalp, cs, 1.65, 2.0, only="local", **kw)) == 1
    assert simulate(hi_local_low_scalp, cs, 1.65, 2.0, only="scalp", **kw) == []
    assert len(simulate(hi_scalp_low_local, cs, 1.65, 2.0, only="scalp", **kw)) == 1
    assert simulate(hi_scalp_low_local, cs, 1.65, 2.0, only="local", **kw) == []


def test_only_applies_to_shorts_too():
    cs = [c(0, 100, 100, 100, 100), c(1, 100, 100, 96, 96)] + [c(i, 96, 96, 96, 96) for i in range(2, 20)]
    kw = dict(tp_pct=3, sl_pct=1, fee=0, size=0.1, short=(1.05, 0.65))
    sigs = [(0, 1.0, 0.9)]                  # Local <= 1.05 проходит, Scalp <= 0.65 нет
    assert simulate(sigs, cs, 1.65, 2.0, **kw) == []
    t = simulate(sigs, cs, 1.65, 2.0, only="local", **kw)
    assert len(t) == 1 and t[0]["side"] == "short"
    assert simulate(sigs, cs, 1.65, 2.0, only="scalp", **kw) == []


def test_side_short_only_disables_longs():
    inf = float("inf")
    cs = [c(0, 100, 100, 100, 100), c(1, 100, 104, 96, 100)] + [c(i, 100, 100, 100, 100) for i in range(2, 20)]
    kw = dict(tp_pct=3, sl_pct=1, fee=0, size=0.1)
    sigs = [(0, 2.0, 2.5), (M, 2.0, 2.5), (2 * M, 0.5, 0.2), (3 * M, 0.5, 0.2)]
    both = simulate(sigs, cs, 1.65, 2.0, confirm=2, short=(1.05, 0.65), **kw)
    assert {t["side"] for t in both} == {"long", "short"}
    only_short = simulate(sigs, cs, inf, inf, confirm=2, short=(1.05, 0.65), **kw)   # то, что делает --side short
    assert {t["side"] for t in only_short} == {"short"}
    only_long = simulate(sigs, cs, 1.65, 2.0, confirm=2, short=None, **kw)           # --side long
    assert {t["side"] for t in only_long} == {"long"}
