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
import json
import os
import shutil
import time
from bisect import bisect_left, bisect_right
from datetime import datetime, timezone

from . import tradingweek
from .entries import attach_liq, entries, load_signals
from .signal_parser import should_long, should_short

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
CACHE_DIR = os.getenv("CACHE_DIR") or os.path.join(REPO_ROOT, "data")  # не зависит от папки запуска


def cache_path(exchange, symbol, tf, cache_dir=CACHE_DIR):
    key = "binanceusdm" if exchange == "binance" and ":" in symbol else exchange  # один и тот же рынок -> один кеш
    return os.path.join(cache_dir, f"{key}_{symbol.replace('/', '').replace(':', '_')}_{tf}.csv")


def read_cache(path):
    """Свечи из CSV-кеша [(ts,o,h,l,c)] или [], если файла нет."""
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return [tuple(float(x) if i else int(x) for i, x in enumerate(r)) for r in csv.reader(f)]


def _meta_path(path):
    return path + ".meta.json"


def _read_requested_since(path):
    """С какой даты просили историю при полной загрузке (если актив появился на бирже позже — кеш начинается позже запрошенного)."""
    try:
        with open(_meta_path(path), encoding="utf-8") as f:
            return int(json.load(f)["requested_since"])
    except (OSError, ValueError, KeyError):
        return None


# ---------- загрузка отдельных окон (топ-коины: момент сделки + N суток) ----------
def tf_to_ms(tf):
    """'1m' -> 60000, '4h' -> 14400000 (без обращения к ccxt)."""
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    try:
        return int(tf[:-1]) * units[tf[-1]] * 1000
    except (ValueError, KeyError, IndexError):
        raise ValueError(f"Неизвестный таймфрейм '{tf}'")


def _windows_path(path):
    return path + ".windows.json"


def covered_intervals(path, tf_ms):
    """Какие интервалы (мс, начала свечей, включительно) уже есть в кеше: карта окон, а если её нет — весь файл целиком."""
    try:
        with open(_windows_path(path), encoding="utf-8") as f:
            return merge_intervals([tuple(x) for x in json.load(f)["covered"]], tf_ms)
    except (OSError, ValueError, KeyError):
        pass
    candles = read_cache(path)
    return [(candles[0][0], candles[-1][0])] if candles else []


def merge_intervals(intervals, tf_ms):
    """Склеивает пересекающиеся и соседние (встык по свечам) интервалы."""
    out = []
    for a, b in sorted(intervals):
        if out and a <= out[-1][1] + tf_ms:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def missing_intervals(covered, a, b, tf_ms):
    """Части [a, b], которых нет в covered: [(начало, конец)], границы — начала свечей."""
    gaps, cur = [], a
    for x, y in merge_intervals(covered, tf_ms):
        if y < cur:
            continue
        if x > b:
            break
        if x > cur:
            gaps.append((cur, min(x - tf_ms, b)))
        cur = max(cur, y + tf_ms)
        if cur > b:
            break
    if cur <= b:
        gaps.append((cur, b))
    return gaps


def last_closed_start(tf_ms, now_ms=None):
    """Начало последней завершённой свечи (свеча, которая ещё формируется, не качается и не кешируется)."""
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    return now_ms // tf_ms * tf_ms - tf_ms


def fetch_window(exchange, symbol, tf, start_ms, end_ms, cache_dir=CACHE_DIR, progress=None):
    """Докачивает в кеш свечи [start_ms, end_ms] монеты, не трогая уже скачанное (карта окон `*.windows.json`).
    Возвращает число скачанных свечей. Окно, ещё не наступившее целиком, качается до последней завершённой свечи."""
    import ccxt

    os.makedirs(cache_dir, exist_ok=True)
    path = cache_path(exchange, symbol, tf, cache_dir)
    ex = getattr(ccxt, exchange)({"enableRateLimit": True})
    tf_ms = ex.parse_timeframe(tf) * 1000
    end_eff = min(end_ms, last_closed_start(tf_ms))
    covered = covered_intervals(path, tf_ms)
    gaps = missing_intervals(covered, start_ms, end_eff, tf_ms) if start_ms <= end_eff else []
    if not gaps:
        return 0
    have = {c[0]: c for c in read_cache(path)}
    downloaded = 0
    new_cover = list(covered)
    for gx, gy in gaps:
        cur = gx
        while cur <= gy:
            for attempt in range(8):
                try:
                    batch = ex.fetch_ohlcv(symbol, tf, since=cur, limit=1000)
                    break
                except (ccxt.RateLimitExceeded, ccxt.NetworkError, ccxt.RequestTimeout) as e:
                    time.sleep(min(60, 5 * 2 ** attempt))
            else:
                _save_window(path, have, new_cover, tf_ms)      # что успели — сохраняем, остальное докачается в следующий раз
                raise RuntimeError(f"биржа не отвечает при загрузке {symbol}")
            batch = [tuple(c[:5]) for c in batch if cur <= c[0] <= gy]
            if not batch:
                break
            for c in batch:
                have[c[0]] = c
            downloaded += len(batch)
            cur = batch[-1][0] + tf_ms
            if progress:
                progress(downloaded, batch[-1][0])
            time.sleep(0.2)
        new_cover.append((gx, gy))       # пустой кусок тоже считаем закрытым: монеты тогда ещё не было на бирже
    _save_window(path, have, new_cover, tf_ms)
    return downloaded


def _save_window(path, have, covered, tf_ms):
    rows = [have[t] for t in sorted(have)]
    with open(path + ".tmp", "w", newline="") as f:
        csv.writer(f).writerows(rows)
    os.replace(path + ".tmp", path)
    with open(_windows_path(path) + ".tmp", "w", encoding="utf-8") as f:
        json.dump({"covered": [list(x) for x in merge_intervals(covered, tf_ms)]}, f)
    os.replace(_windows_path(path) + ".tmp", _windows_path(path))


