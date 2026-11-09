"""Доборы пирамидинга лимиткой в автомоде: ордер вместо рынка (опция entry_limit_adds), стоп и тейки — только на добавленный объём, отмена при закрытии позиции / сроке /
обратном сигнале, разбор исполнения, когда биржа не отдаёт объём ордера (Upscale)."""
import pytest

from conftest import *  # noqa: F401,F403
from test_bybit_bot import Book, _patch  # noqa: F401
from test_monitor_ui import CHAT, KEY, WALLET, add_cfg, cb, fake_verify, tmsg  # noqa: F401
from test_signalbot import H, T0, env  # noqa: F401

SYM = "BTC/USDT:USDT"
LIM = dict(pyramid_enabled=True, pyramid_max=3, pyramid_pct=5, entry_limit_pct=0.5, entry_limit_dir="pullback", entry_limit_ttl_h=2, entry_limit_adds=True)


def setup(env, fake_verify, monkeypatch, name="Добор лимиткой", **extra):
    """Подключён Hyperliquid (имитация с одной «книгой»), в книге уже открытый лонг размером 2.0; автомод «открывать» от баланса счёта; цена 100."""
    from app import trading
    env.handle_update(cb("hl:t"))
    env.handle_update(tmsg(f"{WALLET} {KEY}", mid=840))
    book = Book(1000.0)
    _patch(monkeypatch, trading.HyperliquidAdapter, book)
    book.positions = [{"symbol": SYM, "side": "long", "size": 2.0, "entry": 100.0}]
    cid = add_cfg(env, name, **{**LIM, **extra})
    env.handle_update(cb(f"ay:{cid}:o"))
    env.handle_update(cb(f"ab:{cid}"))
    env.engine.price_fn = lambda sym: 100.0
    return book, cid


def add_signal(env, cid, ts=None, leg=2):
    cfg = env.db.get_config(cid)
    res = env.engine.evaluate(cfg["params"])
    sig = {"ts": ts or T0 + 40 * H, "side": "long", "pyramid": True, "leg": leg}
    env._open_on(cfg, res, sig, env.engine.plan("long", 100.0, cfg["params"]["tps"], cfg["params"]["sl_pct"]), "hyperliquid")
    return cfg, res, sig


def test_add_signal_places_a_limit_instead_of_a_market_order_and_remembers_it(env, fake_verify, monkeypatch):
    book, cid = setup(env, fake_verify, monkeypatch)
    add_signal(env, cid)
    assert book.opened == [] and len(book.placed) == 1
    sym, side, notional, price, lev = book.placed[0]
    assert (sym, side) == (SYM, "long") and price == pytest.approx(100.0 * 0.995) and notional == pytest.approx(1000.0 * 0.05 * lev)             # добор: 5% баланса × плечо
    pend = env.db.get_pending(cid)
    assert pend["pyramid"] == 1 and pend["leg"] == 2 and pend["order_id"] == "L1" and pend["base_size"] == 2.0 and pend["level"] == pytest.approx(99.5)
    assert any("лимитку добора" in t for t in env.tg.texts(CHAT))
    assert [x["kind"] for x in env.db.list_auto_log(env.alice["id"]) if x["kind"] in ("limit_add", "limit_entry", "add")] == ["limit_add"]
    add_signal(env, cid, ts=T0 + 41 * H, leg=3)                                                          # пока лимитка добора стоит, следующий сигнал ничего не ставит
    assert len(book.placed) == 1 and book.cancelled == []


def test_without_the_option_the_add_goes_to_market_as_before(env, fake_verify, monkeypatch):
    book, cid = setup(env, fake_verify, monkeypatch, entry_limit_adds=False)
    add_signal(env, cid)
    assert book.placed == [] and len(book.opened) == 1 and env.db.get_pending(cid) is None


def test_filled_add_gets_stop_and_takes_only_for_the_added_volume(env, fake_verify, monkeypatch):
    book, cid = setup(env, fake_verify, monkeypatch)
    add_signal(env, cid)
    book.state = {"status": "filled", "filled": 0.4, "amount": 0.4, "avg": 99.5}
    book.positions = [{"symbol": SYM, "side": "long", "size": 2.4, "entry": 99.9}]
    env._tick_pending()
    assert env.db.get_pending(cid) is None and len(book.protected) == 1
    sym, side, amount, stop, takes = book.protected[0]
    assert amount == pytest.approx(0.4) and stop == pytest.approx(99.5 * 0.6) and [round(t[0], 4) for t in takes] == [round(99.5 * 1.5, 4), round(99.5 * 1.8, 4)]      # от цены исполнения добора, объём — 0.4, не вся позиция 2.4
    assert any("лимитка добора исполнена" in t and "0.4" in t for t in env.tg.texts(CHAT))
    env._tick_pending()
    assert len(book.protected) == 1


