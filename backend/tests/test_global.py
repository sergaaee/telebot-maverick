import csv

from app.core import backtest
from app.core.backtest import resolve_entry, strong_checker
from app.core.entries import entries, load_signals
from app.core.signal_parser import should_long, should_short

H = 3_600_000
T0 = 1_700_000_000_000


def sigs(*rows):
    """(local, scalp, global) на каждом часе."""
    return [(T0 + i * H, l, s, None, None, None, None, g) for i, (l, s, g) in enumerate(rows)]


def test_should_long_short_global_optional():
    s = {"local": 2, "scalp": 2, "global": 1.0}
    assert should_long(s, 1, 1) and should_long(s, 1, 1, 0.9) and not should_long(s, 1, 1, 1.1)
    assert should_short({"local": 0, "scalp": 0, "global": 1.0}, 1, 1, 1.0) and not should_short({"local": 0, "scalp": 0, "global": 1.2}, 1, 1, 1.0)
    # порог задан, а Deep Global в отчёте нет — условие не выполнено (fail-closed)
    assert not should_long({"local": 2, "scalp": 2, "global": None}, 1, 1, 0.0)
    assert not should_short({"local": 0, "scalp": 0}, 1, 1, 5.0)
    # порог не задан — Global не важен, даже если его нет
    assert should_long({"local": 2, "scalp": 2}, 1, 1)


def test_entries_global_threshold_and_backward_compat():
    s = sigs((2, 2, 1.0), (2, 2, 0.5), (2, 2, 1.2), (2, 2, 1.3))
    base = entries(s, 1, 1, 2)
    assert [t for t, _ in base] == [s[1][0], s[2][0], s[3][0]]
    assert entries(s, 1, 1, 2, None, None, None, None) == base                       # без порога — как раньше
    got = entries(s, 1, 1, 2, global_min=1.1)
    assert [t for t, _ in got] == [s[3][0]]                                           # DG ≥ 1.1 два отчёта подряд — только 3-й и 4-й
    old7 = [x[:7] for x in s]                                                        # старый 7-элементный формат отчётов
    assert entries(old7, 1, 1, 2) == base and entries(old7, 1, 1, 2, global_min=0.0) == []


def test_resolve_entry_only_modes():
    inf = float("inf")
    assert resolve_entry(1, 2, 3, (4, 5), 6, None) == (1, 2, 3, (4, 5), 6)
    assert resolve_entry(1, 2, 3, (4, 5), 6, "local") == (1, -inf, None, (4, inf), None)
    assert resolve_entry(1, 2, 3, (4, 5), 6, "scalp") == (-inf, 2, None, (inf, 5), None)
    assert resolve_entry(1, 2, 3, (4, 5), 6, "global") == (-inf, -inf, 3, (inf, inf), 6)


def test_strong_checker_uses_global():
    s = sigs((2, 2, 1.0), (2, 2, 0.5))
    hold = strong_checker(s, 1, 1, (0, 0), 0.8, None)
    assert hold(1, s[0][0]) and not hold(1, s[1][0])
    assert strong_checker(s, 1, 1, None)(1, s[1][0])                                  # без порога Global — сильны по DL/DS


def test_load_signals_reads_global_column_and_text(tmp_path):
    p = tmp_path / "h.csv"
    with open(p, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["id", "date_utc", "deep_global", "deep_local", "deep_scalp", "text"])
        w.writerow([1, "2024-01-01T00:00:00+00:00", "1.12", "1.5", "1.4", ""])
        w.writerow([2, "2024-01-01T01:00:00+00:00", "", "1.5", "1.4", "Deep Global = 0.77\nDeep Local = 1.5\nDeep Scalp = 1.4"])
        w.writerow([3, "2024-01-01T02:00:00+00:00", "", "1.5", "1.4", ""])
    rows = load_signals(p)
    assert [r[7] for r in rows] == [1.12, 0.77, None] and all(len(r) == 8 for r in rows)


def test_fills_carry_metrics_at_fill_time():
    from app.service import _metrics_at
    s = sigs((1.0, 1.1, 0.9), (2.0, 2.1, 1.9), (3.0, 3.1, 2.9))
    at = _metrics_at([x[0] for x in s], s)
    assert at(T0 - 1) is None
    assert at(T0 + H + 5) == {"dg": 1.9, "dl": 2.0, "ds": 2.1}
    assert at(T0 + 10 * H)["dl"] == 3.0
    assert _metrics_at([T0], [(T0, 1, 2, None, None, None, None)])(T0) == {"dg": None, "dl": 1, "ds": 2}      # старый формат без DG
