"""Защита портфеля: лимит занятой маржи и пауза стороны после серии убытков (в расчёте портфеля), а также исправленные метрики — просадка от пика капитала и маржа от баланса."""
import pytest

from app import portfolio_guards as pg
from app.config import load_settings
from app.main import create_app
from app.portfolio import build_portfolio, joint_sizing, margin_profile
from test_auth import H_, auth_env, client, login  # noqa: F401
from test_portfolio import H, SZ, leg, srow

HOUR = 3_600_000


def pos(entry_h, exit_h, pnl_pct, group, side="long", leg_no=1, reason="TP"):
    t = leg(entry_h, exit_h, pnl_pct, reason, side, leg_no)
    t["group"] = group
    return t


# ---------------------------------------------------------------- настройки

def test_normalize_and_validate():
    assert pg.normalize(None) == {"margin_cap_pct": None, "side_pause": None}
    assert pg.normalize({"margin_cap_pct": 70, "side_pause": {"losses": 6, "window_h": 24, "pause_h": 24}}) == {"margin_cap_pct": 70.0, "side_pause": {"losses": 6, "window_h": 24.0, "pause_h": 24.0}}
    for junk in ({"margin_cap_pct": 0}, {"margin_cap_pct": -5}, {"margin_cap_pct": 5000}, {"margin_cap_pct": True}, {"margin_cap_pct": "много"}, {"side_pause": {"losses": 0, "window_h": 1, "pause_h": 1}},
                 {"side_pause": {"losses": 3}}, {"side_pause": "да"}, {"side_pause": {"losses": 2, "window_h": 0, "pause_h": 1}}):
        assert pg.normalize(junk) == {"margin_cap_pct": None, "side_pause": None}, junk
    assert pg.validate_cap(None) is None and pg.validate_cap(50) == 50.0
    for bad in (0, -1, 1001, True, "50"):
        with pytest.raises(ValueError):
            pg.validate_cap(bad)
    assert pg.validate_side_pause(None) is None and pg.validate_side_pause({"losses": 3, "window_h": 12, "pause_h": 6}) == {"losses": 3, "window_h": 12.0, "pause_h": 6.0}
    for bad in ({"losses": 2.5, "window_h": 1, "pause_h": 1}, {"losses": 0, "window_h": 1, "pause_h": 1}, {"losses": 2, "window_h": 0, "pause_h": 1}, {"losses": 2, "window_h": 1, "pause_h": 10000}, {"losses": 2}, [], 5):
        with pytest.raises(ValueError):
            pg.validate_side_pause(bad)
    assert not pg.active(None) and not pg.active({"margin_cap_pct": None, "side_pause": None}) and pg.active({"margin_cap_pct": 50, "side_pause": None})


def test_losing_closes_window_and_side():
    closed = [(10 * HOUR, "short", -1.0), (11 * HOUR, "short", 2.0), (12 * HOUR, "long", -1.0), (13 * HOUR, "short", -3.0)]
    assert pg.losing_closes(closed, "short", 14 * HOUR, 24) == 2 and pg.losing_closes(closed, "long", 14 * HOUR, 24) == 1
    assert pg.losing_closes(closed, "short", 14 * HOUR, 1.5) == 1                                        # окно (12.5; 14] — осталась только закрытая в 13
    assert pg.losing_closes(closed, "short", 12 * HOUR, 24) == 1 and pg.losing_closes(closed, "short", 13 * HOUR, 3) == 1


# ---------------------------------------------------------------- лимит маржи

def two_overlapping():
    return [srow(1, "A", [pos(0, 10, 2.0, 1)], trade_pct=40.0, leverage=2.0), srow(2, "B", [pos(1, 11, 3.0, 1)], trade_pct=40.0, leverage=2.0)]   # по 40% маржи — вместе 80%


