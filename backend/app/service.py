"""Запуск бэктеста и подготовка данных для интерфейса (сделки, сигналы, статистика)."""
import heapq
import logging
import math
from bisect import bisect_left, bisect_right
from datetime import datetime, timezone

import time

from .core import backtest, montecarlo, topcoins, trend
from .core import equity as equity_mod
from .core import tradingweek
from .core.entries import entries

from . import runcache
from .data import DataStore
from .schemas import BacktestParams

INF = float("inf")
log = logging.getLogger("service")


class ParamError(ValueError):
    pass


def _clean(o):
    """inf/nan -> None, чтобы ответ был валидным JSON."""
    if isinstance(o, float):
        return None if (math.isinf(o) or math.isnan(o)) else o
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    return o


def _day_ms(s, end=False):
    d = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(d.timestamp() * 1000) + (86_400_000 - 1 if end else 0)


def effective_params(defaults: dict, p: BacktestParams) -> dict:
    eff = dict(defaults)
    eff.update({k: getattr(p, k) for k in p.model_fields_set})
    if "tp_pct" in p.model_fields_set and "tps" not in p.model_fields_set and p.tp_pct is not None:
        eff["tps"] = [{"pct": p.tp_pct, "share": 100.0}]          # совместимость: один тейк на всю позицию
    eff.pop("tp_pct", None)
    eff["tps"] = [l.model_dump() if hasattr(l, "model_dump") else dict(l) for l in eff["tps"]]
    eff["top_tps"] = [l.model_dump() if hasattr(l, "model_dump") else dict(l) for l in eff["top_tps"]]
    eff.setdefault("date_from", None)
    eff.setdefault("date_to", None)
    return eff


def _filters(eff):
    extra = {}
    if eff.get("cooldown_h"):
        extra["cooldown_h"] = eff["cooldown_h"]
    liq = cool = None
    if eff.get("liq_hours"):
        liq = dict(hours=eff["liq_hours"], frac=eff["liq_frac"], pct=eff["liq_pct"])
    if eff.get("cool_drop"):
        cool = dict(metric=eff["cool_metric"], drop=eff["cool_drop"])
    elif eff.get("cool_declines"):
        cool = dict(metric=eff["cool_metric"], declines=eff["cool_declines"])
    return extra, liq, cool


DAY_MS = 86_400_000


def apply_sizing(trades, deposit, trade_pct, leverage, compound=True, pyramid_pct=None, pyramid_leverage=None):
    """Размер каждой сделки = база × доля% × плечо (база: при compound — текущий баланс, иначе начальный депозит; баланс = депозит + УЖЕ закрытые
    результаты, т.е. сложный процент). Первая нога позиции берёт trade_pct, доборы (leg > 1) — pyramid_pct.
    pnl_pct у сделки — доходность на объём, поэтому pnl_usd пересчитывается по порядку открытия; результат ноги попадает в баланс с момента её
    закрытия (ноги одной позиции перекрываются). Баланс ≤ 0 — торговля прекращается (счёт слит)."""
    bal = deposit
    pending = []                                         # (время закрытия, результат $) ещё открытых ног
    for t in sorted(trades, key=lambda x: (x["entry_ts"], x.get("leg", 1))):
        while pending and pending[0][0] <= t["entry_ts"]:
            bal += heapq.heappop(pending)[1]
        base = bal if compound else deposit            # простой процент: каждый раз доля от НАЧАЛЬНОГО депозита
        lev_add = pyramid_leverage or leverage            # плечо доборов; не задано — как у первой ноги
        live = max(base, 0.0) if bal > 0 else 0.0
        if t.get("avg"):                                 # усреднённая позиция: одна сделка на вход + все доборы
            adds_pct = t.get("adds_n", 0) * (pyramid_pct or 0)
            margin = live * (trade_pct + adds_pct) / 100
            notional = live * (trade_pct * leverage + adds_pct * lev_add) / 100
        else:
            add_leg = t.get("leg", 1) > 1 and pyramid_pct
            lev = lev_add if add_leg else leverage
            notional = live * (pyramid_pct if add_leg else trade_pct) / 100 * lev
            margin = notional / lev
        t["margin"] = margin
        t["notional"] = notional
        t["pnl_usd"] = notional * t["pnl_pct"] / 100
        heapq.heappush(pending, (t["exit_ts"], t["pnl_usd"]))
        t["balance_after"] = bal + t["pnl_usd"]


