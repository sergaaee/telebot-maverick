"""«Стоп-лосс по маркету» в автоторговле: вход без стоп-ордера; на новом отчёте бот закрывает позицию по рынку, если метрики ниже порогов входа, а цена за стопом."""
import pytest

from conftest import *  # noqa: F401,F403
from test_monitor_ui import _fresh_monitor_cache, _restart, _signal_cycle, add_cfg, cb, fake_verify  # noqa: F401
from test_sl_hold import START, StopBook, _cool, _entry_px, _hl, _report  # noqa: F401
from test_signalbot import CHAT, H, T0, append_reports, env  # noqa: F401

M = 60_000


def _mcfg(env, name, manage=True, **extra):
    cid = add_cfg(env, name, sl_market=True, be_after_tp1=False, **extra)
    for a in ([f"ay:{cid}:o", f"ay:{cid}:m", f"ab:{cid}"] if manage else [f"ay:{cid}:o", f"ab:{cid}"]):
        env.handle_update(cb(a))
    return cid


def _strong(env, minute, rid):
    _report(env, [(START + minute * M, 1.2, 2.0, 2.5)], rid)
    env.now["t"] = (START + (minute + 5) * M) / 1000


def _weak(env, minute, rid):
    _report(env, [(START + minute * M, 1.0, 1.3, 1.0)], rid)
    env.now["t"] = (START + (minute + 5) * M) / 1000


def _open(env, fake_verify, monkeypatch, name="Маркет", **extra):
    book = _hl(env, fake_verify, monkeypatch)
    cid = _mcfg(env, name, **extra)
    env.engine.price_fn = lambda sym: 1000.0                                                                # цена бота выше стопа расчёта (свечи теста растут сами): отчёт входа ничего не закрывает
    _signal_cycle(env)
    entry = _entry_px(env, cid)
    return book, cid, entry


def test_entry_has_no_stop_order_and_weak_report_with_price_beyond_the_stop_closes_at_market(env, fake_verify, monkeypatch):
    book, cid, entry = _open(env, fake_verify, monkeypatch)
    assert len(book.opened) == 1 and book.opened[0][4] is None and len(book.opened[0][5]) == 2             # только тейки: стоп-ордера нет
    assert any("БЕЗ стоп-ордера" in t and "по маркету" in t for t in env.tg.texts(CHAT))
    assert book.stops == [] and book.set_stops == [] and book.closed == []
    _entry_px(env, cid, 0.5)                                                                                # цена ушла за стоп (−40% от входа), но нового отчёта ещё нет
    env.cycle()
    assert book.closed == []                                                                                # касание стопа ничего не закрывает
    _strong(env, 100, 50)                                                                                   # новый отчёт, метрики сильны -> ждём
    env.cycle()
    assert book.closed == [] and any("всё ещё выше порогов" in t and "жду следующего отчёта" in t for t in env.tg.texts(CHAT))
    _weak(env, 110, 51)                                                                                     # следующий отчёт ниже порогов, цена всё ещё за стопом -> по рынку
    env.cycle()
    assert book.closed == [("long", 1.0)] and any("закрыта по рынку (стоп-лосс по маркету)" in t for t in env.tg.texts(CHAT))
    assert [x["details"]["reason"] for x in env.db.list_auto_log(env.alice["id"], 10) if x["kind"] == "sl_market"] == ["отчёт ниже порогов, цена за стопом"]
    env.cycle()
    _restart(env).cycle()                                                                                   # отчёт уже учтён: ни следующая проверка, ни перезапуск не закрывают повторно
    assert book.closed == [("long", 1.0)]