def test_margin_cap_skips_a_position_that_would_exceed_the_limit():
    rows = two_overlapping()
    free = build_portfolio(rows, total_deposit=1000.0)
    assert free["portfolio"]["peak_util_pct"] == pytest.approx(80.0) and free["portfolio"]["guards"] is None and len(free["portfolio"]["curve"]) == 2
    r = build_portfolio(two_overlapping(), total_deposit=1000.0, guards={"margin_cap_pct": 60.0, "side_pause": None})
    p = r["portfolio"]
    assert p["strategies"] == 2 and [i["trades"] for i in r["items"]] == [1, 0] and p["trades"] == 1
    assert p["peak_util_pct"] == pytest.approx(40.0) and p["total_usd"] == pytest.approx(1000 * 0.4 * 2 * 0.02)          # вторая позиция пропущена: ни в кривой, ни в результате
    g = p["guards"]
    assert g["margin_cap_pct"] == 60.0 and g["skipped"] == {"margin": 1, "margin_add": 0, "side_pause": 0} and g["skipped_total"] == 1
    assert g["last_skipped"][0]["name"] == "B" and g["last_skipped"][0]["reason"] == "margin" and r["items"][1]["skipped_margin"] == 1 and r["items"][0]["skipped_margin"] == 0
    assert r["settings"]["margin_cap_pct"] == 60.0 and r["settings"]["side_pause"] is None
    ok = build_portfolio(two_overlapping(), total_deposit=1000.0, guards={"margin_cap_pct": 80.0, "side_pause": None})        # ровно в лимит — проходит
    assert ok["portfolio"]["trades"] == 2 and ok["portfolio"]["guards"]["skipped_total"] == 0


def test_margin_cap_uses_the_balance_at_the_moment_and_frees_margin_on_exit():
    a = srow(1, "A", [pos(0, 5, 50.0, 1)], trade_pct=50.0, leverage=2.0)                 # +50% × 100% объёма = +500 $ к депозиту 1000 -> баланс 1500
    b = srow(2, "B", [pos(6, 12, 1.0, 1)], trade_pct=100.0, leverage=1.0)                # после выхода A: маржа 1500 при балансе 1500 — это 100% (сложный процент от баланса)
    r = build_portfolio([a, b], total_deposit=1000.0, guards={"margin_cap_pct": 100.0, "side_pause": None})
    assert r["portfolio"]["trades"] == 2 and r["portfolio"]["peak_util_pct"] == pytest.approx(100.0)
    c = srow(3, "C", [pos(3, 12, 1.0, 1)], trade_pct=60.0, leverage=1.0)                 # открывается, пока A ещё держит 50%: 50 + 60 > 100 -> пропуск
    r = build_portfolio([srow(1, "A", [pos(0, 5, 50.0, 1)], trade_pct=50.0, leverage=2.0), c], total_deposit=1000.0, guards={"margin_cap_pct": 100.0, "side_pause": None})
    assert r["portfolio"]["trades"] == 1 and r["portfolio"]["guards"]["skipped"]["margin"] == 1


def test_a_rejected_position_takes_its_adds_with_it_but_a_rejected_add_alone_does_not():
    rows = [srow(1, "A", [pos(0, 20, 1.0, 1), pos(2, 20, 1.0, 1, leg_no=2)], trade_pct=40.0, leverage=1.0, pyramid_pct=40.0),
            srow(2, "B", [pos(1, 20, 1.0, 1), pos(3, 20, 1.0, 1, leg_no=2)], trade_pct=40.0, leverage=1.0, pyramid_pct=40.0)]
    g = {"margin_cap_pct": 90.0, "side_pause": None}
    # A: 40% (вход 0) -> B: +40% = 80% (вход 1) -> A-добор: +40% = 120% > 90% (пропуск добора) -> B-добор: тоже пропуск; позиции A и B остаются
    r = build_portfolio(rows, total_deposit=1000.0, guards=g)
    assert r["portfolio"]["trades"] == 2 and r["portfolio"]["guards"]["skipped"] == {"margin": 0, "margin_add": 2, "side_pause": 0}
    one = [srow(1, "A", [pos(0, 20, 1.0, 1)], trade_pct=70.0, leverage=1.0), srow(2, "B", [pos(1, 20, 1.0, 1), pos(2, 20, 1.0, 1, leg_no=2)], trade_pct=40.0, leverage=1.0, pyramid_pct=5.0)]
    r = build_portfolio(one, total_deposit=1000.0, guards=g)                             # B отклонена целиком (70 + 40 > 90) — её добор молча выпадает, а не считается отдельным пропуском
    assert r["portfolio"]["trades"] == 1 and r["portfolio"]["guards"]["skipped"] == {"margin": 1, "margin_add": 0, "side_pause": 0}


