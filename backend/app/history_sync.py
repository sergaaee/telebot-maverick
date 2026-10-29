"""Фоновая подгрузка новых сообщений Telegram-ботов в файлы истории: при запуске сервера и затем либо в заданные минуты каждого часа по UTC (по умолчанию в :02 и :32 —
раз в полчаса, ровно когда боты публикуют отчёты), либо, если расписание не задано, каждые sync_min минут.

Источники обрабатываются по очереди (одна сессия Telegram). Ошибка одного источника не останавливает остальные и не роняет сервер:
причина сохраняется в статусе и видна в интерфейсе."""
import threading
import time
from datetime import datetime, timedelta, timezone

DEFAULT_AT = (2, 32)


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


def default_exporter(path, chat):
    """Реальная подгрузка через Telethon (без интерактивного входа). -> число добавленных сообщений."""
    import asyncio

    from tools import export_history

    return asyncio.run(export_history.main(str(path), chat, full=False, interactive=False))


class HistorySync:
    def __init__(self, sources, interval_min=32.0, exporter=None, on_updated=None, enabled=True, disabled_reason=None, at_minutes=None):
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
            self.run_once()
            self.next_run = next_slot(time.time(), self.at_minutes) if self.at_minutes else time.time() + self.interval
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
