"""Загрузка и кеширование данных в памяти: отчёты (сигналы) и свечи."""
import os
import threading
from bisect import bisect_left, bisect_right

from .core import backtest
from .core.entries import attach_liq, load_signals

from .config import Settings

# интервалы для автоматического выбора таймфрейма графика, секунды
TIMEFRAMES = [60, 300, 900, 1800, 3600, 7200, 14400, 21600, 43200, 86400, 259200, 604800]


def _mtime(p):
    try:
        return os.path.getmtime(p)
    except OSError:
        return None


class DataStore:
    def __init__(self, s: Settings):
        self.s = s
        self._lock = threading.Lock()
        self._sig = (None, None)        # (ключ-mtime, данные)
        self._candles = (None, None)
        self._ts = []

    # ---------- отчёты ----------
    def signals(self):
        s = self.s
        key = (_mtime(s.history_file), _mtime(s.liq_file) if s.liq_file else None)
        with self._lock:
            if self._sig[0] == key and self._sig[1] is not None:
                return self._sig[1]
            if key[0] is None:
                raise FileNotFoundError(f"Нет файла с историей: {s.history_file}")
            sigs = load_signals(str(s.history_file))
            hit = None
            if s.liq_file and key[1] is not None:
                sigs, hit = attach_liq(sigs, load_signals(str(s.liq_file)), s.liq_max_age_h)
            self._sig = (key, (sigs, hit))
            return self._sig[1]

    # ---------- свечи ----------
    @property
    def candle_path(self):
        return backtest.cache_path(self.s.exchange, self.s.symbol, self.s.candle_tf, str(self.s.cache_dir))

    def candles(self):
        path = self.candle_path
        key = _mtime(path)
        with self._lock:
            if self._candles[0] == key and self._candles[1] is not None:
                return self._candles[1]
            if key is None:
                raise FileNotFoundError(f"Нет кеша свечей: {path}")
            data = backtest.read_cache(path)
            self._candles = (key, data)
            self._ts = [c[0] for c in data]
            return data

    def candle_ts(self):
        self.candles()
        return self._ts

    def reload_candles(self):
        with self._lock:
            self._candles = (None, None)

    # ---------- окно для графика ----------
    def window(self, start_ms, end_ms, max_bars):
        """Свечи в [start_ms, end_ms], агрегированные до таймфрейма, при котором баров не больше max_bars."""
        candles = self.candles()
        ts = self._ts
        native = (ts[1] - ts[0]) // 1000 if len(ts) > 1 else 60
        lo = bisect_left(ts, start_ms) if start_ms is not None else 0
        hi = bisect_right(ts, end_ms) if end_ms is not None else len(ts)
        span_s = max(((ts[hi - 1] - ts[lo]) / 1000) if hi > lo else 0, 1)
        tf = next((t for t in TIMEFRAMES if t >= native and span_s / t <= max_bars), TIMEFRAMES[-1])
        if tf == native:
            bars = [[c[0] // 1000, c[1], c[2], c[3], c[4]] for c in candles[lo:hi]]
            return tf, bars
        step = tf * 1000
        bars, cur = [], None
        for i in range(lo, hi):
            t, o, h, l, c = candles[i]
            b = t // step * step
            if cur is None or cur[0] != b:
                if cur:
                    bars.append(cur)
                cur = [b, o, h, l, c]
            else:
                if h > cur[2]:
                    cur[2] = h
                if l < cur[3]:
                    cur[3] = l
                cur[4] = c
        if cur:
            bars.append(cur)
        for b in bars:
            b[0] //= 1000
        return tf, bars

    def info(self):
        out = {"signals": None, "candles": None}
        try:
            sigs, hit = self.signals()
            out["signals"] = {
                "count": len(sigs), "first_ts": sigs[0][0], "last_ts": sigs[-1][0],
                "has_liq": any(x[3] is not None for x in sigs), "liq_matched": hit,
                "file": str(self.s.history_file), "liq_file": str(self.s.liq_file) if self.s.liq_file else None,
            }
        except FileNotFoundError as e:
            out["signals_error"] = str(e)
        try:
            c = self.candles()
            out["candles"] = {"count": len(c), "first_ts": c[0][0], "last_ts": c[-1][0], "tf": self.s.candle_tf,
                              "file": self.candle_path}
        except FileNotFoundError as e:
            out["candles_error"] = str(e)
        return out