def test_margin_cap_works_without_a_common_deposit_too():
    rows = [srow(1, "A", [pos(0, 10, 2.0, 1)], deposit=500.0, trade_pct=40.0, leverage=2.0), srow(2, "B", [pos(1, 11, 3.0, 1)], deposit=500.0, trade_pct=40.0, leverage=2.0)]
    for r_ in rows:                                                                         # сделки уже посчитаны на депозиты стратегий (как в расчёте бэктеста)
        for t in r_["trades"]:
            t["margin"], t["notional"], t["pnl_usd"] = 200.0, 400.0, 400.0 * t["pnl_pct"] / 100
    r = build_portfolio(rows, guards={"margin_cap_pct": 60.0, "side_pause": None})       # баланс — сумма депозитов 1000: 200 + 200 = 40% — проходит; лимит 30% пропустит вторую
    assert r["portfolio"]["trades"] == 2 and not r["portfolio"]["joint"]
    r = build_portfolio(rows, guards={"margin_cap_pct": 30.0, "side_pause": None})
    assert r["portfolio"]["trades"] == 1 and r["portfolio"]["guards"]["skipped"]["margin"] == 1 and r["portfolio"]["strategies"] == 2


# ---------------------------------------------------------------- пауза стороны

def losers_then_probe(side="short", n=4):
    """n убыточных позиций стороны (по часу каждая, закрытие в часы 1…n), затем кандидаты в часы n+1 … — по одной на стратегию."""
    rows = [srow(i + 1, f"L{i + 1}", [pos(i, i + 1, -1.0, 1, side)], trade_pct=10.0, leverage=1.0) for i in range(n)]
    return rows


def test_side_pause_skips_new_positions_of_that_side_after_a_series_of_losses():
    rows = losers_then_probe("short", 4) + [srow(10, "S", [pos(5, 9, 5.0, 1, "short")], trade_pct=10.0, leverage=1.0),             # 4 убытка шорта за последние 24 ч -> пауза, пропуск
                                           srow(11, "Л", [pos(5, 9, 5.0, 1, "long")], trade_pct=10.0, leverage=1.0),              # лонг не затронут
                                           srow(12, "S2", [pos(40, 42, 5.0, 1, "short")], trade_pct=10.0, leverage=1.0)]          # пауза давно кончилась, убытки (часы 1–4) уже вне 24-часового окна -> вход
    rule = {"losses": 4, "window_h": 24.0, "pause_h": 12.0}
    r = build_portfolio(rows, total_deposit=1000.0, guards={"margin_cap_pct": None, "side_pause": rule})
    names = {i["name"]: i["trades"] for i in r["items"]}
    assert names["S"] == 0 and names["Л"] == 1 and names["L1"] == 1                                       # шорт 5-го часа пропущен, лонг открыт
    g = r["portfolio"]["guards"]
    assert g["skipped"]["side_pause"] == 1 and g["pauses_n"] == {"long": 0, "short": 1} and g["pauses"][0]["side"] == "short" and g["pauses"][0]["until_ts"] - g["pauses"][0]["from_ts"] == 12 * HOUR
    assert g["side_pause"] == rule and r["items"][4]["skipped_pause"] == 1
    assert names["S2"] == 1


