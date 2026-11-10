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


def _drop_candles_from(env, ts_from, k=0.5):
    """Цена с момента ts_from (мс) падает в k раз: свечи в кеше теста переписываются, как будто рынок на самом деле упал (а не только цена бота)."""
    import csv
    from app.core import backtest
    path = backtest.cache_path("binanceusdm", "BTC/USDT:USDT", "1m", str(env.tmp / "cache"))
    rows = backtest.read_cache(path)
    rows = [(r[0], *[x * k for x in r[1:5]]) if r[0] >= ts_from else tuple(r[:5]) for r in rows]
    with open(path, "w", newline="") as f:
        csv.writer(f).writerows(rows)
    env.engine.store.reload_candles("BTC/USDT:USDT")
    return next(r for r in rows if r[0] >= ts_from)[1]


def test_calculation_closes_at_stop_when_metrics_cool_and_price_is_already_beyond_it_the_exchange_position_is_closed_too(env, fake_verify, monkeypatch):
    """Настоящий ход событий: рынок упал за стоп, пока метрики были сильны (стопа на бирже нет), потом пришёл отчёт с остывшими метриками. Расчёт к этому моменту уже видит выход по стопу
    (свеча отчёта есть в кеше), позиции с признаком OPEN в нём нет — раньше бот присылал «Стоп-лосс», а позицию на бирже не закрывал."""
    book = _hl(env, fake_verify, monkeypatch)
    cid = _hold_cfg(env, "Упало")
    cool_ts = START + 3 * H // 2
    _drop_candles_from(env, cool_ts)
    env.engine.price_fn = lambda sym: 1000.0                                                              # цена бота: вход по любой цене (размер считается по балансу счёта)
    _signal_cycle(env)
    assert len(book.opened) == 1 and book.opened[0][4] is None and book.closed == []                          # вход без стопа: метрики сильны, падение пока ничего не значит
    entry = _entry_px(env, cid)
    env.engine.price_fn = lambda sym: entry * 0.5                                                           # реальная цена уже далеко за стопом (−40% от входа)
    _cool(env)
    env.cycle()
    assert book.closed == [("long", 1.0)] and book.stops == [] and book.positions == []
    kinds = [(x["kind"], x["status"]) for x in env.db.list_auto_log(env.alice["id"], 20)]
    assert ("close_sl", "sent") in kinds
    texts = env.tg.texts(CHAT) + [t for _c, t in env.tg.fills]
    assert any("Стоп-лосс" in t for t in texts) and any("по расчёту сработал стоп" in t and "закрыл по рынку" in t for t in texts)
    env.cycle()
    assert book.closed == [("long", 1.0)]                                                                    # повторно не закрывается


