"""Фоновая подгрузка свечей: при запуске сервера и затем каждые N минут (по умолчанию 5) для выбранных активов.
Докачивается только хвост (fetch_candles инкрементальный), поэтому запрос лёгкий; первый раз по активу — полная загрузка."""
import threading
import time


class CandleSync:
    def __init__(self, symbols_fn, fetch_fn, interval_min=5.0, enabled=True, skip_fn=None, on_updated=None, disabled_reason=None):
        self.symbols_fn, self._fetch, self._skip, self._on_updated = symbols_fn, fetch_fn, skip_fn, on_updated
        self.interval = max(interval_min, 0.02) * 60
        self.enabled, self.disabled_reason = enabled, (None if enabled else disabled_reason)
        self._wake, self._stop, self._thread = threading.Event(), threading.Event(), None
        self.running = False
        self.next_run = None
        self.state = {}

    def start(self):
        if self.enabled and not self._thread:
            self._thread = threading.Thread(target=self._loop, name="candle-sync", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def trigger(self):
        if not self.enabled or self.running:
            return False
        self._wake.set()
        return True

    def _loop(self):
        while not self._stop.is_set():
            self.run_once()
            self.next_run = time.time() + self.interval
            self._wake.wait(self.interval)
            self._wake.clear()

    def run_once(self):
        self.running = True
        try:
            for sym in self.symbols_fn():
                st = self.state.setdefault(sym, {"status": "idle", "last_run": None, "last_ok": None, "error": None})
                if self._skip and self._skip(sym):          # по активу уже идёт ручная загрузка
                    continue
                st.update(status="running", last_run=time.time(), error=None)
                try:
                    self._fetch(sym)
                    st.update(status="ok", last_ok=time.time())
                    if self._on_updated:
                        self._on_updated(sym)
                except BaseException as e:                  # fetch_candles завершается SystemExit при недоступной бирже
                    st.update(status="error", error=str(e) or type(e).__name__)
        finally:
            self.running = False

    def snapshot(self):
        return {"enabled": self.enabled, "reason": self.disabled_reason, "running": self.running, "interval_min": self.interval / 60,
                "next_run": self.next_run, "symbols": [{"symbol": k, **v} for k, v in self.state.items()]}