def test_pause_lasts_pause_hours_then_losses_are_counted_again():
    base = losers_then_probe("short", 3)                                                                  # убытки закрылись в часы 1, 2, 3
    cand = [srow(10 + k, f"C{k}", [pos(h, h + 1, 5.0, 1, "short")], trade_pct=10.0, leverage=1.0) for k, h in enumerate((4, 8, 14, 40))]
    r = build_portfolio(base + cand, total_deposit=1000.0, guards={"margin_cap_pct": None, "side_pause": {"losses": 3, "window_h": 10.0, "pause_h": 6.0}})
    got = {i["name"]: i["trades"] for i in r["items"]}
    # час 4: 3 убытка за окно -> пауза до 10 ч, пропуск. час 8: пауза идёт -> пропуск (новая пауза не запускается). час 14: окно (4; 14] без убытков (они в 1–3) -> вход. час 40: вход.
    assert (got["C0"], got["C1"], got["C2"], got["C3"]) == (0, 0, 1, 1) and r["portfolio"]["guards"]["pauses_n"]["short"] == 1


def test_wins_do_not_count_and_adds_are_never_blocked_by_the_pause():
    wins = [srow(i + 1, f"W{i}", [pos(i, i + 1, 2.0, 1, "short")], trade_pct=10.0, leverage=1.0) for i in range(5)]
    cand = srow(9, "C", [pos(6, 12, 1.0, 1, "short"), pos(7, 12, 1.0, 1, "short", leg_no=2)], trade_pct=10.0, leverage=1.0, pyramid_pct=5.0)
    rule = {"losses": 2, "window_h": 24.0, "pause_h": 24.0}
    r = build_portfolio(wins + [cand], total_deposit=1000.0, guards={"margin_cap_pct": None, "side_pause": rule})
    assert r["portfolio"]["guards"]["skipped_total"] == 0 and r["portfolio"]["trades"] == 7
    losses = losers_then_probe("short", 2)
    cand2 = srow(9, "C", [pos(0.5, 12, 1.0, 9, "short"), pos(7, 12, 1.0, 9, "short", leg_no=2)], trade_pct=10.0, leverage=1.0, pyramid_pct=5.0)   # позиция открылась ДО серии убытков: добор в разгар паузы разрешён
    r = build_portfolio(losses + [cand2], total_deposit=1000.0, guards={"margin_cap_pct": None, "side_pause": {"losses": 2, "window_h": 24.0, "pause_h": 24.0}})
    assert [i["trades"] for i in r["items"] if i["name"] == "C"] == [2] and r["portfolio"]["guards"]["skipped_total"] == 0


def test_both_guards_together_and_no_guards_change_nothing():
    rows = lambda: [srow(1, "A", [pos(0, 10, 2.0, 1)], trade_pct=10.0, leverage=2.0), srow(2, "B", [pos(1, 11, -3.0, 1, "short")], trade_pct=10.0, leverage=2.0)]
    plain = build_portfolio(rows(), total_deposit=1000.0)
    off = build_portfolio(rows(), total_deposit=1000.0, guards={"margin_cap_pct": None, "side_pause": None})
    assert plain["portfolio"]["total_usd"] == off["portfolio"]["total_usd"] and plain["portfolio"]["guards"] is None and off["portfolio"]["guards"] is None
    both = build_portfolio(rows(), total_deposit=1000.0, guards={"margin_cap_pct": 100.0, "side_pause": {"losses": 5, "window_h": 24.0, "pause_h": 24.0}})
    assert both["portfolio"]["total_usd"] == plain["portfolio"]["total_usd"] and both["portfolio"]["guards"]["skipped_total"] == 0
    info = {}
    out = joint_sizing(rows(), 1000.0, {"margin_cap_pct": 1.0, "side_pause": None}, info)
    assert all(not v for v in out.values()) and len(info["skipped"]) == 2                                   # лимит 1% — не проходит ничего