def _stopped_by_calculation(env, fake_verify, monkeypatch, name="Упало", **extra):
    """Общая подготовка: вход без стопа, рынок упал за стоп, метрики остыли — но ещё не обработаны (последний цикл — следующий вызов env.cycle())."""
    book = _hl(env, fake_verify, monkeypatch)
    cid = add_cfg(env, name, **{"sl_hold_strong": True, "be_after_tp1": False, **extra})
    for a in (f"ay:{cid}:o", f"ay:{cid}:m", f"ab:{cid}"):
        env.handle_update(cb(a))
    _drop_candles_from(env, START + 3 * H // 2)
    env.engine.price_fn = lambda sym: 1000.0
    _signal_cycle(env)
    entry = _entry_px(env, cid)
    env.engine.price_fn = lambda sym: entry * 0.5
    _cool(env)
    return book, cid


def test_a_failed_close_is_repeated_until_the_position_is_gone(env, fake_verify, monkeypatch):
    from app import trading
    book, cid = _stopped_by_calculation(env, fake_verify, monkeypatch)
    real_close = trading.HyperliquidAdapter.close_market
    monkeypatch.setattr(trading.HyperliquidAdapter, "close_market", lambda s, sym, side, share=1.0: (_ for _ in ()).throw(trading.ExchangeError("биржа не отвечает")))
    env.cycle()
    assert book.closed == [] and book.positions                                                          # позиция осталась
    assert any("закрыть её на бирже НЕ удалось" in t and "биржа не отвечает" in t for t in env.tg.texts(CHAT))
    assert env.db.get_meta(f"slc:{cid}:hyperliquid")                                                      # долг запомнен
    env.cycle()
    assert sum("закрыть её на бирже НЕ удалось" in t for t in env.tg.texts(CHAT)) == 1                      # то же сообщение не повторяется каждую проверку
    monkeypatch.setattr(trading.HyperliquidAdapter, "close_market", lambda s, sym, side, share=1.0: book.closed.append((side, share)) or setattr(book, "positions", []) or {"status": "sent"})
    env.now["t"] += 600                                                                                    # следующий период проверки исполнений
    env.cycle()
    assert book.closed == [("long", 1.0)] and not env.db.get_meta(f"slc:{cid}:hyperliquid")
    assert any("позицию закрыл по рынку" in t for t in env.tg.texts(CHAT))
    del real_close


def test_nothing_is_sent_when_the_exchange_already_closed_the_position(env, fake_verify, monkeypatch):
    book, cid = _stopped_by_calculation(env, fake_verify, monkeypatch)
    book.positions = []                                                                                   # стоп-ордер на бирже стоял и сработал сам
    env.cycle()
    assert book.closed == [] and not any(x["kind"] == "close_sl" for x in env.db.list_auto_log(env.alice["id"], 20))


def test_a_plain_stop_is_left_to_the_exchange_order(env, fake_verify, monkeypatch):
    """Без «не ставить SL, пока метрики сильны» и без стопа по маркету стоп — обычный ордер на бирже: исполнение стопа из расчёта бот сам не дублирует."""
    book = _hl(env, fake_verify, monkeypatch)
    cid = add_cfg(env, "Обычный", be_after_tp1=False)
    for a in (f"ay:{cid}:o", f"ay:{cid}:m", f"ab:{cid}"):
        env.handle_update(cb(a))
    env.engine.price_fn = lambda sym: 1000.0
    _signal_cycle(env)
    assert book.opened[0][4] is not None                                                                  # стоп встал вместе со входом
    entry = _entry_px(env, cid)
    _drop_candles_from(env, START + 3 * H // 2)                                                           # рынок падает уже после входа
    env.engine.price_fn = lambda sym: entry * 0.5
    _cool(env)
    env.cycle()
    assert book.closed == [] and not any(x["kind"] == "close_sl" for x in env.db.list_auto_log(env.alice["id"], 20))


def test_stop_by_market_rule_is_also_closed_on_the_exchange_when_the_calculation_already_sees_it(env, fake_verify, monkeypatch):
    book, cid = _stopped_by_calculation(env, fake_verify, monkeypatch, "По маркету", sl_hold_strong=False, sl_market=True)
    env.cycle()
    assert book.closed == [("long", 1.0)] and any(x["kind"] == "close_sl" for x in env.db.list_auto_log(env.alice["id"], 20))


def test_the_last_fill_of_a_leg_closes_the_whole_remaining_position_not_a_share_of_the_original_size():
    from sigbot.bot import SignalService
    trades = [{"side": "long", "entry_ts": 1, "leg": 1, "reason": "WKND", "exit_ts": 100}]
    ev = {"side": "long", "ts": 100, "share": 0.5, "closed": True, "entry_ts": 1, "leg": 1, "label": "WKND"}        # половину позиции уже забрал TP1 — на бирже осталась вторая половина
    assert SignalService._close_share({"trades": trades}, ev) == 1.0
    assert SignalService._close_share({"trades": trades}, {**ev, "closed": False}) == 0.5                                # не последнее исполнение — как в расчёте
    other = {"side": "long", "entry_ts": 50, "leg": 2, "reason": "OPEN"}                                                   # открыта ещё одна нога пирамидинга
    assert SignalService._close_share({"trades": trades + [other]}, ev) == 0.5
    both_close = {"side": "long", "entry_ts": 50, "leg": 2, "reason": "REV", "exit_ts": 100}                               # вторая нога закрывается в ту же минуту — первое закрытие снимает всё
    assert SignalService._close_share({"trades": trades + [both_close]}, ev) == 1.0
    assert SignalService._close_share({"trades": trades}, {"side": "long", "ts": 2, "share": 1.0, "label": "WKND"}) == 1.0
