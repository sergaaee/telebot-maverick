"""Бэкенд для e2e: настоящий API, но «биржа» и загрузчик свечей поддельные — без сети и с управляемой задержкой.

Запуск: cd backend && python -m uvicorn server:app --app-dir ../e2e (так делает e2e/run.py)."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.config import load_settings  # noqa: E402
from app.core import backtest  # noqa: E402
from app.main import create_app  # noqa: E402
from app.markets import MarketsCache  # noqa: E402

EXCHANGE_SYMBOLS = {"BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "DOGE/USDT:USDT", "FAIL/USDT:USDT", "ETHW/USDT:USDT", "TIA/USDT:USDT", "ARB/USDT:USDT"}
SCALE = {"SOL": 0.002, "DOGE": 0.00001}      # во сколько раз цена актива меньше «биткоина» из демо-данных
SECONDS = {"SOL": 3.0, "DOGE": 7.0}          # сколько «качается» актив


def runner(symbol, progress):
    s = load_settings()
    base = symbol.split("/")[0]
    if base == "FAIL":
        time.sleep(1.5)
        raise RuntimeError("биржа вернула ошибку 429")
    src = backtest.read_cache(backtest.cache_path(s.exchange, "BTC/USDT:USDT", s.candle_tf, str(s.cache_dir)))
    k = SCALE.get(base, 0.01)
    steps = int(SECONDS.get(base, 3.0) / 0.2)
    for i in range(steps):
        time.sleep(0.2)
        progress(int(len(src) * (i + 1) / steps), src[min(len(src) - 1, int(len(src) * (i + 1) / steps))][0])
    path = backtest.cache_path(s.exchange, symbol, s.candle_tf, str(s.cache_dir))
    import csv
    with open(path, "w", newline="") as f:
        csv.writer(f).writerows([(t, o * k, h * k, l * k, c * k) for t, o, h, l, c in src])


def top_runner(job, progress):
    """Окна свечей топ-коинов: берём минутки «биткоина» за окно и пересчитываем в цену монеты; карта окон ведётся как в боевой загрузке."""
    s = load_settings()
    tf_ms = backtest.tf_to_ms(s.candle_tf)
    src = backtest.read_cache(backtest.cache_path(s.exchange, "BTC/USDT:USDT", s.candle_tf, str(s.cache_dir)))
    job.total = len(job.payload["windows"])
    count = 0
    for sym, windows in job.payload["windows"].items():
        time.sleep(0.8)
        path = backtest.cache_path(s.exchange, sym, s.candle_tf, str(s.cache_dir))
        have = {c[0]: c for c in backtest.read_cache(path)}
        covered = backtest.covered_intervals(path, tf_ms)
        k = 0.001
        for a, b in windows:
            for t, o, h, l, c in src:
                if a <= t <= b:
                    have[t] = (t, o * k, h * k, l * k, c * k)
                    count += 1
            covered.append((a, b))
        backtest._save_window(path, have, covered, tf_ms)
        job.done += 1
        progress(count, windows[-1][1])


app = create_app(load_settings(), markets=MarketsCache(lambda: EXCHANGE_SYMBOLS), runner=runner, top_runner=top_runner)
