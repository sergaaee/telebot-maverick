"""Загрузка и кеширование данных в памяти: отчёты (сигналы) и свечи."""
import os
import threading
from bisect import bisect_left, bisect_right

from .core import backtest
from .core.entries import attach_liq, load_signals

from .config import Settings

# интервалы для автоматического выбора таймфрейма графика, секунды
UI_TIMEFRAMES = [60, 300, 900, 1800, 3600, 14400, 86400]   # что предлагает переключатель в интерфейсе
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
        self._raw = {}                  # id источника -> (mtime, [отчёты])
        self._sig = {}                  # (источник, источник ликвидаций) -> (ключ, данные)
        self._candles = {}              # символ -> (mtime, свечи, времена)

    # ---------- отчёты ----------
    def source(self, source_id):
        src = self.s.sources.get(source_id)
        if src is None:
            raise KeyError(f"Неизвестный источник '{source_id}'. Доступны: {', '.join(self.s.sources)}")
        return src

    def _raw_signals(self, source_id):
        src = self.source(source_id)
        m = _mtime(src.file)
        if m is None:
            raise FileNotFoundError(f"Нет файла с историей источника «{src.label}»: {src.file}")
        with self._lock:
            cached = self._raw.get(source_id)
            if cached and cached[0] == m:
                return cached[1]
        data = load_signals(str(src.file))
        with self._lock:
            self._raw[source_id] = (m, data)
        return data

    def signals(self, source_id=None, liq_source_id=None):
        """Отчёты источника; ликвидации берутся из него самого или (liq_source_id) из другого источника.
        Возвращает (отчёты, сколько отчётов получили ликвидации из другого источника | None)."""
        source_id = source_id or self.s.default_source
        liq_id = liq_source_id if liq_source_id and liq_source_id != source_id else None
        sigs = self._raw_signals(source_id)
        if liq_id is None:
            return sigs, None
        key = (id(sigs), id(self._raw_signals(liq_id)))
        with self._lock:
            cached = self._sig.get((source_id, liq_id))
            if cached and cached[0] == key:
                return cached[1]
        merged = attach_liq(sigs, self._raw_signals(liq_id), self.s.liq_max_age_h)
        with self._lock:
            self._sig[(source_id, liq_id)] = (key, merged)
        return merged

    def first_signal_ts(self):
        """Самый ранний отчёт среди всех доступных источников — с него качаем свечи."""
        firsts = []
        for sid in self.s.sources:
            try:
                firsts.append(self._raw_signals(sid)[0][0])
            except FileNotFoundError:
                pass
        if not firsts:
            raise FileNotFoundError("Нет ни одного файла истории")
        return min(firsts)

    # ---------- свечи ----------
    def _symbol(self, symbol):
        symbol = symbol or self.s.symbol
        if symbol not in self.s.symbols:
            raise KeyError(f"Неизвестный актив '{symbol}'. Доступны: {', '.join(self.s.symbols)}")
        return symbol

    def candle_path(self, symbol=None):
        return backtest.cache_path(self.s.exchange, self._symbol(symbol), self.s.candle_tf, str(self.s.cache_dir))

    def _load(self, symbol):
        symbol = self._symbol(symbol)
        path = self.candle_path(symbol)
        key = _mtime(path)
        with self._lock:
            c = self._candles.get(symbol)
            if c and c[0] == key and key is not None:
                return c
        if key is None:
            raise FileNotFoundError(f"Нет кеша свечей для {symbol}: {path}")
        data = backtest.read_cache(path)
        entry = (key, data, [x[0] for x in data])
        with self._lock:
            self._candles[symbol] = entry
        return entry

    def candles(self, symbol=None):
        return self._load(symbol)[1]

    def candle_ts(self, symbol=None):
        return self._load(symbol)[2]

    def reload_candles(self, symbol=None):
        with self._lock:
            self._candles.pop(self._symbol(symbol), None)

    # ---------- окно для графика ----------
    def native_tf(self, symbol=None):
        ts = self.candle_ts(symbol)
        return (ts[1] - ts[0]) // 1000 if len(ts) > 1 else 60

    def timeframes(self, symbol=None):
        """Таймфреймы (сек), которые можно выбрать в интерфейсе: не меньше родного."""
        native = self.native_tf(symbol)
        return [t for t in UI_TIMEFRAMES if t >= native]

    def window(self, start_ms, end_ms, max_bars, tf=None, symbol=None):
        """Свечи в [start_ms, end_ms]. Без tf таймфрейм подбирается автоматически (баров не больше max_bars);
        с tf (сек) свечи агрегируются до него, а при превышении max_bars остаётся последняя часть окна.
        Возвращает (tf, bars, truncated)."""
        candles = self.candles(symbol)
        ts = self.candle_ts(symbol)
        native = self.native_tf(symbol)
        lo = bisect_left(ts, start_ms) if start_ms is not None else 0
        hi = bisect_right(ts, end_ms) if end_ms is not None else len(ts)
        span_s = max(((ts[hi - 1] - ts[lo]) / 1000) if hi > lo else 0, 1)
        truncated = False
        if tf is None:
            tf = next((t for t in TIMEFRAMES if t >= native and span_s / t <= max_bars), TIMEFRAMES[-1])
        else:
            tf = max(int(tf), native)
            if span_s / tf > max_bars:          # окно слишком широкое для этого ТФ — оставляем последние max_bars баров
                truncated = True
                lo = max(lo, bisect_left(ts, ts[hi - 1] - max_bars * tf * 1000)) if hi > lo else lo
        if tf == native:
            bars = [[c[0] // 1000, c[1], c[2], c[3], c[4]] for c in candles[lo:hi]]
            return tf, bars, truncated
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
        return tf, bars, truncated

    def info(self):
        out = {"sources": []}
        for src in self.s.sources.values():
            row = {"id": src.id, "label": src.label, "file": str(src.file), "exists": src.file.exists(), "count": 0,
                   "first_ts": None, "last_ts": None, "has_liq": False}
            try:
                sigs = self._raw_signals(src.id)
                row.update(count=len(sigs), first_ts=sigs[0][0] if sigs else None, last_ts=sigs[-1][0] if sigs else None,
                           has_liq=any(x[3] is not None for x in sigs))
            except FileNotFoundError:
                pass
            out["sources"].append(row)
        out["assets"] = []
        for sym in self.s.symbols:
            row = {"symbol": sym, "count": 0, "first_ts": None, "last_ts": None, "tf": self.s.candle_tf,
                   "file": self.candle_path(sym), "error": None}
            try:
                c = self.candles(sym)
                row.update(count=len(c), first_ts=c[0][0], last_ts=c[-1][0])
            except FileNotFoundError as e:
                row["error"] = str(e)
            out["assets"].append(row)
        return out
