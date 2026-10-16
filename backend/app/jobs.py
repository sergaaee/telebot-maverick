"""Фоновые задачи (загрузка свечей актива): очередь с одним рабочим потоком, чтобы не упереться в лимиты биржи."""
import itertools
import queue
import threading
import time
from dataclasses import asdict, dataclass, field


@dataclass
class Job:
    id: int
    symbol: str
    status: str = "queued"          # queued | running | done | error
    count: int = 0                  # сколько свечей скачано к этому моменту
    last_ts: "int | None" = None    # время последней скачанной свечи, мс
    error: "str | None" = None
    created_at: float = field(default_factory=time.time)
    started_at: "float | None" = None
    finished_at: "float | None" = None
    acked: bool = False             # пользователь уже видел уведомление о завершении

    def to_dict(self):
        return asdict(self)


class JobManager:
    """runner(symbol, progress) выполняется в рабочем потоке; progress(count, last_ts) обновляет прогресс задачи."""

    def __init__(self, runner, on_done=None, keep=50):
        self._runner, self._on_done, self._keep = runner, on_done, keep
        self._q = queue.Queue()
        self._ids = itertools.count(1)
        self._jobs = {}
        self._lock = threading.Lock()
        self._worker = None

    def submit(self, symbol):
        """Ставит загрузку актива в очередь. Если по символу уже идёт/ждёт задача — возвращает её. -> (job, создана_ли_новая)"""
        with self._lock:
            for j in self._jobs.values():
                if j.symbol == symbol and j.status in ("queued", "running"):
                    return j, False
            job = Job(id=next(self._ids), symbol=symbol)
            self._jobs[job.id] = job
            self._trim()
            self._q.put(job.id)
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._loop, name="candle-jobs", daemon=True)
                self._worker.start()
            return job, True

    def _trim(self):
        done = [j for j in self._jobs.values() if j.status in ("done", "error")]
        for j in sorted(done, key=lambda x: x.finished_at or 0)[: max(0, len(done) - self._keep)]:
            self._jobs.pop(j.id, None)

    def _loop(self):
        while True:
            try:
                job_id = self._q.get(timeout=30)
            except queue.Empty:
                return                      # простаивающий поток завершается, при новой задаче запустится заново
            job = self._jobs.get(job_id)
            if job is None:
                continue
            job.status, job.started_at = "running", time.time()

            def progress(count, last_ts, job=job):
                job.count, job.last_ts = count, last_ts

            try:
                self._runner(job.symbol, progress)
                job.status = "done"
            except BaseException as e:      # SystemExit из fetch_candles («биржа не отвечает») тоже считаем ошибкой задачи
                job.status, job.error = "error", str(e) or type(e).__name__
            finally:
                job.finished_at = time.time()
                if self._on_done:
                    try:
                        self._on_done(job)
                    except Exception:
                        pass

    def get(self, job_id):
        return self._jobs.get(job_id)

    def list(self):
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.id)

    def active(self, symbol=None):
        return [j for j in self.list() if j.status in ("queued", "running") and (symbol is None or j.symbol == symbol)]

    def ack(self, job_id):
        j = self._jobs.get(job_id)
        if j:
            j.acked = True
        return j