def fetch_candles(exchange, symbol, tf, since_ms, cache_dir=CACHE_DIR, progress=None):
    """Скачивает свечи [since_ms, now] с кешем в CSV. Возвращает [(ts,o,h,l,c)].
    progress(n_свечей, ts_последней_мс) вызывается по ходу загрузки (для фоновых задач).""" 
    import ccxt

    os.makedirs(cache_dir, exist_ok=True)
    key = "binanceusdm" if exchange == "binance" and ":" in symbol else exchange
    path = cache_path(exchange, symbol, tf, cache_dir)
    name = os.path.basename(path)
    candles = []
    legacy = os.path.join(cache_dir, name.replace(key + "_", "binance_", 1))  # кеш старых запусков с --exchange binance
    if not os.path.exists(path) and key != exchange and os.path.exists(legacy):
        shutil.copy(legacy, path)
    if os.path.exists(path):
        candles = read_cache(path)[:-1]  # последняя могла быть записана незавершённой старой версией — перекачаем
    ex = getattr(ccxt, exchange)({"enableRateLimit": True})
    tf_ms = ex.parse_timeframe(tf) * 1000
    # докачиваем хвост; если кеш начинается позже нужного, качаем заново с нуля
    asked = _read_requested_since(path)
    if candles and candles[0][0] > since_ms + tf_ms and not (asked is not None and asked <= since_ms):
        print(f"  кеш {os.path.basename(path)} начинается позже нужного — качаю заново")
        candles = []
    elif candles:
        print(f"  кеш {os.path.basename(path)}: {len(candles)} свечей, докачиваю хвост")
    else:
        print(f"  кеша нет ({os.path.basename(path)}), качаю с начала")
    if not candles:
        with open(_meta_path(path), "w", encoding="utf-8") as f:   # запоминаем, с какой даты просили (актив мог появиться позже)
            json.dump({"requested_since": since_ms}, f)
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
        if progress:
            try:
                progress(len(candles), batch[-1][0])
            except BaseException:          # загрузку прервали (например, командой отмены): сохраняем скачанное, оно не пропадёт
                save()
                raise
        if pages % 20 == 0:
            save()
        time.sleep(0.3)  # мягче к лимитам, чем enableRateLimit по умолчанию
    print()
    save()
    try:
        os.remove(_windows_path(path))        # кеш теперь сплошной, карта «окон» больше не нужна
    except OSError:
        pass
    return candles


MAX_TPS = 3
LIMIT_PENDING = "лимитка ждёт исполнения"       # сигнал подтверждён, ордер ещё не исполнен, а данные кончились (в бэктесте — не пропуск, а ожидание)


def normalize_tps(tps):
    """Проверяет тейки частями: [(расстояние от входа в %, доля позиции в %), ...] -> [(pct, доля 0..1)].
    От 1 до 3 уровней, расстояния строго растут, доли > 0 и в сумме дают 100%."""
    levels = [(float(p), float(s)) for p, s in tps]
    if not 1 <= len(levels) <= MAX_TPS:
        raise ValueError(f"Тейков должно быть от 1 до {MAX_TPS}")
    if any(p <= 0 for p, _ in levels):
        raise ValueError("Расстояние до тейка должно быть больше 0%")
    if any(b <= a for (a, _), (b, _) in zip(levels, levels[1:])):
        raise ValueError("Каждый следующий тейк должен быть дальше предыдущего")
    if any(s <= 0 for _, s in levels):
        raise ValueError("Доля позиции на каждом тейке должна быть больше 0%")
    if abs(sum(s for _, s in levels) - 100) > 1e-6:
        raise ValueError(f"Доли тейков должны давать в сумме 100% (сейчас {sum(s for _, s in levels):g}%)")
    return [(p, s / 100) for p, s in levels]


def _scan(candles, i, d, entry, tps, sl, slip, be, hold=None, liq=None, until=None, no_tp_first=False):
    """Ход позиции, открытой на open свечи i. tps: [(цена, доля)]. Возвращает события [(idx, цена, доля, метка)],
    метки: TP1..TP3, SL, BE (стоп в безубытке), LIQ (ликвидация), WKND (закрытие по расписанию — конец торговой недели), OPEN (данные кончились).

    until (мс) — необязательно: на первой свече, начавшейся не раньше этого момента, остаток закрывается по её open (режим «только будни»).

    liq=(цена ликвидации, цена банкротства) — необязательно: касание цены ликвидации закрывает остаток по цене банкротства (теряется вся маржа ноги: -100%/плечо
    от объёма). Если стоп ближе к входу, чем ликвидация, раньше сработает он; если стоп дальше (или снят из-за hold) — раньше ликвидация.

    Порядок внутри свечи (внутри минуты он неизвестен, поэтому выбираем худшее для результата):
      1. сначала проверяется действующий стоп (SL, а после первого тейка при be — вход): если задет, остаток закрыт по нему;
      2. затем по порядку исполняются тейки, до которых дошла цена (при гэпе — по цене открытия);
      3. если в этой свече исполнился первый тейк и стоп перенесён в безубыток, а цена также коснулась входа,
         считаем, что возврат к входу был после тейка: остаток закрыт в безубытке.

    hold(ts_свечи_мс) -> bool (необязательно): пока истинно (метрики всё ещё удовлетворяют условию входа), СТОП-ЛОСС не действует; как только
    стало ложно, он действует с этой свечи (если цена уже за стопом — выход по её open). Стоп в безубытке после TP1 это не касается."""
    stop, stop_label = sl, "SL"
    remaining, k_tp, events = 1.0, 0, []
    for k in range(i, len(candles)):
        _, o, h, l, _ = candles[k]
        if until is not None and candles[k][0] >= until:
            events.append((k, o * (1 - slip) if d == 1 else o * (1 + slip), remaining, "WKND"))
            return events
        stop_on = not (hold is not None and stop_label == "SL" and hold(candles[k][0]))
        stop_hit = stop_on and ((l <= stop) if d == 1 else (h >= stop))
        if liq is not None and ((l <= liq[0]) if d == 1 else (h >= liq[0])) and not (stop_hit and ((stop >= liq[0]) if d == 1 else (stop <= liq[0]))):
            events.append((k, liq[1], remaining, "LIQ"))
            return events
        if stop_hit:
            px = min(stop, o) * (1 - slip) if d == 1 else max(stop, o) * (1 + slip)
            events.append((k, px, remaining, stop_label))
            return events
        be_now = False
        while k_tp < len(tps) and not (no_tp_first and k == i):
            lvl, share = tps[k_tp]
            if not ((h >= lvl) if d == 1 else (l <= lvl)):
                break
            px = max(lvl, o) if d == 1 else min(lvl, o)
            take = remaining if k_tp == len(tps) - 1 else min(share, remaining)  # последний тейк забирает остаток
            events.append((k, px, take, f"TP{k_tp + 1}"))
            remaining -= take
            k_tp += 1
            if k_tp == 1 and be and len(tps) > 1:
                stop, stop_label, be_now = entry, "BE", True
        if remaining <= 1e-12:
            return events
        if be_now and ((l <= stop) if d == 1 else (h >= stop)):
            events.append((k, stop * (1 - slip) if d == 1 else stop * (1 + slip), remaining, "BE"))
            return events
    events.append((len(candles) - 1, candles[-1][4], remaining, "OPEN"))
    return events


