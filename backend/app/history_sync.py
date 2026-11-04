"""Фоновая подгрузка новых сообщений Telegram-ботов в файлы истории: при запуске сервера и затем либо в заданные минуты каждого часа по UTC (расписание at_minutes, например :02 и :32),
либо, если расписание не задано (по умолчанию), каждые sync_min минут — по умолчанию КАЖДУЮ МИНУТУ (отсчёт от начала прошлой подгрузки, без накопления сдвига).
Сессия Telegram (файл SQLite) одна на сайт и бота: пока подгружает один процесс, другой ждёт (блокировка файла), иначе при подгрузке раз в минуту они сталкивались бы («database is locked»).

Источники обрабатываются по очереди (одна сессия Telegram). Ошибка одного источника не останавливает остальные и не роняет сервер:
причина сохраняется в статусе и видна в интерфейсе."""
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

DEFAULT_INTERVAL_MIN = 1.0                 # как часто подгружать, если расписание по минутам часа не задано
LOCK_WAIT_SEC = 120.0                      # сколько ждать, пока другой процесс закончит подгрузку


def parse_minutes(raw):
    """'2,32' -> [2, 32]; пусто или мусор -> [] (расписание не задано). Минуты 0–59, без повторов."""
    out = set()
    for x in str(raw or "").replace(";", ",").split(","):
        x = x.strip().lstrip(":")
        if x.isdigit() and 0 <= int(x) <= 59:
            out.add(int(x))
    return sorted(out)


def next_slot(now, minutes):
    """Ближайший момент (unix, секунды) строго после now, у которого минута часа (UTC) входит в minutes, а секунды равны 0."""
    base = datetime.fromtimestamp(now, timezone.utc).replace(minute=0, second=0, microsecond=0)
    for hours in (0, 1):
        for m in minutes:
            t = (base + timedelta(hours=hours, minutes=m)).timestamp()
            if t > now:
                return t
    raise ValueError("нет минут расписания")


@contextmanager
def session_lock(root, wait=LOCK_WAIT_SEC):
    """Межпроцессная блокировка на время работы с сессией Telegram (на системах без fcntl блокировки нет). Не дождались за wait секунд — RuntimeError (статус «ошибка», повтор в следующий раз)."""
    try:
        import fcntl
    except ImportError:
        yield
        return
    f = open(os.path.join(str(root), ".history_sync.lock"), "a")
    try:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise RuntimeError("подгрузка занята другим процессом (сайт или бот) дольше двух минут — повторю в следующий раз")
                time.sleep(0.2)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    finally:
        f.close()


def default_exporter(path, chat):
    """Реальная подгрузка через Telethon (без интерактивного входа). -> число добавленных сообщений."""
    import asyncio

    from tools import export_history

    with session_lock(export_history.ROOT):
        return asyncio.run(export_history.main(str(path), chat, full=False, interactive=False))


class HistorySync:
    def __init__(self, sources, interval_min=DEFAULT_INTERVAL_MIN, exporter=None, on_updated=None, enabled=True, disabled_reason=None, at_minutes=None):
        self.sources = [s for s in sources if s.chat]
        self.interval = max(interval_min, 0.05) * 60
        self.at_minutes = sorted(set(at_minutes or []))             # непусто — по расписанию (минуты часа, UTC), период interval_min не используется
        self._export = exporter or default_exporter
        self._on_updated = on_updated
        self.enabled = enabled and bool(self.sources)
        self.disabled_reason = disabled_reason if not enabled else (None if self.sources else "ни у одного источника не задан чат (SOURCE_<ID>_CHAT)")
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self.running = False
        self.next_run = None
        self.state = {s.id: {"status": "idle", "last_run": None, "last_ok": None, "added": 0, "added_total": 0, "error": None} for s in self.sources}

    # ---------- управление ----------
    def start(self):
        if not self.enabled or self._thread:
            return
        self._thread = threading.Thread(target=self._loop, name="history-sync", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def trigger(self):
        """Запустить подгрузку прямо сейчас. -> False, если она уже идёт или отключена."""
        if not self.enabled or self.running:
            return False
        self._wake.set()
        return True

    # ---------- цикл ----------
    def _loop(self):
        while not self._stop.is_set():
            started = time.time()
            self.run_once()
            now = time.time()
            # по периоду — от начала прошлой подгрузки (ровно раз в период); если сама подгрузка шла дольше периода, пауза всё равно не нулевая (десятая часть периода, не больше 5 с)
            self.next_run = next_slot(now, self.at_minutes) if self.at_minutes else max(started + self.interval, now + min(self.interval * 0.1, 5.0))
            self._wake.wait(max(0.0, self.next_run - time.time()))
            self._wake.clear()

    def run_once(self):
        with self._lock:
            self.running = True
        try:
            for s in self.sources:
                st = self.state[s.id]
                st.update(status="running", error=None)
                st["last_run"] = time.time()
                try:
                    added = int(self._export(s.file, s.chat) or 0)
                    st.update(status="ok", added=added, last_ok=time.time(), error=None)
                    st["added_total"] += added
                    if added and self._on_updated:
                        self._on_updated(s)
                except BaseException as e:          # SystemExit («чат не найден») и ошибки сети тоже не должны ронять поток
                    st.update(status="error", added=0, error=str(e) or type(e).__name__)
        finally:
            with self._lock:
                self.running = False

    def snapshot(self):
        return {
            "enabled": self.enabled, "reason": self.disabled_reason, "running": self.running,
            "interval_min": (30.0 if len(self.at_minutes) == 2 and (self.at_minutes[1] - self.at_minutes[0]) == 30 else self.interval / 60) if self.at_minutes else self.interval / 60, "next_run": self.next_run,
            "schedule": ("в " + " и ".join(f":{m:02d}" for m in self.at_minutes) + " каждого часа (UTC)") if self.at_minutes else None,
            "sources": [{"id": s.id, "label": s.label, "chat": s.chat, **self.state[s.id]} for s in self.sources],
        }


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None