def sharpe_ratio(trades, deposit):
    """Шарп по дневным доходностям баланса (результат за день / баланс на начало дня), годовой: × √365, безрисковая ставка 0.
    Дни без сделок считаются нулевой доходностью. Меньше 2 дней или нулевой разброс — None."""
    if not trades:
        return None
    d0 = trades[0]["entry_ts"] // DAY_MS
    d1 = max(t["exit_ts"] for t in trades) // DAY_MS
    if d1 - d0 < 1:
        return None
    by_day = {}
    for t in trades:
        d = t["exit_ts"] // DAY_MS
        by_day[d] = by_day.get(d, 0.0) + t["pnl_usd"]
    bal, rets = deposit, []
    for d in range(d0, d1 + 1):
        p = by_day.get(d, 0.0)
        rets.append(p / bal if bal > 0 else 0.0)
        bal += p
    n = len(rets)
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / (n - 1)
    if var <= 0:
        return None
    return mean / math.sqrt(var) * math.sqrt(365)


def _extra_stats(trades, span_ms, deposit=None):
    pn = [t["pnl_usd"] for t in trades]
    wins = [x for x in pn if x > 0]
    losses = [x for x in pn if x <= 0]
    streak = best = 0
    for x in (t["pnl_usd"] for t in sorted(trades, key=lambda x: (x["exit_ts"], x["entry_ts"]))):
        streak = streak + 1 if x <= 0 else 0
        best = max(best, streak)
    held, cur_end = 0, None                            # время в позиции: объединение интервалов (ноги одной позиции перекрываются)
    for t in sorted(trades, key=lambda x: x["entry_ts"]):
        a, b = t["entry_ts"], t["exit_ts"]
        if cur_end is None or a > cur_end:
            held += b - a
            cur_end = b
        elif b > cur_end:
            held += b - cur_end
            cur_end = b
    return {
        "avg_win_usd": sum(wins) / len(wins) if wins else 0.0,
        "avg_loss_usd": sum(losses) / len(losses) if losses else 0.0,
        "best_usd": max(pn) if pn else 0.0,
        "worst_usd": min(pn) if pn else 0.0,
        "max_loss_streak": best,
        "exposure_pct": 100 * held / span_ms if span_ms > 0 else 0.0,
        "expectancy_usd": sum(pn) / len(pn) if pn else 0.0,
        "sharpe": sharpe_ratio(trades, deposit) if deposit else None,
        "return_pct": 100 * sum(pn) / deposit if deposit else None,
        "max_dd_pct": _max_dd_pct(pn, deposit) if deposit else None,
        # сколько сделок дошли до первого / второго / третьего тейка
        "tp_reach": [sum(t["tp_hits"] >= k for t in trades) for k in (1, 2, 3)],
        "positions": len({t.get("group", i) for i, t in enumerate(trades)}),
        "adds": sum(1 for t in trades if t.get("leg", 1) > 1) + sum(t.get("adds_n", 0) for t in trades),
    }


def _metrics_at(report_ts, reports, hold=None):
    """Метрики последнего отчёта не позже ts (то, что показывал бот в момент события): lookup(ts, side) -> {dg, dl, ds[, strong]} или None.
    strong (только если включено «не ставить SL, пока метрики сильны») — выполнялось ли в этот момент условие входа, т.е. был ли снят стоп."""
    def at(ts, d=None):
        k = bisect_right(report_ts, ts) - 1
        if k < 0:
            return None
        r = reports[k]
        m = {"dg": r[7] if len(r) > 7 else None, "dl": r[1], "ds": r[2]}
        if hold is not None and d is not None:
            m["strong"] = bool(hold(d, ts))
        return m
    return at


METRIC_NAMES = {"scalp": "DS", "local": "DL", "global": "DG"}
WAIT_ORDER = ("cool", "liq", "trend")


def waiting_lines(d, w):
    """Что именно ждёт вход, человеческим языком (одни и те же строки идут в интерфейс и в бота): w = {"cool": …, "liq": …, "trend": …} с цифрами."""
    side = "лонга" if d == 1 else "шорта"
    out = []
    c = w.get("cool")
    if c:
        name = METRIC_NAMES.get(c["metric"], c["metric"])
        if c["kind"] == "drop":
            ref = "максимума" if d == 1 else "минимума"
            out.append(f"откат {name}: нужен ≥ {c['need']:g} от {ref} серии ({c['ext']:.2f}), сейчас {c['have']:.3f} (значение {c['now']:.2f})")
        else:
            out.append(f"остывание {name}: {c['have']} из {c['need']} отчётов подряд")
    q = w.get("liq")
    if q:
        who = "лонгов" if d == 1 else "шортов"
        if q["reason"] == "no_data":
            out.append(f"ликвидации {who}: в отчёте нет данных")
        elif q["reason"] == "no_spike":
            thr = f", нужен всплеск не ниже {q['thr']:.1f}k" if q.get("thr") is not None else ", мало истории для перцентиля"
            out.append(f"ликвидации {who}: за {q['hours']:g} ч не было достаточного всплеска (максимум {q['max']:.1f}k{thr})")
        else:
            out.append(f"ликвидации {who}: сейчас {q['now']:.1f}k, нужно ≤ {q['limit']:.1f}k ({q['frac']:g} от пика {q['max']:.1f}k)")
    t = w.get("trend")
    if t:
        out.append(f"тренд: для {side} цена должна быть {'выше' if d == 1 else 'ниже'} EMA {'/'.join(str(x) for x in t['emas'])} на {t['tf']}")
    return out


