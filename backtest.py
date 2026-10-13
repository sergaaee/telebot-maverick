"""Бэктест: python backtest.py history.csv [--exchange bybit] [--symbol BTC/USDT:USDT] [--sweep]

Правила симуляции (те же, что у бота):
  * шорт: deep_local <= --short-local-max И deep_scalp <= --short-scalp-max (0 сделок с --no-short)
  * лонг: deep_local >= local_min И deep_scalp >= scalp_min в --confirm отчётах подряд (по умолчанию 2)
  * вход по open первой свечи, начавшейся не раньше сообщения
  * сигнал в ту же сторону при открытой позиции игнорируется; в противоположную — разворот: закрытие по open свечи входа
  * SL / TP от цены входа; если в одной свече достигнуты оба — считаем, что сработал SL
  * комиссия taker берётся за каждую сторону сделки
"""
import argparse
import csv
import os
import time
from bisect import bisect_left
from datetime import datetime, timezone

from entries import attach_liq, entries, load_signals


def fetch_candles(exchange, symbol, tf, since_ms, cache_dir="data"):
    """Скачивает свечи [since_ms, now] с кешем в CSV. Возвращает [(ts,o,h,l,c)]."""
    import ccxt

    os.makedirs(cache_dir, exist_ok=True)
    name = f"{exchange}_{symbol.replace('/', '').replace(':', '_')}_{tf}.csv"
    path = os.path.join(cache_dir, name)
    candles = []
    if os.path.exists(path):
        with open(path, newline="") as f:
            candles = [tuple(float(x) if i else int(x) for i, x in enumerate(r)) for r in csv.reader(f)]
    ex = getattr(ccxt, exchange)({"enableRateLimit": True})
    tf_ms = ex.parse_timeframe(tf) * 1000
    # докачиваем хвост; если кеш начинается позже нужного, качаем заново с нуля
    if candles and candles[0][0] > since_ms + tf_ms:
        candles = []
    cur = candles[-1][0] + tf_ms if candles else since_ms
    now = int(time.time() * 1000)

    def save():
        with open(path + ".tmp", "w", newline="") as f:
            csv.writer(f).writerows(candles)
        os.replace(path + ".tmp", path)

    pages = 0
    while cur < now:
        for attempt in range(8):
            try:
                batch = ex.fetch_ohlcv(symbol, tf, since=cur, limit=1000)
                break
            except (ccxt.RateLimitExceeded, ccxt.NetworkError, ccxt.RequestTimeout) as e:
                wait = min(60, 5 * 2 ** attempt)
                print(f"\n  {type(e).__name__}, жду {wait}с и повторяю ({attempt + 1}/8)")
                time.sleep(wait)
        else:
            save()
            raise SystemExit("Биржа не отвечает; прогресс сохранён, запустите ещё раз — докачает с места остановки.")
        batch = [tuple(c[:5]) for c in batch if c[0] >= cur]
        if not batch:
            break
        candles.extend(batch)
        cur = batch[-1][0] + tf_ms
        pages += 1
        print(f"\r  свечи: {len(candles)}", end="", flush=True)
        if pages % 20 == 0:
            save()
        time.sleep(0.3)  # мягче к лимитам, чем enableRateLimit по умолчанию
    print()
    save()
    return candles


def _scan(candles, i, d, tp, sl, slip):
    """Естественный выход позиции, открытой на open свечи i: (reason, exit_price, idx)."""
    for k in range(i, len(candles)):
        _, o, h, l, _ = candles[k]
        if d == 1:
            if l <= sl:
                return "SL", min(sl, o) * (1 - slip), k
            if h >= tp:
                return "TP", max(tp, o), k
        else:
            if h >= sl:
                return "SL", max(sl, o) * (1 + slip), k
            if l <= tp:
                return "TP", min(tp, o), k
    return "OPEN", candles[-1][4], len(candles) - 1


