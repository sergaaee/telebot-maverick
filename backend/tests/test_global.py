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


def test_fill_metrics_report_strong_flag_for_sl_hold():
    from app.service import _metrics_at
    s = sigs((2.0, 2.4, None), (1.7, 2.4, None))                       # DS проходит порог, но DL упал ниже — стоп должен действовать
    hold = strong_checker(s, 1.9, 2.0, None)
    at = _metrics_at([x[0] for x in s], s, hold)
    assert at(T0 + 5, 1)["strong"] is True and at(T0 + H + 5, 1)["strong"] is False
    assert "strong" not in _metrics_at([x[0] for x in s], s)(T0 + 5, 1)


def test_very_long_message_text_does_not_break_history_loading(tmp_path, caplog):
    import logging
    p = tmp_path / "h.csv"
    big = "x" * 300_000                                              # больше лимита csv по умолчанию (128 КБ)
    with open(p, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["id", "date_utc", "deep_global", "deep_local", "deep_scalp", "text"])
        w.writerow([1, "2024-01-01T00:00:00+00:00", "1.0", "1.5", "1.4", big])
        w.writerow([2, "2024-01-01T01:00:00+00:00", "1.0", "1.6", "1.5", ""])
    with caplog.at_level(logging.WARNING, logger="entries"):
        rows = load_signals(p)
    assert [r[1] for r in rows] == [1.5, 1.6]
    assert any("лишняя кавычка" in m for m in caplog.messages)


# ---------------- «ждёт подтверждения» ----------------
def sig_rows(values, liq_long=None):
    return [(T0 + i * H, 1.0, v, (liq_long[i] if liq_long else None), None, None, None, None) for i, v in enumerate(values)]


def test_pending_reports_which_filter_holds_the_entry():
    from app.core.entries import entries
    pend = []
    ev = entries(sig_rows([2.1, 2.2, 2.3, 2.4, 2.4, 2.3]), 0, 2.0, 4, None, None, {"metric": "scalp", "drop": 0.075}, pending=pend)
    assert [t for t, _ in ev] == [T0 + 5 * H]                                                    # вход только на откате
    assert [p[0] for p in pend] == [T0 + 3 * H, T0 + 4 * H]                                      # 4-й и 5-й отчёты: серия есть, отката нет
    w = pend[0][2]["cool"]
    assert w["kind"] == "drop" and w["need"] == 0.075 and w["have"] == 0.0 and w["ext"] == 2.4
    assert "liq" not in pend[0][2]
    assert entries(sig_rows([2.1, 2.2, 2.3, 2.4, 2.4, 2.3]), 0, 2.0, 4, None, None, {"metric": "scalp", "drop": 0.075}) == ev           # без pending результат тот же


def test_pending_cool_declines_and_liq_reasons():
    from app.core.entries import entries
    pend = []
    entries(sig_rows([2.1, 2.2, 2.3, 2.4, 2.5]), 0, 2.0, 4, None, None, {"metric": "scalp", "declines": 2}, pending=pend)
    assert pend[-1][2]["cool"] == {"kind": "declines", "metric": "scalp", "need": 2, "have": 0}
    # только ликвидации: всплеск 500 в окне, сейчас 450 > 0.8 × 500 = 400 — ждём спада
    pend = []
    rows = sig_rows([2.1, 2.2, 2.3, 2.4, 2.5], liq_long=[100, 500, 480, 470, 450])
    ev = entries(rows, 0, 2.0, 4, None, {"hours": 120, "frac": 0.8, "pct": 0}, None, pending=pend)
    assert ev == [] and [p[2]["liq"]["reason"] for p in pend] == ["not_calmed", "not_calmed"]
    assert pend[-1][2]["liq"]["limit"] == 400.0 and pend[-1][2]["liq"]["max"] == 500
    # данных о ликвидациях в отчёте нет
    pend = []
    entries(sig_rows([2.1, 2.2, 2.3, 2.4]), 0, 2.0, 4, None, {"hours": 120, "frac": 0.8, "pct": 0}, None, pending=pend)
    assert pend[0][2]["liq"] == {"reason": "no_data"}
    # перцентиль 90 при короткой истории (<50 значений): всплеска «нет»
    pend = []
    entries(rows, 0, 2.0, 4, None, {"hours": 120, "frac": 0.8, "pct": 90}, None, pending=pend)
    assert pend[0][2]["liq"]["reason"] == "no_spike" and pend[0][2]["liq"]["thr"] is None


def test_waiting_lines_text():
    from app.service import waiting_lines
    t = waiting_lines(1, {"cool": {"kind": "drop", "metric": "scalp", "need": 0.075, "have": 0.0, "ext": 2.4, "now": 2.4},
                         "liq": {"reason": "not_calmed", "now": 450.0, "max": 500.0, "limit": 400.0, "frac": 0.8},
                         "trend": {"emas": [20, 50], "tf": "1d"}})
    assert "откат DS" in t[0] and "максимума" in t[0] and "0.075" in t[0]
    assert "ликвидации лонгов" in t[1] and "≤ 400.0k" in t[1]
    assert "выше EMA 20/50 на 1d" in t[2]
    assert "минимума" in waiting_lines(-1, {"cool": {"kind": "drop", "metric": "scalp", "need": 0.1, "have": 0.0, "ext": 0.8, "now": 0.8}})[0]
