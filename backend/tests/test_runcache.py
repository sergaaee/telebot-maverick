"""Кеш расчёта: результат на дописанных данных (по снимку) должен совпадать с расчётом с нуля, и при этом считаться должны только новые данные."""
import json
import random
from types import SimpleNamespace

import pytest

from app import runcache, service
from app.config import load_settings
from app.core import backtest, entries as entries_mod
from app.schemas import BacktestParams

H = 3_600_000


def make(seed=5, n_rep=900):
    random.seed(seed)
    sig, t = [], 0
    for _ in range(n_rep // 3):
        kind = random.choice("LLSN")
        for _ in range(random.randint(1, 5)):
            if kind == "L":
                sig.append((t, random.uniform(1.65, 2.3), random.uniform(2.0, 3.0), random.uniform(0, 50), random.uniform(0, 50), None, None, random.uniform(1.0, 1.4)))
            elif kind == "S":
                sig.append((t, random.uniform(0.6, 1.05), random.uniform(0.2, 0.65), random.uniform(0, 50), random.uniform(0, 50), None, None, random.uniform(0.8, 1.0)))
            else:
                sig.append((t, random.uniform(1.1, 1.6), random.uniform(0.8, 1.9), random.uniform(0, 50), random.uniform(0, 50), None, None, random.uniform(0.9, 1.2)))
            t += H // 2
    price, candles = 100.0, []
    for i in range(t // 60000 + 3000):
        price *= 1 + random.gauss(0, 0.0007)
        candles.append((i * 60000, price, price * 1.0007, price * 0.9993, price))
    return sig, candles


class FakeStore:
    def __init__(self, tmp, sigs, candles):
        self.s = SimpleNamespace(default_source="a", symbol="BTC/USDT:USDT", cache_dir=tmp, candle_tf="1m", liq_max_age_h=3)
        self.sigs, self.candles_ = sigs, candles

    def source(self, i):
        return SimpleNamespace(id=i, label=i)

    def signals(self, a, b=None):
        return self.sigs, None

    def candles(self, sym):
        return self.candles_

    def candle_ts(self, sym):
        return [c[0] for c in self.candles_]


CASES = [
    {},
    {"side": "both", "confirm": 2},
    {"side": "both", "cool_drop": 0.1, "cool_metric": "scalp", "liq_hours": 3, "liq_frac": 0.5, "liq_pct": 0},
    {"side": "both", "trend_enabled": True, "trend_tf": "1h", "trend_emas": [20, 50]},
    {"side": "both", "pyramid_enabled": True, "pyramid_max": 3, "pyramid_pct": 10, "trade_pct": 20, "pyramid_gap_h": 1},
    {"side": "both", "pyramid_enabled": True, "pyramid_max": 3, "pyramid_pct": 10, "trade_pct": 20, "pyramid_avg": True},
    {"side": "both", "sl_hold_strong": True, "cooldown_h": 2, "signal_gap_h": 1},
    {"side": "both", "entry_limit_pct": 0.15, "entry_limit_dir": "pullback", "entry_limit_ttl_h": 2},
    {"side": "both", "entry_limit_pct": 0.1, "entry_limit_dir": "breakout", "entry_limit_ttl_h": 1.5, "pyramid_enabled": True, "pyramid_max": 2, "pyramid_pct": 10, "trade_pct": 20},
    {"side": "both", "weekdays_only": True, "liquidation_sim": True, "leverage": 10, "tps": [{"pct": 1, "share": 50}, {"pct": 2, "share": 50}], "be_after_tp1": True},
]


def run(store, params, monkeypatch, cache):
    monkeypatch.setenv("RESULT_CACHE", "on" if cache else "off")
    defaults = load_settings().defaults
    res = service.run_backtest(store, defaults, BacktestParams(**{"top_enabled": False, "sl_pct": 0.3, "tps": [{"pct": 0.3, "share": 100}], **params}), now_ms=0)
    return json.dumps(res, sort_keys=True, default=str)


@pytest.mark.parametrize("params", CASES)
def test_incremental_equals_from_scratch(tmp_path, monkeypatch, params):
    sigs, candles = make()
    full = run(FakeStore(tmp_path / "off", sigs, candles), params, monkeypatch, False)
    d = tmp_path / "on"
    for frac in (0.4, 0.7, 0.9, 1.0):                                  # данные дописываются порциями; отчёты отстают от свечей
        ns = int(len(sigs) * frac)
        nc = min(len(candles), candles.index(next(c for c in candles if c[0] >= sigs[ns - 1][0])) + 90) if ns < len(sigs) else len(candles)
        part = run(FakeStore(d, sigs[:ns], candles[:nc]), params, monkeypatch, True)
        again = run(FakeStore(d, sigs[:ns], candles[:nc]), params, monkeypatch, True)       # тот же набор данных ещё раз: из снимка
        assert part == again
        assert part == run(FakeStore(tmp_path / "ref", sigs[:ns], candles[:nc]), params, monkeypatch, False)
    assert again == full


def test_only_new_data_is_processed(tmp_path, monkeypatch):
    sigs, candles = make()
    params = {"side": "both", "trend_enabled": True, "trend_tf": "1h", "trend_emas": [20]}
    seen = {"rows": [], "events": []}
    real_ent, real_run = service.entries, backtest.run_events

    def spy_ent(signals, *a, **kw):
        st = kw.get("state")
        seen["rows"].append(len(signals) - (st.get("n", 0) if st else 0))
        return real_ent(signals, *a, **kw)

    def spy_run(events, *a, **kw):
        seen["events"].append(len(events))
        return real_run(events, *a, **kw)

    monkeypatch.setattr(service, "entries", spy_ent)
    monkeypatch.setattr(backtest, "run_events", spy_run)
    ns = len(sigs) * 3 // 4
    run(FakeStore(tmp_path, sigs[:ns], candles[:ns * 30 + 100]), params, monkeypatch, True)
    run(FakeStore(tmp_path, sigs, candles), params, monkeypatch, True)
    assert seen["rows"] == [ns, len(sigs) - ns]                                # второй раз — только новые отчёты
    assert seen["events"][1] < seen["events"][0]


def test_changed_history_falls_back_to_full(tmp_path, monkeypatch):
    sigs, candles = make()
    p = {"side": "both"}
    run(FakeStore(tmp_path, sigs[:500], candles[:20000]), p, monkeypatch, True)
    edited = list(sigs)
    edited[100] = (*edited[100][:1], 9.9, *edited[100][2:])                   # отчёт в середине истории изменён
    got = run(FakeStore(tmp_path, edited, candles), p, monkeypatch, True)
    assert got == run(FakeStore(tmp_path / "x", edited, candles), p, monkeypatch, False)


def test_cache_off_and_corrupt_file(tmp_path, monkeypatch):
    sigs, candles = make()
    st = FakeStore(tmp_path, sigs, candles)
    run(st, {}, monkeypatch, False)
    assert not (tmp_path / "runcache").exists()
    run(st, {}, monkeypatch, True)
    for f in (tmp_path / "runcache").glob("*.pkl"):
        f.write_bytes(b"garbage")
    runcache._caches.clear()
    assert run(st, {}, monkeypatch, True) == run(st, {}, monkeypatch, False)