# ---------------------------------------------------------------- метрики: баланс и пик капитала

def test_peak_margin_is_a_share_of_the_balance_not_of_the_initial_deposit():
    a = srow(1, "A", [pos(0, 10, 100.0, 1)], trade_pct=50.0, leverage=1.0)              # маржа 500 при балансе 1000 (50%); +100% на объём = +500 -> баланс 1500
    b = srow(2, "B", [pos(11, 20, 1.0, 1)], trade_pct=100.0, leverage=1.0)               # вся маржа 1500 при балансе 1500 = 100% (сложный процент: размер растёт вместе с балансом)
    r = build_portfolio([a, b], total_deposit=1000.0)
    p = r["portfolio"]
    assert p["peak_margin"] == pytest.approx(1500.0) and p["peak_margin_pct"] == pytest.approx(150.0)       # от НАЧАЛЬНОГО депозита — «раздутая» цифра, оставлена для совместимости
    assert p["peak_util_pct"] == pytest.approx(100.0) and p["peak_util_balance"] == pytest.approx(1500.0) and p["peak_util_margin"] == pytest.approx(1500.0) and p["peak_util_ts"] == 11 * H
    assert p["balance"] == pytest.approx(1500.0 + 15.0) and not any("доля занятой маржи" in w for w in r["exposure"]["warnings"])           # ровно 100% — ещё не «больше баланса»
    assert margin_profile([], 1000.0)["util_pct"] == 0.0


def test_closed_drawdown_is_a_percent_of_the_equity_peak():
    a = srow(1, "A", [pos(0, 10, 100.0, 1), pos(11, 20, -50.0, 2)], trade_pct=100.0, leverage=1.0)        # 1000 -> 2000 -> 1000: просадка 50% от пика, а не 100% от депозита
    r = build_portfolio([a], total_deposit=1000.0)
    assert r["portfolio"]["max_dd_usd"] == pytest.approx(1000.0) and r["portfolio"]["max_dd_pct"] == pytest.approx(50.0) and r["items"][0]["max_dd_pct"] == pytest.approx(50.0)
    b = srow(2, "B", [pos(0, 10, 300.0, 1), pos(11, 20, -50.0, 2)], trade_pct=100.0, leverage=1.0)       # 1000 -> 4000 -> 2000: просадка 2000 $ = 50% пика (раньше — «200% депозита»)
    r = build_portfolio([b], total_deposit=1000.0)
    assert r["portfolio"]["max_dd_pct"] == pytest.approx(100 * 2000 / 4000)


# ---------------------------------------------------------------- API настроек

def test_api_saves_and_validates_the_guards_and_applies_them(auth_env):
    db = auth_env
    alice = db.check_login("alice", "alice-password")
    app = create_app(load_settings())
    a = client(app)
    login(a, "alice", "alice-password")
    assert a.get("/api/portfolio/prefs").json()["margin_cap_pct"] is None and a.get("/api/portfolio/prefs").json()["side_pause"] is None
    r = a.put("/api/portfolio/prefs", json={"margin_cap_pct": 70, "side_pause": {"losses": 6, "window_h": 24, "pause_h": 24}}, headers=H_)
    assert r.status_code == 200 and r.json()["margin_cap_pct"] == 70.0 and r.json()["side_pause"] == {"losses": 6, "window_h": 24.0, "pause_h": 24.0}
    assert a.get("/api/portfolio/prefs").json()["margin_cap_pct"] == 70.0
    assert a.put("/api/portfolio/prefs", json={"total_deposit": 1000}, headers=H_).json()["margin_cap_pct"] == 70.0         # другие настройки не затирают защиту
    for bad in ({"margin_cap_pct": 0}, {"margin_cap_pct": "много"}, {"margin_cap_pct": 5000}, {"side_pause": {"losses": 0, "window_h": 1, "pause_h": 1}}, {"side_pause": {"losses": 3}}, {"side_pause": 7}):
        assert a.put("/api/portfolio/prefs", json=bad, headers=H_).status_code == 422, bad
    assert a.put("/api/portfolio/prefs", json={"margin_cap_pct": None, "side_pause": None}, headers=H_).json() == {**a.get("/api/portfolio/prefs").json(), "margin_cap_pct": None, "side_pause": None}
    a.put("/api/portfolio/prefs", json={"margin_cap_pct": 55}, headers=H_)
    pf = a.post("/api/portfolio", headers=H_).json()
    assert pf["settings"]["margin_cap_pct"] == 55.0 and pf["portfolio"]["guards"]["margin_cap_pct"] == 55.0 and pf["portfolio"]["guards"]["side_pause"] is None
    b = client(app)
    login(b, "bob", "bob-password-1")
    assert b.get("/api/portfolio/prefs").json()["margin_cap_pct"] is None                                      # настройки у каждого пользователя свои
    del alice


