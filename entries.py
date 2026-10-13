"""Генерация точек входа из потока отчётов + необязательные фильтры (ликвидации, «остывание» метрик).

entries(signals, ...) -> [(ts_ms, d)], d = +1 лонг / -1 шорт. Без фильтров ведёт себя так же, как Confirmer в боте.

Фильтры (оба действуют только на отчёте, где серия подтверждена):

  cool  — «заходить, когда метрики начали остывать». Внутри текущей серии подряд идущих подходящих отчётов:
          {"metric": "scalp"|"local", "drop": x}      лонг: метрика ушла вниз от максимума серии на >= x
                                                      (шорт зеркально: поднялась от минимума серии на >= x);
          {"metric": ..., "declines": k}              метрика k отчётов подряд двигалась в «остывающую» сторону.
  liq   — «заходить, когда закончились ликвидации» (для лонга смотрим ликвидации лонгов, для шорта — шортов):
          {"hours": L, "frac": f, "pct": p}           за последние L часов был всплеск (максимум >= p-го перцентиля
                                                      прошлых значений; p=0 — достаточно max > 0), а сейчас значение
                                                      <= f * максимум, т.е. ликвидации спали.
          Перцентиль считается только по прошлым отчётам — заглядывания в будущее нет.
"""
import csv
from bisect import bisect_right, insort
from collections import deque
from datetime import datetime

from signal_parser import parse_liquidation, should_long, should_short


def _ts(s):
    return int(datetime.fromisoformat(s).timestamp() * 1000)


def load_signals(path):
    """[(ts, local, scalp, liq_long_k, liq_short_k)] — ликвидации из колонок liq_*_k или из текста, иначе None."""
    out = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if not r["deep_local"] or not r["deep_scalp"]:
                continue
            ll = ls = None
            if r.get("liq_long_k") not in (None, ""):
                ll, ls = float(r["liq_long_k"]), float(r["liq_short_k"])
            else:
                liq = parse_liquidation(r.get("text") or "")
                if liq:
                    ll, ls = liq["long"], liq["short"]
            out.append((_ts(r["date_utc"]), float(r["deep_local"]), float(r["deep_scalp"]), ll, ls))
    out.sort(key=lambda x: x[0])
    return out


def attach_liq(signals, liq_signals, max_age_h=3.0):
    """Подставляет в отчёты ликвидации из другого файла (последняя запись не старше max_age_h часов)."""
    series = [(s[0], s[3], s[4]) for s in liq_signals if s[3] is not None]
    keys = [x[0] for x in series]
    out, hit = [], 0
    for s in signals:
        k = bisect_right(keys, s[0]) - 1
        if k >= 0 and s[0] - keys[k] <= max_age_h * 3.6e6:
            out.append((*s[:3], series[k][1], series[k][2]))
            hit += 1
        else:
            out.append((*s[:3], None, None))
    return out, hit


def entries(signals, local_min, scalp_min, confirm=1, short=None, liq=None, cool=None):
    sides = [(1, lambda s: should_long(s, local_min, scalp_min))]
    if short is not None:
        sides.append((-1, lambda s: should_short(s, *short)))
    st = {d: {"streak": 0, "ext": None, "prev": None, "dec": 0} for d, _ in sides}
    window = {1: deque(), -1: deque()}   # монотонная очередь для скользящего максимума
    past = {1: [], -1: []}               # отсортированные прошлые значения ликвидаций
    metric = (cool or {}).get("metric", "scalp")
    out = []
    for row in signals:
        sts, loc, scalp = row[:3]
        liqv = {1: row[3] if len(row) > 3 else None, -1: row[4] if len(row) > 4 else None}
        sig = {"local": loc, "scalp": scalp}
        m = sig[metric]
        thr, mx = {}, {}
        if liq:
            for d in (1, -1):
                v = liqv[d]
                if v is None:
                    continue
                w = window[d]
                while w and w[0][0] < sts - liq["hours"] * 3.6e6:
                    w.popleft()
                while w and w[-1][1] <= v:
                    w.pop()
                w.append((sts, v))
                mx[d] = w[0][1]
                p = liq.get("pct", 0)
                thr[d] = 0.0 if p <= 0 else (past[d][int(len(past[d]) * p / 100)] if len(past[d]) >= 50 else float("inf"))
        chosen = 0
        for d, pred in sides:
            s = st[d]
            if pred(sig):
                s["streak"] += 1
                s["ext"] = m if s["ext"] is None else (max(s["ext"], m) if d == 1 else min(s["ext"], m))
                cooling = s["prev"] is not None and ((m < s["prev"]) if d == 1 else (m > s["prev"]))
                s["dec"] = s["dec"] + 1 if cooling else 0
                s["prev"] = m
            else:
                s.update(streak=0, ext=None, prev=None, dec=0)
            ok = s["streak"] >= confirm
            if ok and cool:
                if "drop" in cool:
                    ok = abs(m - s["ext"]) >= cool["drop"] - 1e-9 and (m < s["ext"] if d == 1 else m > s["ext"])
                else:
                    ok = s["dec"] >= cool["declines"]
            if ok and liq:
                ok = d in mx and mx[d] > 0 and mx[d] >= thr[d] and liqv[d] <= liq["frac"] * mx[d]
            if ok and not chosen:
                chosen = d
        if liq:
            for d in (1, -1):
                if liqv[d] is not None:
                    insort(past[d], liqv[d])
        if chosen:
            out.append((sts, chosen))
    return out
