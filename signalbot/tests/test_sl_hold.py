"""«Не ставить SL, пока метрики сильны» в автоторговле: вход без стопа, стоп ставится, когда метрики остыли, снимается, когда снова сильны; всё сверяется с биржей."""
import pytest

from conftest import *  # noqa: F401,F403
from test_monitor_ui import _fresh_monitor_cache, _restart, _signal_cycle, add_cfg, cb, fake_verify, tmsg, _real_account  # noqa: F401
from test_bybit_bot import BYKEY, BYSECRET, Book, _patch, fake_bybit  # noqa: F401
from test_signalbot import CHAT, H, T0, append_reports, env  # noqa: F401

START = T0 + 38 * H           # момент, как в _signal_cycle: три лонговых отчёта, «сейчас» — через 1,5 часа от первого


class StopBook:
    """Имитация биржи со стопами: что отправил бот и что сейчас стоит."""
    def __init__(self, monkeypatch, cls):
        self.opened, self.set_stops, self.cleared, self.closed = [], [], 0, []
        self.stops = []
        self.positions = []

        def open_market(s, sym, side, notional, lev=1.0, stop=None, takes=None):
            self.opened.append((sym, side, notional, lev, stop, takes))
            self.positions = [{"symbol": sym, "side": side, "size": 4.0, "entry": 100.0}]
            if stop:
                self.stops = [stop]
            return {"status": "sent"}

        def set_stop(s, sym, side, price):
            self.set_stops.append(price)
            self.stops = [price]
            return {"status": "sent"}

        def clear_stops(s, sym, side):
            self.cleared += 1
            n, self.stops = len(self.stops), []
            return n

        def close_market(s, sym, side, share=1.0):
            self.closed.append((side, share))
            self.positions = []
            self.stops = []
            return {"status": "sent"}

        monkeypatch.setattr(cls, "positions", lambda s: self.positions)
        monkeypatch.setattr(cls, "open_market", open_market)
        monkeypatch.setattr(cls, "set_stop", set_stop)
        monkeypatch.setattr(cls, "stop_prices", lambda s, sym, side: list(self.stops))
        monkeypatch.setattr(cls, "clear_stops", clear_stops)
        monkeypatch.setattr(cls, "close_market", close_market)


def _hl(env, fake_verify, monkeypatch):
    from app import trading
    _real_account(env, fake_verify, monkeypatch, equity=1000.0, free=900.0)
    return StopBook(monkeypatch, trading.HyperliquidAdapter)


def _hold_cfg(env, name, **extra):
    """Конфиг с опцией, автомод открывать + управлять от баланса счёта; без БУ после TP1 (иначе расчёт к моменту остывания уже держит безубыток, а не SL)."""
    cid = add_cfg(env, name, sl_hold_strong=True, be_after_tp1=False, **extra)
    for a in (f"ay:{cid}:o", f"ay:{cid}:m", f"ab:{cid}"):
        env.handle_update(cb(a))
    return cid


def _entry_px(env, cid, k=1.0):
    """Цена входа в расчёте (свечи теста растут, а price_fn бота по умолчанию стоит на 100): подгоняем цену бота под неё, умножая на k."""
    legs = [t for t in env.engine.evaluate(env.db.get_config(cid)["params"])["trades"] if t["reason"] == "OPEN"]
    px = legs[0]["entry"] * k
    env.engine.price_fn = lambda sym: px
    return legs[0]["entry"]


def _report(env, rows, rid):
    append_reports(env.hist, rows, rid)
    from app import monitor
    monitor._cache.clear()


