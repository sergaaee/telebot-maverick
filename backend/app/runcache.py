"""Кеш промежуточных результатов расчёта стратегии, чтобы при новых данных считать только их, а не всю историю заново.

Для каждой стратегии (ключ — все параметры, влияющие на ход сделок, + источник, источник ликвидаций и актив) хранится снимок:
  ent   — состояние генератора входов (core/entries.py) после последнего отчёта,
  trend — свечи таймфрейма и EMA фильтра тренда (core/trend.py),
  sim   — уже закрытые сделки (без размеров позиции — они считаются заново) и пропущенные сигналы.
Снимок годится, только если данные, по которым он посчитан, остались началом нынешних (проверка по контрольным точкам: последние строки, равномерная выборка,
первая строка): отчёты и свечи только дописываются. Любое расхождение (докачана история, изменён файл посередине) — считаем заново.

Снимки лежат в памяти процесса и в файлах CACHE_DIR/runcache/*.pkl: так бот и веб-сервер, это разные процессы, пользуются расчётами друг друга.
RESULT_CACHE=off отключает кеш целиком."""
import hashlib
import json
import logging
import os
import pickle
import threading

log = logging.getLogger("runcache")
VERSION = 1
MEM_SIZE = 64
DISK_SIZE = 400
TAIL = 300                      # последние строки сверяются все: ликвидации из другого источника могут «доехать» к последним отчётам позже
SAMPLES = 64

# параметры, которые не влияют на сделки до расчёта размера позиции и топ-коинов — смена депозита и т.п. не сбрасывает снимок
IGNORED = {"deposit", "compound"}


def enabled():
    return os.getenv("RESULT_CACHE", "").strip().lower() not in ("off", "0", "false", "no")


def make_key(eff, source_id, liq_id, symbol):
    p = {k: v for k, v in eff.items() if k not in IGNORED and not k.startswith("top_")}
    p.update(_src=source_id, _liq=liq_id, _sym=symbol)
    return hashlib.sha1(json.dumps(p, sort_keys=True, default=str).encode()).hexdigest()


def probe(rows):
    n = len(rows)
    if not n:
        return []
    idx = set(range(max(0, n - TAIL), n)) | {0} | {i * n // SAMPLES for i in range(SAMPLES)}
    return [(i, tuple(rows[i])) for i in sorted(idx)]


def matches(pr, rows, n):
    """Первые n строк rows — те же, по которым снят probe."""
    if n > len(rows) or not pr:
        return False
    try:
        return all(tuple(rows[i]) == r for i, r in pr)
    except IndexError:
        return False


class RunCache:
    def __init__(self, directory=None):
        self.dir = os.path.join(str(directory), "runcache") if directory else None
        self._mem = {}                               # ключ -> (mtime файла | None, снимок)
        self._lock = threading.Lock()

    def _path(self, key):
        return os.path.join(self.dir, key + ".pkl") if self.dir else None

    def get(self, key):
        p = self._path(key)
        try:
            m = os.path.getmtime(p) if p else None
        except OSError:
            m = None
        with self._lock:
            hit = self._mem.get(key)
            if hit and (m is None or hit[0] is None or hit[0] >= m):
                self._mem[key] = self._mem.pop(key)                     # свежий — в конец
                return hit[1]
        if m is None:
            return None
        try:
            with open(p, "rb") as f:
                snap = pickle.load(f)
        except Exception as e:                       # повреждённый или чужой файл — как будто кеша нет
            log.warning("runcache: не прочитать %s: %s", p, e)
            return None
        if not isinstance(snap, dict) or snap.get("version") != VERSION:
            return None
        self._remember(key, m, snap)
        return snap

    def put(self, key, snap, to_disk=True):
        snap["version"] = VERSION
        m = None
        p = self._path(key)
        if p and to_disk:
            try:
                os.makedirs(self.dir, exist_ok=True)
                tmp = f"{p}.{os.getpid()}.{threading.get_ident()}.tmp"
                with open(tmp, "wb") as f:
                    pickle.dump(snap, f, protocol=pickle.HIGHEST_PROTOCOL)
                os.replace(tmp, p)
                m = os.path.getmtime(p)
                self._trim()
            except Exception as e:
                log.warning("runcache: не записать %s: %s", p, e)
        self._remember(key, m, snap)

    def _remember(self, key, m, snap):
        with self._lock:
            self._mem.pop(key, None)
            self._mem[key] = (m, snap)
            while len(self._mem) > MEM_SIZE:
                self._mem.pop(next(iter(self._mem)))

    def _trim(self):
        try:
            names = [n for n in os.listdir(self.dir) if n.endswith(".pkl")]
            if len(names) <= DISK_SIZE:
                return
            names.sort(key=lambda n: os.path.getmtime(os.path.join(self.dir, n)))
            for n in names[:len(names) - DISK_SIZE]:
                os.remove(os.path.join(self.dir, n))
        except OSError:
            pass


_caches, _glock = {}, threading.Lock()


def for_dir(directory):
    k = str(directory)
    with _glock:
        c = _caches.get(k)
        if c is None:
            c = _caches[k] = RunCache(directory)
        return c
