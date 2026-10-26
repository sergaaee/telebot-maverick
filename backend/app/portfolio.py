"""Портфель стратегий пользователя: сравнение кривых капитала, корреляция результатов и суммарный риск (экспозиция по активам).

Все цифры — расчёт стратегий по истории (как в бэктесте), кроме блока «реальная биржа»: он берётся с подключённого счёта.
build_portfolio — чистая функция от уже посчитанных результатов (удобно тестировать); collect — достаёт результаты через monitor.cached_result."""
import math

from .monitor import cached_result
from .service import ParamError

DAY = 86_400_000
MAX_POINTS = 400


def _thin(points, n=MAX_POINTS):
    if len(points) <= n:
        return points
    step = len(points) / n
    return [points[int(i * step)] for i in range(n)] + [points[-1]]


def _daily(trades):
    d = {}
    for t in trades:
        if t["reason"] != "OPEN":
            k = t["exit_ts"] // DAY
            d[k] = d.get(k, 0.0) + t["pnl_usd"]
    return d


def _pearson(a, b):
    n = len(a)
    if n < 2:
        return None
    ma, mb = sum(a) / n, sum(b) / n
    va, vb = sum((x - ma) ** 2 for x in a), sum((y - mb) ** 2 for y in b)
    if va <= 0 or vb <= 0:
        return None
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / math.sqrt(va * vb)


def _curve(trades, deposit):
    """[[ts, % от депозита]] по времени закрытия + максимальная просадка в % от депозита (по закрытым сделкам)."""
    cum, peak, dd, pts = 0.0, 0.0, 0.0, []
    for t in sorted((t for t in trades if t["reason"] != "OPEN"), key=lambda x: x["exit_ts"]):
        cum += t["pnl_usd"]
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
        pts.append([t["exit_ts"], 100 * cum / deposit if deposit else 0.0])
    return pts, (100 * dd / deposit if deposit else 0.0)


