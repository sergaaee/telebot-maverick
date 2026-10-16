"""Торговля топ-коином отчёта: по сигналу основной стратегии открываем ту же сторону по топ-коину из этого отчёта.

Свечи монеты нужны только на окно «момент сигнала + N суток», поэтому они качаются точечно (backtest.fetch_window).
build_top() ничего не скачивает: если окна в кеше нет, сделка попадает в pending и считается после загрузки.
"""
from bisect import bisect_left, bisect_right

from . import backtest

QUOTES = ("USDT", "USDC", "BUSD", "USD")
PREFIXES = ("", "1000", "10000", "1000000")        # мелкие монеты на фьючерсах торгуются пачками: PEPE -> 1000PEPE
DAY_MS = 86_400_000


def split_coin(raw):
    """'TIA-USD' -> 'TIA', 'WBETHUSDT' -> 'WBETH', 'btc/usdt' -> 'BTC'."""
    s = (raw or "").strip().upper()
    for sep in ("-", "/", "_"):
        if sep in s:
            return s.split(sep)[0]
    for q in QUOTES:
        if s.endswith(q) and len(s) > len(q):
            return s[: -len(q)]
    return s


def resolve_symbol(raw, available):
    """Тикер из отчёта -> бессрочный USDT-контракт биржи или None, если такого контракта нет."""
    base = split_coin(raw)
    if not base:
        return None
    for pre in PREFIXES:
        sym = f"{pre}{base}/USDT:USDT"
        if sym in available:
            return sym
    return None


def build_top(triggers, p, available, covered_of, candles_of, tf_ms, now_ms):
    """triggers: [{ts, d, coin, value}] — подтверждённые сигналы основной стратегии (coin/value из отчёта, могут быть None).
    p: top_* параметры. available: множество символов биржи (None — список рынков недоступен).
    covered_of(symbol) -> [(a, b)] скачанные интервалы; candles_of(symbol) -> свечи монеты.
    Возвращает {trades, skipped, pending, windows_total}. Сделки без полного окна свечей не считаются, а попадают в pending."""
    levels = backtest.normalize_tps([(l["pct"], l["share"]) for l in p["top_tps"]])
    days_ms = int(p["top_days"] * DAY_MS)
    want_side = p["top_side"]
    skipped, per_symbol = [], {}

    def skip(t, reason, symbol=None):
        skipped.append({"ts": t["ts"], "side": "long" if t["d"] == 1 else "short", "coin": t.get("coin"), "symbol": symbol, "reason": reason})

    for t in triggers:
        if want_side != "both" and (t["d"] == 1) != (want_side == "long"):
            continue                                            # сторона не выбрана — это не пропуск, а отключённый режим
        if not t.get("coin"):
            skip(t, "в отчёте нет топ-коина")
        elif p.get("top_min_value") and (t.get("value") or 0) < p["top_min_value"]:
            skip(t, f"значение {t.get('value')} ниже порога {p['top_min_value']}")
        else:
            sym = resolve_symbol(t["coin"], available) if available is not None else None
            if sym is None:
                skip(t, "нет бессрочного USDT-контракта на бирже" if available is not None else "список рынков биржи недоступен")
            else:
                per_symbol.setdefault(sym, []).append(t)

    last = backtest.last_closed_start(tf_ms, now_ms)
    trades, pending = [], {}
    for sym, items in per_symbol.items():
        covered = covered_of(sym)
        candles, ts_list, busy = None, None, -1
        for t in sorted(items, key=lambda x: x["ts"]):
            if t["ts"] > last:
                skip(t, "свеча входа ещё не закрылась", sym)
                continue
            if t["ts"] < busy:
                skip(t, "по этой монете уже открыта позиция", sym)
                continue
            end = t["ts"] + days_ms
            miss = backtest.missing_intervals(covered, t["ts"], min(end, last), tf_ms)
            if miss:
                pending.setdefault(sym, []).extend(miss)
                skipped.append({"ts": t["ts"], "side": "long" if t["d"] == 1 else "short", "coin": t["coin"], "symbol": sym, "reason": "нужно скачать свечи", "pending": True})
                continue
            if candles is None:
                candles = candles_of(sym)
                ts_list = [c[0] for c in candles]
            lo, hi = bisect_left(ts_list, t["ts"]), bisect_right(ts_list, end)
            window = candles[lo:hi]
            if not window or window[0][0] - t["ts"] > 5 * tf_ms:
                skip(t, "монеты ещё не было на бирже", sym)
                continue
            res = backtest.run_events([(t["ts"], t["d"])], window, levels, p["top_sl_pct"], p["top_fee"], 1.0, p["top_slip"], 0.0,
                                      bool(p.get("top_be_after_tp1")), notional=p["top_size"])
            tr = res[0]
            partial = end > last                                # окно ещё не закончилось — сделка может быть незавершённой
            if tr["reason"] == "OPEN" and not partial:
                tr["reason"] = "TIME"                           # время вышло (N суток) — закрыто по рынку
                tr["fills"][-1]["label"] = "TIME"
            tr.update(symbol=sym, coin=t["coin"], top_value=t.get("value"), partial=partial, window_end=min(end, last))
            busy = tr["exit_ts"]
            trades.append(tr)
    trades.sort(key=lambda x: x["entry_ts"])
    merged = {s: backtest.merge_intervals(w, tf_ms) for s, w in pending.items()}
    return {"trades": trades, "skipped": sorted(skipped, key=lambda x: x["ts"]), "pending": merged}


def estimate_requests(pending, tf_ms, batch=1000):
    return sum(-(-((b - a) // tf_ms + 1) // batch) for ws in pending.values() for a, b in ws)
