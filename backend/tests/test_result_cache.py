import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app import service
from app.config import load_settings
from app.data import DataStore
from app.main import create_app
from app.result_cache import ResultCache
from app.schemas import BacktestParams
from test_api import H, M, T0, client  # noqa: F401  (fixture: история + свечи)
from test_assets import _write_candles  # noqa: F401


@pytest.fixture()
def counted(monkeypatch):
    """Считает, сколько раз бэктест реально выполнялся (а не был взят из кеша)."""
    calls = []
    real = service._compute

    def spy(*a, **k):
        calls.append(1)
        return real(*a, **k)
    monkeypatch.setattr(service, "_compute", spy)
    return calls


def run(client, **params):
    st = client.app.state
    return service.run_backtest(st.store, st.settings.defaults, BacktestParams(**params))


def test_same_config_is_computed_once(client, counted):  # noqa: F811
    a = run(client)
    b = run(client)
    assert len(counted) == 1 and a is b                                   # второй раз — тот же объект из памяти
    st = client.app.state.store.results.stats()
    assert st["hits"] == 1 and st["misses"] == 1
    run(client, sl_pct=2.5)                                               # другие параметры — новый расчёт
    run(client, sl_pct=2.5)
    assert len(counted) == 2
    run(client, tps=[{"pct": 3, "share": 100}])                           # то же, что умолчание из env (TP_PCT=3): параметры равны -> кеш
    assert len(counted) == 2
    run(client, tps=[{"pct": 4, "share": 100}])
    assert len(counted) == 3


def test_cached_result_equals_fresh_one(client, tmp_path, monkeypatch):  # noqa: F811
    cached = run(client)
    again = run(client)
    monkeypatch.setattr(client.app.state.store, "results", None)          # без кеша
    fresh = run(client)
    assert json.loads(json.dumps(cached)) == json.loads(json.dumps(fresh)) == json.loads(json.dumps(again))


def test_new_reports_or_candles_recompute_only_then(client, counted):  # noqa: F811
    st = client.app.state
    hist = st.settings.sources[st.settings.default_source].file
    first = run(client)
    run(client)
    assert len(counted) == 1
    # пришли новые отчёты -> отпечаток истории изменился -> конфиг досчитывается на свежих данных
    with open(hist, "a", encoding="utf-8", newline="") as f:
        from datetime import datetime, timezone
        f.write(f"999,{datetime.fromtimestamp((T0 + 40 * H) / 1000, timezone.utc).isoformat()},1.0,2.0,2.5,10.0,20.0,\n")
    second = run(client)
    assert len(counted) == 2 and second["range"]["last_ts"] > first["range"]["last_ts"]
    run(client)
    assert len(counted) == 2
    # докачались свечи -> снова пересчёт
    path = st.store.candle_path()
    with open(path, "a", newline="") as f:
        f.write(f"{T0 + 99999 * M},1,1,1,1\n")
    st.store.reload_candles()
    run(client)
    assert len(counted) == 3


def test_survives_restart_via_disk_and_is_shared_between_instances(client, counted):  # noqa: F811
    a = run(client)
    assert len(counted) == 1
    s = client.app.state.settings
    other = DataStore(s)                                                  # «другой процесс» (например, бот) с теми же файлами
    b = service.run_backtest(other, s.defaults, BacktestParams())
    assert len(counted) == 1 and other.results.stats()["disk_hits"] == 1  # взято с диска, считать не пришлось
    assert json.loads(json.dumps(a)) == json.loads(json.dumps(b))


def test_corrupt_disk_entry_is_ignored_and_errors_are_not_cached(client, counted):  # noqa: F811
    run(client)
    s = client.app.state.settings
    for f in (s.cache_dir / "results").glob("*.json.gz"):
        f.write_bytes(b"not gzip")
    fresh = DataStore(s)
    service.run_backtest(fresh, s.defaults, BacktestParams())
    assert len(counted) == 2                                              # повреждённый файл молча проигнорирован
    for _ in range(2):
        with pytest.raises(service.ParamError):
            service.run_backtest(fresh, s.defaults, BacktestParams(date_from="2000-01-01", date_to="2000-01-02"))
    assert len(counted) == 4                                              # ошибки не кешируются


def test_code_change_invalidates_cache(client, counted):  # noqa: F811
    run(client)
    client.app.state.store.results.code = "other-version"
    run(client)
    assert len(counted) == 2


def test_top_section_depends_on_minute_and_coin_files(client, counted):  # noqa: F811
    st = client.app.state
    p = BacktestParams(top_enabled=True)
    now = T0 + 50 * H

    def go(n, token=1):
        return service.run_backtest(st.store, st.settings.defaults, p, now_ms=n, available_token=token)
    go(now)
    go(now + 1000)                                                        # та же минута
    assert len(counted) == 1
    go(now + 2 * M)                                                       # минута сменилась: окна топ-коинов могли закончиться
    assert len(counted) == 2
    go(now + 2 * M, token=2)                                              # обновился список рынков биржи
    assert len(counted) == 3
    _write_candles(st.settings.cache_dir, "TIA/USDT:USDT", 10)             # скачали свечи монеты
    go(now + 2 * M, token=2)
    assert len(counted) == 4
    # основной расчёт (топ-коины выключены) от минуты не зависит
    q = BacktestParams(top_enabled=False)
    for n in (now, now + 5 * M, now + 9 * M):
        service.run_backtest(st.store, st.settings.defaults, q, now_ms=n)
    assert len(counted) == 5


def test_parallel_identical_requests_compute_once():
    cache = ResultCache(None, mem_size=4)
    started = []

    def slow():
        started.append(1)
        time.sleep(0.3)
        return {"x": 1}
    out = []
    ts = [threading.Thread(target=lambda: out.append(cache.get_or_compute("k", slow))) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(started) == 1 and len(out) == 8 and all(v == {"x": 1} for v, _ in out)


def test_lru_and_disk_eviction(tmp_path):
    cache = ResultCache(tmp_path, mem_size=2, disk_files=3)
    for i in range(6):
        cache.get_or_compute(f"k{i}", lambda i=i: {"i": i})
        time.sleep(0.01)
    assert cache.stats()["memory_entries"] == 2
    cache._evict()
    assert len(list(tmp_path.glob("*.json.gz"))) == 3
    assert cache.get_or_compute("k5", lambda: {"i": -1})[0] == {"i": 5}   # свежие записи остались


def test_cache_can_be_disabled(tmp_path, monkeypatch, client):  # noqa: F811
    monkeypatch.setenv("RESULT_CACHE", "false")
    assert DataStore(load_settings()).results is None