def _close(trade, exit_p, reason, idx, candles, fee, size):
    d = 1 if trade["side"] == "long" else -1
    pnl = d * (exit_p / trade["entry"] - 1) - 2 * fee
    trade.update(exit=exit_p, reason=reason, exit_ts=candles[idx][0], _j=idx,
                 pnl_pct=pnl * 100, pnl_usd=pnl * trade["entry"] * size)


def simulate(signals, candles, local_min, scalp_min, tp_pct, sl_pct, fee=0.00055, size=0.1, slip=0.0, confirm=1,
             short=None, liq=None, cool=None):
    """short=(local_max, scalp_max) включает шорты; liq/cool — фильтры входа (см. entries.py).
    Одна позиция одновременно: сигнал в ту же сторону игнорируется, в противоположную — разворот
    (закрытие по open свечи входа + новая позиция)."""
    ts = [c[0] for c in candles]
    n = len(candles)
    trades, cur = [], None
    for sts, d in entries(signals, local_min, scalp_min, confirm, short, liq, cool):
        side = "long" if d == 1 else "short"
        i = bisect_left(ts, sts)
        if i >= n:
            break
        if cur is not None and cur["_j"] < i:
            cur = None  # закрылась сама по SL/TP
        if cur is not None:
            if cur["side"] == side:
                continue  # такая позиция уже открыта
            o = candles[i][1]
            _close(cur, o * (1 - slip) if cur["side"] == "long" else o * (1 + slip), "REV", i, candles, fee, size)
        entry = candles[i][1] * (1 + d * slip)
        tp, sl = entry * (1 + d * tp_pct / 100), entry * (1 - d * sl_pct / 100)
        cur = {"side": side, "signal_ts": sts, "entry_ts": candles[i][0], "entry": entry}
        reason, exit_p, j = _scan(candles, i, d, tp, sl, slip)
        _close(cur, exit_p, reason, j, candles, fee, size)
        trades.append(cur)
    return trades


def stats(trades):
    if not trades:
        return {"trades": 0, "tp": 0, "sl": 0, "open": 0, "rev": 0, "winrate": 0, "total_usd": 0,
                "avg_pct": 0, "profit_factor": 0, "max_dd_usd": 0}
    wins = [t["pnl_usd"] for t in trades if t["pnl_usd"] > 0]
    losses = [-t["pnl_usd"] for t in trades if t["pnl_usd"] <= 0]
    eq = peak = dd = 0.0
    for t in trades:
        eq += t["pnl_usd"]
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return {"trades": len(trades),
            "tp": sum(t["reason"] == "TP" for t in trades),
            "sl": sum(t["reason"] == "SL" for t in trades),
            "open": sum(t["reason"] == "OPEN" for t in trades),
            "rev": sum(t["reason"] == "REV" for t in trades),
            "winrate": 100 * len(wins) / len(trades),
            "total_usd": eq,
            "avg_pct": sum(t["pnl_pct"] for t in trades) / len(trades),
            "profit_factor": sum(wins) / sum(losses) if losses else float("inf"),
            "max_dd_usd": dd}


def fmt(s):
    return (f"сделок {s['trades']} (TP {s['tp']} / SL {s['sl']} / разворот {s['rev']} / открыта {s['open']}), "
            f"winrate {s['winrate']:.1f}%, итого {s['total_usd']:+.0f} $, "
            f"средняя {s['avg_pct']:+.3f}%, PF {s['profit_factor']:.2f}, макс. просадка {s['max_dd_usd']:.0f} $")


def wilson_low(wins, n, z=1.96):
    """Нижняя граница 95% доверительного интервала для доли побед — штраф за малую выборку."""
    if n == 0:
        return 0.0
    p = wins / n
    return ((p + z * z / (2 * n)) - z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)) / (1 + z * z / n)


