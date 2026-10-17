"""Кеш результатов бэктеста: тот же конфиг на тех же данных не считается повторно.

Ключ — параметры + «отпечаток» всех данных, от которых зависит результат (файлы истории и свечей: время изменения и размер) +
отпечаток кода движка. Когда приходят новые отчёты или свечи, отпечаток меняется и конфиг досчитывается заново на свежих данных;
старые записи вытесняются. Хранится в памяти (быстро) и на диске data/results (переживает перезапуск, общий для сайта и бота)."""
import gzip
import hashlib
import json
import os
import threading
from collections import OrderedDict
from pathlib import Path

# файлы, от которых зависит логика расчёта: при их изменении (обновление сервера) прежние результаты недействительны
_CODE = ("core/backtest.py", "core/entries.py", "core/signal_parser.py", "core/topcoins.py", "core/trend.py", "service.py", "schemas.py")


def code_fingerprint():
    h = hashlib.sha1()
    base = Path(__file__).resolve().parent
    for name in _CODE:
        try:
            h.update((base / name).read_bytes())
        except OSError:
            h.update(name.encode())
    return h.hexdigest()[:12]


class ResultCache:
    def __init__(self, directory=None, mem_size=64, disk_files=300):
        self.dir = Path(directory) if directory else None
        self.mem_size, self.disk_files = mem_size, disk_files
        self._mem = OrderedDict()
        self._lock = threading.Lock()
        self._key_locks = {}
        self._writes = 0
        self.hits = self.disk_hits = self.misses = 0
        self.code = code_fingerprint()

    def key(self, *parts):
        raw = json.dumps([self.code, *parts], sort_keys=True, default=str, ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()

    def _path(self, key):
        return self.dir / f"{key}.json.gz" if self.dir else None

    def _disk_get(self, key):
        p = self._path(key)
        if not p:
            return None
        try:
            with gzip.open(p, "rt", encoding="utf-8") as f:
                data = json.load(f)
            os.utime(p)                                   # «использован недавно» — для вытеснения
            return data
        except (OSError, ValueError, EOFError):
            return None                                   # нет файла или он повреждён — просто посчитаем заново

    def _disk_put(self, key, value):
        p = self._path(key)
        if not p:
            return
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(f".tmp{os.getpid()}{threading.get_ident()}")
            with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=3) as f:
                json.dump(value, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, p)
            self._writes += 1
            if self._writes % 10 == 1:
                self._evict()
        except OSError:
            pass                                          # кеш — оптимизация: сбой записи не должен ломать расчёт

    def _evict(self):
        try:
            files = sorted(self.dir.glob("*.json.gz"), key=lambda f: f.stat().st_mtime)
            for f in files[: max(0, len(files) - self.disk_files)]:
                f.unlink(missing_ok=True)
        except OSError:
            pass

    def get_or_compute(self, key, compute):
        """-> (результат, откуда: 'memory' | 'disk' | 'computed'). Одинаковые одновременные запросы считаются один раз."""
        with self._lock:
            if key in self._mem:
                self._mem.move_to_end(key)
                self.hits += 1
                return self._mem[key], "memory"
            klock = self._key_locks.setdefault(key, threading.Lock())
        with klock:
            with self._lock:
                if key in self._mem:                      # пока ждали, другой поток уже посчитал
                    self.hits += 1
                    return self._mem[key], "memory"
            value, src = self._disk_get(key), "disk"
            if value is None:
                value, src = compute(), "computed"
                self._disk_put(key, value)
                self.misses += 1
            else:
                self.disk_hits += 1
            with self._lock:
                self._mem[key] = value
                while len(self._mem) > self.mem_size:
                    self._mem.popitem(last=False)
                self._key_locks.pop(key, None)
            return value, src

    def stats(self):
        return {"memory_entries": len(self._mem), "hits": self.hits, "disk_hits": self.disk_hits, "misses": self.misses}