# ---------------------------------------------------------------- период «с даты по дату»

def test_period_takes_positions_opened_in_the_period_and_sizes_them_from_the_deposit_at_its_start():
    a = srow(1, "A", [leg(10, 20, 10.0), leg(50, 60, 5.0), leg(70, 100, 3.0), leg(80, 90, 7.0)], trade_pct=10.0, leverage=2.0)
    a["trades"][2].update(entry=100.0, side="long", fills=[{"ts": 75 * H, "price": 105.0, "share": 0.5}, {"ts": 95 * H, "price": 106.0, "share": 0.5}])
    full = build_portfolio([a], total_deposit=1000.0)
    assert full["portfolio"]["period"] is None and full["portfolio"]["trades"] == 4
    r = build_portfolio([a], total_deposit=1000.0, date_from="1970-01-03", date_to="1970-01-03")          # 1970-01-03: часы 48…71 (UTC, конец дня включительно)
    p = r["portfolio"]
    assert p["period"] == {"date_from": "1970-01-03", "date_to": "1970-01-03", "from_ts": 48 * H, "to_ts": 72 * H - 1, "open_at_end": 1}
    assert p["trades"] == 1 and p["total_usd"] == pytest.approx(1000 * 0.10 * 2 * 0.05)                    # единственная закрытая в периоде: 5% × 200 $ от депозита 1000 (а не от баланса с прошлого периода)
    assert r["items"][0]["trades"] == 2 and r["settings"]["date_from"] == "1970-01-03" and r["settings"]["date_to"] == "1970-01-03"      # у стратегии считаются и открытые ноги (1 закрыта + 1 открыта)
    # нога, открытая в периоде, но не закрытая к его концу, — открытая: в результат не входит, в экспозиции есть
    assert r["exposure"]["symbols"][0]["long"] > 0 and p["curve"][-1][1] == pytest.approx(p["total_usd"])
    only_from = build_portfolio([a], total_deposit=1000.0, date_from="1970-01-03")
    assert only_from["portfolio"]["trades"] == 3 and only_from["portfolio"]["period"]["open_at_end"] == 0     # без конца периода ноги закрываются как в расчёте
    only_to = build_portfolio([a], total_deposit=1000.0, date_to="1970-01-03")
    assert only_to["portfolio"]["trades"] == 2 and only_to["portfolio"]["period"]["open_at_end"] == 1 and only_to["portfolio"]["period"]["from_ts"] is None