def filter_grid(kind):
    grid = []
    if kind == "liq":
        for hours in (2, 6, 12, 24):
            for frac in (0.5, 0.25, 0.1):
                for pct in (0, 75, 90):
                    grid.append((f"liq  окно {hours:>2}ч  спад до {frac:<4}  всплеск>=p{pct:<2}",
                                 dict(liq=dict(hours=hours, frac=frac, pct=pct))))
    else:
        for metric in ("scalp", "local"):
            for drop in (0.05, 0.1, 0.2, 0.3, 0.5):
                grid.append((f"cool {metric:5} упала на >= {drop}", dict(cool=dict(metric=metric, drop=drop))))
            for k_ in (1, 2, 3):
                grid.append((f"cool {metric:5} {k_} отчёт(а) подряд вниз", dict(cool=dict(metric=metric, declines=k_))))
    return grid


def half_str(trades):
    s = stats(trades)
    return f"{s['trades']:>3} {s['winrate']:>3.0f}% {s['total_usd']:>+6.0f}$" if s["trades"] else "  0   -       -"


def run_filter_sweep(kind, signals, candles, base_kw, short, min_trades):
    split_ts = signals[len(signals) // 2][0]
    rows = []
    for name, extra in [("БАЗА (без фильтра)", {})] + filter_grid(kind):
        trades = simulate(signals, candles, short=short, **base_kw, **extra)
        s = stats(trades)
        h1 = [t for t in trades if t["entry_ts"] < split_ts]
        h2 = [t for t in trades if t["entry_ts"] >= split_ts]
        wins = sum(t["pnl_usd"] > 0 for t in trades)
        rows.append((name, s, h1, h2, wilson_low(wins, s["trades"])))
    print(f"\n{'конфигурация':<46}| всего: n  win%  PF  итого$ | 1-я половина        | 2-я половина")
    for name, s, h1, h2, _ in rows:
        print(f"{name:<46}| {s['trades']:>3} {s['winrate']:>5.1f} {s['profit_factor']:>5.2f} {s['total_usd']:>+7.0f} | "
              f"{half_str(h1)} | {half_str(h2)}")
    base_wr = rows[0][1]["winrate"]
    good = [r for r in rows[1:] if r[1]["trades"] >= min_trades]
    good.sort(key=lambda r: r[4], reverse=True)
    print(f"\nТоп-5 по нижней границе доверительного интервала winrate (сделок >= {min_trades}); база: {base_wr:.1f}%")
    for name, s, h1, h2, lo in good[:5]:
        print(f"  {name:<46} n={s['trades']:>3} winrate {s['winrate']:.1f}% (нижняя граница {100*lo:.0f}%) итого {s['total_usd']:+.0f}$")
    print(f"\nПроверено {len(rows) - 1} конфигураций на одной истории: лучшие из них частично просто повезло."
          " Верьте только тем, у кого плюс в ОБЕИХ половинах и достаточно сделок.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("history")
    ap.add_argument("--exchange", default="binanceusdm")
    ap.add_argument("--symbol", default="BTC/USDT:USDT")
    ap.add_argument("--tf", default="1m")
    ap.add_argument("--local-min", type=float, default=1.65)
    ap.add_argument("--scalp-min", type=float, default=2.0)
    ap.add_argument("--tp", type=float, default=3.0)
    ap.add_argument("--sl", type=float, default=1.0)
    ap.add_argument("--fee", type=float, default=0.0005, help="taker-комиссия за сторону, доля")
    ap.add_argument("--slip", type=float, default=0.0, help="проскальзывание, доля")
    ap.add_argument("--short-local-max", type=float, default=1.05)
    ap.add_argument("--short-scalp-max", type=float, default=0.65)
    ap.add_argument("--no-short", action="store_true", help="только лонги")
    ap.add_argument("--confirm", type=int, default=2, help="сколько отчётов подряд нужно для сигнала")
    ap.add_argument("--size", type=float, default=0.1)
    ap.add_argument("--out", default="trades.csv")
    ap.add_argument("--sweep", action="store_true", help="сетка порогов local/scalp (только лонги)")
    ap.add_argument("--liq-file", help="CSV с ликвидациями (например history_binance.csv); по умолчанию — основной файл")
    ap.add_argument("--liq-max-age", type=float, default=3.0, help="макс. возраст записи ликвидаций из --liq-file, ч")
    ap.add_argument("--sweep-liq", action="store_true", help="свип фильтра «ликвидации закончились»")
    ap.add_argument("--sweep-cool", action="store_true", help="свип фильтра «метрики остывают»")
    ap.add_argument("--min-trades", type=int, default=15, help="минимум сделок для попадания в топ свипа")
    ap.add_argument("--liq-hours", type=float, help="применить фильтр ликвидаций: окно, ч")
    ap.add_argument("--liq-frac", type=float, default=0.25, help="...спад до доли от максимума окна")
    ap.add_argument("--liq-pct", type=float, default=0, help="...всплеск не ниже этого перцентиля прошлых значений")
    ap.add_argument("--cool-metric", choices=["scalp", "local"], default="scalp")
    ap.add_argument("--cool-drop", type=float, help="применить фильтр остывания: падение от максимума серии")
    ap.add_argument("--cool-declines", type=int, help="...или: столько отчётов подряд вниз")
    a = ap.parse_args()

    signals = load_signals(a.history)
    if a.liq_file:
        signals, hit = attach_liq(signals, load_signals(a.liq_file), a.liq_max_age)
        print(f"Ликвидации из {a.liq_file}: найдены для {hit} из {len(signals)} отчётов")
    elif a.sweep_liq or a.liq_hours:
        have = sum(s[3] is not None for s in signals)
        print(f"Ликвидации в {a.history}: есть в {have} из {len(signals)} отчётов")
        if not have:
            raise SystemExit("В файле нет данных о ликвидациях — укажите --liq-file.")
    print(f"Сигналов с Deep-значениями: {len(signals)}, "
          f"{datetime.fromtimestamp(signals[0][0]/1000, timezone.utc):%Y-%m-%d} .. {datetime.fromtimestamp(signals[-1][0]/1000, timezone.utc):%Y-%m-%d}")
    candles = fetch_candles(a.exchange, a.symbol, a.tf, signals[0][0])
    common = dict(tp_pct=a.tp, sl_pct=a.sl, fee=a.fee, size=a.size, slip=a.slip, confirm=a.confirm)
    short = None if a.no_short else (a.short_local_max, a.short_scalp_max)

    if a.sweep:
        print("\nlocal  scalp | результат")
        for lm in (1.25, 1.45, 1.65, 1.85, 2.05):
            for sm in (1.5, 1.75, 2.0, 2.25, 2.5):
                print(f"{lm:5.2f} {sm:5.2f} | {fmt(stats(simulate(signals, candles, lm, sm, **common)))}")
        print("\nОсторожно: подбор порогов по одной и той же истории — это переобучение.")
        return
    if a.sweep_liq or a.sweep_cool:
        base_kw = dict(local_min=a.local_min, scalp_min=a.scalp_min, **common)
        for kind, on in (("liq", a.sweep_liq), ("cool", a.sweep_cool)):
            if on:
                run_filter_sweep(kind, signals, candles, base_kw, short, a.min_trades)
        return

    extra = {}
    if a.liq_hours:
        extra["liq"] = dict(hours=a.liq_hours, frac=a.liq_frac, pct=a.liq_pct)
    if a.cool_drop is not None:
        extra["cool"] = dict(metric=a.cool_metric, drop=a.cool_drop)
    elif a.cool_declines:
        extra["cool"] = dict(metric=a.cool_metric, declines=a.cool_declines)
    trades = simulate(signals, candles, a.local_min, a.scalp_min, short=short, **common, **extra)
    print("\nВсего:  " + fmt(stats(trades)))
    for side in ("long", "short"):
        part = [t for t in trades if t["side"] == side]
        if part:
            print(f"{side:6}: " + fmt(stats(part)))
    if trades:
        with open(a.out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=[x for x in trades[0] if not x.startswith('_')], extrasaction='ignore')
            w.writeheader()
            w.writerows(trades)
        print(f"Сделки -> {a.out}")


if __name__ == "__main__":
    main()
