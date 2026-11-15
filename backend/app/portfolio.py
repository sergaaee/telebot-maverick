"""Портфель стратегий пользователя: сравнение кривых капитала, корреляция результатов и суммарный риск (экспозиция по активам).

Все цифры — расчёт стратегий по истории (как в бэктесте), кроме блока «реальная биржа»: он берётся с подключённого счёта.
Два режима:
  * у каждой стратегии свой депозит (как в её конфиге) — портфель складывает результаты, база для процентов — сумма депозитов;
  * ОБЩИЙ депозит (total_deposit): все стратегии торгуют с одного счёта. Размер каждой сделки пересчитывается от общего баланса в момент входа (доля и плечо — из конфига
    стратегии; сложный процент — от общего баланса), поэтому доходности стратегий складываются честно, а пик одновременно занятой маржи виден в процентах общего депозита.
Отключённые стратегии (exclude) не участвуют ни в кривой, ни в корреляции, ни в экспозиции, ни в общем депозите.
Защита портфеля (portfolio_guards): лимит занятой маржи и пауза стороны после серии убытков — пропущенные входы не попадают ни в кривую, ни в статистику, ни в экспозицию.
Просадка «по закрытым» считается от ПИКА капитала (а не от начального депозита), занятая маржа — от баланса на момент входа: при сложном проценте депозит растёт в разы, и проценты от
начального депозита (2000 % просадки, 19000 % маржи) ничего не говорят.
build_portfolio — чистая функция от уже посчитанных результатов (удобно тестировать); collect — достаёт результаты через monitor.cached_result."""
import heapq
import math

from . import portfolio_guards as pg
from .core import backtest
from .core import equity as equity_mod
from .monitor import cached_result
from .service import ParamError, _clean, _day_ms, sharpe_ratio

DAY = 86_400_000
MAX_POINTS = 1500


def _thin(points, n=MAX_POINTS):
    if len(points) <= n:
        return points
    step = len(points) / n
    return [points[int(i * step)] for i in range(n)] + [points[-1]]


def period_trades(trades, t0=None, t1=None):
    """Сделки для расчёта «за период» [t0; t1] (мс, оба необязательны): берутся ноги, ОТКРЫТЫЕ внутри периода. Нога, не закрытая к концу периода (t1), считается открытой на этот момент:
    reason OPEN, выход — t1, в результат входит только уже зафиксированная часть (тейки до t1), без комиссии за выход и фандинга. Размеры позже пересчитываются от депозита на начало периода."""
    out = []
    for t in trades:
        if (t0 is not None and t["entry_ts"] < t0) or (t1 is not None and t["entry_ts"] > t1):
            continue
        if t1 is not None and (t["reason"] == "OPEN" or t["exit_ts"] > t1):
            c = dict(t)
            fills = [f for f in (t.get("fills") or []) if f["ts"] <= t1]
            d = 1 if t["side"] == "long" else -1
            entry = t.get("entry") or 0.0
            c["fills"] = fills
            c["pnl_pct"] = sum(d * (f["price"] / entry - 1) * f["share"] * 100 for f in fills) if entry else 0.0
            c["reason"], c["exit_ts"] = "OPEN", t1
            if "funding_pct" in c:
                c["funding_pct"] = 0.0
            out.append(c)
        else:
            out.append(t)
    return out


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


def _curve(trades, base=None):
    """[[ts, накопленный результат $]] по времени закрытия + максимальная просадка в $ (по закрытым сделкам). Если задан начальный капитал base, третьим значением — наибольшая просадка
    в процентах от ПИКА капитала (base + накопленный результат на пике); без base (None) — 0.0."""
    cum, peak, dd, dd_pct, pts = 0.0, 0.0, 0.0, 0.0, []
    for t in sorted((t for t in trades if t["reason"] != "OPEN"), key=lambda x: x["exit_ts"]):
        cum += t["pnl_usd"]
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
        if base and base + peak > 0:
            dd_pct = max(dd_pct, 100 * (peak - cum) / (base + peak))
        pts.append([t["exit_ts"], cum])
    return pts, dd, min(dd_pct, 100.0)