def _finalize(trade, events, candles, fee, size, notional=None):
    """Итог сделки по списку событий (частичных и финального закрытия): PnL = сумма долей × доходность - комиссия за вход и выход."""
    d = 1 if trade["side"] == "long" else -1
    entry = trade["entry"]
    gross = sum(s * d * (p / entry - 1) for _, p, s, _ in events)
    pnl = gross - 2 * fee
    last = events[-1]
    label = last[3]
    total = sum(s for _, _, s, _ in events)
    tp1 = next((e for e in events if e[3] == "TP1"), None)
    trade.update(
        exit=last[1], exit_avg=sum(s * p for _, p, s, _ in events) / total, exit_ts=candles[last[0]][0],
        reason="TP" if label.startswith("TP") else label,
        tp_hits=sum(e[3].startswith("TP") for e in events),
        be_ts=candles[tp1[0]][0] if (tp1 and trade["be"]) else None,
        fills=[{"ts": candles[idx][0], "price": p, "share": s, "label": lab} for idx, p, s, lab in events],
        _events=events, _j=last[0],
        pnl_pct=pnl * 100, pnl_usd=pnl * (notional if notional else entry * size),
    )


def apply_only(local_min, scalp_min, short, only):
    """Режим одной метрики: вторая метрика не учитывается ни для лонга, ни для шорта."""
    inf = float("inf")
    if only == "local":
        scalp_min = -inf
        short = None if short is None else (short[0], inf)
    elif only == "scalp":
        local_min = -inf
        short = None if short is None else (inf, short[1])
    return local_min, scalp_min, short


def resolve_entry(local_min, scalp_min, global_min, short, short_global_max, only):
    """Пороги входа с учётом режима «только одна метрика»: остальные метрики не участвуют ни для лонга, ни для шорта.
    only: None | 'local' | 'scalp' | 'global'. Возвращает (local_min, scalp_min, global_min, short, short_global_max)."""
    inf = float("inf")
    if only == "global":
        local_min = scalp_min = -inf
        short = None if short is None else (inf, inf)
        return local_min, scalp_min, global_min, short, short_global_max
    local_min, scalp_min, short = apply_only(local_min, scalp_min, short, only)
    if only in ("local", "scalp"):
        global_min = short_global_max = None
    return local_min, scalp_min, global_min, short, short_global_max


