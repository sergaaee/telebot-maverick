"""Монте-Карло по сделкам бэктеста: насколько результат зависит от порядка и состава сделок.

Каждая сделка — доходность в долях баланса на момент её входа (сложный процент) или от начального депозита (простой). Прогоны строятся из этих
доходностей: либо та же серия в случайном порядке (меняется просадка, итог при сложном проценте тот же), либо выборка С ВОЗВРАЩЕНИЕМ — случайный
набор из тех же сделок (меняется и итог). Допущение: сделки идут одна за другой; пересечение ног пирамидинга по времени не учитывается."""
import random

PCTS = (5, 25, 50, 75, 95)
DD_LEVELS = (10, 20, 30, 50)


def _pct(sorted_vals, p):
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * p / 100
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def path(returns, compound):
    """Кривая капитала в долях депозита (старт 1.0) и максимальная просадка в долях. Баланс не опускается ниже 0 (счёт слит — дальше без сделок)."""
    e, peak, dd, curve = 1.0, 1.0, 0.0, [1.0]
    for r in returns:
        e = max(0.0, e * (1 + r) if compound else e + r)
        peak = max(peak, e)
        dd = max(dd, (peak - e) / peak if peak > 0 else 0.0)
        curve.append(e)
    return curve, dd


def run(returns, runs=1000, mode="shuffle", compound=True, seed=None, points=60):
    n = len(returns)
    if n < 2:
        return None
    rng = random.Random(seed)
    steps = sorted({round(i * n / min(points, n)) for i in range(1, min(points, n) + 1)} | {n})
    finals, dds, at_step = [], [], {k: [] for k in steps}
    ruined = 0
    for _ in range(runs):
        seq = rng.choices(returns, k=n) if mode == "bootstrap" else rng.sample(returns, n)
        curve, dd = path(seq, compound)
        finals.append(curve[-1])
        dds.append(dd)
        ruined += min(curve) <= 0.0
        for k in steps:
            at_step[k].append(curve[k])
    fs, ds = sorted(finals), sorted(dds)
    actual_curve, actual_dd = path(returns, compound)
    return {
        "runs": runs, "mode": mode, "trades": n, "compound": compound,
        "final_pct": {str(p): (_pct(fs, p) - 1) * 100 for p in PCTS} | {"mean": (sum(fs) / runs - 1) * 100},
        "max_dd_pct": {str(p): _pct(ds, p) * 100 for p in PCTS} | {"mean": sum(ds) / runs * 100},
        "prob_loss_pct": sum(1 for f in finals if f < 1) / runs * 100,
        "prob_ruin_pct": ruined / runs * 100,
        "prob_dd_pct": {str(lv): sum(1 for d in dds if d * 100 >= lv) / runs * 100 for lv in DD_LEVELS},
        "actual": {"final_pct": (actual_curve[-1] - 1) * 100, "max_dd_pct": actual_dd * 100},
        "fan": [{"n": k, **{str(p): _pct(sorted(at_step[k]), p) for p in PCTS}} for k in steps],
    }
