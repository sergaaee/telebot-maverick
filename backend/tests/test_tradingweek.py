from datetime import datetime, timezone

from app.core.tradingweek import in_window, week_bounds, window_close


def ms(*a):
    return int(datetime(*a, tzinfo=timezone.utc).timestamp() * 1000)


def test_window_edges_winter():
    # зима (EST): 17:00 ET = 22:00 UTC; 2026-01-05 — понедельник
    assert not in_window(ms(2026, 1, 4, 23, 59))                 # воскресенье
    assert in_window(ms(2026, 1, 5, 0, 0))                       # открытие Токио
    assert in_window(ms(2026, 1, 9, 21, 59))                     # пятница до закрытия NY
    assert not in_window(ms(2026, 1, 9, 22, 0))                  # закрытие
    assert not in_window(ms(2026, 1, 10, 12)) and not in_window(ms(2026, 1, 11, 12))
    assert window_close(ms(2026, 1, 6, 10)) == ms(2026, 1, 9, 22)


def test_window_close_shifts_with_us_dst():
    assert window_close(ms(2026, 7, 7, 10)) == ms(2026, 7, 10, 21)      # лето (EDT): 21:00 UTC
    assert window_close(ms(2026, 3, 4, 10)) == ms(2026, 3, 6, 22)       # 2-е воскресенье марта 2026 — 8-е: пятница 6-го ещё зимой
    assert window_close(ms(2026, 3, 10, 10)) == ms(2026, 3, 13, 21)     # после перехода — летнее время
    assert window_close(ms(2026, 10, 27, 10)) == ms(2026, 10, 30, 21)   # 1-е воскресенье ноября — 1-е: пятница 30 октября ещё летом
    assert window_close(ms(2026, 11, 3, 10)) == ms(2026, 11, 6, 22)


def test_close_before_minutes_and_week_bounds():
    assert window_close(ms(2026, 1, 6, 10), before_min=30) == ms(2026, 1, 9, 21, 30)
    assert not in_window(ms(2026, 1, 9, 21, 45), before_min=30)
    o, c = week_bounds(ms(2026, 1, 11, 23, 0))                           # воскресенье относится к уже закончившейся неделе
    assert o == ms(2026, 1, 5) and c == ms(2026, 1, 9, 22)


def test_weekday_split_by_exit_moment():
    from app.service import weekday_split
    def tr(reason, entry, exit_, pnl):
        return {"reason": reason, "entry_ts": entry, "exit_ts": exit_, "pnl_usd": pnl}
    trades = [tr("TP", ms(2026, 1, 5, 3), ms(2026, 1, 6, 3), 100.0),                 # будни
              tr("SL", ms(2026, 1, 8, 3), ms(2026, 1, 9, 12), -40.0),
              tr("WKND", ms(2026, 1, 8, 4), ms(2026, 1, 9, 22), -10.0),             # закрытие пятницы ровно в 22:00 UTC — будний выход
              tr("SL", ms(2026, 1, 9, 20), ms(2026, 1, 10, 3), -30.0),              # вход в пятницу, выход в субботу
              tr("TP", ms(2026, 1, 10, 5), ms(2026, 1, 11, 5), 20.0)]               # вход и выход в выходные
    r = weekday_split(trades)
    assert r["weekday"]["trades"] == 3 and r["weekend"]["trades"] == 2
    assert r["weekday"]["reasons"]["TP"] == 1 and r["weekday"]["reasons"]["SL"] == 1 and r["weekday"]["reasons"]["WKND"] == 1
    assert r["weekend"]["reasons"]["SL"] == 1 and r["weekend"]["reasons"]["TP"] == 1
    assert abs(r["weekday"]["pnl_usd"] - 50.0) < 1e-9 and r["weekday"]["wins"] == 1 and r["weekend"]["wins"] == 1
    assert r["entered"] == {"weekday": 4, "weekend": 1}
