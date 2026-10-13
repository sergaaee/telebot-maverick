import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import ccxt
import backtest

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
