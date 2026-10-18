"""Запуск бэктеста и подготовка данных для интерфейса (сделки, сигналы, статистика)."""
import heapq
import math
from bisect import bisect_left, bisect_right
from datetime import datetime, timezone

import time

from .core import backtest, topcoins, trend
from .core.entries import entries

from .data import DataStore
from .schemas import BacktestParams

INF = float("inf")


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


def apply_sizing(trades, deposit, trade_pct, leverage, compound=True, pyramid_pct=None):
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
        if t.get("avg"):                                 # усреднённая позиция: одна сделка на вход + все доборы
            pct = trade_pct + t.get("adds_n", 0) * (pyramid_pct or 0)
        else:
            pct = pyramid_pct if (t.get("leg", 1) > 1 and pyramid_pct) else trade_pct
        notional = (max(base, 0.0) if bal > 0 else 0.0) * pct / 100 * leverage
        t["margin"] = notional / leverage
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


def _trade_out(n, t, rep, at=None):
    r = rep.get(t["signal_ts"])
    out = {
        "n": n, "side": t["side"], "signal_ts": t["signal_ts"], "entry_ts": t["entry_ts"], "exit_ts": t["exit_ts"],
        "entry": t["entry"], "exit": t["exit"], "exit_avg": t["exit_avg"], "tp": t["tp"], "tps": t["tps"],
        "tp_shares": t["tp_shares"], "sl": t["sl"], "be": t["be"], "be_ts": t["be_ts"], "tp_hits": t["tp_hits"],
        "fills": [{**f, "metrics": at(f["ts"], 1 if t["side"] == "long" else -1)} for f in t["fills"]] if at else t["fills"], "reason": t["reason"],
        "pnl_pct": t["pnl_pct"], "pnl_usd": t["pnl_usd"], "notional": t.get("notional"), "margin": t.get("margin"), "hold_h": (t["exit_ts"] - t["entry_ts"]) / 3.6e6,
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


def run_backtest(store: DataStore, defaults: dict, params: BacktestParams, available_fn=None, now_ms=None) -> dict:
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
    common = dict(tp_pct=tps[0][0], tps=tps, be_after_tp1=bool(eff.get("be_after_tp1")), sl_pct=eff["sl_pct"], fee=eff["fee"], size=eff["size"], slip=eff["slip"],
                  confirm=eff["confirm"], only=eff["only"], global_min=glob_min, short_global_max=short_glob)

    allow = None
    if eff.get("trend_enabled"):
        emas = sorted(set(eff.get("trend_emas") or []))
        if not emas:
            raise ParamError("Фильтр тренда: выберите хотя бы одну EMA")
        allow = trend.build_trend(candles_all, eff["trend_tf"], emas)       # EMA считается по всей истории свечей (прогрев), а не только по периоду
    pyramid, pyr_pct = None, None
    if eff.get("pyramid_enabled"):
        room = int((100 - eff["trade_pct"]) // eff["pyramid_pct"])          # сколько доборов помещается в 100% депозита вместе с первой ногой
        if room < 1:
            raise ParamError(f"Пирамидинг: первая нога {eff['trade_pct']:g}% депозита, на добор ({eff['pyramid_pct']:g}%) места не остаётся")
        if room < eff["pyramid_max"]:
            warnings.append(f"Пирамидинг: {eff['trade_pct']:g}% + {eff['pyramid_max']} × {eff['pyramid_pct']:g}% больше депозита — доборов не больше {room}")
        pyramid, pyr_pct = {"max": min(eff["pyramid_max"], room), "gap_h": eff.get("pyramid_gap_h") or 0.0, "avg": bool(eff.get("pyramid_avg")),
                            "w0": eff["trade_pct"], "w": eff["pyramid_pct"]}, eff["pyramid_pct"]
    skipped = []
    trades = backtest.simulate(sigs, candles, long_min, scalp_min, short=short, liq=liq, cool=cool, event_filter=allow, pyramid=pyramid,
                               signal_gap_h=eff.get("signal_gap_h") or 0.0, sl_hold_strong=bool(eff.get("sl_hold_strong")), skipped=skipped,
                               **common, **extra)

    apply_sizing(trades, eff["deposit"], eff["trade_pct"], eff["leverage"], eff.get("compound", True), pyr_pct)
    if eff["sl_pct"] * eff["leverage"] >= 100:
        warnings.append(f"Плечо {eff['leverage']:g}× при стопе {eff['sl_pct']:g}%: стоп находится за ценой ликвидации — на бирже позицию ликвидируют раньше стопа, бэктест этого не моделирует")

    # все подтверждённые сигналы (в том числе пропущенные: позиция открыта, пауза после стопа)
    lm, sm, gm, sh, sgm = backtest.resolve_entry(long_min, scalp_min, glob_min, short, short_glob, eff["only"])
    confirmed = entries(sigs, lm, sm, eff["confirm"], sh, liq, cool, gm, sgm)
    trend_blocked = 0
    if allow:
        passed = [e for e in confirmed if allow(*e)]
        trend_blocked = len(confirmed) - len(passed)
        blocked_ts = {e[0] for e in confirmed} - {e[0] for e in passed}
    else:
        blocked_ts = set()
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
            "total": block(trades),
            "long": block([t for t in trades if t["side"] == "long"]),
            "short": block([t for t in trades if t["side"] == "short"]),
            "first_half": block(first_half),
            "second_half": block(second_half),
        },
        "trades": out_trades,
        "signals": signals_out,
        "signals_truncated": len(confirmed) > limit,
        "skipped_reasons": skipped_reasons,
        "last_signals": last_signals,                  # самые свежие подтверждённые сигналы (для бота: список signals обрезается по первым 5000)
        "equity": equity,
        "monthly": list(monthly.values()),
        "top": top,
        "trend": {"enabled": bool(allow), "blocked": trend_blocked, "confirmed": len(confirmed)},
    })