SPLIT_REASONS = ("TP", "BE", "SL", "REV", "LIQ", "WKND", "OPEN")


def weekday_split(trades, before_min=0.0):
    """Сделки по причине выхода отдельно для будней и выходных. Будни — внутри торговой недели (с открытия Токио в понедельник до закрытия Нью-Йорка в пятницу,
    см. core/tradingweek.py), выходные — пятница после закрытия, суббота, воскресенье. Причина относится к моменту ВЫХОДА (закрытие в пятницу считается будним),
    «открыто» — отдельно по моменту входа."""
    out = {k: {"trades": 0, "reasons": {r: 0 for r in SPLIT_REASONS}, "pnl_usd": 0.0, "wins": 0} for k in ("weekday", "weekend")}
    entered = {"weekday": 0, "weekend": 0}
    for t in trades:
        k = "weekday" if tradingweek.in_window(t["exit_ts"] - 60_000, before_min) else "weekend"
        b = out[k]
        b["trades"] += 1
        b["reasons"][t["reason"] if t["reason"] in b["reasons"] else "TP"] += 1
        b["pnl_usd"] += t["pnl_usd"]
        b["wins"] += t["pnl_usd"] > 0
        entered["weekday" if tradingweek.in_window(t["entry_ts"], before_min) else "weekend"] += 1
    return {**out, "entered": entered}


SESSIONS = (("asia", "Азия (Токио)", 0, 8), ("europe", "Европа (Лондон)", 8, 13), ("us", "США (Нью-Йорк)", 13, 21), ("night", "Вечер / ночь", 21, 24))     # часы UTC, без пересечений


