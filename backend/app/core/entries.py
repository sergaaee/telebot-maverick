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
import logging
import sys
from bisect import bisect_right, insort
from collections import deque
from datetime import datetime

from .signal_parser import parse_liquidation, parse_signal, parse_top_coin, should_long, should_short


# по умолчанию csv не читает поля длиннее 128 КБ («field larger than field limit»): в истории бывают очень длинные сообщения
# (или лишняя кавычка, склеившая строки), поэтому лимит поднят; такие строки попадают в лог
csv.field_size_limit(min(sys.maxsize, 2 ** 30))
TEXT_WARN = 100_000
log = logging.getLogger("entries")


def _ts(s):
    return int(datetime.fromisoformat(s).timestamp() * 1000)


def load_signals(path):
    """[(ts, local, scalp, liq_long_k, liq_short_k, top_coin, top_value, global)] — global (Deep Global) может быть None, если его нет в отчёте; ликвидации из колонок liq_*_k или из текста, иначе None;
    топ-коин (тикер как в отчёте и его значение) берётся из текста отчёта, если он там есть."""
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
            top = parse_top_coin(r.get("text") or "")
            if len(r.get("text") or "") > TEXT_WARN:
                log.warning("%s: строка id=%s содержит текст на %d символов — возможно, в файле лишняя кавычка (строки склеились)", path, r.get("id"), len(r["text"]))
            dg = None
            if r.get("deep_global") not in (None, ""):
                dg = float(r["deep_global"])
            else:
                dg = (parse_signal(r.get("text") or "") or {}).get("global")
            out.append((_ts(r["date_utc"]), float(r["deep_local"]), float(r["deep_scalp"]), ll, ls,
                        top["coin"] if top else None, top["value"] if top else None, dg))
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
            out.append((*s[:3], series[k][1], series[k][2], *s[5:]))
            hit += 1
        else:
            out.append((*s[:3], None, None, *s[5:]))
    return out, hit


def entries(signals, local_min, scalp_min, confirm=1, short=None, liq=None, cool=None, global_min=None, short_global_max=None):
    """global_min / short_global_max — необязательные пороги Deep Global для лонга (≥) и шорта (≤); None — метрика не участвует."""
    sides = [(1, lambda s: should_long(s, local_min, scalp_min, global_min))]
    if short is not None:
        sides.append((-1, lambda s: should_short(s, *short, short_global_max)))
    st = {d: {"streak": 0, "ext": None, "prev": None, "dec": 0} for d, _ in sides}
    window = {1: deque(), -1: deque()}   # монотонная очередь для скользящего максимума
    past = {1: [], -1: []}               # отсортированные прошлые значения ликвидаций
    metric = (cool or {}).get("metric", "scalp")
    out = []
    for row in signals:
        sts, loc, scalp = row[:3]
        liqv = {1: row[3] if len(row) > 3 else None, -1: row[4] if len(row) > 4 else None}
        sig = {"local": loc, "scalp": scalp, "global": row[7] if len(row) > 7 else None}
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
            if pred(sig) and m is not None:          # нет значения метрики остывания (например, Deep Global) — отчёт серию не продолжает
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
