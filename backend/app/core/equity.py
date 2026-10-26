"""Просадка с учётом открытых позиций (по рыночной цене), а не только по закрытым сделкам.

Кривая капитала: депозит + результат закрытых ног + для каждой открытой ноги исполненные части и нереализованный результат остатка по худшей цене
свечи (low у лонга, high у шорта). Так видно, как глубоко счёт проседал ВНУТРИ сделок — с учётом плеча, нескольких одновременных ног (пирамидинг)
и размера каждой. Просадка считается от пика капитала по закрытию свечей.

Приближения: свечи группируются в блоки (не больше ~15 000 блоков), экстремумы ног берутся по блоку; в свече выхода по SL / БУ / ликвидации цена не хуже цены
выхода; для усреднённой позиции берётся полный размер и средняя цена входа с первой свечи (доборы раньше своего времени — консервативно, но не точно);
комиссии учтены остатком при закрытии ноги, чтобы итог совпал с результатом сделки."""
from bisect import bisect_left


def mtm_drawdown(trades, candles, deposit, max_blocks=15000):
    """{max_dd_usd, max_dd_pct, min_equity_usd} по сделкам с notional / pnl_usd (после apply_sizing); пусто — нули."""
    out = {"max_dd_real_usd": 0.0, "max_dd_real_pct": 0.0, "min_equity_usd": deposit}
    legs = [t for t in trades if t.get("notional")]
    if not legs or not candles:
        return out
    n = len(candles)
    S = max(1, -(-n // max_blocks))
    nb = -(-n // S)
    lo, hi, cl = [0.0] * nb, [0.0] * nb, [0.0] * nb
    for b in range(nb):
        chunk = candles[b * S:(b + 1) * S]
        lo[b], hi[b], cl[b] = min(c[3] for c in chunk), max(c[2] for c in chunk), chunk[-1][4]
    ts = [c[0] for c in candles]

    def blk(t):
        return min(n - 1, bisect_left(ts, t)) // S

    worst, close = {}, {}
    closings = []
    for t in legs:
        d = 1 if t["side"] == "long" else -1
        entry, notional = t["entry"], t["notional"]
        b0, b1 = blk(t["entry_ts"]), blk(t["exit_ts"])
        fills = sorted(((blk(f["ts"]), f["share"], d * (f["price"] / entry - 1) * notional * f["share"]) for f in t["fills"]), key=lambda x: x[0])
        booked = sum(f[2] for f in fills)
        resid = t["pnl_usd"] - booked                                  # комиссии и округления — на последней фиксации
        rem, realized, fi = 1.0, 0.0, 0
        for b in range(b0, b1 + 1):
            while fi < len(fills) and fills[fi][0] < b:
                rem -= fills[fi][1]
                realized += fills[fi][2]
                fi += 1
            adv = lo[b] if d == 1 else hi[b]
            if b == b1 and t["reason"] in ("SL", "BE", "LIQ"):
                adv = max(adv, t["exit"]) if d == 1 else min(adv, t["exit"])
            if b == b1:
                realized += resid
            rem_ = max(rem, 0.0)
            worst[b] = worst.get(b, 0.0) + realized + d * (adv / entry - 1) * notional * rem_
            close[b] = close.get(b, 0.0) + realized + d * (cl[b] / entry - 1) * notional * rem_
        closings.append((b1, t["pnl_usd"]))
    closings.sort()
    ci, closed, peak = 0, 0.0, deposit
    dd_usd = dd_pct = 0.0
    low_eq = deposit
    for b in sorted(worst):
        while ci < len(closings) and closings[ci][0] < b:
            closed += closings[ci][1]
            ci += 1
        peak = max(peak, deposit + closed)
        eq_low = deposit + closed + worst[b]
        low_eq = min(low_eq, eq_low)
        if peak > 0:
            dd_usd = max(dd_usd, peak - eq_low)
            dd_pct = max(dd_pct, (peak - eq_low) / peak * 100)
        peak = max(peak, deposit + closed + close[b])
    return {"max_dd_real_usd": dd_usd, "max_dd_real_pct": min(dd_pct, 100.0), "min_equity_usd": low_eq}


def mtm_drawdown_multi(groups, deposit, max_blocks=15000):
    """Просадка по рыночной цене для НЕСКОЛЬКИХ стратегий на одном счёте (портфель). groups — [(сделки с notional / pnl_usd, свечи их актива)].
    Все свечи приводятся к общей сетке времени (не больше max_blocks блоков), как в mtm_drawdown: внутри блока каждая нога берётся по худшей цене своего актива,
    ноги складываются на один счёт; пик и просадка считаются от капитала по закрытию блоков. Приближение консервативно: экстремумы разных активов внутри блока
    считаются совпавшими. -> {max_dd_real_usd, max_dd_real_pct, min_equity_usd, max_dd_real_ts (момент самого глубокого провала)}."""
    out = {"max_dd_real_usd": 0.0, "max_dd_real_pct": 0.0, "min_equity_usd": deposit, "max_dd_real_ts": None}
    legs = [(t, c) for tr, c in groups if c for t in tr if t.get("notional")]
    if not legs:
        return out
    t0 = min(t["entry_ts"] for t, _ in legs)
    t1 = max(t["exit_ts"] for t, _ in legs)
    span = max(t1 - t0, 1)
    step = max(span // max_blocks + 1, 60_000)
    nb = span // step + 1
    series = {}                                              # id(свечи) -> (low[], high[], close[]) на общей сетке

    def grid(candles):
        key = id(candles)
        if key not in series:
            lo, hi, cl = [None] * nb, [None] * nb, [None] * nb
            for ts, _o, h, l, c in candles:
                if ts < t0 - step or ts > t1 + step:
                    continue
                b = min(max((ts - t0) // step, 0), nb - 1)
                lo[b] = l if lo[b] is None else min(lo[b], l)
                hi[b] = h if hi[b] is None else max(hi[b], h)
                cl[b] = c
            last = None
            for b in range(nb):                               # блоки без свечей (пропуски в данных) держат последнюю цену
                if cl[b] is None:
                    if last is not None:
                        lo[b] = hi[b] = cl[b] = last
                else:
                    last = cl[b]
            series[key] = (lo, hi, cl)
        return series[key]

    worst, close, closings = {}, {}, []
    for t, candles in legs:
        lo, hi, cl = grid(candles)
        d = 1 if t["side"] == "long" else -1
        entry, notional = t["entry"], t["notional"]
        blk = lambda ts: min(max((ts - t0) // step, 0), nb - 1)
        b0, b1 = blk(t["entry_ts"]), blk(t["exit_ts"])
        fills = sorted(((blk(f["ts"]), f["share"], d * (f["price"] / entry - 1) * notional * f["share"]) for f in t["fills"]), key=lambda x: x[0])
        resid = t["pnl_usd"] - sum(f[2] for f in fills)
        rem, realized, fi = 1.0, 0.0, 0
        for b in range(b0, b1 + 1):
            while fi < len(fills) and fills[fi][0] < b:
                rem -= fills[fi][1]
                realized += fills[fi][2]
                fi += 1
            adv_src, close_px = (lo[b] if d == 1 else hi[b]), cl[b]
            if adv_src is None or close_px is None:           # нет цены в этом блоке (данных актива ещё/уже нет) — нога не оценивается
                continue
            adv = adv_src
            if b == b1 and t["reason"] in ("SL", "BE", "LIQ"):
                adv = max(adv, t["exit"]) if d == 1 else min(adv, t["exit"])
            if b == b1:
                realized += resid
            rem_ = max(rem, 0.0)
            worst[b] = worst.get(b, 0.0) + realized + d * (adv / entry - 1) * notional * rem_
            close[b] = close.get(b, 0.0) + realized + d * (close_px / entry - 1) * notional * rem_
        closings.append((b1, t["pnl_usd"]))
    closings.sort()
    ci, closed, peak = 0, 0.0, deposit
    low_eq, dd_usd, dd_pct, dd_b = deposit, 0.0, 0.0, None
    for b in sorted(worst):
        while ci < len(closings) and closings[ci][0] < b:
            closed += closings[ci][1]
            ci += 1
        peak = max(peak, deposit + closed)
        eq_low = deposit + closed + worst[b]
        low_eq = min(low_eq, eq_low)
        if peak > 0:
            dd_usd = max(dd_usd, peak - eq_low)
            if (peak - eq_low) / peak * 100 > dd_pct:
                dd_pct, dd_b = (peak - eq_low) / peak * 100, b
        peak = max(peak, deposit + closed + close[b])
    return {"max_dd_real_usd": dd_usd, "max_dd_real_pct": min(dd_pct, 100.0), "min_equity_usd": low_eq, "max_dd_real_ts": t0 + dd_b * step if dd_b is not None else None}