class AvgPosition:
    """Позиция с усреднением (пирамидинг «усредняет позу»): доборы входят в ОДНУ позицию, и после каждого добора тейки, стоп и безубыток
    считаются от новой средней цены входа. Позиция идёт по свечам постепенно (advance), потому что добор приходит в середине её жизни.

    Размеры задаются весами w (в % депозита): первый вход w0, добор — w1. Количество = вес / цена; средняя цена A = стоимость / количество.
    Тейк k закрывает долю share_k / (сумма долей ещё не исполненных тейков) от ОСТАТКА позиции, поэтому без доборов результат совпадает
    с обычной одиночной сделкой, а после добора остаток делится между оставшимися тейками в прежних пропорциях.
    Порядок внутри свечи и правила исполнения — те же, что в _scan (стоп → тейки → возврат в безубыток в той же свече, гэпы по open, hold)."""

    def __init__(self, candles, d, i0, price0, w0, levels, sl_pct, slip, be, hold=None, lev0=1.0, mmr=None, until=None):
        self.c, self.d, self.n = candles, d, len(candles)
        self.until = until               # закрыть остаток на первой свече не раньше этого момента (конец торговой недели)
        self.mmr = mmr                   # доля поддерживающей маржи (None — ликвидация не моделируется)
        self.M = w0 / (lev0 or 1.0)      # маржа в тех же весах: вес / плечо
        self.k = i0                      # следующая необработанная свеча
        self.A, self.q, self.W = price0, w0 / price0, w0
        self.levels, self.sl_pct, self.slip, self.be, self.hold = levels, sl_pct, slip, be, hold
        self.shares = [s for _, s in levels]
        self.k_tp, self.label = 0, "SL"
        self.fills = []                  # (индекс свечи, цена, количество, средняя цена на момент исполнения, метка)
        self.adds = [(i0, price0, w0)]
        self.done = False
        self.q0 = self.q
        self.no_tp_k = None              # свеча, в которой тейки не засчитываются (исполнение лимитного входа)

    def add(self, i, price, w, lev=None):
        """Добор по open свечи i (все предыдущие свечи уже обработаны): новая средняя цена, количество и вес (вес — объём, т.е. доля × плечо)."""
        self.M += w / (lev or 1.0)
        q_new = w / price
        self.A = (self.q * self.A + w) / (self.q + q_new)
        self.q += q_new
        self.W += w
        self.q0 += q_new
        self.adds.append((i, price, w))

    def prices(self):
        return [self.A * (1 + self.d * p / 100) for p, _ in self.levels]

    def sl(self):
        return self.A * (1 - self.d * self.sl_pct / 100)

    def liq(self):
        """(цена ликвидации, цена банкротства) объединённой позиции: эффективное плечо = объём / маржа, маржа изолированная."""
        if self.mmr is None:
            return None
        r = self.M / self.W
        return self.A * (1 - self.d * (r - self.mmr)), self.A * (1 - self.d * r)

    def _close(self, k, px, qty, label):
        self.fills.append((k, px, qty, self.A, label))
        self.q -= qty

    def advance(self, until):
        """Обрабатывает свечи [self.k, until). Возвращает True, если позиция закрыта."""
        d, slip = self.d, self.slip
        while not self.done and self.k < min(until, self.n):
            k = self.k
            _, o, h, l, _c = self.c[k]
            if self.until is not None and self.c[k][0] >= self.until:
                self._close(k, o * (1 - slip) if d == 1 else o * (1 + slip), self.q, "WKND")
                self.done = True
                break
            stop = self.A if self.label == "BE" else self.sl()
            stop_on = not (self.hold is not None and self.label == "SL" and self.hold(self.c[k][0]))
            stop_hit = stop_on and ((l <= stop) if d == 1 else (h >= stop))
            liq = self.liq()
            if liq is not None and ((l <= liq[0]) if d == 1 else (h >= liq[0])) and not (stop_hit and ((stop >= liq[0]) if d == 1 else (stop <= liq[0]))):
                self._close(k, liq[1], self.q, "LIQ")
                self.done = True
                break
            if stop_hit:
                px = min(stop, o) * (1 - slip) if d == 1 else max(stop, o) * (1 + slip)
                self._close(k, px, self.q, self.label)
                self.done = True
                break
            be_now = False
            while self.k_tp < len(self.levels) and k != self.no_tp_k:
                lvl = self.A * (1 + d * self.levels[self.k_tp][0] / 100)
                if not ((h >= lvl) if d == 1 else (l <= lvl)):
                    break
                px = max(lvl, o) if d == 1 else min(lvl, o)
                tail = sum(self.shares[self.k_tp:])
                qty = self.q if self.k_tp == len(self.levels) - 1 else self.q * self.shares[self.k_tp] / tail
                self._close(k, px, qty, f"TP{self.k_tp + 1}")
                self.k_tp += 1
                if self.k_tp == 1 and self.be and len(self.levels) > 1:
                    self.label, be_now = "BE", True
            if self.q <= 1e-12 * self.q0:
                self.done = True
                break
            if be_now and ((l <= self.A) if d == 1 else (h >= self.A)):
                self._close(k, self.A * (1 - slip) if d == 1 else self.A * (1 + slip), self.q, "BE")
                self.done = True
                break
            self.k += 1
        return self.done

    def close_market(self, k, price, label="REV"):
        """Закрыть весь остаток по цене (разворот): price уже со сдвигом на проскальзывание."""
        self._close(k, price, self.q, label)
        self.done = True

    def finalize(self, trade, fee, size, notional=None):
        """Записывает итог в словарь сделки; если данные кончились раньше, остаток закрывается по close последней свечи (метка OPEN)."""
        if not self.done:
            self._close(self.n - 1, self.c[-1][4], self.q, "OPEN")
            self.done = True
        d = self.d
        realized = sum(d * qty * (px - a) for _, px, qty, a, _ in self.fills)
        pnl = realized / self.W - 2 * fee
        last = self.fills[-1]
        qty_sum = sum(f[2] for f in self.fills)
        tp1 = next((f for f in self.fills if f[4] == "TP1"), None)
        label = last[4]
        trade.update(
            entry=self.A, tp=self.prices()[-1], tps=self.prices(), sl=self.sl(),
            exit=last[1], exit_avg=sum(f[1] * f[2] for f in self.fills) / qty_sum, exit_ts=self.c[last[0]][0],
            reason="TP" if label.startswith("TP") else label,
            tp_hits=sum(f[4].startswith("TP") for f in self.fills),
            be_ts=self.c[tp1[0]][0] if (tp1 and trade["be"]) else None,
            fills=[{"ts": self.c[k][0], "price": px, "share": qty * a / self.W, "label": lab} for k, px, qty, a, lab in self.fills],
            adds=[{"ts": self.c[k][0], "price": px, "weight": w, "signal_ts": None} for k, px, w in self.adds],
            adds_n=len(self.adds) - 1, w_total=self.W, avg=True,
            _events=[], _j=last[0],
            pnl_pct=pnl * 100, pnl_usd=pnl * (notional if notional else self.A * size),
        )