def session_of(ts_ms):
    h = (ts_ms // 3_600_000) % 24
    return next(k for k, _n, a, b in SESSIONS if a <= h < b)


def _bucket():
    return {"trades": 0, "reasons": {r: 0 for r in SPLIT_REASONS}, "pnl_usd": 0.0, "wins": 0, "gross_win": 0.0, "gross_loss": 0.0}


def _add(b, t):
    b["trades"] += 1
    b["reasons"][t["reason"] if t["reason"] in b["reasons"] else "TP"] += 1
    b["pnl_usd"] += t["pnl_usd"]
    if t["pnl_usd"] > 0:
        b["wins"] += 1
        b["gross_win"] += t["pnl_usd"]
    else:
        b["gross_loss"] += -t["pnl_usd"]


def time_stats(trades):
    """Статистика по дням недели (пн…вс, UTC) и по сессиям (Азия 00–08, Европа 08–13, США 13–21, вечер/ночь 21–24 UTC; границы фиксированные, без перехода на летнее время)
    отдельно по моменту ВХОДА и по моменту ВЫХОДА: число сделок, причины выхода, winrate, итог. Ноги пирамидинга считаются сделками."""
    out = {}
    for which, key in (("entry", "entry_ts"), ("exit", "exit_ts")):
        days = [_bucket() for _ in range(7)]
        sess = {k: _bucket() for k, *_ in SESSIONS}
        for t in trades:
            ts = t[key]
            _add(days[(ts // 86_400_000 + 3) % 7], t)           # 1970-01-01 — четверг: (дни от эпохи + 3) % 7 → 0 = понедельник
            _add(sess[session_of(ts)], t)
        out[which] = {"day": days, "session": [{"key": k, "name": n, "hours": [a, b], **sess[k]} for k, n, a, b in SESSIONS]}
    return out


HOLD_EDGES = (0, 1, 4, 12, 24, 48, 96)            # часы; последняя группа — дольше 96 ч
HOLD_LABELS = ("< 1 ч", "1–4 ч", "4–12 ч", "12–24 ч", "1–2 сут", "2–4 сут", "> 4 сут")


def _quantile(sorted_vals, q):
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def hold_stats(trades):
    """Сколько держатся позиции и как это связано с результатом. Берутся закрытые сделки (открытые на конец данных не считаются — их срок ещё не известен).
    buckets — группы по сроку удержания (те же поля, что у дней/сессий), summary — медиана/среднее/p90 в часах (всего, у плюсовых, у минусовых, по причинам выхода)
    и корреляция Пирсона между log(срок) и результатом сделки в % (None, если мало данных или нет разброса)."""
    closed = [t for t in trades if t["reason"] != "OPEN"]
    buckets = [dict(_bucket(), label=lab, from_h=lo, to_h=(HOLD_EDGES[i + 1] if i + 1 < len(HOLD_EDGES) else None)) for i, (lab, lo) in enumerate(zip(HOLD_LABELS, HOLD_EDGES))]
    for t in closed:
        h = t["hold_h"]
        k = max(i for i, lo in enumerate(HOLD_EDGES) if h >= lo)
        _add(buckets[k], t)
        buckets[k]["hold_sum"] = buckets[k].get("hold_sum", 0.0) + h

    def stat(sel):
        v = sorted(t["hold_h"] for t in sel)
        return {"n": len(v), "median": _quantile(v, 0.5), "mean": (sum(v) / len(v)) if v else None, "p90": _quantile(v, 0.9)}

    corr = None
    if len(closed) >= 10:
        xs = [math.log(max(t["hold_h"], 1 / 60)) for t in closed]
        ys = [t["pnl_pct"] for t in closed]
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
        sy = math.sqrt(sum((y - my) ** 2 for y in ys))
        if sx > 0 and sy > 0:
            corr = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)
    reasons = {r: stat([t for t in closed if t["reason"] == r]) for r in SPLIT_REASONS if r != "OPEN" and any(t["reason"] == r for t in closed)}
    return {"buckets": buckets, "open": len(trades) - len(closed),
            "summary": {"all": stat(closed), "wins": stat([t for t in closed if t["pnl_usd"] > 0]), "losses": stat([t for t in closed if t["pnl_usd"] <= 0]), "by_reason": reasons, "corr": corr}}


def _trade_out(n, t, rep, at=None):
    r = rep.get(t["signal_ts"])
    out = {
        "n": n, "side": t["side"], "signal_ts": t["signal_ts"], "entry_ts": t["entry_ts"], "exit_ts": t["exit_ts"],
        "entry": t["entry"], "exit": t["exit"], "exit_avg": t["exit_avg"], "tp": t["tp"], "tps": t["tps"],
        "tp_shares": t["tp_shares"], "sl": t["sl"], "be": t["be"], "be_ts": t["be_ts"], "tp_hits": t["tp_hits"],
        "fills": [{**f, "metrics": at(f["ts"], 1 if t["side"] == "long" else -1)} for f in t["fills"]] if at else t["fills"], "reason": t["reason"],
        "pnl_pct": t["pnl_pct"], "pnl_usd": t["pnl_usd"], "notional": t.get("notional"), "margin": t.get("margin"), "hold_h": (t["exit_ts"] - t["entry_ts"]) / 3.6e6,
        "bal_entry": (t["balance_after"] - t["pnl_usd"]) if "balance_after" in t else None,
        "local": r[1] if r else None, "scalp": r[2] if r else None, "global": r[7] if r and len(r) > 7 else None,
        "liq_long_k": r[3] if r and len(r) > 3 else None, "liq_short_k": r[4] if r and len(r) > 4 else None,
    }
    for k in ("symbol", "coin", "top_value", "partial", "window_end", "leg", "group", "pyramid", "avg", "adds_n", "adds", "entry_first", "w_total"):
        if k in t:
            out[k] = t[k]
    return out


def _top_section(store, eff, confirmed, rep, available_fn, now_ms, warnings, at=None):
    """Сделки по топ-коинам для подтверждённых сигналов основной стратегии (см. core/topcoins.py)."""
    if not eff.get("top_enabled"):
        return {"enabled": False}
    try:
        backtest.normalize_tps([(l["pct"], l["share"]) for l in eff["top_tps"]])
    except ValueError as e:
        raise ParamError(f"Топ-коины: {e}")
    triggers = []
    for ts, d in confirmed:
        r = rep.get(ts)
        triggers.append({"ts": ts, "d": d, "coin": r[5] if r and len(r) > 5 else None, "value": r[6] if r and len(r) > 6 else None})
    available = None
    if any(t["coin"] for t in triggers):            # список рынков нужен только если в отчётах есть топ-коины
        if available_fn is not None:
            try:
                available = set(available_fn())
            except Exception as e:                  # биржа недоступна: работаем только с уже скачанными монетами
                warnings.append(f"Топ-коины: список рынков биржи недоступен ({e}); использованы только уже скачанные монеты")
        if available is None:
            available = store.cached_symbols()
    tf_ms = backtest.tf_to_ms(store.s.candle_tf)
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    res = topcoins.build_top(triggers, eff, available, store.coin_covered, store.coin_candles, tf_ms, now_ms)
    trades = res["trades"]
    out_trades = [_trade_out(i, t, rep, at) for i, t in enumerate(trades, 1)]
    span_ms = (max(t["exit_ts"] for t in trades) - trades[0]["entry_ts"]) if trades else 0
    by = {}
    for t in trades:
        d = by.setdefault(t["symbol"], {"symbol": t["symbol"], "coin": t["coin"], "trades": 0, "wins": 0, "pnl_usd": 0.0})
        d["trades"] += 1
        d["wins"] += t["pnl_usd"] > 0
        d["pnl_usd"] += t["pnl_usd"]
    equity, cum = [], 0.0
    for t in sorted(trades, key=lambda x: x["exit_ts"]):
        cum += t["pnl_usd"]
        equity.append([t["exit_ts"], cum])
    pending = res["pending"]
    items = [{"symbol": sym, "from": a, "to": b} for sym, ws in pending.items() for a, b in ws]
    return {
        "enabled": True,
        "stats": {**backtest.stats(trades), **_extra_stats(trades, span_ms)},
        "trades": out_trades,
        "by_coin": sorted(by.values(), key=lambda x: -x["pnl_usd"]),
        "skipped": res["skipped"][:2000],
        "skipped_total": len(res["skipped"]),
        "equity": equity,
        "pending": {"symbols": len(pending), "windows": len(items), "requests": topcoins.estimate_requests(pending, tf_ms), "items": items},
    }


def _max_dd_pct(pn, deposit):
    eq = peak = deposit
    dd = 0.0
    for x in pn:
        eq += x
        peak = max(peak, eq)
        dd = max(dd, (peak - eq) / peak if peak > 0 else 0.0)
    return 100 * dd


def _closed_prefix(sim):
    """Из снимка прошлого расчёта берёт позиции, результат которых уже не изменится от новых данных: закрыты (не OPEN) строго раньше последних данных, по которым считали
    (safe_ts = меньшее из времени последней свечи и последнего отчёта: отчёт мог бы прийти «задним числом» внутри уже посчитанных свечей).
    -> (сделки, пропущенные сигналы, resume для run_events, с какого момента подавать события); пусто — считать с начала."""
    safe_ts, trades = sim["safe_ts"], sim["trades"]
    bad = next((t for t in trades if t["reason"] == "OPEN" or t["exit_ts"] >= safe_ts), None)
    if bad is not None:
        first = min(t["signal_ts"] for t in trades if t["group"] == bad["group"])
        kept = [t for t in trades if t["group"] < bad["group"]]
        cut = min(safe_ts, first)
    else:
        kept, cut = list(trades), safe_ts
    if not kept:
        return [], [], None, None
    last = [t for t in kept if t["group"] == kept[-1]["group"]]
    resume = {"group": kept[-1]["group"], "last_group": last, "last_open_ts": min(t["entry_ts"] for t in last)}
    return kept, [x for x in sim["skipped"] if x[0] < cut], resume, cut


def run_backtest(store: DataStore, defaults: dict, params: BacktestParams, available_fn=None, now_ms=None, use_cache=True) -> dict:
    eff = effective_params(defaults, params)
    source_id = eff.get("source") or store.s.default_source
    liq_id = eff.get("liq_source")
    try:
        store.source(source_id)
        if liq_id:
            store.source(liq_id)
        sigs_all, _hit = store.signals(source_id, liq_id)
    except KeyError as e:
        raise ParamError(str(e.args[0]))
    symbol = eff.get("symbol") or store.s.symbol
    try:
        candles_all = store.candles(symbol)
        ts_all = store.candle_ts(symbol)
    except KeyError as e:
        raise ParamError(str(e.args[0]))
    warnings = []
    # отчёты раньше первой свечи в кеше симулировать нельзя (вход «по open первой свечи» был бы ложным)
    early = sum(1 for s in sigs_all if s[0] < ts_all[0])
    if early:
        sigs_all = [s for s in sigs_all if s[0] >= ts_all[0]]
        warnings.append(f"{early} отчётов раньше начала свечей отброшены — нажмите «Обновить свечи», чтобы докачать историю")
    if not sigs_all:
        raise ParamError("Нет отчётов внутри диапазона свечей")

    t0 = _day_ms(eff["date_from"]) if eff.get("date_from") else None
    t1 = _day_ms(eff["date_to"], end=True) if eff.get("date_to") else None
    sigs = [s for s in sigs_all if (t0 is None or s[0] >= t0) and (t1 is None or s[0] <= t1)]
    if not sigs:
        raise ParamError("В выбранном диапазоне дат нет отчётов")
    lo = bisect_left(ts_all, t0) if t0 is not None else 0
    hi = bisect_right(ts_all, t1) if t1 is not None else len(ts_all)
    candles = candles_all if (lo == 0 and hi == len(ts_all)) else candles_all[lo:hi]
    if not candles:
        raise ParamError("В выбранном диапазоне дат нет свечей")

    extra, liq, cool = _filters(eff)
    if liq and not any(s[3] is not None for s in sigs):
        raise ParamError("Для фильтра ликвидаций нет данных: выберите «Ликвидации из» источник, где они есть")

    side = eff["side"]
    long_min, scalp_min = eff["local_min"], eff["scalp_min"]
    glob_min, short_glob = eff.get("global_min"), eff.get("short_global_max")
    short = None if side == "long" else (eff["short_local_max"], eff["short_scalp_max"])
    if side == "short":
        long_min = scalp_min = INF          # лонг никогда не срабатывает
        glob_min = INF
    if eff["only"] == "global":
        need_long = side != "short" and glob_min is None
        need_short = side != "long" and short_glob is None
        if need_long or need_short:
            raise ParamError("Режим «только Deep Global»: задайте порог Global для " + ("лонга" if need_long else "шорта"))
    if (glob_min is not None and glob_min != INF) or short_glob is not None or eff["only"] == "global" or eff.get("cool_metric") == "global":
        if not any(len(s) > 7 and s[7] is not None for s in sigs):
            raise ParamError("В отчётах выбранного источника нет Deep Global — уберите условия по Global")
    try:
        tps = [(l["pct"], l["share"]) for l in eff["tps"]]
        backtest.normalize_tps(tps)
    except ValueError as e:
        raise ParamError(str(e))

    # снимок прошлого расчёта этой стратегии: если данные только дописались, считаем только новые отчёты и свечи (см. runcache.py)
    rc = runcache.for_dir(store.s.cache_dir) if (use_cache and runcache.enabled()) else None
    ckey = runcache.make_key(eff, source_id, liq_id, symbol) if rc else None
    snap = rc.get(ckey) if rc else None
    if snap and not (snap["n_sigs"] <= len(sigs) and runcache.matches(snap["sig_probe"], sigs, snap["n_sigs"])
                     and snap["n_candles"] <= len(candles_all) and runcache.matches(snap["c_probe"], candles_all, snap["n_candles"])):
        snap = None
    est = dict(snap["ent"]) if snap else {}
    tst = dict(snap["trend"]) if snap and snap.get("trend") else {}
    allow = None
    if eff.get("trend_enabled"):
        emas = sorted(set(eff.get("trend_emas") or []))
        if not emas:
            raise ParamError("Фильтр тренда: выберите хотя бы одну EMA")
        allow = trend.build_trend(candles_all, eff["trend_tf"], emas, state=tst)       # EMA считается по всей истории свечей (прогрев), а не только по периоду; докладываются только новые свечи
    lev = eff["leverage"]
    lev_add = eff.get("pyramid_leverage") or lev                  # плечо доборов; не задано — как у первой ноги
    mmr = eff["maint_margin_pct"] / 100 if eff.get("liquidation_sim") else None
    pyramid, pyr_pct = None, None
    if eff.get("pyramid_enabled"):
        room = int((100 - eff["trade_pct"]) // eff["pyramid_pct"])          # сколько доборов помещается в 100% депозита вместе с первой ногой
        if room < 1:
            raise ParamError(f"Пирамидинг: первая нога {eff['trade_pct']:g}% депозита, на добор ({eff['pyramid_pct']:g}%) места не остаётся")
        if room < eff["pyramid_max"]:
            warnings.append(f"Пирамидинг: {eff['trade_pct']:g}% + {eff['pyramid_max']} × {eff['pyramid_pct']:g}% больше депозита — доборов не больше {room}")
        pyramid, pyr_pct = {"max": min(eff["pyramid_max"], room), "gap_h": eff.get("pyramid_gap_h") or 0.0, "move_pct": eff.get("pyramid_move_pct") or 0.0, "move_dir": eff.get("pyramid_move_dir") or "any", "avg": bool(eff.get("pyramid_avg")),
                            "w0": eff["trade_pct"] * lev, "w": eff["pyramid_pct"] * lev_add}, eff["pyramid_pct"]      # веса усреднения — объём (доля × плечо)
    week = {"before_min": eff.get("friday_close_before_min") or 0.0} if eff.get("weekdays_only") else None
    lm, sm, gm, sh, sgm = backtest.resolve_entry(long_min, scalp_min, glob_min, short, short_glob, eff["only"])
    pend = []
    confirmed = entries(sigs, lm, sm, eff["confirm"], sh, liq, cool, gm, sgm, pending=pend, state=est)     # все подтверждённые сигналы (с пропущенными)
    events = [e for e in confirmed if allow(*e)] if allow else confirmed
    levels = backtest.normalize_tps(tps)
    hold = backtest.strong_checker(sigs, lm, sm, sh, gm, sgm) if eff.get("sl_hold_strong") else None
    run_kw = dict(sl_pct=eff["sl_pct"], fee=eff["fee"], size=eff["size"], slip=eff["slip"], cooldown_h=extra.get("cooldown_h", 0.0), be_after_tp1=bool(eff.get("be_after_tp1")), pyramid=pyramid,
                  signal_gap_h=eff.get("signal_gap_h") or 0.0, hold=hold, leverage=lev, add_leverage=lev_add, mmr=mmr, week=week)
    kept, kept_skipped, resume, from_ts = [], [], None, None
    if snap and snap.get("sim"):
        kept, kept_skipped, resume, from_ts = _closed_prefix(snap["sim"])
    skipped = list(kept_skipped)
    new_trades = backtest.run_events([e for e in events if from_ts is None or e[0] >= from_ts] if kept else events, candles, levels, skipped=skipped, resume=resume, **run_kw)
    raw_trades = kept + new_trades
    trades = [dict(t) for t in raw_trades]           # размер позиции дописывается в копии: снимок хранит сделки без него
    if rc:
        try:
            changed = not snap or snap["n_sigs"] != len(sigs) or snap["n_candles"] != len(candles_all)
            if changed:
                rc.put(ckey, {"n_sigs": len(sigs), "sig_probe": runcache.probe(sigs), "n_candles": len(candles_all), "c_probe": runcache.probe(candles_all),
                              "ent": est, "trend": tst or None,
                              "sim": {"trades": raw_trades, "skipped": skipped, "safe_ts": min(candles[-1][0], sigs[-1][0])}})
        except Exception as e:
            log.warning("runcache: снимок не сохранён: %s", e)

    apply_sizing(trades, eff["deposit"], eff["trade_pct"], eff["leverage"], eff.get("compound", True), pyr_pct, eff.get("pyramid_leverage"))
    if not mmr:
        for who, lv in (("", eff["leverage"]), (" доборов", lev_add if pyramid else None)):
            if lv and eff["sl_pct"] * lv >= 100:
                warnings.append(f"Плечо{who} {lv:g}× при стопе {eff['sl_pct']:g}%: стоп находится за ценой ликвидации — на бирже позицию ликвидируют раньше стопа; включите «моделировать ликвидацию», чтобы учесть это в расчёте")

    # все подтверждённые сигналы (в том числе пропущенные: позиция открыта, пауза после стопа)
    trend_blocked = 0
    if allow:
        passed = [e for e in confirmed if allow(*e)]
        trend_blocked = len(confirmed) - len(passed)
        blocked_ts = {e[0] for e in confirmed} - {e[0] for e in passed}
    else:
        blocked_ts = set()
    # «ждёт подтверждения»: серия уже подтверждена, но вход держат фильтры (откат метрики, ликвидации, тренд)
    trend_info = {"emas": sorted(set(eff.get("trend_emas") or [])), "tf": eff.get("trend_tf")} if allow else None
    pend_items = {}
    for ts_, d_, w_ in pend:
        item = dict(w_)
        if allow and not allow(ts_, d_):
            item["trend"] = trend_info
        pend_items[(ts_, d_)] = item
    for ts_, d_ in confirmed:
        if ts_ in blocked_ts:
            pend_items.setdefault((ts_, d_), {})["trend"] = trend_info
    prev_ts = {sigs[i][0]: sigs[i - 1][0] for i in range(1, len(sigs))}
    pending_out = [{"ts": ts_, "side": "long" if d_ == 1 else "short", "waiting": [k for k in WAIT_ORDER if k in w_], "lines": waiting_lines(d_, w_),
                    "run_start": (prev_ts.get(ts_), d_) not in pend_items}                       # первый отчёт непрерывного ожидания: на графике отмечается он, а не каждый
                   for (ts_, d_), w_ in sorted(pend_items.items())]
    last_report_ts = sigs[-1][0]
    ready_now = []
    for p_ in pending_out:
        if p_["ts"] != last_report_ts:
            continue
        open_same = any(t["reason"] == "OPEN" and t["side"] == p_["side"] for t in trades)
        if open_same and not eff.get("pyramid_enabled"):
            continue                                   # позиция уже открыта, сигнал всё равно был бы пропущен — ждать нечего
        ready_now.append({**p_, "add": open_same})

    taken = {t["signal_ts"] for t in trades} | {s_ for t in trades for s_ in t.get("add_signals", [])}
    limit = 5000
    why = {ts: reason for ts, _side, reason in skipped}
    for ts in blocked_ts:
        why[ts] = "против тренда"

    def sig_out(ts, d):
        return {"ts": ts, "side": "long" if d == 1 else "short", "taken": ts in taken, "trend_blocked": ts in blocked_ts,
                "reason": None if ts in taken else why.get(ts)}

    signals_out = [sig_out(ts, d) for ts, d in confirmed[:limit]]
    last_signals = [sig_out(ts, d) for ts, d in confirmed[-30:]]
    skipped_reasons = {}
    for ts, d in confirmed:
        if ts not in taken:
            r = why.get(ts, "другое")
            skipped_reasons[r] = skipped_reasons.get(r, 0) + 1

    rep = {s[0]: s for s in sigs}
    hold_chk = backtest.strong_checker(sigs_all, lm, sm, sh, gm, sgm) if eff.get("sl_hold_strong") else None
    at = _metrics_at([s[0] for s in sigs_all], sigs_all, hold_chk)
    out_trades = [_trade_out(i, t, rep, at) for i, t in enumerate(trades, 1)]

    span_ms = (candles[-1][0] - trades[0]["entry_ts"]) if trades else 0
    split_ts = sigs[len(sigs) // 2][0]
    first_half = [t for t in trades if t["entry_ts"] < split_ts]
    second_half = [t for t in trades if t["entry_ts"] >= split_ts]

    def block(ts_):
        return {**backtest.stats(ts_), **_extra_stats(ts_, span_ms, eff["deposit"])}

    equity, cum = [], 0.0
    if trades:
        equity.append([trades[0]["entry_ts"], 0.0])
    for t in sorted(trades, key=lambda x: (x["exit_ts"], x["entry_ts"])):
        cum += t["pnl_usd"]
        equity.append([t["exit_ts"], cum])
    monthly = {}
    for t in trades:
        m = datetime.fromtimestamp(t["exit_ts"] / 1000, timezone.utc).strftime("%Y-%m")
        d = monthly.setdefault(m, {"month": m, "trades": 0, "pnl_usd": 0.0, "wins": 0})
        d["trades"] += 1
        d["pnl_usd"] += t["pnl_usd"]
        d["wins"] += t["pnl_usd"] > 0

    top = _top_section(store, eff, confirmed, rep, available_fn, now_ms, warnings, at)
    src = store.source(source_id)
    return _clean({
        "params": eff,
        "symbol": symbol,
        "source": {"id": src.id, "label": src.label, "liq_source": liq_id if liq_id and liq_id != source_id else None},
        "warnings": warnings,
        "range": {"first_ts": sigs[0][0], "last_ts": sigs[-1][0], "split_ts": split_ts, "candles_last_ts": candles[-1][0]},
        "stats": {
            "total": {**block(trades), **equity_mod.mtm_drawdown(trades, candles, eff["deposit"])},
            "long": block([t for t in trades if t["side"] == "long"]),
            "short": block([t for t in trades if t["side"] == "short"]),
            "first_half": block(first_half),
            "second_half": block(second_half),
        },
        "trades": out_trades,
        "signals": signals_out,
        "signals_truncated": len(confirmed) > limit,
        "skipped_reasons": skipped_reasons,
        "time_stats": time_stats(trades),
        "hold_stats": hold_stats(out_trades),
        "weekday_split": weekday_split(trades, eff.get("friday_close_before_min") or 0.0),
        "pending": pending_out[-3000:],
        "ready_now": ready_now,
        "last_signals": last_signals,                  # самые свежие подтверждённые сигналы (для бота: список signals обрезается по первым 5000)
        "equity": equity,
        "monthly": list(monthly.values()),
        "top": top,
        "trend": {"enabled": bool(allow), "blocked": trend_blocked, "confirmed": len(confirmed)},
    })


def run_montecarlo(store: DataStore, defaults: dict, req, available_fn=None) -> dict:
    """Монте-Карло по сделкам основной стратегии (топ-коины не входят). Сам бэктест считается как обычно, потом сделки превращаются в доходности
    (доля баланса на входе при сложном проценте, доля начального депозита — при простом) и прогоняются в разном порядке."""
    params = BacktestParams(**req.model_dump(exclude={"runs", "mode", "seed"}, exclude_unset=True))
    res = run_backtest(store, defaults, params, available_fn=available_fn)
    eff = res["params"]
    compound = bool(eff.get("compound", True))
    returns = []
    for t in res["trades"]:
        base = t.get("bal_entry") if compound else eff["deposit"]
        if base and base > 0:
            returns.append(t["pnl_usd"] / base)
    out = montecarlo.run(returns, req.runs, req.mode, compound, req.seed)
    if out is None:
        raise ParamError("Для Монте-Карло нужно минимум 2 сделки")
    return _clean({**out, "symbol": res["symbol"], "source": res["source"]})
