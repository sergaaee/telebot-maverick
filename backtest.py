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
import shutil
import time
from bisect import bisect_left
from datetime import datetime, timezone

from entries import attach_liq, entries, load_signals

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")  # не зависит от текущей папки запуска


def fetch_candles(exchange, symbol, tf, since_ms, cache_dir=CACHE_DIR):
    """Скачивает свечи [since_ms, now] с кешем в CSV. Возвращает [(ts,o,h,l,c)]."""
    import ccxt

    os.makedirs(cache_dir, exist_ok=True)
    key = "binanceusdm" if exchange == "binance" and ":" in symbol else exchange  # один и тот же рынок -> один кеш
    name = f"{key}_{symbol.replace('/', '').replace(':', '_')}_{tf}.csv"
    path = os.path.join(cache_dir, name)
    candles = []
    legacy = os.path.join(cache_dir, name.replace(key + "_", "binance_", 1))  # кеш старых запусков с --exchange binance
    if not os.path.exists(path) and key != exchange and os.path.exists(legacy):
        shutil.copy(legacy, path)
    if os.path.exists(path):
        with open(path, newline="") as f:
            candles = [tuple(float(x) if i else int(x) for i, x in enumerate(r)) for r in csv.reader(f)]
        candles = candles[:-1]  # последняя могла быть записана незавершённой старой версией — перекачаем
    ex = getattr(ccxt, exchange)({"enableRateLimit": True})
    tf_ms = ex.parse_timeframe(tf) * 1000
    # докачиваем хвост; если кеш начинается позже нужного, качаем заново с нуля
    if candles and candles[0][0] > since_ms + tf_ms:
        print(f"  кеш {os.path.basename(path)} начинается позже нужного — качаю заново")
        candles = []
    elif candles:
        print(f"  кеш {os.path.basename(path)}: {len(candles)} свечей, докачиваю хвост")
    else:
        print(f"  кеша нет ({os.path.basename(path)}), качаю с начала")
    cur = candles[-1][0] + tf_ms if candles else since_ms
    now = int(time.time() * 1000)
    closed_before = now // tf_ms * tf_ms  # свеча, которая ещё формируется, в кеш не попадает

    def save():
        with open(path + ".tmp", "w", newline="") as f:
            csv.writer(f).writerows(candles)
        os.replace(path + ".tmp", path)

    pages = 0
    while cur < closed_before:
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
        batch = [tuple(c[:5]) for c in batch if cur <= c[0] < closed_before]
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
             short=None, liq=None, cool=None, cooldown_h=0.0):
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
            if cooldown_h and cur["reason"] == "SL" and sts < cur["exit_ts"] + cooldown_h * 3.6e6:
                continue  # пауза после стопа
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
    if kind == "cd":
        for hours in (1, 2, 4, 6, 12, 24, 48):
            grid.append((f"пауза {hours}ч после стопа", dict(cooldown_h=hours)))
    elif kind == "liq":
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


def make_row(name, trades, split_ts):
    s = stats(trades)
    h1 = [t for t in trades if t["entry_ts"] < split_ts]
    h2 = [t for t in trades if t["entry_ts"] >= split_ts]
    wins = sum(t["pnl_usd"] > 0 for t in trades)
    return (name, s, h1, h2, wilson_low(wins, s["trades"]))


def print_table(rows):
    print(f"\n{'конфигурация':<46}| n  win%  PF  итого$ | 1-я половина        | 2-я половина")
    for name, s, h1, h2, _ in rows:
        print(f"{name:<46}| {s['trades']:>3} {s['winrate']:>5.1f} {s['profit_factor']:>5.2f} {s['total_usd']:>+7.0f} | "
              f"{half_str(h1)} | {half_str(h2)}")


def print_top(rows, min_trades, base_label):
    good = [r for r in rows[1:] if r[1]["trades"] >= min_trades]
    good.sort(key=lambda r: r[4], reverse=True)
    print(f"\nТоп-5 по нижней границе доверительного интервала winrate (сделок >= {min_trades}); база ({base_label}): "
          f"{rows[0][1]['winrate']:.1f}%")
    for name, s, h1, h2, lo in good[:5]:
        print(
            f"  {name:<46} n={s['trades']:>3} winrate {s['winrate']:.1f}% (нижняя граница {100 * lo:.0f}%) итого {s['total_usd']:+.0f}$")
    print(f"\nПроверено {len(rows) - 1} конфигураций на одной истории: лучшие из них частично просто повезло."
          " Верьте только тем, у кого плюс в ОБЕИХ половинах и достаточно сделок.")


