"""Бэктест: python backtest.py history.csv [--exchange bybit] [--symbol BTC/USDT:USDT] [--sweep]

Правила симуляции (те же, что у бота):
  * шорт: deep_local <= --short-local-max И deep_scalp <= --short-scalp-max (0 сделок с --no-short)
  * лонг: deep_local >= local_min И deep_scalp >= scalp_min в --confirm отчётах подряд (по умолчанию 2)
  * вход по open первой свечи, начавшейся не раньше сообщения
  * пока позиция открыта, новые сигналы игнорируются
  * SL / TP от цены входа; если в одной свече достигнуты оба — считаем, что сработал SL
  * комиссия taker берётся за каждую сторону сделки
"""
import argparse
import csv
import os
import time
from bisect import bisect_left
from datetime import datetime

from signal_parser import Confirmer, should_long, should_short


def load_signals(path):
    out = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if not r["deep_local"] or not r["deep_scalp"]:
                continue
            ts = int(datetime.fromisoformat(r["date_utc"]).timestamp() * 1000)
            out.append((ts, float(r["deep_local"]), float(r["deep_scalp"])))
    out.sort()
    return out


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


def simulate(signals, candles, local_min, scalp_min, tp_pct, sl_pct, fee=0.00055, size=0.1, slip=0.0, confirm=1,
             short=None):
    """short=(local_max, scalp_max) включает шорты; одновременно открыта максимум одна позиция."""
    ts = [c[0] for c in candles]
    n = len(candles)
    trades, free = [], 0
    c_long = Confirmer(lambda s: should_long(s, local_min, scalp_min), confirm)
    c_short = Confirmer(lambda s: short is not None and should_short(s, *short), confirm)
    for sts, loc, scalp in signals:
        sig = {"local": loc, "scalp": scalp}
        long_ok, short_ok = c_long.update(sig), c_short.update(sig)
        if not (long_ok or short_ok):
            continue  # условие не выполнено или ещё не набралось подряд
        d = 1 if long_ok else -1
        i = bisect_left(ts, sts)
        if i >= n:
            break
        if i < free:
            continue  # позиция ещё открыта
        entry = candles[i][1] * (1 + d * slip)
        tp, sl = entry * (1 + d * tp_pct / 100), entry * (1 - d * sl_pct / 100)
        reason, exit_p, j = "OPEN", candles[-1][4], n - 1
        for k in range(i, n):
            _, o, h, l, _ = candles[k]
            if d == 1:
                if l <= sl:
                    reason, exit_p, j = "SL", min(sl, o) * (1 - slip), k
                    break
                if h >= tp:
                    reason, exit_p, j = "TP", max(tp, o), k
                    break
            else:
                if h >= sl:
                    reason, exit_p, j = "SL", max(sl, o) * (1 + slip), k
                    break
                if l <= tp:
                    reason, exit_p, j = "TP", min(tp, o), k
                    break
        free = j + 1
        pnl = d * (exit_p / entry - 1) - 2 * fee
        trades.append({"side": "long" if d == 1 else "short", "signal_ts": sts, "entry_ts": candles[i][0],
                       "exit_ts": candles[j][0], "entry": entry, "exit": exit_p, "reason": reason,
                       "pnl_pct": pnl * 100, "pnl_usd": pnl * entry * size})
    return trades


def stats(trades):
    if not trades:
        return {"trades": 0, "tp": 0, "sl": 0, "open": 0, "winrate": 0, "total_usd": 0,
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
            "winrate": 100 * len(wins) / len(trades),
            "total_usd": eq,
            "avg_pct": sum(t["pnl_pct"] for t in trades) / len(trades),
            "profit_factor": sum(wins) / sum(losses) if losses else float("inf"),
            "max_dd_usd": dd}


def fmt(s):
    return (f"сделок {s['trades']} (TP {s['tp']} / SL {s['sl']} / открыта {s['open']}), "
            f"winrate {s['winrate']:.1f}%, итого {s['total_usd']:+.0f} $, "
            f"средняя {s['avg_pct']:+.3f}%, PF {s['profit_factor']:.2f}, макс. просадка {s['max_dd_usd']:.0f} $")


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
    ap.add_argument("--sweep", action="store_true", help="сетка порогов local/scalp")
    a = ap.parse_args()

    signals = load_signals(a.history)
    print(f"Сигналов с Deep-значениями: {len(signals)}, "
          f"{datetime.utcfromtimestamp(signals[0][0]/1000):%Y-%m-%d} .. {datetime.utcfromtimestamp(signals[-1][0]/1000):%Y-%m-%d}")
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

    trades = simulate(signals, candles, a.local_min, a.scalp_min, short=short, **common)
    print("\nВсего:  " + fmt(stats(trades)))
    for side in ("long", "short"):
        part = [t for t in trades if t["side"] == side]
        if part:
            print(f"{side:6}: " + fmt(stats(part)))
    if trades:
        with open(a.out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(trades[0]))
            w.writeheader()
            w.writerows(trades)
        print(f"Сделки -> {a.out}")


if __name__ == "__main__":
    main()
