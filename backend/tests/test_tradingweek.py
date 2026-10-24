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


def test_time_stats_by_day_and_session():
    from app.service import session_of, time_stats
    def tr(reason, entry, exit_, pnl):
        return {"reason": reason, "entry_ts": entry, "exit_ts": exit_, "pnl_usd": pnl}
    assert [session_of(ms(2026, 1, 5, h)) for h in (0, 7, 8, 12, 13, 20, 21, 23)] == ["asia", "asia", "europe", "europe", "us", "us", "night", "night"]
    trades = [tr("TP", ms(2026, 1, 5, 1), ms(2026, 1, 5, 9), 100.0),           # пн, Азия -> выход пн, Европа
              tr("SL", ms(2026, 1, 5, 14), ms(2026, 1, 6, 2), -50.0),          # пн, США -> вт, Азия
              tr("TP", ms(2026, 1, 11, 22), ms(2026, 1, 12, 3), 30.0)]         # вс, ночь -> пн, Азия
    r = time_stats(trades)
    d_in, d_out = r["entry"]["day"], r["exit"]["day"]
    assert [b["trades"] for b in d_in] == [2, 0, 0, 0, 0, 0, 1]                  # пн ×2, вс ×1 (по входу)
    assert [b["trades"] for b in d_out] == [2, 1, 0, 0, 0, 0, 0]                 # пн ×2 (09:00 и 03:00 следующего пн), вт ×1
    assert d_in[0]["reasons"]["TP"] == 1 and d_in[0]["reasons"]["SL"] == 1 and abs(d_in[0]["pnl_usd"] - 50.0) < 1e-9 and d_in[0]["wins"] == 1
    assert abs(d_in[0]["gross_win"] - 100.0) < 1e-9 and abs(d_in[0]["gross_loss"] - 50.0) < 1e-9
    s_in = {x["key"]: x for x in r["entry"]["session"]}
    assert (s_in["asia"]["trades"], s_in["europe"]["trades"], s_in["us"]["trades"], s_in["night"]["trades"]) == (1, 0, 1, 1)
    s_out = {x["key"]: x for x in r["exit"]["session"]}
    assert (s_out["asia"]["trades"], s_out["europe"]["trades"]) == (2, 1)
    assert sum(b["trades"] for b in d_in) == sum(x["trades"] for x in r["entry"]["session"]) == 3