def run_events(events, candles, levels, sl_pct, fee=0.00055, size=0.1, slip=0.0, cooldown_h=0.0, be_after_tp1=False, notional=None,
               pyramid=None, signal_gap_h=0.0, hold=None, skipped=None, leverage=1.0, add_leverage=None, mmr=None, week=None, resume=None, entry_limit=None):
    """Прогоняет готовые события входа [(ts_мс, +1 лонг / -1 шорт)] по свечам. levels — результат normalize_tps. notional (USD) задаёт размер
    позиции в долларах вместо size (в единицах актива).

    Позиция — группа «ног» (legs): первая нога открывается по сигналу, остальные — доборы (пирамидинг). У каждой ноги свои вход, тейки и стоп
    от её собственной цены входа; ноги закрываются независимо. Без пирамидинга нога всегда одна (одна позиция одновременно).
      сигнал в ту же сторону, пока позиция открыта: игнорируется, а при pyramid={"max": N, "gap_h": H} — добор (не больше N доборов за позицию
        и не чаще, чем раз в H часов после открытия последней ноги; необязательно "move_pct": X, "move_dir": "any"|"favor"|"against" — цена
        должна уйти от цены последнего входа не меньше чем на X% (в любую сторону / в сторону позиции / против неё)); при "avg": True (с весами "w0" и "w", % депозита) доборы усредняют одну
        позицию (AvgPosition): тейки и стоп пересчитываются от новой средней цены;
      сигнал в противоположную сторону: разворот — все открытые ноги закрываются по open свечи входа (исполненные тейки остаются);
      cooldown_h — пауза после стопа; signal_gap_h — минимум часов между открытиями позиций (включая развороты);
      leverage / add_leverage (плечо первой ноги / доборов; доборы по умолчанию с тем же плечом) и mmr (доля поддерживающей маржи, 0.005 = 0.5%) нужны, только
        чтобы моделировать ликвидацию (mmr задан): иначе плечо на ход сделки не влияет, а размер считается после (apply_sizing);
      week={"before_min": X} — режим «только будни»: вход (и добор, и разворот) разрешён с открытия Токио в понедельник до закрытия Нью-Йорка в пятницу (core/tradingweek.py),
        остальные сигналы пропускаются; позиция, оставшаяся открытой, закрывается по open свечи закрытия пятницы (метка WKND), раньше на X минут;
      hold(d, ts_мс) -> bool — «стоп-лосс не ставить, пока метрики сильны» (см. _scan);
      entry_limit={"pct": X, "dir": "pullback"|"breakout", "ttl_h": H} — первая нога позиции входит НЕ по рынку, а ордером на X % движения цены от цены сигнала (open свечи входа):
        pullback — лимитка на откат (лонг ниже, шорт выше): исполняется, когда цена дошла до уровня (по уровню, а если свеча открылась за ним — по open); breakout — вход по ходу
        (лонг выше, шорт ниже, стоп-вход): исполняется при касании уровня, с проскальзыванием. Ордер живёт ttl_h часов (и до конца торгового окна в режиме «только будни»); не исполнился — сигнал
        пропускается. Пока ордер стоит, повторные сигналы той же стороны игнорируются, обратный — отменяет его. В свече исполнения тейки не засчитываются (порядок внутри свечи неизвестен).
        Доборы пирамидинга по-прежнему идут по рынку;
      skipped — список, в который дописываются (ts, сторона, причина) пропущенных сигналов;
      resume={"group": номер последней уже закрытой позиции, "last_group": её сделки, "last_open_ts": время её открытия} — продолжение после закрытых позиций: events
        должны начинаться с первого события после них, возвращаются только новые сделки (паузы после стопа и между сигналами учитывают сохранённую позицию)."""
    ts = [c[0] for c in candles]
    n = len(candles)
    trades, open_legs = [], []
    group, last_group, last_open_ts, last_leg_ts, last_leg_px, adds = 0, [], None, None, None, 0
    avg = bool(pyramid and pyramid.get("avg"))             # доборы усредняют одну позицию (AvgPosition), а не живут отдельными ногами
    avg_pos = avg_trade = None
    if resume:
        group, last_group, last_open_ts = resume["group"], list(resume["last_group"]), resume["last_open_ts"]
    levels_shares = [s for _, s in levels]
    be = bool(be_after_tp1 and len(levels) > 1)

    def skip(sts, side, reason):
        if skipped is not None:
            skipped.append((sts, side, reason))

    def fin_avg():
        avg_pos.finalize(avg_trade, fee, size, notional)
        for a, sig_ts in zip(avg_trade["adds"][1:], avg_trade["add_signals"]):
            a["signal_ts"] = sig_ts

    def open_leg(sts, d, side, i, leg_no, entry_px=None, no_tp_first=False):
        entry = entry_px if entry_px is not None else candles[i][1] * (1 + d * slip)
        prices = [entry * (1 + d * p / 100) for p, _ in levels]
        sl = entry * (1 - d * sl_pct / 100)
        leg = {"side": side, "signal_ts": sts, "entry_ts": candles[i][0], "entry": entry, "tp": prices[-1], "tps": prices,
               "tp_shares": levels_shares, "sl": sl, "be": be, "leg": leg_no, "group": group, "pyramid": leg_no > 1, "_i": i}
        hold_d = (lambda t: hold(d, t)) if hold else None
        lev = leverage if leg_no == 1 else (add_leverage or leverage)
        liq = (entry * (1 - d * (1 / lev - mmr)), entry * (1 - d / lev)) if mmr is not None else None
        until = tradingweek.window_close(candles[i][0], week["before_min"]) if week else None
        _finalize(leg, _scan(candles, i, d, entry, list(zip(prices, levels_shares)), sl, slip, be, hold_d, liq, until, no_tp_first), candles, fee, size, notional)
        trades.append(leg)
        open_legs.append(leg)
        return leg

    lim = entry_limit if (entry_limit and entry_limit.get("pct")) else None
    pend_state, wait_order = {}, None                   # состояние до открытия ноги (чтобы отмена лимитки его вернула) и «ордер ждёт, а данные кончились»

    def limit_fill(i, d):
        """Исполнение лимитного входа по сигналу на свече i -> ("fill", k, цена) | ("expired", None, None) | ("wait", None, None) — данные кончились раньше срока ордера."""
        ref = candles[i][1]
        pct, mode = lim["pct"] / 100.0, lim.get("dir", "pullback")
        lvl = ref * (1 - d * pct) if mode == "pullback" else ref * (1 + d * pct)
        end_ts = candles[i][0] + lim["ttl_h"] * 3.6e6
        for k in range(i, n):
            tk = candles[k][0]
            if tk > end_ts or (week and not tradingweek.in_window(tk, week["before_min"])):
                return "expired", None, None
            _, o, h, l, _c = candles[k]
            if mode == "pullback":
                if (l <= lvl) if d == 1 else (h >= lvl):
                    return "fill", k, (min(lvl, o) if d == 1 else max(lvl, o))                        # свеча открылась за уровнем — исполнение по open (лучше уровня)
            elif (h >= lvl) if d == 1 else (l <= lvl):
                return "fill", k, (max(lvl, o) * (1 + slip) if d == 1 else min(lvl, o) * (1 - slip))     # стоп-вход: гэп — по open, плюс проскальзывание
        return "wait", None, None

    for sts, d in events:
        side = "long" if d == 1 else "short"
        i = bisect_left(ts, sts)
        if i >= n:
            skip(sts, side, "нет свечи входа")
            continue
        if week and not tradingweek.in_window(candles[i][0], week["before_min"]):
            skip(sts, side, "выходные: вне торгового окна")
            continue
        if wait_order is not None:                          # ордер ждёт исполнения до конца данных
            if wait_order == side:
                skip(sts, side, "лимитка уже стоит")
                continue
            wait_order = None                               # обратный сигнал отменил ожидающий ордер
        pend = [leg for leg in open_legs if leg.get("_i", -1) > i]
        if pend:                                            # нога ещё не исполнена (лимитка стоит)
            if pend[0]["side"] == side:
                skip(sts, side, "лимитка уже стоит")
                continue
            for leg in pend:                                # обратный сигнал отменяет лимитку: возвращаем состояние, как будто её не было
                trades.remove(leg)
                open_legs.remove(leg)
                skip(leg["signal_ts"], leg["side"], "лимитка отменена: пришёл обратный сигнал")
                group, last_group, last_open_ts, last_leg_ts, last_leg_px, adds = pend_state.pop(id(leg))
                if leg is avg_trade:
                    avg_pos = avg_trade = None
        if avg_pos is not None and open_legs:
            if avg_pos.advance(i):                          # позиция закрылась сама до этого сигнала
                fin_avg()
                open_legs.clear()
        else:
            still = [leg for leg in open_legs if leg["_j"] >= i]
            if len(still) != len(open_legs):
                open_legs[:] = still
        if not open_legs:
            # позиции нет (закрылась сама по SL/TP): паузы перед новым открытием
            last = max(last_group, key=lambda t: t["exit_ts"]) if last_group else None
            if cooldown_h and last and last["reason"] == "SL" and sts < last["exit_ts"] + cooldown_h * 3.6e6:
                skip(sts, side, "пауза после стопа")
                continue
            if signal_gap_h and last_open_ts is not None and sts < last_open_ts + signal_gap_h * 3.6e6:
                skip(sts, side, "пауза между сигналами")
                continue
        elif open_legs[0]["side"] == side:
            if not pyramid:
                skip(sts, side, "позиция уже открыта")
                continue
            if adds >= pyramid["max"]:
                skip(sts, side, "достигнут максимум доборов")
                continue
            if pyramid.get("gap_h") and sts < last_leg_ts + pyramid["gap_h"] * 3.6e6:
                skip(sts, side, "пауза между доборами")
                continue
            if pyramid.get("move_pct") and last_leg_px:
                mv = d * (candles[i][1] / last_leg_px - 1) * 100       # движение цены от последнего входа: + в сторону позиции, − против
                mode = pyramid.get("move_dir", "any")
                if not ((mv >= pyramid["move_pct"]) if mode == "favor" else (-mv >= pyramid["move_pct"]) if mode == "against" else (abs(mv) >= pyramid["move_pct"])):
                    skip(sts, side, "мало движения от последнего входа")
                    continue
            adds += 1
            if avg:
                avg_pos.add(i, candles[i][1] * (1 + d * slip), pyramid["w"], add_leverage or leverage)
                avg_trade["add_signals"].append(sts)
            else:
                open_leg(sts, d, side, i, adds + 1)
                last_group.append(trades[-1])
            last_leg_ts, last_leg_px = candles[i][0], candles[i][1]
            continue
        else:
            if signal_gap_h and last_open_ts is not None and sts < last_open_ts + signal_gap_h * 3.6e6:
                skip(sts, side, "пауза между сигналами")
                continue
            # разворот на open свечи входа: исполненные тейки остаются, остаток каждой ноги закрывается по рынку
            o = candles[i][1]
            if avg_pos is not None:
                avg_pos.close_market(i, o * (1 - slip) if avg_trade["side"] == "long" else o * (1 + slip))
                fin_avg()
            else:
                for cur in open_legs:
                    kept = [e for e in cur["_events"] if e[0] < i]
                    rest = 1.0 - sum(e[2] for e in kept)
                    _finalize(cur, kept + [(i, o * (1 - slip) if cur["side"] == "long" else o * (1 + slip), rest, "REV")], candles, fee, size, notional)
            open_legs.clear()
        fill_i, fill_px, wait_h = i, None, 0.0
        if lim:
            kind, fk, fpx = limit_fill(i, d)
            if kind == "expired":
                skip(sts, side, f"лимитка не исполнилась за {lim['ttl_h']:g} ч")
                continue
            if kind == "wait":
                skip(sts, side, LIMIT_PENDING)
                wait_order = side
                continue
            fill_i, fill_px, wait_h = fk, fpx, (candles[fk][0] - candles[i][0]) / 3.6e6
        saved = (group, last_group, last_open_ts, last_leg_ts, last_leg_px, adds)
        group += 1
        adds = 0
        if avg:
            entry = fill_px if fill_px is not None else candles[i][1] * (1 + d * slip)
            hold_d = (lambda t, d=d: hold(d, t)) if hold else None
            avg_pos = AvgPosition(candles, d, fill_i, entry, pyramid["w0"], levels, sl_pct, slip, be, hold_d, leverage, mmr,
                                  tradingweek.window_close(candles[fill_i][0], week["before_min"]) if week else None)
            if lim:
                avg_pos.no_tp_k = fill_i
            avg_trade = {"side": side, "signal_ts": sts, "entry_ts": candles[fill_i][0], "entry_first": entry, "be": be, "leg": 1, "group": group,
                         "pyramid": False, "add_signals": [], "tp_shares": levels_shares, "_j": n, "_events": [], "_i": fill_i}
            trades.append(avg_trade)
            open_legs.append(avg_trade)
            leg = avg_trade
        else:
            leg = open_leg(sts, d, side, fill_i, 1, entry_px=fill_px, no_tp_first=bool(lim))
        if lim:
            leg["limit"] = {"level": fill_px, "ref": candles[i][1], "dir": lim.get("dir", "pullback"), "wait_h": wait_h}
            pend_state[id(leg)] = saved
        last_group = [leg]
        last_open_ts = last_leg_ts = candles[fill_i][0]
        last_leg_px = fill_px if fill_px is not None else candles[i][1]
    if avg_pos is not None and "exit_ts" not in avg_trade:  # данные кончились, пока позиция открыта
        avg_pos.advance(n)
        fin_avg()
    return trades


