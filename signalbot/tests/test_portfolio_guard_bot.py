"""Защита портфеля в автоторговле: лимит занятой маржи (по счёту на бирже) и пауза стороны после серии убытков (по расчёту стратегий) — те же правила, что на странице «Портфель»."""
import pytest

from conftest import *  # noqa: F401,F403
from test_bybit_bot import Book, _patch  # noqa: F401
from test_limit_adds_bot import SYM, add_signal, setup
from test_monitor_ui import CHAT, add_cfg, cb, fake_verify, tmsg  # noqa: F401
from test_signalbot import H, T0, env  # noqa: F401

MARKET = dict(entry_limit_pct=0.0, entry_limit_adds=False)          # вход и добор — по рынку: проверяем именно защиту


def prefs(env, **kw):
    env.db.set_pref(env.alice["id"], "portfolio", kw)


def account(monkeypatch, equity=1000.0, used=0.0):
    from app import trading
    monkeypatch.setattr(trading.HyperliquidAdapter, "account", lambda self: {"equity": equity, "free": equity - used, "used": used})


def entry(env, cid, side="long", ts=None):
    """Первый вход по сигналу (позиции на бирже нет)."""
    cfg = env.db.get_config(cid)
    res = env.engine.evaluate(cfg["params"])
    env._open_on(cfg, res, {"ts": ts or T0 + 40 * H, "side": side, "pyramid": False, "leg": 1}, env.engine.plan(side, 100.0, cfg["params"]["tps"], cfg["params"]["sl_pct"]), "hyperliquid")


def skips(env):
    return [x["details"]["note"] for x in env.db.list_auto_log(env.alice["id"], 20) if x["status"] == "skipped"]


def test_margin_cap_blocks_an_entry_that_would_exceed_the_share_of_equity(env, fake_verify, monkeypatch):
    book, cid = setup(env, fake_verify, monkeypatch, **MARKET)
    book.positions = []
    account(monkeypatch, equity=1000.0, used=600.0)                                   # занято 600; вход по конфигу — маржа 200 (20% баланса счёта)
    prefs(env, margin_cap_pct=70.0)                                                   # 600 + 200 = 800 > 700
    entry(env, cid)
    assert book.opened == [] and any("лимит занятой маржи портфеля" in n and "80%" in n and "70%" in n for n in skips(env))
    assert any("лимит занятой маржи портфеля" in t for t in env.tg.texts(CHAT))
    prefs(env, margin_cap_pct=80.0)                                                   # ровно в лимит — проходит
    entry(env, cid)
    assert len(book.opened) == 1


def test_without_settings_or_with_unknown_account_data_nothing_is_blocked(env, fake_verify, monkeypatch):
    book, cid = setup(env, fake_verify, monkeypatch, **MARKET)
    book.positions = []
    account(monkeypatch, equity=1000.0, used=300.0)
    entry(env, cid)                                                                    # настроек защиты нет
    assert len(book.opened) == 1
    prefs(env, margin_cap_pct=10.0)
    from app import trading
    cfg, ad, sig = env.db.get_config(cid), env.adapter_for(env.alice["id"], "hyperliquid"), {"ts": T0, "side": "long"}
    assert "лимит занятой маржи" in env._guard_reason(cfg, sig, False, ad, "hyperliquid", 200.0)                 # счёт известен: 300 + 200 > 10% от 1000
    monkeypatch.setattr(trading.HyperliquidAdapter, "account", lambda self: (_ for _ in ()).throw(trading.ExchangeError("нет связи")))
    assert env._guard_reason(cfg, sig, False, ad, "hyperliquid", 200.0) is None                                   # не удалось получить счёт — не блокируем (безопасный вариант)
    monkeypatch.setattr(trading.HyperliquidAdapter, "account", lambda self: {"equity": 1000.0, "free": 500.0, "used": None})
    assert env._guard_reason(cfg, sig, False, ad, "hyperliquid", 200.0) is None                                   # биржа не отдаёт занятую маржу — тоже


def test_margin_cap_applies_to_adds_too(env, fake_verify, monkeypatch):
    book, cid = setup(env, fake_verify, monkeypatch, **MARKET)                       # в книге открытый лонг; добор 5% баланса × плечо
    account(monkeypatch, equity=1000.0, used=900.0)                                    # добор — 50 $ маржи: свободной маржи хватает (100), а лимит 90% — нет (950 > 900)
    prefs(env, margin_cap_pct=90.0)
    add_signal(env, cid)
    assert book.opened == [] and any("добор" in n and "лимит занятой маржи" in n for n in skips(env))
    account(monkeypatch, equity=1000.0, used=500.0)
    add_signal(env, cid, ts=T0 + 41 * H, leg=3)
    assert len(book.opened) == 1


