import pytest

from app.config import load_settings
from app.schemas import WalkForwardRequest
from app.service import ParamError
from app.walkforward import run_walkforward
from test_runcache import FakeStore, make

BASE = {"side": "both", "sl_pct": 0.5, "tps": [{"pct": 0.5, "share": 100}]}


def run(tmp_path, **kw):
    sigs, candles = make(seed=3, n_rep=9000)
    req = WalkForwardRequest(**{**BASE, **kw})
    return run_walkforward(FakeStore(tmp_path, sigs, candles), load_settings().defaults, req)


def test_walkforward_windows_do_not_overlap_and_summary_is_consistent(tmp_path):
    r = run(tmp_path, grid=[{"param": "sl_pct", "values": [0.3, 0.6, 1.0]}], train_days=60, test_days=30)
    f = r["folds"]
    assert len(f) >= 3
    for a, b in zip(f, f[1:]):
        assert a["test"][0] > a["train"][1] and b["train"][0] > a["train"][0]                       # проверка строго после подбора, окна сдвигаются вперёд
    assert all(x["train"][1] < x["test"][0] for x in f)
    done = [x for x in f if x["test_stats"]]
    s = r["summary"]
    assert s["folds"] == len(f) and s["folds_with_choice"] == len(done) and s["grid_size"] == 3
    assert abs(s["oos_total_usd"] - sum(x["test_stats"]["total_usd"] for x in done)) < 1e-9
    assert done and all(set(x["chosen"]) == {"sl_pct"} and x["chosen"]["sl_pct"] in (0.3, 0.6, 1.0) for x in done)
    assert r["oos_curve"] and abs(r["oos_curve"][-1][1] - s["oos_total_usd"]) < 1e-9 and r["verdict"]
    assert sum(x["times"] for x in r["stability"]) == len(done)


def test_chosen_values_are_best_on_train_by_metric(tmp_path):
    r = run(tmp_path, grid=[{"param": "sl_pct", "values": [0.3, 1.0]}], train_days=60, test_days=30, metric="pnl")
    sigs, candles = make(seed=3, n_rep=9000)
    from app.schemas import BacktestParams
    from app.service import run_backtest
    st = FakeStore(tmp_path, sigs, candles)
    f = next(x for x in r["folds"] if x["chosen"])
    res = {v: run_backtest(st, load_settings().defaults, BacktestParams(**{**BASE, "top_enabled": False, "sl_pct": v, "date_from": f["train"][0], "date_to": f["train"][1]}), use_cache=False)["stats"]["total"]["total_usd"] for v in (0.3, 1.0)}
    assert f["chosen"]["sl_pct"] == max(res, key=res.get)


def test_walkforward_validation(tmp_path):
    with pytest.raises(ParamError, match="не хватает"):
        run(tmp_path, grid=[{"param": "sl_pct", "values": [0.3, 1.0]}], train_days=300, test_days=200)
    with pytest.raises(ParamError, match="дважды"):
        run(tmp_path, grid=[{"param": "sl_pct", "values": [0.3, 1.0]}, {"param": "sl_pct", "values": [0.5, 2.0]}])
    with pytest.raises(ParamError, match="Слишком много"):
        run(tmp_path, grid=[{"param": "local_min", "values": [1.0 + i / 20 for i in range(30)]}, {"param": "scalp_min", "values": [1.0 + i / 20 for i in range(30)]}], train_days=14, test_days=7)