def joint_sizing(rows, deposit, guards=None, info=None, resize=True):
    """Пересчёт размеров сделок для общего депозита. rows — активные стратегии с "sizing" и "trades". -> {id стратегии: [копии сделок с новыми notional / margin / pnl_usd]}.
    Правила те же, что в apply_sizing стратегии (доля первой ноги / добора, плечо, сложный процент), но база — общий баланс: депозит + уже закрытые результаты ВСЕХ стратегий.
    resize=False — размеры сделок уже посчитаны (у каждой стратегии свой депозит), нужно только применить защиту. guards — правила portfolio_guards: вход пропускается, если превышен лимит
    маржи или сторона на паузе; пропущенная позиция выпадает целиком (вместе с доборами), а пропущенный добор — только он. info — словарь, который заполняется: "skipped" — пропуски
    [{ts, id, side, reason}] (margin / margin_add / side_pause), "pauses" — периоды паузы сторон [{side, from_ts, until_ts}]."""
    guards = guards if pg.active(guards) else None
    cap, rule = (guards or {}).get("margin_cap_pct"), (guards or {}).get("side_pause")
    pause = pg.SidePause(rule) if rule else None
    legs = [(t["entry_ts"], t.get("leg", 1), i, t) for i, r in enumerate(rows) for t in r["trades"]]
    legs.sort(key=lambda x: (x[0], x[1], x[2]))
    out = {r["id"]: [] for r in rows}
    skipped = []
    bal, pending, margin_open, mpending = deposit, [], 0.0, []
    last_key, rejected, admitted = {}, {}, {}
    pos_legs = {}
    if guards:
        for i, r in enumerate(rows):                          # какие ноги образуют позицию: по номеру группы из расчёта стратегии (без него — первая нога начинает позицию)
            cur = None
            for t in sorted(r["trades"], key=lambda x: (x["entry_ts"], x.get("leg", 1))):
                if t.get("group") is not None:
                    cur = (i, t["group"])
                elif t.get("leg", 1) == 1 or cur is None:
                    cur = (i, "e", t["entry_ts"])
                pos_legs.setdefault(cur, []).append(t)
    keys = {id(t): k for k, ls in pos_legs.items() for t in ls}
    for _, _, i, t in legs:
        while pending and pending[0][0] <= t["entry_ts"]:
            bal += heapq.heappop(pending)[1]
        while mpending and mpending[0][0] <= t["entry_ts"]:
            margin_open -= heapq.heappop(mpending)[1]
        key = keys.get(id(t))
        if key in rejected:
            continue
        sz = rows[i]["sizing"]
        if resize:
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
            pnl = notional * t["pnl_pct"] / 100
        else:
            margin, notional, pnl = t.get("margin") or 0.0, t.get("notional") or 0.0, t["pnl_usd"]
        if guards:
            first = key not in admitted
            if first and pause is not None:
                closed = pg.positions_closed({k: v for k, v in admitted.items() if _closes_by(v, t["entry_ts"])})
                if not pause.allow(t["side"], t["entry_ts"], closed):
                    rejected[key] = "side_pause"
                    skipped.append({"ts": t["entry_ts"], "id": rows[i]["id"], "side": t["side"], "reason": "side_pause"})
                    continue
            if cap and (bal <= 0 or margin_open + margin > cap / 100 * bal + 1e-9):
                if first:
                    rejected[key] = "margin"
                skipped.append({"ts": t["entry_ts"], "id": rows[i]["id"], "side": t["side"], "reason": "margin" if first else "margin_add"})
                continue
        c = dict(t)
        c["margin"], c["notional"], c["pnl_usd"] = margin, notional, pnl
        if "funding_pct" in t:
            c["funding_usd"] = notional * t["funding_pct"] / 100
        if guards:
            admitted.setdefault(key, []).append(c)
        heapq.heappush(pending, (t["exit_ts"], c["pnl_usd"]))
        heapq.heappush(mpending, (t["exit_ts"], margin))
        margin_open += margin
        out[rows[i]["id"]].append(c)
    if info is not None:
        info["skipped"], info["pauses"] = skipped, (pause.periods if pause else [])
    return out


def _closes_by(legs, ts):
    """Все ноги позиции закрыты не позже момента ts (у позиции нет открытых ног)."""
    return all(t["reason"] != "OPEN" and t["exit_ts"] <= ts for t in legs)


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