def stub_closed(env, side="short", n=4, pnl=-1.0, hours_ago=1):
    now_ms = env.now["t"] * 1000
    env._portfolio_closed = lambda uid: [(now_ms - (hours_ago + i) * H, side, pnl) for i in range(n)]


def test_side_pause_blocks_new_positions_of_that_side_only_and_remembers_the_pause(env, fake_verify, monkeypatch):
    book, cid = setup(env, fake_verify, monkeypatch, **MARKET)
    book.positions = []
    account(monkeypatch)
    prefs(env, side_pause={"losses": 3, "window_h": 24, "pause_h": 6})
    stub_closed(env, "short", 4)                                                       # 4 убыточных шорта за последние часы
    entry(env, cid, "short")
    assert book.opened == [] and any("4 убыточных закрытий шорта за 24 ч" in n and "на паузе 6 ч" in n for n in skips(env))
    assert float(env.db.get_meta(f"pfpause:{env.alice['id']}:short")) == pytest.approx(env.now["t"] + 6 * 3600)
    entry(env, cid, "long", ts=T0 + 41 * H)                                            # лонг серия не касается
    assert len(book.opened) == 1 and book.opened[0][1] == "long"
    env._portfolio_closed = lambda uid: []                                             # убытков уже нет, но пауза идёт
    book.positions = []
    entry(env, cid, "short", ts=T0 + 42 * H)
    assert len(book.opened) == 1 and any("пауза шортов после серии убытков — до" in n for n in skips(env))
    env.now["t"] += 7 * 3600                                                           # пауза закончилась, серия за окно не набирается
    entry(env, cid, "short", ts=T0 + 43 * H)
    assert len(book.opened) == 2 and book.opened[1][1] == "short"


def test_pause_counts_only_losses_inside_the_window_and_never_blocks_adds(env, fake_verify, monkeypatch):
    book, cid = setup(env, fake_verify, monkeypatch, **MARKET)
    account(monkeypatch)
    prefs(env, side_pause={"losses": 3, "window_h": 4, "pause_h": 6})
    stub_closed(env, "long", 5, hours_ago=10)                                          # серия была, но вне окна в 4 часа
    book.positions = []
    entry(env, cid, "long")
    assert len(book.opened) == 1
    stub_closed(env, "long", 3, hours_ago=1)
    book.positions = [{"symbol": SYM, "side": "long", "size": 1.0, "entry": 100.0}]
    add_signal(env, cid, ts=T0 + 41 * H, leg=2)                                        # добор к открытой позиции паузой не блокируется
    assert len(book.opened) == 2 and env.db.get_meta(f"pfpause:{env.alice['id']}:long") is None


def test_closed_positions_come_from_the_calculation_of_active_unexcluded_strategies(env, fake_verify, monkeypatch):
    book, cid = setup(env, fake_verify, monkeypatch, "Активная", **MARKET)
    other = add_cfg(env, "Не в портфеле", **MARKET)
    paused = add_cfg(env, "На паузе", **MARKET)
    idle = add_cfg(env, "Автомод выключен", **MARKET)
    for c in (other, paused):
        env.handle_update(cb(f"ay:{c}:o"))
    env.db.update_config(paused, paused=True)
    prefs(env, exclude=[other])
    trades = [{"group": 1, "side": "short", "reason": "SL", "exit_ts": 1000, "pnl_usd": -5.0}, {"group": 1, "side": "short", "reason": "TP", "exit_ts": 2000, "pnl_usd": 2.0},          # позиция из двух ног: итог −3
              {"group": 2, "side": "long", "reason": "TP", "exit_ts": 3000, "pnl_usd": 4.0}, {"group": 3, "side": "long", "reason": "OPEN", "exit_ts": 4000, "pnl_usd": 1.0}]            # открытая не считается
    seen = []
    env.engine.evaluate = lambda params: seen.append(params) or {"trades": trades}
    out = sorted(env._portfolio_closed(env.alice["id"]))
    assert out == [(2000, "short", -3.0), (3000, "long", 4.0)] and len(seen) == 1                                 # считалась одна «Активная»: остальные — не в портфеле, на паузе, без автомода
    del idle
