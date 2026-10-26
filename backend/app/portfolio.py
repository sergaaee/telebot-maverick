"""Портфель стратегий пользователя: сравнение кривых капитала, корреляция результатов и суммарный риск (экспозиция по активам).

Все цифры — расчёт стратегий по истории (как в бэктесте), кроме блока «реальная биржа»: он берётся с подключённого счёта.
Два режима:
  * у каждой стратегии свой депозит (как в её конфиге) — портфель складывает результаты, база для процентов — сумма депозитов;
  * ОБЩИЙ депозит (total_deposit): все стратегии торгуют с одного счёта. Размер каждой сделки пересчитывается от общего баланса в момент входа (доля и плечо — из конфига
    стратегии; сложный процент — от общего баланса), поэтому доходности стратегий складываются честно, а пик одновременно занятой маржи виден в процентах общего депозита.
Отключённые стратегии (exclude) не участвуют ни в кривой, ни в корреляции, ни в экспозиции, ни в общем депозите.
build_portfolio — чистая функция от уже посчитанных результатов (удобно тестировать); collect — достаёт результаты через monitor.cached_result."""
import heapq
import math

from .core import backtest
from .core import equity as equity_mod
from .monitor import cached_result
from .service import ParamError, _clean, sharpe_ratio

DAY = 86_400_000
MAX_POINTS = 1500


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


def _curve(trades):
    """[[ts, накопленный результат $]] по времени закрытия + максимальная просадка в $ (по закрытым сделкам)."""
    cum, peak, dd, pts = 0.0, 0.0, 0.0, []
    for t in sorted((t for t in trades if t["reason"] != "OPEN"), key=lambda x: x["exit_ts"]):
        cum += t["pnl_usd"]
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
        pts.append([t["exit_ts"], cum])
    return pts, dd


def joint_sizing(rows, deposit):
    """Пересчёт размеров сделок для общего депозита. rows — активные стратегии с "sizing" и "trades". -> {id стратегии: [копии сделок с новыми notional / margin / pnl_usd]}.
    Правила те же, что в apply_sizing стратегии (доля первой ноги / добора, плечо, сложный процент), но база — общий баланс: депозит + уже закрытые результаты ВСЕХ стратегий."""
    legs = [(t["entry_ts"], t.get("leg", 1), i, t) for i, r in enumerate(rows) for t in r["trades"]]
    legs.sort(key=lambda x: (x[0], x[1], x[2]))
    out = {r["id"]: [] for r in rows}
    bal, pending = deposit, []
    for _, _, i, t in legs:
        while pending and pending[0][0] <= t["entry_ts"]:
            bal += heapq.heappop(pending)[1]
        sz = rows[i]["sizing"]
        base = bal if sz["compound"] else deposit
        live = max(base, 0.0) if bal > 0 else 0.0
        lev, lev_add = sz["leverage"], sz.get("pyramid_leverage") or sz["leverage"]
        if t.get("avg"):
            adds_pct = t.get("adds_n", 0) * (sz.get("pyramid_pct") or 0)
            margin = live * (sz["trade_pct"] + adds_pct) / 100
            notional = live * (sz["trade_pct"] * lev + adds_pct * lev_add) / 100
        else:
            add_leg = t.get("leg", 1) > 1 and sz.get("pyramid_pct")
            use = lev_add if add_leg else lev
            notional = live * (sz["pyramid_pct"] if add_leg else sz["trade_pct"]) / 100 * use
            margin = notional / use if use else 0.0
        c = dict(t)
        c["margin"], c["notional"], c["pnl_usd"] = margin, notional, notional * t["pnl_pct"] / 100
        heapq.heappush(pending, (t["exit_ts"], c["pnl_usd"]))
        out[rows[i]["id"]].append(c)
    return out


def peak_margin(trades):
    """Наибольшая сумма маржи одновременно открытых ног: (маржа, время). Выходы раньше входов в ту же секунду."""
    ev = []
    for t in trades:
        m = t.get("margin") or 0.0
        ev.append((t["entry_ts"], 1, m))
        if t["reason"] != "OPEN":
            ev.append((t["exit_ts"], 0, -m))
    ev.sort()
    cur = best = 0.0
    when = None
    for ts, _, m in ev:
        cur += m
        if cur > best:
            best, when = cur, ts
    return best, when