def build_portfolio(rows, real=None, min_corr_days=10):
    """rows: [{id, name, symbol, paused, deposit, trades, stats, error}] — trades как в результате бэктеста (с notional/margin у открытых).
    real: {"equity", "positions": [{symbol, side, notional}]} с подключённой биржи или None."""
    items, active = [], []
    for r in rows:
        if r.get("error"):
            items.append({"id": r["id"], "name": r["name"], "symbol": r.get("symbol"), "paused": r.get("paused", False), "error": r["error"]})
            continue
        curve, dd = _curve(r["trades"], r["deposit"])
        st = r.get("stats") or {}
        it = {"id": r["id"], "name": r["name"], "symbol": r["symbol"], "paused": r["paused"], "deposit": r["deposit"], "error": None,
              "trades": st.get("trades", 0), "winrate": st.get("winrate"), "profit_factor": st.get("profit_factor"), "total_usd": st.get("total_usd"),
              "return_pct": st.get("return_pct"), "sharpe": st.get("sharpe"), "max_dd_pct": dd, "curve": _thin(curve)}
        items.append(it)
        if not r["paused"]:
            active.append(r)

    # --- общий портфель: сумма результатов активных стратегий, база — сумма их депозитов
    deposit = sum(r["deposit"] for r in active)
    all_tr = [t for r in active for t in r["trades"]]
    pcurve, pdd = _curve(all_tr, deposit)
    total = sum(t["pnl_usd"] for t in all_tr if t["reason"] != "OPEN")
    portfolio = {"strategies": len(active), "deposit": deposit, "total_usd": total, "return_pct": 100 * total / deposit if deposit else None,
                 "max_dd_pct": pdd, "curve": _thin(pcurve),
                 "trades": sum(1 for t in all_tr if t["reason"] != "OPEN")}
    # лучший/худший вклад
    contrib = sorted(((i["total_usd"] or 0.0, i["name"]) for i in items if not i["error"] and not i["paused"]), reverse=True)
    portfolio["best"], portfolio["worst"] = (contrib[0][1], contrib[-1][1]) if contrib else (None, None)

    # --- корреляция дневных результатов (в долях депозита) на общем календаре
    series = {r["id"]: {k: v / r["deposit"] for k, v in _daily(r["trades"]).items()} for r in active if r["deposit"]}
    days = None
    if len(series) >= 2:
        allk = [k for s in series.values() for k in s]
        lo, hi = min(allk), max(allk)
        days = hi - lo + 1
    ids = [r["id"] for r in active if r["id"] in series]
    matrix = []
    for a in ids:
        row = []
        for b in ids:
            if a == b:
                row.append(1.0)
                continue
            if days is None or days < min_corr_days:
                row.append(None)
                continue
            lo = min(k for s in series.values() for k in s)
            xs = [series[a].get(lo + i, 0.0) for i in range(days)]
            ys = [series[b].get(lo + i, 0.0) for i in range(days)]
            row.append(_pearson(xs, ys))
        matrix.append(row)
    names = {r["id"]: r["name"] for r in active}
    correlation = {"ids": ids, "names": [names[i] for i in ids], "matrix": matrix, "days": days,
                   "note": None if days and days >= min_corr_days else f"для корреляции нужно хотя бы {min_corr_days} дней общей истории и две стратегии с результатами"}

    # --- экспозиция: открытые сейчас позиции (по расчёту) по активам
    by = {}
    for r in active:
        for t in r["trades"]:
            if t["reason"] != "OPEN":
                continue
            base = (r["symbol"] or "?").split("/")[0]
            e = by.setdefault(base, {"asset": base, "long": 0.0, "short": 0.0, "margin": 0.0, "configs": []})
            e["long" if t["side"] == "long" else "short"] += t.get("notional") or 0.0
            e["margin"] += t.get("margin") or 0.0
            if r["name"] not in [c["name"] for c in e["configs"]]:
                e["configs"].append({"name": r["name"], "side": t["side"]})
    symbols = []
    warnings = []
    for e in by.values():
        e["net"] = e["long"] - e["short"]
        e["gross"] = e["long"] + e["short"]
        symbols.append(e)
        same = {}
        for c in e["configs"]:
            same.setdefault(c["side"], []).append(c["name"])
        for side, names_ in same.items():
            if len(names_) > 1:
                warnings.append(f"{len(names_)} стратегии одновременно в {'лонге' if side == 'long' else 'шорте'} по {e['asset']} ({', '.join(names_)}): суммарный объём {e['long' if side == 'long' else 'short']:.0f} $ — риск складывается")
        if e["long"] and e["short"]:
            warnings.append(f"По {e['asset']} стратегии держат противоположные позиции (лонг {e['long']:.0f} $ и шорт {e['short']:.0f} $): часть объёма гасит друг друга, а комиссии платятся с обоих")
    symbols.sort(key=lambda x: -x["gross"])
    gross = sum(e["gross"] for e in symbols)
    margin = sum(e["margin"] for e in symbols)
    exposure = {"symbols": symbols, "gross": gross, "net": sum(e["net"] for e in symbols), "margin": margin,
                "leverage": gross / deposit if deposit else None, "margin_pct": 100 * margin / deposit if deposit else None, "warnings": warnings}
    if exposure["margin_pct"] is not None and exposure["margin_pct"] > 100:
        warnings.append(f"Суммарная маржа открытых позиций ({margin:.0f} $) больше суммы депозитов стратегий ({deposit:.0f} $): депозиты стратегий пересекаются — на одном счёте столько не хватит")
    if real:
        eq = real.get("equity") or 0.0
        real_gross = sum(abs(p.get("notional") or 0.0) for p in real.get("positions", []))
        exposure["real"] = {"equity": eq, "gross": real_gross, "leverage": real_gross / eq if eq else None}
        if eq and gross > eq * 10:
            warnings.append(f"Расчётный объём позиций ({gross:.0f} $) больше капитала на бирже ({eq:.0f} $) в {gross / eq:.1f} раз — проверьте плечи и размеры")
    return {"items": items, "portfolio": portfolio, "correlation": correlation, "exposure": exposure}


def collect(store, defaults, configs, available_fn=None):
    rows = []
    for c in configs:
        base = {"id": c["id"], "name": c["name"], "paused": bool(c["paused"]), "symbol": c["params"].get("symbol")}
        try:
            res = cached_result(store, defaults, c["params"], available_fn)
        except (ParamError, FileNotFoundError, KeyError, ValueError) as e:
            rows.append({**base, "error": str(e.args[0]) if e.args else str(e)})
            continue
        rows.append({**base, "symbol": res["symbol"], "deposit": res["params"]["deposit"], "trades": res["trades"], "stats": res["stats"]["total"], "error": None})
    return rows
