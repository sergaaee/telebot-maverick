import os, sys, time
import ccxt
from app.core import backtest

M = 60_000


class Fake:
    calls = []

    def __init__(self, *_):
        pass

    def parse_timeframe(self, tf):
        return 60

    def fetch_ohlcv(self, symbol, tf, since=None, limit=1000):
        Fake.calls.append(since)
        now = int(time.time() * 1000) // M * M
        out, t = [], -(-since // M) * M
        while t <= now and len(out) < limit:   # последняя свеча (t == now) ещё формируется
            out.append([t, 1, 2, 0.5, 1.5, 10])
            t += M
        return out


def test_second_run_downloads_only_tail(tmp_path, monkeypatch):
    monkeypatch.setattr(ccxt, "binanceusdm", Fake, raising=False)
    monkeypatch.setattr(backtest.time, "sleep", lambda s: None)
    since = (int(time.time() * 1000) // M - 3000) * M
    c1 = backtest.fetch_candles("binanceusdm", "BTC/USDT:USDT", "1m", since, cache_dir=str(tmp_path))
    first_calls = len(Fake.calls)
    assert first_calls >= 3
    Fake.calls.clear()
    c2 = backtest.fetch_candles("binanceusdm", "BTC/USDT:USDT", "1m", since, cache_dir=str(tmp_path))
    assert len(Fake.calls) <= 2 and len(c2) >= len(c1) - 1       # докачан только хвост
    now = int(time.time() * 1000) // M * M
    assert all(c[0] < now for c in c2)                            # незавершённая свеча в кеш не попадает


def test_binance_alias_shares_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(ccxt, "binanceusdm", Fake, raising=False)
    monkeypatch.setattr(ccxt, "binance", Fake, raising=False)
    monkeypatch.setattr(backtest.time, "sleep", lambda s: None)
    since = (int(time.time() * 1000) // M - 2000) * M
    backtest.fetch_candles("binance", "BTC/USDT:USDT", "1m", since, cache_dir=str(tmp_path))
    Fake.calls.clear()
    backtest.fetch_candles("binanceusdm", "BTC/USDT:USDT", "1m", since, cache_dir=str(tmp_path))
    assert len(Fake.calls) <= 2


def test_asset_listed_after_signals_start_is_not_redownloaded_every_time(tmp_path, monkeypatch):
    """Актив появился на бирже позже первого отчёта: кеш начинается с даты листинга и это нормально — перекачивать всё не нужно."""
    now = int(time.time() * 1000) // M * M
    listing = now - 3000 * M

    class Late(Fake):
        def fetch_ohlcv(self, symbol, tf, since=None, limit=1000):
            return super().fetch_ohlcv(symbol, tf, since=max(since, listing), limit=limit)

    monkeypatch.setattr(ccxt, "binanceusdm", Late, raising=False)
    monkeypatch.setattr(backtest.time, "sleep", lambda s: None)
    since = now - 10_000 * M                                      # история сигналов начинается намного раньше листинга
    progress = []
    c1 = backtest.fetch_candles("binanceusdm", "NEW/USDT:USDT", "1m", since, cache_dir=str(tmp_path), progress=lambda n, ts: progress.append(n))
    assert c1[0][0] == listing and progress and progress[-1] == len(c1)
    Fake.calls.clear()
    c2 = backtest.fetch_candles("binanceusdm", "NEW/USDT:USDT", "1m", since, cache_dir=str(tmp_path))
    assert len(Fake.calls) <= 2 and c2[0][0] == listing and len(c2) >= len(c1) - 1       # докачан только хвост, а не вся история


def test_fetch_window_downloads_only_missing_parts(tmp_path, monkeypatch):
    monkeypatch.setattr(ccxt, "binanceusdm", Fake, raising=False)
    monkeypatch.setattr(backtest.time, "sleep", lambda s: None)
    base = (int(time.time() * 1000) // M - 20000) * M
    sym, path = "TIA/USDT:USDT", None
    n1 = backtest.fetch_window("binanceusdm", sym, "1m", base, base + 1999 * M, cache_dir=str(tmp_path))
    assert n1 == 2000 and len(Fake.calls) >= 2
    path = backtest.cache_path("binanceusdm", sym, "1m", str(tmp_path))
    assert backtest.covered_intervals(path, M) == [(base, base + 1999 * M)]
    Fake.calls.clear()
    assert backtest.fetch_window("binanceusdm", sym, "1m", base + 100 * M, base + 900 * M, cache_dir=str(tmp_path)) == 0     # уже есть
    assert Fake.calls == []
    # второе окно с разрывом: докачивается только оно, карта окон хранит два интервала, свечи между ними не выдумываются
    far = base + 10000 * M
    assert backtest.fetch_window("binanceusdm", sym, "1m", far, far + 499 * M, cache_dir=str(tmp_path)) == 500
    assert backtest.covered_intervals(path, M) == [(base, base + 1999 * M), (far, far + 499 * M)]
    assert backtest.missing_intervals(backtest.covered_intervals(path, M), base, far + 499 * M, M) == [(base + 2000 * M, far - M)]
    # пересекающееся окно: качается только недостающий хвост
    Fake.calls.clear()
    assert backtest.fetch_window("binanceusdm", sym, "1m", base + 1500 * M, base + 2499 * M, cache_dir=str(tmp_path)) == 500
    assert backtest.covered_intervals(path, M)[0] == (base, base + 2499 * M)
    # будущее окно обрезается по последней завершённой свече
    now = int(time.time() * 1000)
    got = backtest.fetch_window("binanceusdm", sym, "1m", now - 5 * M, now + 3 * 86_400_000, cache_dir=str(tmp_path))
    assert 3 <= got <= 6
    assert max(c[0] for c in backtest.read_cache(path)) <= backtest.last_closed_start(M)
    # склейка соседних интервалов
    assert backtest.merge_intervals([(0, 5 * M), (6 * M, 9 * M), (20 * M, 30 * M)], M) == [(0, 9 * M), (20 * M, 30 * M)]