def simulate(signals, candles, local_min, scalp_min, tp_pct, sl_pct, fee=0.00055, size=0.1, slip=0.0, confirm=1,
             short=None, liq=None, cool=None, cooldown_h=0.0, only=None, tps=None, be_after_tp1=False, event_filter=None,
             pyramid=None, signal_gap_h=0.0, sl_hold_strong=False, skipped=None, global_min=None, short_global_max=None,
             leverage=1.0, add_leverage=None, mmr=None, week=None):
    """global_min / short_global_max — пороги Deep Global (None — не учитывается; в режиме only='global' учитывается только он).
    short=(local_max, scalp_max) включает шорты; liq/cool — фильтры входа (см. entries.py).
    tps — тейки частями [(% от цены входа, % позиции)], 1–3 уровня (по умолчанию один тейк tp_pct на 100%);
    be_after_tp1 — после первого тейка переносить стоп в цену входа.
    Одна позиция одновременно: сигнал в ту же сторону игнорируется, в противоположную — разворот
    (закрытие по open свечи входа + новая позиция)."""
    local_min, scalp_min, global_min, short, short_global_max = resolve_entry(local_min, scalp_min, global_min, short, short_global_max, only)
    levels = normalize_tps(tps if tps else [(tp_pct, 100)])
    events = entries(signals, local_min, scalp_min, confirm, short, liq, cool, global_min, short_global_max)
    if event_filter:
        events = [e for e in events if event_filter(*e)]
    hold = strong_checker(signals, local_min, scalp_min, short, global_min, short_global_max) if sl_hold_strong else None
    return run_events(events, candles, levels, sl_pct, fee, size, slip, cooldown_h, be_after_tp1,
                      pyramid=pyramid, signal_gap_h=signal_gap_h, hold=hold, skipped=skipped, leverage=leverage, add_leverage=add_leverage, mmr=mmr, week=week)


