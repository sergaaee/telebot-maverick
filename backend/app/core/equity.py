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