def run_filter_sweep(kind, signals, candles, base_kw, short, min_trades):
    split_ts = signals[len(signals) // 2][0]
    rows = [make_row(name, simulate(signals, candles, short=short, **base_kw, **extra), split_ts)
            for name, extra in [("БАЗА (без фильтра)", {})] + filter_grid(kind)]
    print_table(rows)
    print_top(rows, min_trades, "без фильтра")


def run_threshold_sweep(side, signals, candles, common, a):
    """Свип порогов одной стороны; вторая сторона отключена, чтобы не смешивать результаты."""
    split_ts = signals[len(signals) // 2][0]
    never = float("inf")  # пороги, при которых лонг (Local/Scalp >= inf) никогда не срабатывает
    rows = []
    if side == "long":
        base = (a.local_min, a.scalp_min)
        grid = [(lm, sm) for lm in (1.25, 1.45, 1.65, 1.85, 2.05, 2.25, 2.5, 2.75, 3) for sm in
                (1.25, 1.5, 1.75, 2.0, 2.25, 2.5, 2.75, 3)]
        run = lambda p: simulate(signals, candles, p[0], p[1], short=None, **common)
        label = lambda p: f"long  local>={p[0]:.2f} scalp>={p[1]:.2f}"
    else:
        base = (a.short_local_max, a.short_scalp_max)
        grid = [(lm, sm) for lm in (0.5, 0.6, 0.7, 0.85, 0.95, 1.05, 1.15, 1.25) for sm in (0.35, 0.5, 0.65, 0.8, 0.95, 1.05, 1.2)]
        run = lambda p: simulate(signals, candles, never, never, short=p, **common)
        label = lambda p: f"short local<={p[0]:.2f} scalp<={p[1]:.2f}"
    rows.append(make_row(f"БАЗА: {label(base)}", run(base), split_ts))
    for p in grid:
        rows.append(make_row(label(p) + ("  *" if p == base else ""), run(p), split_ts))
    print_table(rows)
    print_top(rows, a.min_trades, "текущие пороги")


def explain(signals, candles, a, short, common, extra, trades):
    """Сравнивает сделки с фильтром и без: что убрано, что появилось из-за каскада (иной порядок позиций)."""
    base = simulate(signals, candles, a.local_min, a.scalp_min, short=short, **common)
    kept = {t["signal_ts"] for t in trades}
    base_ts = {t["signal_ts"] for t in base}
    rep = {s[0]: s for s in signals}

    def line(t):
        r = rep.get(t["signal_ts"])
        liq = f"liq L/S {r[3]:.0f}/{r[4]:.0f}k" if r and r[3] is not None else "liq -"
        return (f"  {datetime.fromtimestamp(t['signal_ts'] / 1000, timezone.utc):%Y-%m-%d %H:%M} {t['side']:5} "
                f"{t['reason']:4} {t['pnl_usd']:>+7.0f}$  {liq}")

    removed = [t for t in base if t["signal_ts"] not in kept]
    added = [t for t in trades if t["signal_ts"] not in base_ts]
    for title, part in (("УБРАНО фильтром", removed), ("ДОБАВИЛОСЬ из-за каскада позиций", added)):
        w = sum(t["pnl_usd"] > 0 for t in part)
        print(f"\n{title}: {len(part)} сделок, из них прибыльных {w}, сумма {sum(t['pnl_usd'] for t in part):+.0f}$")
        for t in part:
            print(line(t))
    for side in ("long", "short"):
        b_ = [t for t in base if t["side"] == side]
        f_ = [t for t in trades if t["side"] == side]
        print(f"\n{side}: база {fmt(stats(b_))}\n{side}: фильтр {fmt(stats(f_))}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("history")
    ap.add_argument("--exchange", default="binanceusdm")
    ap.add_argument("--symbol", default="BTC/USDT:USDT")
    ap.add_argument("--tf", default="1m")
    ap.add_argument("--local-min", type=float, default=1.85)
    ap.add_argument("--scalp-min", type=float, default=1.25)
    ap.add_argument("--tp", type=float, default=6.0)
    ap.add_argument("--sl", type=float, default=2.0)
    ap.add_argument("--fee", type=float, default=0.0005, help="taker-комиссия за сторону, доля")
    ap.add_argument("--slip", type=float, default=0.0, help="проскальзывание, доля")
    ap.add_argument("--short-local-max", type=float, default=0.7)
    ap.add_argument("--short-scalp-max", type=float, default=1.2)
    ap.add_argument("--no-short", action="store_true", help="только лонги")
    ap.add_argument("--confirm", type=int, default=4, help="сколько отчётов подряд нужно для сигнала")
    ap.add_argument("--size", type=float, default=0.1)
    ap.add_argument("--out", default="trades.csv")
    ap.add_argument("--sweep-long", "--sweep", dest="sweep_long", action="store_true",
                    help="свип порогов лонга (Local/Scalp >=), шорты отключены; --sweep — прежнее имя")
    ap.add_argument("--sweep-short", action="store_true", help="свип порогов шорта (Local/Scalp <=), лонги отключены")
    ap.add_argument("--liq-file",
                    help="CSV с ликвидациями (например history_binance.csv); по умолчанию — основной файл")
    ap.add_argument("--liq-max-age", type=float, default=3.0, help="макс. возраст записи ликвидаций из --liq-file, ч")
    ap.add_argument("--sweep-liq", action="store_true", help="свип фильтра «ликвидации закончились»")
    ap.add_argument("--sweep-cool", action="store_true", help="свип фильтра «метрики остывают»")
    ap.add_argument("--sweep-cooldown", action="store_true",
                    help="контроль: простая пауза после стопа (без ликвидаций)")
    ap.add_argument("--cooldown-h", type=float, default=0.0, help="пауза после стопа, ч")
    ap.add_argument("--min-trades", type=int, default=15, help="минимум сделок для попадания в топ свипа")
    ap.add_argument("--liq-hours", type=float, help="применить фильтр ликвидаций: окно, ч")
    ap.add_argument("--liq-frac", type=float, default=0.25, help="...спад до доли от максимума окна")
    ap.add_argument("--liq-pct", type=float, default=0, help="...всплеск не ниже этого перцентиля прошлых значений")
    ap.add_argument("--explain", action="store_true",
                    help="показать, какие сделки базы фильтр убрал/добавил и их ликвидации")
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
          f"{datetime.fromtimestamp(signals[0][0] / 1000, timezone.utc):%Y-%m-%d} .. {datetime.fromtimestamp(signals[-1][0] / 1000, timezone.utc):%Y-%m-%d}")
    candles = fetch_candles(a.exchange, a.symbol, a.tf, signals[0][0])
    common = dict(tp_pct=a.tp, sl_pct=a.sl, fee=a.fee, size=a.size, slip=a.slip, confirm=a.confirm)
    short = None if a.no_short else (a.short_local_max, a.short_scalp_max)

    if a.sweep_long or a.sweep_short:
        for side, on in (("long", a.sweep_long), ("short", a.sweep_short)):
            if on:
                run_threshold_sweep(side, signals, candles, common, a)
        return
    if a.sweep_liq or a.sweep_cool or a.sweep_cooldown:
        base_kw = dict(local_min=a.local_min, scalp_min=a.scalp_min, **common)
        for kind, on in (("liq", a.sweep_liq), ("cool", a.sweep_cool), ("cd", a.sweep_cooldown)):
            if on:
                run_filter_sweep(kind, signals, candles, base_kw, short, a.min_trades)
        return

    extra = {}
    if a.cooldown_h:
        extra["cooldown_h"] = a.cooldown_h
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
    if a.explain and extra:
        explain(signals, candles, a, short, common, extra, trades)
    if trades:
        with open(a.out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=[x for x in trades[0] if not x.startswith('_')], extrasaction='ignore')
            w.writeheader()
            w.writerows(trades)
        print(f"Сделки -> {a.out}")


if __name__ == "__main__":
    main()