def margin_profile(trades, deposit):
    """Занятая маржа относительно БАЛАНСА на тот же момент (депозит + закрытые результаты): наибольшая доля -> {"util_pct", "ts", "margin", "balance"}. Выходы раньше входов в ту же секунду.
    В отличие от процента от начального депозита, не раздувается сложным процентом: при балансе в 150 раз больше депозита маржа в 100 раз больше депозита — это всего 70 % баланса."""
    ev = []
    for t in trades:
        m = t.get("margin") or 0.0
        ev.append((t["entry_ts"], 1, m, 0.0))
        if t["reason"] != "OPEN":
            ev.append((t["exit_ts"], 0, -m, t["pnl_usd"]))
    ev.sort(key=lambda e: (e[0], e[1]))
    bal, cur = deposit, 0.0
    best = {"util_pct": 0.0, "ts": None, "margin": 0.0, "balance": deposit}
    for ts, kind, m, pnl in ev:
        cur += m
        bal += pnl
        if kind == 1 and cur > 0:
            u = 100 * cur / bal if bal > 0 else math.inf
            if u > best["util_pct"]:
                best = {"util_pct": u, "ts": ts, "margin": cur, "balance": bal}
    return best


def build_portfolio(rows, real=None, min_corr_days=10, exclude=(), total_deposit=None, sizing=None, compound=None, guards=None, date_from=None, date_to=None):
    """rows: [{id, name, symbol, paused, deposit, sizing, trades, stats, error}] — trades как в результате бэктеста (с notional/margin у открытых).
    exclude — id стратегий, отключённых из расчётов портфеля; total_deposit — общий депозит (None — у каждой свой).
    compound — True / False: включить / выключить сложный процент сразу для ВСЕХ стратегий портфеля (None — как в конфигах).
    sizing — {id стратегии: {"trade_pct", "leverage"}}: размер входа ТОЛЬКО для расчётов портфеля («что если»), конфиги не меняются; сделки таких стратегий пересчитываются.
    guards — правила portfolio_guards (лимит маржи, пауза стороны): пропущенные входы не попадают в расчёты.
    date_from / date_to — «ГГГГ-ММ-ДД» (UTC, конец дня включительно), любая граница необязательна: считаются только позиции, открытые в этом периоде, размеры — от депозита на начало периода;
    позиции, не закрытые к концу, считаются открытыми на эту дату (period_trades): в результат не входят, а в экспозиции («риск») видны.
    real: {"equity", "positions": [{symbol, side, notional}]} с подключённой биржи или None."""
    exclude = set(exclude or ())
    guards = guards if pg.active(guards) else None
    p0 = _day_ms(date_from) if date_from else None
    p1 = _day_ms(date_to, end=True) if date_to else None
    in_period = p0 is not None or p1 is not None
    if in_period:                                            # сделки стратегий — только за период (исходные строки не меняем: исключённые стратегии показывают свои полные цифры)
        rows = [r if r.get("error") else {**r, "trades": period_trades(r["trades"], p0, p1)} for r in rows]
    sizing = {int(k): v for k, v in (sizing or {}).items()}
    active = [r for r in rows if not r.get("error") and not r["paused"] and r["id"] not in exclude]
    for r in rows:                                            # эффективный размер: из конфига, а если задан «что если» — он
        if r.get("error"):
            continue
        r.setdefault("sizing", {"trade_pct": 10.0, "leverage": 1.0, "pyramid_pct": None, "pyramid_leverage": None, "compound": True})
        o = sizing.get(r["id"]) or {}
        r["eff_sizing"] = {**r["sizing"], **{k: o[k] for k in ("trade_pct", "leverage") if o.get(k) is not None}}
        r["overridden"] = r["eff_sizing"]["trade_pct"] != r["sizing"]["trade_pct"] or r["eff_sizing"]["leverage"] != r["sizing"]["leverage"]
        if compound is not None:
            r["eff_sizing"]["compound"] = bool(compound)
        r["resized"] = r["overridden"] or in_period or bool(r["eff_sizing"].get("compound", True)) != bool(r["sizing"].get("compound", True))       # размер сделок отличается от расчёта стратегии (за период — от депозита на его начало)
        r["recompute"] = r["resized"] or guards is not None                                                                           # цифры стратегии пересчитываются по сделкам портфеля
    joint = bool(total_deposit) and bool(active)
    deposit = float(total_deposit) if joint else sum(r["deposit"] for r in active)
    ginfo = {"skipped": [], "pauses": []}
    if joint:
        sized = joint_sizing([{**r, "sizing": r["eff_sizing"]} for r in active], deposit, guards, ginfo)
    else:                                                     # у каждой свой депозит: пересчитываем только стратегии с изменённым размером
        sized = {}
        for r in active:
            sized[r["id"]] = joint_sizing([{**r, "sizing": r["eff_sizing"]}], r["deposit"])[r["id"]] if r["resized"] else r["trades"]
        if guards:                                            # защита портфеля над сделками, уже посчитанными на депозиты стратегий: баланс — сумма депозитов плюс закрытые результаты
            sized = joint_sizing([{"id": r["id"], "trades": sized[r["id"]], "sizing": r["eff_sizing"]} for r in active], deposit, guards, ginfo, resize=False)

    items = []
    for r in rows:
        base = {"id": r["id"], "name": r["name"], "symbol": r.get("symbol"), "paused": r.get("paused", False), "included": r["id"] not in exclude and not r.get("paused", False)}
        if r.get("error"):
            items.append({**base, "included": False, "error": r["error"]})
            continue
        sz_info = {"cfg_trade_pct": r["sizing"]["trade_pct"], "cfg_leverage": r["sizing"]["leverage"], "trade_pct": r["eff_sizing"]["trade_pct"], "leverage": r["eff_sizing"]["leverage"],
                   "overridden": bool(r["overridden"]), "liq_sim": bool(r["sizing"].get("liquidation_sim")),
                   "compound": bool(r["eff_sizing"].get("compound", True)), "cfg_compound": bool(r["sizing"].get("compound", True))}
        if r["id"] in sized:
            tr = sized[r["id"]]
            dep_i = deposit if joint else r["deposit"]
            curve, dd, dd_pct = _curve(tr, dep_i)
            if joint or r["recompute"]:
                st = backtest.stats(tr)
                st = {**st, "sharpe": sharpe_ratio(tr, dep_i) if tr else None, "return_pct": 100 * st["total_usd"] / dep_i}
                if r.get("candles"):
                    st.update(equity_mod.mtm_drawdown_multi([(tr, r["candles"])], dep_i))
            else:
                st = r.get("stats") or {}
            items.append({**base, **sz_info, "error": None, "deposit": r["deposit"], "trades": st.get("trades", 0), "winrate": st.get("winrate"), "profit_factor": st.get("profit_factor"),
                          "total_usd": st.get("total_usd"), "return_pct": st.get("return_pct"), "sharpe": st.get("sharpe"),
                          "max_dd_usd": dd, "max_dd_pct": dd_pct,
                          "max_dd_real_pct": st.get("max_dd_real_pct"), "max_dd_real_usd": st.get("max_dd_real_usd"), "curve": _thin(curve)})
        else:                                           # отключена из портфеля или на паузе: показываем собственные цифры, но в расчёты она не входит
            curve, dd, dd_pct = _curve(r["trades"], r["deposit"])
            st = r.get("stats") or {}
            items.append({**base, **sz_info, "error": None, "deposit": r["deposit"], "trades": st.get("trades", 0), "winrate": st.get("winrate"), "profit_factor": st.get("profit_factor"),
                          "total_usd": st.get("total_usd"), "return_pct": st.get("return_pct"), "sharpe": st.get("sharpe"),
                          "max_dd_usd": dd, "max_dd_pct": dd_pct,
                          "max_dd_real_pct": st.get("max_dd_real_pct"), "max_dd_real_usd": st.get("max_dd_real_usd"), "curve": _thin(curve)})

    # --- общий портфель
    all_tr = [t for tr in sized.values() for t in tr]
    pcurve, pdd, pdd_pct = _curve(all_tr, deposit)
    total = sum(t["pnl_usd"] for t in all_tr if t["reason"] != "OPEN")
    pk, pk_ts = peak_margin(all_tr)
    mprof = margin_profile(all_tr, deposit)
    mtm = equity_mod.mtm_drawdown_multi([(sized[r["id"]], r["candles"]) for r in active if r.get("candles")], deposit) if active and all(r.get("candles") for r in active) else {}
    portfolio = {"strategies": len(active), "deposit": deposit, "joint": joint, "total_usd": total, "return_pct": 100 * total / deposit if deposit else None,
                 "max_dd_usd": pdd, "max_dd_pct": pdd_pct, "curve": _thin(pcurve), "balance": deposit + total,
                 "max_dd_real_usd": mtm.get("max_dd_real_usd"), "max_dd_real_pct": mtm.get("max_dd_real_pct"), "min_equity_usd": mtm.get("min_equity_usd"), "max_dd_real_ts": mtm.get("max_dd_real_ts"),
                 "trades": sum(1 for t in all_tr if t["reason"] != "OPEN"),
                 "peak_margin": pk, "peak_margin_pct": 100 * pk / deposit if deposit else None, "peak_margin_ts": pk_ts,
                 "peak_util_pct": mprof["util_pct"] if mprof["ts"] is not None else 0.0, "peak_util_ts": mprof["ts"], "peak_util_margin": mprof["margin"], "peak_util_balance": mprof["balance"]}
    portfolio["period"] = {"date_from": date_from or None, "date_to": date_to or None, "from_ts": p0, "to_ts": p1,
                           "open_at_end": sum(1 for tr in sized.values() for t in tr if t["reason"] == "OPEN")} if in_period else None
    if guards:                                                # что защита пропустила: сколько входов, по каким причинам, у каких стратегий, когда стороны стояли на паузе
        names_ = {r["id"]: r["name"] for r in active}
        counts, per = {"margin": 0, "margin_add": 0, "side_pause": 0}, {}
        for x in ginfo["skipped"]:
            counts[x["reason"]] += 1
            per.setdefault(x["id"], {"margin": 0, "margin_add": 0, "side_pause": 0})[x["reason"]] += 1
        for it in items:
            c = per.get(it["id"], {})
            it["skipped_margin"], it["skipped_pause"] = c.get("margin", 0) + c.get("margin_add", 0), c.get("side_pause", 0)
        portfolio["guards"] = {"margin_cap_pct": guards["margin_cap_pct"], "side_pause": guards["side_pause"], "skipped": counts, "skipped_total": sum(counts.values()),
                               "pauses": ginfo["pauses"][-30:], "pauses_n": {sd: sum(1 for p_ in ginfo["pauses"] if p_["side"] == sd) for sd in ("long", "short")},
                               "last_skipped": [{**x, "name": names_.get(x["id"])} for x in ginfo["skipped"][-20:]]}
    else:
        portfolio["guards"] = None
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
    balance = deposit + total                                  # баланс портфеля по расчёту: депозит плюс закрытые результаты
    exposure = {"symbols": symbols, "gross": gross, "net": sum(e["net"] for e in symbols), "margin": margin,
                "leverage": gross / balance if balance and balance > 0 else None, "margin_pct": 100 * margin / balance if balance and balance > 0 else None, "balance": balance, "warnings": warnings}
    if exposure["margin_pct"] is not None and exposure["margin_pct"] > 100:
        warnings.append(f"Сейчас открыто маржи ({margin:.0f} $) больше баланса портфеля по расчёту ({balance:.0f} $ = {'общий депозит' if joint else 'сумма депозитов'} плюс закрытые результаты): на одном счёте столько не хватит")
    if portfolio["peak_util_pct"] > 100:
        warnings.append(f"За всю историю доля занятой маржи доходила до {portfolio['peak_util_pct']:.0f}% баланса ({portfolio['peak_util_margin']:.0f} $ маржи при балансе {portfolio['peak_util_balance']:.0f} $): "
                        "на реальном счёте часть входов биржа бы отклонила — уменьшите доли входа или плечи либо включите «лимит занятой маржи» в защите портфеля")
    if real:
        eq = real.get("equity") or 0.0
        real_gross = sum(abs(p.get("notional") or 0.0) for p in real.get("positions", []))
        exposure["real"] = {"equity": eq, "gross": real_gross, "leverage": real_gross / eq if eq else None}
        if eq and gross > eq * 10:
            warnings.append(f"Расчётный объём позиций ({gross:.0f} $) больше капитала на бирже ({eq:.0f} $) в {gross / eq:.1f} раз — проверьте плечи и размеры")
    return _clean({"items": items, "portfolio": portfolio, "correlation": correlation, "exposure": exposure, "settings": {"date_from": date_from or None, "date_to": date_to or None, "margin_cap_pct": (guards or {}).get("margin_cap_pct"), "side_pause": (guards or {}).get("side_pause"), "exclude": sorted(exclude), "total_deposit": total_deposit if joint else None, "compound": compound, "sizing": {str(k): v for k, v in sizing.items() if any(r["id"] == k and r.get("overridden") for r in rows)}}})


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
