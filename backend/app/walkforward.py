"""Walk-forward: проверка, не подогнаны ли параметры под прошлое.

История режется на окна: на окне «подбора» перебираются значения из сетки и выбираются лучшие по метрике, затем они применяются на СЛЕДУЮЩЕМ окне «проверки», которого
подбор не видел. Окна сдвигаются вперёд. Итог — результат на проверочных отрезках (честный, «вне выборки») рядом с результатом тех же значений на подборе и с результатом
исходных (неизменных) параметров на тех же проверочных отрезках. Если подбор даёт на проверке заметно хуже, чем на подборе, параметры — подгонка."""
import math
from datetime import datetime, timezone

from .schemas import BacktestParams, WalkForwardRequest
from .service import ParamError, _clean, run_backtest

DAY = 86_400_000
MAX_RUNS = 600                 # все прогоны одной проверки (окна × значения сетки)
MAX_FOLDS = 24


def _date(day):
    return datetime.fromtimestamp(day * DAY / 1000, timezone.utc).strftime("%Y-%m-%d")


def _score(stats, metric):
    pnl = stats.get("total_usd") or 0.0
    if metric == "pnl":
        return pnl
    if metric == "sharpe":
        v = stats.get("sharpe")
        return v if v is not None else -math.inf
    if metric == "profit_factor":
        v = stats.get("profit_factor")
        return min(v, 1e6) if v is not None else -math.inf
    dd = stats.get("max_dd_usd") or 0.0
    return pnl / dd if dd > 0 else (pnl if pnl > 0 else -math.inf)       # calmar: результат / максимальная просадка


def _brief(res):
    t = res["stats"]["total"]
    return {"trades": t["trades"], "total_usd": t["total_usd"], "winrate": t["winrate"], "profit_factor": t["profit_factor"], "sharpe": t.get("sharpe"),
            "max_dd_usd": t["max_dd_usd"], "return_pct": t.get("return_pct")}


def _combos(grid):
    out = [{}]
    for ax in grid:
        out = [{**o, ax.param: (int(v) if ax.param == "confirm" else float(v))} for o in out for v in ax.values]
    return out


def run_walkforward(store, defaults, req: WalkForwardRequest, available_fn=None):
    base_kw = req.model_dump(exclude={"grid", "train_days", "test_days", "step_days", "metric", "min_trades"}, exclude_unset=True)
    base_kw["top_enabled"] = False                                   # топ-коины для подбора не нужны (и требуют скачивания свечей)
    if len({ax.param for ax in req.grid}) != len(req.grid):
        raise ParamError("Один и тот же параметр нельзя перебирать дважды")
    probe = run_backtest(store, defaults, BacktestParams(**base_kw), available_fn=available_fn, use_cache=False)
    first, last = probe["range"]["first_ts"] // DAY, probe["range"]["last_ts"] // DAY
    train, test = req.train_days, req.test_days
    step = req.step_days or test
    folds = []
    d = first
    while d + train + test - 1 <= last:
        folds.append((d, d + train - 1, d + train, d + train + test - 1))
        d += step
    if not folds:
        raise ParamError(f"Истории ({last - first + 1} дн.) не хватает на одно окно: подбор {train} + проверка {test} дней. Уменьшите окна или расширьте диапазон дат")
    if len(folds) > MAX_FOLDS:
        folds = folds[-MAX_FOLDS:]                                   # самые свежие окна
    combos = _combos(req.grid)
    runs = len(folds) * (len(combos) + 2)
    if runs > MAX_RUNS:
        raise ParamError(f"Слишком много прогонов: {len(folds)} окон × {len(combos) + 2} = {runs} (максимум {MAX_RUNS}). Уменьшите сетку или увеличьте шаг окон")

    def run(extra, a, b):
        kw = {**base_kw, **extra, "date_from": _date(a), "date_to": _date(b)}
        return run_backtest(store, defaults, BacktestParams(**kw), available_fn=available_fn, use_cache=False)

    out_folds, cum_oos, curve = [], 0.0, []
    chosen_count = {}
    for (a, b, c, e) in folds:
        scored = []
        for combo in combos:
            try:
                r = run(combo, a, b)
            except ParamError:
                continue                                              # на этом окне значения неприменимы (нет отчётов, нет Global и т.п.)
            st = r["stats"]["total"]
            if st["trades"] >= req.min_trades:
                scored.append((_score(st, req.metric), combo, _brief(r)))
        fold = {"train": [_date(a), _date(b)], "test": [_date(c), _date(e)], "chosen": None, "train_stats": None, "test_stats": None, "base_test_stats": None}
        try:
            fold["base_test_stats"] = _brief(run({}, c, e))
        except ParamError:
            pass
        if scored:
            scored.sort(key=lambda x: x[0], reverse=True)
            score, combo, tr = scored[0]
            fold.update(chosen=combo, train_stats=tr, train_score=None if score in (math.inf, -math.inf) else score)
            key = tuple(sorted(combo.items()))
            chosen_count[key] = chosen_count.get(key, 0) + 1
            try:
                ts = _brief(run(combo, c, e))
                fold["test_stats"] = ts
                cum_oos += ts["total_usd"]
                curve.append([(e + 1) * DAY, cum_oos])
            except ParamError:
                pass
        out_folds.append(fold)

    done = [f for f in out_folds if f["test_stats"]]
    oos = sum(f["test_stats"]["total_usd"] for f in done)
    ins = sum(f["train_stats"]["total_usd"] for f in done)
    base = sum((f["base_test_stats"] or {}).get("total_usd", 0.0) for f in done)
    per_day_is, per_day_oos = (ins / (len(done) * train) if done else 0.0), (oos / (len(done) * test) if done else 0.0)
    summary = {
        "folds": len(out_folds), "folds_with_choice": len(done),
        "oos_total_usd": oos, "oos_trades": sum(f["test_stats"]["trades"] for f in done),
        "is_total_usd": ins, "base_oos_total_usd": base,
        "profitable_folds": sum(1 for f in done if f["test_stats"]["total_usd"] > 0),
        # во сколько раз результат в день на проверке отличается от результата в день на подборе: 1 — подбор не подогнан, <0.5 — вероятна подгонка
        "efficiency": (per_day_oos / per_day_is) if per_day_is > 0 else None,
        "metric": req.metric, "grid_size": len(combos), "runs": runs,
    }
    stability = [{"values": dict(k), "times": n} for k, n in sorted(chosen_count.items(), key=lambda kv: -kv[1])]
    if summary["efficiency"] is None:
        verdict = "На окнах подбора нет прибыли — оценивать устойчивость не по чему."
    elif summary["efficiency"] >= 0.6 and oos > 0:
        verdict = "Подбор держится на новых данных: результат на проверке сопоставим с результатом на подборе."
    elif oos > 0:
        verdict = "На проверке прибыль заметно слабее, чем на подборе: часть результата — подгонка. Закладывайте запас."
    else:
        verdict = "На проверочных отрезках убыток: подобранные значения, скорее всего, подгонка под прошлое."
    return _clean({"folds": out_folds, "summary": summary, "oos_curve": curve, "stability": stability, "verdict": verdict, "base": {k: base_kw.get(k) for k in ("symbol", "source")}})