def strong_checker(signals, local_min, scalp_min, short, global_min=None, short_global_max=None):
    """hold(d, ts_мс) -> выполняется ли СЕЙЧАС условие входа по последнему отчёту не позже ts_мс (для лонга — пороги лонга, для шорта — шорта).
    Пороги уже учитывают режим «только Local / только Scalp» (apply_only): лишняя метрика не участвует. Нет отчёта — не сильны."""
    rep_ts = [s[0] for s in signals]
    def sig(s):
        return {"local": s[1], "scalp": s[2], "global": s[7] if len(s) > 7 else None}
    long_ok = [should_long(sig(s), local_min, scalp_min, global_min) for s in signals]
    short_ok = [should_short(sig(s), *short, short_global_max) if short is not None else False for s in signals]

    def hold(d, t):
        k = bisect_right(rep_ts, t) - 1
        return k >= 0 and (long_ok if d == 1 else short_ok)[k]
    return hold


def stats(trades):
    if not trades:
        return {"trades": 0, "tp": 0, "sl": 0, "be": 0, "open": 0, "time": 0, "rev": 0, "liq": 0, "wknd": 0, "winrate": 0, "total_usd": 0,
                "avg_pct": 0, "profit_factor": 0, "max_dd_usd": 0}
    wins = [t["pnl_usd"] for t in trades if t["pnl_usd"] > 0]
    losses = [-t["pnl_usd"] for t in trades if t["pnl_usd"] <= 0]
    eq = peak = dd = 0.0
    for t in sorted(trades, key=lambda x: (x.get("exit_ts", 0), x.get("entry_ts", 0))):      # с доборами ноги перекрываются: капитал растёт по времени закрытия
        eq += t["pnl_usd"]
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return {"trades": len(trades),
            "tp": sum(t["reason"] == "TP" for t in trades),
            "sl": sum(t["reason"] == "SL" for t in trades),
            "be": sum(t["reason"] == "BE" for t in trades),
            "open": sum(t["reason"] == "OPEN" for t in trades),
            "time": sum(t["reason"] == "TIME" for t in trades),
            "rev": sum(t["reason"] == "REV" for t in trades),
            "liq": sum(t["reason"] == "LIQ" for t in trades),
            "wknd": sum(t["reason"] == "WKND" for t in trades),
            "winrate": 100 * len(wins) / len(trades),
            "total_usd": eq,
            "avg_pct": sum(t["pnl_pct"] for t in trades) / len(trades),
            "profit_factor": sum(wins) / sum(losses) if losses else float("inf"),
            "max_dd_usd": dd}


def fmt(s):
    return (f"сделок {s['trades']} (TP {s['tp']} / SL {s['sl']} / БУ {s['be']} / разворот {s['rev']} / открыта {s['open']}), "
            f"winrate {s['winrate']:.1f}%, итого {s['total_usd']:+.0f} $, "
            f"средняя {s['avg_pct']:+.3f}%, PF {s['profit_factor']:.2f}, макс. просадка {s['max_dd_usd']:.0f} $")


def wilson_low(wins, n, z=1.96):
    """Нижняя граница 95% доверительного интервала для доли побед — штраф за малую выборку."""
    if n == 0:
        return 0.0
    p = wins / n
    return ((p + z * z / (2 * n)) - z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)) / (1 + z * z / n)


