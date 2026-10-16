"""Запуск бэктеста и подготовка данных для интерфейса (сделки, сигналы, статистика)."""
import math
from bisect import bisect_left, bisect_right
from datetime import datetime, timezone

from .core import backtest
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


def _extra_stats(trades, span_ms):
    pn = [t["pnl_usd"] for t in trades]
    wins = [x for x in pn if x > 0]
    losses = [x for x in pn if x <= 0]
    streak = best = 0
    for x in pn:
        streak = streak + 1 if x <= 0 else 0
        best = max(best, streak)
    held = sum(t["exit_ts"] - t["entry_ts"] for t in trades)
    return {
        "avg_win_usd": sum(wins) / len(wins) if wins else 0.0,
        "avg_loss_usd": sum(losses) / len(losses) if losses else 0.0,
        "best_usd": max(pn) if pn else 0.0,
        "worst_usd": min(pn) if pn else 0.0,
        "max_loss_streak": best,
        "exposure_pct": 100 * held / span_ms if span_ms > 0 else 0.0,
        "expectancy_usd": sum(pn) / len(pn) if pn else 0.0,
        # сколько сделок дошли до первого / второго / третьего тейка
        "tp_reach": [sum(t["tp_hits"] >= k for t in trades) for k in (1, 2, 3)],
    }


def run_backtest(store: DataStore, defaults: dict, params: BacktestParams) -> dict:
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
    short = None if side == "long" else (eff["short_local_max"], eff["short_scalp_max"])
    if side == "short":
        long_min = scalp_min = INF          # лонг никогда не срабатывает
    try:
        tps = [(l["pct"], l["share"]) for l in eff["tps"]]
        backtest.normalize_tps(tps)
    except ValueError as e:
        raise ParamError(str(e))
    common = dict(tp_pct=tps[0][0], tps=tps, be_after_tp1=bool(eff.get("be_after_tp1")), sl_pct=eff["sl_pct"], fee=eff["fee"], size=eff["size"], slip=eff["slip"],
                  confirm=eff["confirm"], only=eff["only"])

    trades = backtest.simulate(sigs, candles, long_min, scalp_min, short=short, liq=liq, cool=cool, **common, **extra)

    # все подтверждённые сигналы (в том числе пропущенные: позиция открыта, пауза после стопа)
    lm, sm, sh = backtest.apply_only(long_min, scalp_min, short, eff["only"])
    confirmed = entries(sigs, lm, sm, eff["confirm"], sh, liq, cool)
    taken = {t["signal_ts"] for t in trades}
    limit = 5000
    signals_out = [{"ts": ts, "side": "long" if d == 1 else "short", "taken": ts in taken} for ts, d in confirmed[:limit]]

    rep = {s[0]: s for s in sigs}
    out_trades = []
    for i, t in enumerate(trades, 1):
        r = rep.get(t["signal_ts"])
        out_trades.append({
            "n": i, "side": t["side"], "signal_ts": t["signal_ts"], "entry_ts": t["entry_ts"], "exit_ts": t["exit_ts"],
            "entry": t["entry"], "exit": t["exit"], "exit_avg": t["exit_avg"], "tp": t["tp"], "tps": t["tps"],
            "tp_shares": t["tp_shares"], "sl": t["sl"], "be": t["be"], "be_ts": t["be_ts"], "tp_hits": t["tp_hits"],
            "fills": t["fills"], "reason": t["reason"],
            "pnl_pct": t["pnl_pct"], "pnl_usd": t["pnl_usd"], "hold_h": (t["exit_ts"] - t["entry_ts"]) / 3.6e6,
            "local": r[1] if r else None, "scalp": r[2] if r else None,
            "liq_long_k": r[3] if r and len(r) > 3 else None, "liq_short_k": r[4] if r and len(r) > 4 else None,
        })

    span_ms = (candles[-1][0] - trades[0]["entry_ts"]) if trades else 0
    split_ts = sigs[len(sigs) // 2][0]
    first_half = [t for t in trades if t["entry_ts"] < split_ts]
    second_half = [t for t in trades if t["entry_ts"] >= split_ts]

    def block(ts_):
        return {**backtest.stats(ts_), **_extra_stats(ts_, span_ms)}

    equity, cum = [], 0.0
    if trades:
        equity.append([trades[0]["entry_ts"], 0.0])
    for t in trades:
        cum += t["pnl_usd"]
        equity.append([t["exit_ts"], cum])
    monthly = {}
    for t in trades:
        m = datetime.fromtimestamp(t["exit_ts"] / 1000, timezone.utc).strftime("%Y-%m")
        d = monthly.setdefault(m, {"month": m, "trades": 0, "pnl_usd": 0.0, "wins": 0})
        d["trades"] += 1
        d["pnl_usd"] += t["pnl_usd"]
        d["wins"] += t["pnl_usd"] > 0

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
        "equity": equity,
        "monthly": list(monthly.values()),
    })