def _cool(env):
    """Новый отчёт «остыл»: Local и Scalp ниже порогов входа."""
    _report(env, [(START + 3 * H // 2, 1.0, 1.3, 1.0)], 40)


def test_entry_without_stop_then_stop_placed_when_metrics_cool_and_removed_when_strong_again(env, fake_verify, monkeypatch):
    book = _hl(env, fake_verify, monkeypatch)
    cid = _hold_cfg(env, "Держим")
    _signal_cycle(env)
    entry = _entry_px(env, cid)
    assert len(book.opened) == 1 and book.opened[0][4] is None and len(book.opened[0][5]) == 2             # вход только с тейками: метрики сильны
    assert any("БЕЗ стопа" in t for t in env.tg.texts(CHAT))
    assert any(x["kind"] == "open" and x["details"].get("stop", "").startswith("не ставится") for x in env.db.list_auto_log(env.alice["id"], 10))
    assert book.set_stops == [] and book.stops == []                                                       # метрики всё ещё сильны: стопа нет
    env.cycle()
    assert book.set_stops == [] and book.cleared == 0
    # метрики остыли -> стоп ставится на цену стопа расчёта (SL 40% от входа)
    _cool(env)
    env.cycle()
    assert len(book.set_stops) == 1 and abs(book.set_stops[0] - entry * 0.6) < 1e-6 and book.stops == book.set_stops
    assert any("метрики остыли" in t and "поставил стоп" in t for t in env.tg.texts(CHAT))
    assert [x["details"]["action"] for x in env.db.list_auto_log(env.alice["id"], 10) if x["kind"] == "sl_sync"] == ["поставить стоп"]
    env.cycle()
    _restart(env).cycle()                                                                                  # ни следующая проверка, ни перезапуск бота ничего не дублируют: сверка с биржей
    assert len(book.set_stops) == 1 and book.cleared == 0
    # метрики снова сильны -> стоп снимается
    env.now["t"] += 20 * 60
    _report(env, [(START + 3 * H // 2 + 10 * 60_000, 1.2, 2.0, 2.5)], 41)
    env.cycle()
    assert book.cleared == 1 and book.stops == [] and any("метрики снова сильны" in t and "стоп снят" in t for t in env.tg.texts(CHAT))
    env.cycle()
    assert book.cleared == 1


def test_price_already_beyond_the_stop_when_metrics_cool_closes_the_position(env, fake_verify, monkeypatch):
    book = _hl(env, fake_verify, monkeypatch)
    cid = _hold_cfg(env, "Закрыть")
    _signal_cycle(env)
    assert book.opened and book.stops == []
    _entry_px(env, cid, 0.5)                                                                               # цена ушла сильно ниже стопа (−40% от входа), а стопа не было
    _cool(env)
    env.cycle()
    assert book.closed == [("long", 1.0)] and book.set_stops == [] and any("за стопом" in t and "закрыта по рынку" in t for t in env.tg.texts(CHAT))


def test_hold_option_needs_manage_otherwise_stop_is_placed_at_entry(env, fake_verify, monkeypatch):
    book = _hl(env, fake_verify, monkeypatch)
    cid = add_cfg(env, "Без управления", sl_hold_strong=True, be_after_tp1=False)
    env.handle_update(cb(f"ay:{cid}:o"))                                                                   # «управлять» выключено: потом стоп поставить некому
    env.handle_update(cb(f"ab:{cid}"))
    _signal_cycle(env)
    assert len(book.opened) == 1 and book.opened[0][4] is not None and book.set_stops == []
    assert not any("БЕЗ стопа" in t for t in env.tg.texts(CHAT))
    _cool(env)
    env.cycle()
    assert book.set_stops == [] and book.cleared == 0                                                      # и синхронизация не вмешивается


def test_without_the_option_nothing_changes(env, fake_verify, monkeypatch):
    book = _hl(env, fake_verify, monkeypatch)
    cid = add_cfg(env, "Обычный", be_after_tp1=False)
    for a in (f"ay:{cid}:o", f"ay:{cid}:m", f"ab:{cid}"):
        env.handle_update(cb(a))
    _signal_cycle(env)
    _entry_px(env, cid)
    assert book.opened[0][4] is not None and not any("БЕЗ стопа" in t for t in env.tg.texts(CHAT))
    _cool(env)
    env.cycle()
    assert book.set_stops == [] and book.cleared == 0 and book.closed == []


def test_breakeven_stop_after_tp1_is_not_removed_by_strong_metrics(env, fake_verify, monkeypatch):
    """Стоп в безубытке от метрик не зависит: расчёт после TP1 держит его, даже если метрики снова сильны."""
    book = _hl(env, fake_verify, monkeypatch)
    cid = _hold_cfg(env, "БУ")
    _signal_cycle(env)
    _entry_px(env, cid, 1.1)
    cfg = env.db.get_config(cid)
    res = env.engine.evaluate(cfg["params"])
    legs = [t for t in res["trades"] if t["reason"] == "OPEN"]
    assert legs and res["hold_now"]["long"] is True
    legs[0].update(be=True, tp_hits=1)                                                                     # расчёт: TP1 взят, стоп в безубытке
    book.stops = [90.0]
    env._sync_stops(cfg, res, CHAT)
    assert book.cleared == 0 and len(book.set_stops) == 1 and abs(book.set_stops[0] - legs[0]["entry"]) < 1e-9       # стоп переставлен на вход, а не снят


def test_only_exchanges_with_manage_on_follow_the_rule(env, fake_verify, fake_bybit, monkeypatch):
    from app import trading
    from test_bybit_bot import _connect_both
    hl, by = _connect_both(env, fake_verify, fake_bybit, monkeypatch)
    hs, bs = StopBook(monkeypatch, trading.HyperliquidAdapter), StopBook(monkeypatch, trading.BybitAdapter)
    cid = add_cfg(env, "Две биржи", sl_hold_strong=True, be_after_tp1=False)
    for code in ("", ":b"):
        env.handle_update(cb(f"ay:{cid}:o{code}"))
        env.handle_update(cb(f"ab:{cid}{code}"))
    env.handle_update(cb(f"ay:{cid}:m:b"))                                                                 # «управлять» — только на Bybit
    _signal_cycle(env)
    _entry_px(env, cid)
    assert hs.opened[0][4] is not None and bs.opened[0][4] is None                                         # Hyperliquid — со стопом, Bybit — без
    _cool(env)
    env.cycle()
    assert hs.set_stops == [] and len(bs.set_stops) == 1
    assert any("[Bybit]" in t and "поставил стоп" in t for t in env.tg.texts(CHAT))


def test_failed_stop_sync_is_reported_once_per_hour(env, fake_verify, monkeypatch):
    from app import trading
    book = _hl(env, fake_verify, monkeypatch)
    cid = _hold_cfg(env, "Сбой")
    _signal_cycle(env)
    _entry_px(env, cid)

    def boom(s, sym, side, price):
        raise trading.ExchangeError("биржа не отвечает")
    monkeypatch.setattr(trading.HyperliquidAdapter, "set_stop", boom)
    _cool(env)
    env.cycle()
    n = sum("не удалось синхронизировать стоп" in t for t in env.tg.texts(CHAT))
    assert n == 1
    env.forget(cid)
    env.cycle()
    assert sum("не удалось синхронизировать стоп" in t for t in env.tg.texts(CHAT)) == 1                   # то же сообщение не повторяется
    env.now["t"] += 2 * 3600
    env.forget(cid)
    env.cycle()
    assert sum("не удалось синхронизировать стоп" in t for t in env.tg.texts(CHAT)) == 2


def test_cards_show_that_the_stop_is_not_in_force_while_metrics_are_strong(env, fake_verify, monkeypatch):
    book = _hl(env, fake_verify, monkeypatch)
    cid = add_cfg(env, "Карточка", sl_hold_strong=True, be_after_tp1=False)
    _signal_cycle(env)
    env.handle_update(cb(f"c:{cid}"))
    body = env.tg.edits[-1][2]
    assert "пока не действует" in body
    _cool(env)
    env.handle_update(cb(f"c:{cid}"))
    assert "пока не действует" not in env.tg.edits[-1][2] and "запас" in env.tg.edits[-1][2]
