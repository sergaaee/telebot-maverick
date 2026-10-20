from app.core import montecarlo


def test_path_and_drawdown():
    curve, dd = montecarlo.path([0.1, -0.5, 0.2], True)
    assert [round(x, 4) for x in curve] == [1.0, 1.1, 0.55, 0.66] and abs(dd - 0.5) < 1e-9
    curve, dd = montecarlo.path([0.1, -0.5, 0.2], False)                  # простой процент: суммируется
    assert [round(x, 4) for x in curve] == [1.0, 1.1, 0.6, 0.8] and abs(dd - 0.4545454) < 1e-6
    curve, dd = montecarlo.path([-1.5, 0.3], True)                        # счёт слит — остаётся 0
    assert curve == [1.0, 0.0, 0.0] and dd == 1.0


def test_shuffle_keeps_compound_final_but_changes_drawdown():
    rets = [0.05, -0.03, 0.04, -0.02, 0.06, -0.04] * 10
    r = montecarlo.run(rets, runs=300, mode="shuffle", compound=True, seed=1)
    f = r["final_pct"]
    assert abs(f["5"] - f["95"]) < 1e-6 and abs(f["50"] - r["actual"]["final_pct"]) < 1e-6            # итог не зависит от порядка при сложном проценте
    assert r["max_dd_pct"]["5"] < r["max_dd_pct"]["95"] or r["max_dd_pct"]["5"] == r["max_dd_pct"]["95"]
    assert r["max_dd_pct"]["95"] >= r["max_dd_pct"]["50"] >= r["max_dd_pct"]["5"] >= 0


def test_bootstrap_spreads_final_and_is_reproducible():
    rets = [0.05, -0.03, 0.04, -0.02, 0.06, -0.04, 0.1, -0.08] * 5
    a = montecarlo.run(rets, runs=400, mode="bootstrap", compound=True, seed=7)
    b = montecarlo.run(rets, runs=400, mode="bootstrap", compound=True, seed=7)
    assert a == b
    f = a["final_pct"]
    assert f["5"] < f["50"] < f["95"]
    assert 0 <= a["prob_loss_pct"] <= 100 and a["prob_dd_pct"]["10"] >= a["prob_dd_pct"]["50"]
    fan = a["fan"]
    assert fan[0]["n"] >= 1 and fan[-1]["n"] == len(rets) and all(p["5"] <= p["50"] <= p["95"] for p in fan)


def test_all_losers_means_certain_loss_and_too_few_trades():
    r = montecarlo.run([-0.05] * 10, runs=100, mode="shuffle", compound=True, seed=0)
    assert r["prob_loss_pct"] == 100 and r["final_pct"]["50"] < 0
    assert montecarlo.run([0.1], runs=100) is None