def test_price_above_the_stop_at_the_weak_report_does_nothing_and_the_next_report_decides(env, fake_verify, monkeypatch):
    book, cid, entry = _open(env, fake_verify, monkeypatch)
    _entry_px(env, cid, 0.9)                                                                                # выше стопа (0.6 от входа)
    _weak(env, 100, 50)
    env.cycle()
    assert book.closed == [] and not any("закрыта по рынку" in t or "жду следующего отчёта" in t for t in env.tg.texts(CHAT))
    _entry_px(env, cid, 0.5)                                                                                # потом цена ушла за стоп; пока нового отчёта нет — ничего
    env.cycle()
    assert book.closed == []
    _weak(env, 110, 51)
    env.cycle()
    assert book.closed == [("long", 1.0)]


def test_without_manage_the_stop_order_is_placed_at_entry_as_usual(env, fake_verify, monkeypatch):
    book = _hl(env, fake_verify, monkeypatch)
    cid = _mcfg(env, "Без управления", manage=False)
    env.engine.price_fn = lambda sym: 1000.0
    _signal_cycle(env)
    _entry_px(env, cid, 0.5)
    assert len(book.opened) == 1 and book.opened[0][4] is not None
    _weak(env, 100, 50)
    env.cycle()
    assert book.closed == [] and book.cleared == 0                                                          # бот ничего не закрывает и стоп не снимает


def test_stale_report_is_not_applied_and_a_failed_close_is_retried(env, fake_verify, monkeypatch):
    from app import trading
    book, cid, entry = _open(env, fake_verify, monkeypatch)
    _entry_px(env, cid, 0.5)
    _weak(env, 100, 50)
    env.now["t"] += 6 * 3600                                                                                # бот «не работал»: отчёт старше предела SIGNALBOT_MAX_AGE_MIN
    env.forget(cid)
    env.cycle()
    assert book.closed == []
    # сбой закрытия: сообщение один раз, отчёт не помечается — повтор при следующей проверке
    _weak(env, 110, 51)
    real_close = trading.HyperliquidAdapter.close_market
    state = {"fail": True}

    def close_market(s, sym, side, share=1.0):
        if state["fail"]:
            raise trading.ExchangeError("биржа не отвечает")
        return real_close(s, sym, side, share)
    monkeypatch.setattr(trading.HyperliquidAdapter, "close_market", close_market)
    env.cycle()
    assert book.closed == [] and sum("не удалось синхронизировать стоп" in t for t in env.tg.texts(CHAT)) == 1
    state["fail"] = False
    env.forget(cid)
    env.cycle()
    assert book.closed == [("long", 1.0)]


def test_leftover_stop_order_is_removed_and_breakeven_legs_are_left_alone(env, fake_verify, monkeypatch):
    book, cid, entry = _open(env, fake_verify, monkeypatch)
    book.stops = [entry * 0.6]                                                                              # остался стоп-ордер от прежнего входа
    env.forget(cid)
    env.cycle()
    assert book.cleared == 1 and book.stops == [] and any("стоп-лосс работает по маркету" in t for t in env.tg.texts(CHAT))
    # нога в безубытке после TP1: обычный стоп-ордер, по маркету не закрывается и не снимается
    cfg = env.db.get_config(cid)
    res = env.engine.evaluate(cfg["params"])
    legs = [t for t in res["trades"] if t["reason"] == "OPEN"]
    legs[0].update(be=True, tp_hits=1)
    _entry_px(env, cid, 1.1)
    book.stops = [90.0]
    env._sync_stops(cfg, res, CHAT)
    assert len(book.set_stops) == 1 and abs(book.set_stops[0] - legs[0]["entry"]) < 1e-9
    _weak(env, 110, 52)
    _entry_px(env, cid, 0.5)
    env._market_stop(cfg, res, CHAT)
    assert book.closed == []


def test_cards_show_market_stop_and_option_is_off_by_default(env, fake_verify, monkeypatch):
    book, cid, entry = _open(env, fake_verify, monkeypatch)
    env.handle_update(cb(f"c:{cid}"))
    assert "по маркету" in env.tg.edits[-1][2]
    cid2 = add_cfg(env, "Обычный")
    assert not env.db.get_config(cid2)["params"].get("sl_market")