def test_truncated_leg_keeps_only_the_part_fixed_before_the_end_date():
    from app.portfolio import period_trades
    t = leg(70, 100, 3.0)
    t.update(entry=100.0, side="long", fills=[{"ts": 75 * H, "price": 105.0, "share": 0.5}, {"ts": 95 * H, "price": 106.0, "share": 0.5}], funding_pct=0.4)
    [c] = period_trades([t], None, 80 * H)
    assert c["reason"] == "OPEN" and c["exit_ts"] == 80 * H and c["pnl_pct"] == pytest.approx(2.5) and c["funding_pct"] == 0.0 and len(c["fills"]) == 1
    assert t["reason"] == "TP" and t["pnl_pct"] == 3.0                                                       # исходная сделка не изменена
    assert period_trades([t], 71 * H, None) == [] and period_trades([t], None, 69 * H) == []                # открыта не в периоде
    short = leg(70, 100, 3.0, side="short")
    short.update(entry=100.0, fills=[{"ts": 75 * H, "price": 95.0, "share": 1.0}])
    assert period_trades([short], None, 80 * H)[0]["pnl_pct"] == pytest.approx(5.0)                          # у шорта прибыль при падении цены


def test_period_works_with_guards_and_per_strategy_deposits():
    rows = [srow(1, "A", [leg(10, 20, 10.0), leg(50, 60, 5.0)], deposit=500.0, trade_pct=10.0, leverage=2.0), srow(2, "B", [leg(55, 70, -4.0)], deposit=500.0, trade_pct=10.0, leverage=2.0)]
    r = build_portfolio(rows, date_from="1970-01-03", date_to="1970-01-04")
    assert not r["portfolio"]["joint"] and r["portfolio"]["trades"] == 2 and r["items"][0]["trades"] == 1 and r["items"][1]["trades"] == 1
    assert r["items"][0]["total_usd"] == pytest.approx(500 * 0.10 * 2 * 0.05) and r["items"][1]["total_usd"] == pytest.approx(-500 * 0.10 * 2 * 0.04)
    g = build_portfolio(rows, total_deposit=1000.0, date_from="1970-01-03", guards={"margin_cap_pct": 15.0, "side_pause": None})      # в периоде A (100 $) и B (100 $) пересекаются в часы 55–60: 20% > 15%
    assert g["portfolio"]["trades"] == 1 and g["portfolio"]["guards"]["skipped"]["margin"] == 1
    empty = build_portfolio(rows, total_deposit=1000.0, date_from="2030-01-01")
    assert empty["portfolio"]["trades"] == 0 and empty["portfolio"]["total_usd"] == 0 and empty["portfolio"]["curve"] == []


def test_api_period_prefs_are_validated_stored_and_applied(auth_env):
    db = auth_env
    app = create_app(load_settings())
    a = client(app)
    login(a, "alice", "alice-password")
    assert a.get("/api/portfolio/prefs").json()["date_from"] is None
    r = a.put("/api/portfolio/prefs", json={"date_from": "2026-01-01", "date_to": "2026-06-30"}, headers=H_)
    assert r.status_code == 200 and r.json()["date_from"] == "2026-01-01" and r.json()["date_to"] == "2026-06-30"
    assert a.put("/api/portfolio/prefs", json={"total_deposit": 500}, headers=H_).json()["date_from"] == "2026-01-01"          # другие настройки период не трогают
    for bad in ({"date_from": "01.01.2026"}, {"date_to": "вчера"}, {"date_from": 20260101}, {"date_from": "2026-07-01", "date_to": "2026-06-30"}):
        assert a.put("/api/portfolio/prefs", json=bad, headers=H_).status_code == 422, bad
    assert a.get("/api/portfolio/prefs").json()["date_from"] == "2026-01-01"                                                    # неверный запрос ничего не испортил
    pf = a.post("/api/portfolio", headers=H_).json()
    assert pf["settings"]["date_from"] == "2026-01-01" and pf["portfolio"]["period"]["date_to"] == "2026-06-30"
    assert a.put("/api/portfolio/prefs", json={"date_from": None, "date_to": None}, headers=H_).json()["date_to"] is None
    assert a.post("/api/portfolio", headers=H_).json()["portfolio"]["period"] is None
    del db