def filter_grid(kind, side="both"):
    # для шорта «остывание» зеркально: метрика растёт от минимума серии
    verb, way = {"long": ("упала", "вниз"), "short": ("выросла", "вверх")}.get(side, ("упала/выросла", "вниз/вверх"))
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
                grid.append((f"cool {metric:5} {verb} на >= {drop}", dict(cool=dict(metric=metric, drop=drop))))
            for k_ in (1, 2, 3):
                grid.append((f"cool {metric:5} {k_} отчёт(а) подряд {way}", dict(cool=dict(metric=metric, declines=k_))))
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


def run_filter_sweep(kind, signals, candles, base_kw, short, min_trades, side="both"):
    split_ts = signals[len(signals) // 2][0]
    rows = [make_row(name, simulate(signals, candles, short=short, **base_kw, **extra), split_ts)
            for name, extra in [("БАЗА (без фильтра)", {})] + filter_grid(kind, side)]
    print_table(rows)
    print_top(rows, min_trades, "без фильтра")


def run_threshold_sweep(side, signals, candles, common, a):
    """Свип порогов одной стороны; вторая сторона отключена, чтобы не смешивать результаты.
    С --only local/scalp перебирается только одна метрика."""
    split_ts = signals[len(signals) // 2][0]
    never = float("inf")  # лонг (Local/Scalp >= inf) никогда не срабатывает
    only = common.get("only")
    if side == "long":
        base = (a.local_min, a.scalp_min)
        locals_ = (1.25, 1.45, 1.65, 1.85, 2.05, 2.25, 2.5, 2.75, 3)
        scalps_ = (1.25, 1.5, 1.75, 2.0, 2.25, 2.5, 2.75, 3)
        run = lambda p: simulate(signals, candles, p[0], p[1], short=None, **common)
        label = lambda p: (f"long  local>={p[0]:.2f}" if only == "local" else f"long  scalp>={p[1]:.2f}" if only == "scalp"
                           else f"long  local>={p[0]:.2f} scalp>={p[1]:.2f}")
    else:
        base = (a.short_local_max, a.short_scalp_max)
        locals_ = (0.5, 0.6, 0.7, 0.85, 0.95, 1.05, 1.15, 1.25)
        scalps_ = (0.35, 0.5, 0.65, 0.8, 0.95, 1.05, 1.2)
        run = lambda p: simulate(signals, candles, never, never, short=p, **common)
        label = lambda p: (f"short local<={p[0]:.2f}" if only == "local" else f"short scalp<={p[1]:.2f}" if only == "scalp"
                           else f"short local<={p[0]:.2f} scalp<={p[1]:.2f}")
    if only == "local":
        grid = [(lm, base[1]) for lm in locals_]
    elif only == "scalp":
        grid = [(base[0], sm) for sm in scalps_]
    else:
        grid = [(lm, sm) for lm in locals_ for sm in scalps_]
    rows = [make_row(f"БАЗА: {label(base)}", run(base), split_ts)]
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
    ap.add_argument("--tps", help="тейки частями 'расстояние%:доля%,...', до 3, доли в сумме 100, например 3:50,6:50; заменяет --tp")
    ap.add_argument("--be-after-tp1", action="store_true", help="после первого тейка переносить стоп в цену входа (безубыток)")
    ap.add_argument("--sl", type=float, default=2.0)
    ap.add_argument("--fee", type=float, default=0.0005, help="taker-комиссия за сторону, доля")
    ap.add_argument("--slip", type=float, default=0.0, help="проскальзывание, доля")
    ap.add_argument("--short-local-max", type=float, default=0.7)
    ap.add_argument("--short-scalp-max", type=float, default=1.2)
    ap.add_argument("--no-short", action="store_true", help="только лонги")
    ap.add_argument("--side", choices=["both", "long", "short"], default="both",
                    help="для фильтр-свипов (--sweep-liq/-cool/-cooldown), --explain и одиночного прогона: торговать обе стороны, "
                         "только лонги или только шорты")
    ap.add_argument("--only", choices=["local", "scalp"], help="торговать только по одной метрике (вторая игнорируется)")
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
    if a.only:
        print(f"РЕЖИМ: только Deep {a.only.capitalize()} (вторая метрика игнорируется)")
    candles = fetch_candles(a.exchange, a.symbol, a.tf, signals[0][0])
    tps = None
    if a.tps:
        try:
            tps = [(float(x.split(":")[0]), float(x.split(":")[1])) for x in a.tps.split(",")]
            normalize_tps(tps)
        except (ValueError, IndexError) as e:
            raise SystemExit(f"--tps: {e}. Формат: расстояние%:доля%,... например 3:50,6:50")
    common = dict(tp_pct=a.tp, sl_pct=a.sl, fee=a.fee, size=a.size, slip=a.slip, confirm=a.confirm, only=a.only,
                  tps=tps, be_after_tp1=a.be_after_tp1)
    short = None if a.no_short else (a.short_local_max, a.short_scalp_max)

    if a.sweep_long or a.sweep_short:
        for side, on in (("long", a.sweep_long), ("short", a.sweep_short)):
            if on:
                run_threshold_sweep(side, signals, candles, common, a)
        return
    if a.side == "long":
        short = None
    elif a.side == "short":
        if short is None:
            raise SystemExit("--side short несовместим с --no-short")
        a.local_min = a.scalp_min = float("inf")   # лонг (Local/Scalp >= inf) никогда не срабатывает
    if a.side != "both":
        print(f"СТОРОНА: только {a.side}")
    if a.sweep_liq or a.sweep_cool or a.sweep_cooldown:
        base_kw = dict(local_min=a.local_min, scalp_min=a.scalp_min, **common)
        for kind, on in (("liq", a.sweep_liq), ("cool", a.sweep_cool), ("cd", a.sweep_cooldown)):
            if on:
                run_filter_sweep(kind, signals, candles, base_kw, short, a.min_trades, a.side)
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
            w = csv.DictWriter(f, fieldnames=[x for x in trades[0] if not x.startswith('_') and not isinstance(trades[0][x], list)],
                               extrasaction='ignore')
            w.writeheader()
            w.writerows(trades)
        print(f"Сделки -> {a.out}")


if __name__ == "__main__":
    main()