def build_portfolio(rows, real=None, min_corr_days=10, exclude=(), total_deposit=None, sizing=None):
    """rows: [{id, name, symbol, paused, deposit, sizing, trades, stats, error}] — trades как в результате бэктеста (с notional/margin у открытых).
    exclude — id стратегий, отключённых из расчётов портфеля; total_deposit — общий депозит (None — у каждой свой).
    sizing — {id стратегии: {"trade_pct", "leverage"}}: размер входа ТОЛЬКО для расчётов портфеля («что если»), конфиги не меняются; сделки таких стратегий пересчитываются.
    real: {"equity", "positions": [{symbol, side, notional}]} с подключённой биржи или None."""
    exclude = set(exclude or ())
    sizing = {int(k): v for k, v in (sizing or {}).items()}
    active = [r for r in rows if not r.get("error") and not r["paused"] and r["id"] not in exclude]
    for r in rows:                                            # эффективный размер: из конфига, а если задан «что если» — он
        if r.get("error"):
            continue
        r.setdefault("sizing", {"trade_pct": 10.0, "leverage": 1.0, "pyramid_pct": None, "pyramid_leverage": None, "compound": True})
        o = sizing.get(r["id"]) or {}
        r["eff_sizing"] = {**r["sizing"], **{k: o[k] for k in ("trade_pct", "leverage") if o.get(k) is not None}}
        r["overridden"] = r["eff_sizing"]["trade_pct"] != r["sizing"]["trade_pct"] or r["eff_sizing"]["leverage"] != r["sizing"]["leverage"]
    joint = bool(total_deposit) and bool(active)
    deposit = float(total_deposit) if joint else sum(r["deposit"] for r in active)
    if joint:
        sized = joint_sizing([{**r, "sizing": r["eff_sizing"]} for r in active], deposit)
    else:                                                     # у каждой свой депозит: пересчитываем только стратегии с изменённым размером
        sized = {}
        for r in active:
            sized[r["id"]] = joint_sizing([{**r, "sizing": r["eff_sizing"]}], r["deposit"])[r["id"]] if r["overridden"] else r["trades"]

    items = []
    for r in rows:
        base = {"id": r["id"], "name": r["name"], "symbol": r.get("symbol"), "paused": r.get("paused", False), "included": r["id"] not in exclude and not r.get("paused", False)}
        if r.get("error"):
            items.append({**base, "included": False, "error": r["error"]})
            continue
        sz_info = {"cfg_trade_pct": r["sizing"]["trade_pct"], "cfg_leverage": r["sizing"]["leverage"], "trade_pct": r["eff_sizing"]["trade_pct"], "leverage": r["eff_sizing"]["leverage"],
                   "overridden": bool(r["overridden"]), "liq_sim": bool(r["sizing"].get("liquidation_sim"))}
        if r["id"] in sized:
            tr = sized[r["id"]]
            curve, dd = _curve(tr)
            dep_i = deposit if joint else r["deposit"]
            if joint or r["overridden"]:
                st = backtest.stats(tr)
                st = {**st, "sharpe": sharpe_ratio(tr, dep_i) if tr else None, "return_pct": 100 * st["total_usd"] / dep_i}
                if r.get("candles"):
                    st.update(equity_mod.mtm_drawdown_multi([(tr, r["candles"])], dep_i))
            else:
                st = r.get("stats") or {}
            items.append({**base, **sz_info, "error": None, "deposit": r["deposit"], "trades": st.get("trades", 0), "winrate": st.get("winrate"), "profit_factor": st.get("profit_factor"),
                          "total_usd": st.get("total_usd"), "return_pct": st.get("return_pct"), "sharpe": st.get("sharpe"),
                          "max_dd_usd": dd, "max_dd_pct": 100 * dd / dep_i if dep_i else 0.0,
                          "max_dd_real_pct": st.get("max_dd_real_pct"), "max_dd_real_usd": st.get("max_dd_real_usd"), "curve": _thin(curve)})
        else:                                           # отключена из портфеля или на паузе: показываем собственные цифры, но в расчёты она не входит
            curve, dd = _curve(r["trades"])
            st = r.get("stats") or {}
            items.append({**base, **sz_info, "error": None, "deposit": r["deposit"], "trades": st.get("trades", 0), "winrate": st.get("winrate"), "profit_factor": st.get("profit_factor"),
                          "total_usd": st.get("total_usd"), "return_pct": st.get("return_pct"), "sharpe": st.get("sharpe"),
                          "max_dd_usd": dd, "max_dd_pct": 100 * dd / r["deposit"] if r["deposit"] else 0.0,
                          "max_dd_real_pct": st.get("max_dd_real_pct"), "max_dd_real_usd": st.get("max_dd_real_usd"), "curve": _thin(curve)})

    # --- общий портфель
    all_tr = [t for tr in sized.values() for t in tr]
    pcurve, pdd = _curve(all_tr)
    total = sum(t["pnl_usd"] for t in all_tr if t["reason"] != "OPEN")
    pk, pk_ts = peak_margin(all_tr)
    mtm = equity_mod.mtm_drawdown_multi([(sized[r["id"]], r["candles"]) for r in active if r.get("candles")], deposit) if active and all(r.get("candles") for r in active) else {}
    portfolio = {"strategies": len(active), "deposit": deposit, "joint": joint, "total_usd": total, "return_pct": 100 * total / deposit if deposit else None,
                 "max_dd_usd": pdd, "max_dd_pct": 100 * pdd / deposit if deposit else 0.0, "curve": _thin(pcurve),
                 "max_dd_real_usd": mtm.get("max_dd_real_usd"), "max_dd_real_pct": mtm.get("max_dd_real_pct"), "min_equity_usd": mtm.get("min_equity_usd"), "max_dd_real_ts": mtm.get("max_dd_real_ts"),
                 "trades": sum(1 for t in all_tr if t["reason"] != "OPEN"),
                 "peak_margin": pk, "peak_margin_pct": 100 * pk / deposit if deposit else None, "peak_margin_ts": pk_ts}
    contrib = sorted(((i["total_usd"] or 0.0, i["name"]) for i in items if i["included"] and not i["error"]), reverse=True)
    portfolio["best"], portfolio["worst"] = (contrib[0][1], contrib[-1][1]) if contrib else (None, None)

    # --- корреляция дневных результатов (в долях депозита стратегии / общего депозита) на общем календаре
    div = {r["id"]: (deposit if joint else r["deposit"]) for r in active}
    series = {r["id"]: {k: v / div[r["id"]] for k, v in _daily(sized[r["id"]]).items()} for r in active if div[r["id"]]}
    days, lo = None, 0
    allk = [k for s in series.values() for k in s]
    if len(series) >= 2 and allk:
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
        for t in sized[r["id"]]:
            if t["reason"] != "OPEN":
                continue
            base_ = (r["symbol"] or "?").split("/")[0]
            e = by.setdefault(base_, {"asset": base_, "long": 0.0, "short": 0.0, "margin": 0.0, "configs": []})
            e["long" if t["side"] == "long" else "short"] += t.get("notional") or 0.0
            e["margin"] += t.get("margin") or 0.0
            if r["name"] not in [c["name"] for c in e["configs"]]:
                e["configs"].append({"name": r["name"], "side": t["side"]})
    symbols, warnings = [], []
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
        warnings.append(f"Сейчас открыто маржи ({margin:.0f} $) больше {'общего депозита' if joint else 'суммы депозитов стратегий'} ({deposit:.0f} $): на одном счёте столько не хватит")
    if portfolio["peak_margin_pct"] is not None and portfolio["peak_margin_pct"] > 100:
        warnings.append(f"За всю историю пик одновременно занятой маржи — {portfolio['peak_margin']:.0f} $ ({portfolio['peak_margin_pct']:.0f}% {'общего депозита' if joint else 'суммы депозитов'}): "
                        "при таком депозите часть входов биржа бы отклонила — уменьшите доли входа или плечи, либо увеличьте депозит")
    if real:
        eq = real.get("equity") or 0.0
        real_gross = sum(abs(p.get("notional") or 0.0) for p in real.get("positions", []))
        exposure["real"] = {"equity": eq, "gross": real_gross, "leverage": real_gross / eq if eq else None}
        if eq and gross > eq * 10:
            warnings.append(f"Расчётный объём позиций ({gross:.0f} $) больше капитала на бирже ({eq:.0f} $) в {gross / eq:.1f} раз — проверьте плечи и размеры")
    return _clean({"items": items, "portfolio": portfolio, "correlation": correlation, "exposure": exposure, "settings": {"exclude": sorted(exclude), "total_deposit": total_deposit if joint else None, "sizing": {str(k): v for k, v in sizing.items() if any(r["id"] == k and r.get("overridden") for r in rows)}}})


def collect(store, defaults, configs, available_fn=None):
    rows = []
    for c in configs:
        base = {"id": c["id"], "name": c["name"], "paused": bool(c["paused"]), "symbol": c["params"].get("symbol")}
        try:
            res = cached_result(store, defaults, c["params"], available_fn)
        except (ParamError, FileNotFoundError, KeyError, ValueError) as e:
            rows.append({**base, "error": str(e.args[0]) if e.args else str(e)})
            continue
        p = res["params"]
        try:
            candles = store.candles(res["symbol"])
        except (KeyError, FileNotFoundError):
            candles = None
        rows.append({**base, "symbol": res["symbol"], "deposit": p["deposit"], "trades": res["trades"], "candles": candles, "stats": res["stats"]["total"], "error": None,
                     "sizing": {"trade_pct": p["trade_pct"], "leverage": p["leverage"], "pyramid_pct": p.get("pyramid_pct"), "pyramid_leverage": p.get("pyramid_leverage"),
                                "compound": bool(p.get("compound", True)), "liquidation_sim": bool(p.get("liquidation_sim"))}})
    return rows
