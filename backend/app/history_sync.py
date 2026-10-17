"""Фоновая подгрузка новых сообщений Telegram-ботов в файлы истории: при запуске сервера и затем каждые sync_min минут.

Источники обрабатываются по очереди (одна сессия Telegram). Ошибка одного источника не останавливает остальные и не роняет сервер:
причина сохраняется в статусе и видна в интерфейсе."""
import threading
import time
from datetime import datetime, timezone


def default_exporter(path, chat):
    """Реальная подгрузка через Telethon (без интерактивного входа). -> число добавленных сообщений."""
    import asyncio

    from tools import export_history

    return asyncio.run(export_history.main(str(path), chat, full=False, interactive=False))


class HistorySync:
    def __init__(self, sources, interval_min=32.0, exporter=None, on_updated=None, enabled=True, disabled_reason=None):
        self.sources = [s for s in sources if s.chat]
        self.interval = max(interval_min, 0.05) * 60
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
            self.next_run = time.time() + self.interval
            self._wake.wait(self.interval)
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
            "interval_min": self.interval / 60, "next_run": self.next_run,
            "sources": [{"id": s.id, "label": s.label, "chat": s.chat, **self.state[s.id]} for s in self.sources],
        }


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None