def test_when_the_exchange_does_not_report_the_order_the_added_volume_is_the_position_growth(env, fake_verify, monkeypatch):
    book, cid = setup(env, fake_verify, monkeypatch)
    add_signal(env, cid)
    book.state = {"status": "unknown", "filled": 0.0, "amount": 0.0, "avg": None}                         # как у Upscale: ордера нет ни среди активных, ни по id
    book.positions = [{"symbol": SYM, "side": "long", "size": 2.3, "entry": 99.9}]
    env._tick_pending()
    assert len(book.protected) == 1 and book.protected[0][2] == pytest.approx(0.3) and book.protected[0][3] == pytest.approx(99.5 * 0.6)       # цена исполнения — уровень лимитки
    book2, cid2 = book, cid
    add_signal(env, cid2, ts=T0 + 50 * H)
    book2.state = {"status": "unknown", "filled": 0.0, "amount": 0.0, "avg": None}
    book2.positions = [{"symbol": SYM, "side": "long", "size": 2.3, "entry": 99.9}]                       # позиция не выросла: ордер просто снят снаружи
    env._tick_pending()
    assert len(book.protected) == 1 and env.db.get_pending(cid) is None and any("добор не состоялся" in t for t in env.tg.texts(CHAT))


def test_expired_add_order_is_cancelled_and_a_partial_fill_is_protected(env, fake_verify, monkeypatch):
    book, cid = setup(env, fake_verify, monkeypatch)
    add_signal(env, cid)
    env.now["t"] += 3 * 3600                                                                              # срок жизни — 2 ч
    book.state = {"status": "open", "filled": 0.1, "amount": 0.4, "avg": 99.5}
    book.positions = [{"symbol": SYM, "side": "long", "size": 2.1, "entry": 99.9}]
    env._tick_pending()
    assert book.cancelled == ["L1"] and env.db.get_pending(cid) is None
    assert len(book.protected) == 1 and book.protected[0][2] == pytest.approx(0.1)                        # успевшая исполниться часть защищена
    assert any("добор" in t and "снят" in t for t in env.tg.texts(CHAT))


def test_add_order_is_cancelled_when_the_position_is_gone(env, fake_verify, monkeypatch):
    book, cid = setup(env, fake_verify, monkeypatch)
    add_signal(env, cid)
    book.positions = []                                                                                   # позицию закрыл стоп
    env._tick_pending()
    assert book.cancelled == ["L1"] and env.db.get_pending(cid) is None and book.protected == []
    assert any("позиция закрыта до исполнения добора" in t for t in env.tg.texts(CHAT))


def test_opposite_signal_and_switching_automode_off_cancel_the_add_order(env, fake_verify, monkeypatch):
    book, cid = setup(env, fake_verify, monkeypatch)
    add_signal(env, cid)
    env.handle_update(cb(f"a:{cid}:o"))                                                                   # «открывать» выключили
    env._tick_pending()
    assert book.cancelled == ["L1"] and env.db.get_pending(cid) is None


def test_breakout_add_waits_for_the_price_and_then_adds_at_market(env, fake_verify, monkeypatch):
    book, cid = setup(env, fake_verify, monkeypatch, entry_limit_dir="breakout")
    add_signal(env, cid)
    assert book.placed == [] and book.opened == []
    pend = env.db.get_pending(cid)
    assert pend["pyramid"] == 1 and pend["order_id"] is None and pend["level"] == pytest.approx(100.5)       # жду движения цены вверх на 0.5%
    env._tick_pending()
    assert book.opened == []                                                                              # цена 100 — уровень не достигнут
    env.engine.price_fn = lambda sym: 100.6
    env._tick_pending()
    assert len(book.opened) == 1 and env.db.get_pending(cid) is None
    assert any(x["kind"] == "add" and x["status"] == "sent" for x in env.db.list_auto_log(env.alice["id"]))
    assert any("добираю по рынку" in t for t in env.tg.texts(CHAT))


def test_old_databases_get_the_new_pending_columns(tmp_path):
    import sqlite3
    from app.db import Database
    path = tmp_path / "old.db"
    Database(path)
    con = sqlite3.connect(path)
    con.executescript("DROP TABLE pending_entries; CREATE TABLE pending_entries (config_id INTEGER NOT NULL, exchange TEXT NOT NULL DEFAULT 'hyperliquid', user_id INTEGER NOT NULL, signal_ts INTEGER NOT NULL, "
                      "symbol TEXT NOT NULL, side TEXT NOT NULL, mode TEXT NOT NULL, level REAL NOT NULL, ref REAL NOT NULL, notional REAL NOT NULL, margin REAL NOT NULL, leverage REAL NOT NULL, "
                      "order_id TEXT, created REAL NOT NULL, expires REAL NOT NULL, PRIMARY KEY (config_id, exchange));")
    con.commit()
    con.close()
    db = Database(path)
    cols = {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(pending_entries)")}
    assert {"pyramid", "leg", "base_size"} <= cols and db.list_pending() == []
