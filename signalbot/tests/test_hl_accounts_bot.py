"""Несколько аккаунтов Hyperliquid в боте: подключение второго аккаунта (/hl 2), отдельные переключатели / размер / база у каждого, вход по сигналу на обоих со своим размером,
отключение одного не трогает другой, СТОП ВСЁ закрывает оба. Bybit и Upscale — по-прежнему один счёт."""
import pytest

from conftest import *  # noqa: F401,F403
from test_bybit_bot import Book, _patch  # noqa: F401
from test_monitor_ui import (CHAT, KEY, WALLET, _fresh_monitor_cache, _signal_cycle, add_cfg, cb, fake_verify, kb_data, last_edit, texts_all, tmsg)  # noqa: F401
from test_signalbot import H, T0, env  # noqa: F401

WALLET2 = "0x" + "cd" * 20
KEY2 = "22" * 32


class _Rec:
    """Подмена monkeypatch для _patch: складывает функции-подмены в объект-приёмник, чтобы потом вызвать их с нужной «книгой»."""
    def __init__(self, sink):
        self.sink = sink

    def setattr(self, _cls, name, fn):
        setattr(self.sink, name, fn)


def _connect_two(env, fake_verify, monkeypatch, eq1=1000.0, eq2=500.0):
    """Оба аккаунта подключены; адаптеры — имитации с отдельной «книгой» на каждый слот (маршрутизация по имени слота адаптера)."""
    from app import trading
    env.handle_update(cb("hl:t"))
    env.handle_update(tmsg(f"{WALLET} {KEY}", mid=840))
    env.handle_update(tmsg("/hl 2", mid=841))
    env.handle_update(cb("hl:t:2"))
    env.handle_update(tmsg(f"{WALLET2} {KEY2}", mid=842))
    books = {"hyperliquid": Book(eq1), "hyperliquid2": Book(eq2)}
    sinks = {}
    for name, book in books.items():
        sinks[name] = type("Sink", (), {})()
        _patch(_Rec(sinks[name]), None, book)

    def route(method):
        def call(self, *a, **k):
            return getattr(sinks[self.name], method)(self, *a, **k)
        return call
    for method in ("positions", "account", "open_market", "place_limit_entry", "order_state", "cancel_order_id", "protect", "set_stop", "close_market", "close_all"):
        monkeypatch.setattr(trading.HyperliquidAdapter, method, route(method))
    monkeypatch.setattr(trading.HyperliquidAdapter, "open_orders", lambda self: [])                 # экран биржи тоже спрашивает ордера и исполнения
    monkeypatch.setattr(trading.HyperliquidAdapter, "fills", lambda self, limit=20: [])
    return books


def test_connect_second_account_with_its_own_commands_and_labels(env, fake_verify):
    env.handle_update(cb("hl:t"))
    env.handle_update(tmsg(f"{WALLET} {KEY}", mid=840))
    env.handle_update(tmsg("/hl 2", mid=841))                                                  # команда открывает экран новым сообщением
    body, kb = env.tg.texts(CHAT)[-1], env.tg.keyboards[-1][1]
    assert "Hyperliquid 2" in body and "не подключён" in body and {"hl:t:2", "chl:2"} <= set(kb_data(kb)) and "x" in kb_data(kb)           # из экрана второго — обратно к первому
    env.handle_update(cb("hl:t:2"))
    assert "Подключение Hyperliquid 2" in env.tg.texts(CHAT)[-1] and "hlx:2" in kb_data(env.tg.keyboards[-1][1])
    env.handle_update(tmsg(WALLET2, mid=843))
    assert (CHAT, 843) in env.tg.deleted and "hlx:2" in kb_data(env.tg.keyboards[-1][1])
    env.handle_update(tmsg(KEY2, mid=844))
    assert (CHAT, 844) in env.tg.deleted
    a1, a2 = env.db.get_exchange_account(env.alice["id"]), env.db.get_exchange_account(env.alice["id"], "hyperliquid2")
    assert a1["wallet"] == WALLET and a2["wallet"] == WALLET2 and a2["testnet"] is True and KEY2 not in a2["key_enc"] and a2["key_enc"].startswith("gAAAA")
    assert fake_verify == [(WALLET, KEY, True), (WALLET2, KEY2, True)]
    ok = env.tg.texts(CHAT)[-1]
    assert "Hyperliquid 2 подключён" in ok and "тестовая сеть" in ok and "/ex" in ok and KEY2 not in texts_all(env)
    assert env.alice["id"] not in env.ui.svc._hl
    from app.trading import adapter_for
    s1, s2 = adapter_for(env.db, env.alice["id"]), adapter_for(env.db, env.alice["id"], "hyperliquid2")
    assert (s1.name, s2.name, s1.key, s2.key) == ("hyperliquid", "hyperliquid2", "0x" + KEY, "0x" + KEY2)
    log = [x for x in env.db.list_auto_log(env.alice["id"], 10) if x["kind"] == "exchange_connected"]
    assert {x["details"]["exchange"] for x in log} == {"hyperliquid", "hyperliquid2"}


def test_same_wallet_cannot_be_connected_twice_and_bad_numbers_are_explained(env, fake_verify):
    env.handle_update(cb("hl:t"))
    env.handle_update(tmsg(f"{WALLET} {KEY}", mid=840))
    env.handle_update(cb("hl:t:2"))
    env.handle_update(tmsg(f"{WALLET} {KEY2}", mid=841))                                       # тот же кошелёк в тестовой сети — отказ
    assert "уже подключён как" in env.tg.texts(CHAT)[-1] and "Hyperliquid" in env.tg.texts(CHAT)[-1]
    assert env.db.get_exchange_account(env.alice["id"], "hyperliquid2") is None and env.alice["id"] not in env.ui.svc._hl
    assert fake_verify == [(WALLET, KEY, True)]                                                # до проверки на бирже не дошли
    env.handle_update(tmsg("/hl 9"))
    assert "от 1 до 3" in env.tg.texts(CHAT)[-1] and "/hl 2" in env.tg.texts(CHAT)[-1]
    env.handle_update(tmsg("/hl два"))
    assert "от 1 до 3" in env.tg.texts(CHAT)[-1]
    env.handle_update(tmsg("/hl 1"))
    assert "Кошелёк" in env.tg.texts(CHAT)[-1] and "Hyperliquid" in env.tg.texts(CHAT)[-1]


def test_mainnet_confirmation_and_cancel_are_per_slot(env, fake_verify):
    env.handle_update(cb("x:2"))
    env.handle_update(cb("chl:2"))
    body, kb = last_edit(env)[2], last_edit(env)[3]
    assert "БОЕВУЮ" in body and "Hyperliquid 2" in body and kb_data(kb) == ["hl:m:2", "x:2"]
    env.handle_update(cb("hl:m:2"))
    assert "БОЕВАЯ" in env.tg.texts(CHAT)[-1] and env.ui.svc._hl[env.alice["id"]]["slot"] == "hyperliquid2" and env.ui.svc._hl[env.alice["id"]]["testnet"] is False
    env.handle_update(cb("hlx:2"))
    assert env.alice["id"] not in env.ui.svc._hl and "Hyperliquid 2" in last_edit(env)[2]
    env.handle_update(cb("chl"))                                                               # первый аккаунт — прежние кнопки без номера
    assert kb_data(last_edit(env)[3]) == ["hl:m", "x"]


def test_per_account_toggles_size_and_screens(env, fake_verify, monkeypatch):
    _connect_two(env, fake_verify, monkeypatch)
    cid = add_cfg(env, "Два HL")
    env.handle_update(cb(f"c:{cid}"))
    body, kb = last_edit(env)[2], last_edit(env)[3]
    assert "Hyperliquid 2" in body and {f"a:{cid}:o", f"a:{cid}:o:h2", f"a:{cid}:m:h2", f"ab:{cid}:h2", f"sz:{cid}:h2", f"sz:{cid}:h"} <= set(kb_data(kb))
    assert not any("Hyperliquid 3" in x for x in [body]) and not any(d.endswith(":h3") for d in kb_data(kb))              # третий слот не подключён — не показывается
    env.handle_update(cb(f"a:{cid}:o:h2"))
    assert "Hyperliquid 2" in last_edit(env)[2] and f"ay:{cid}:o:h2" in kb_data(last_edit(env)[3])
    env.handle_update(cb(f"ay:{cid}:o:h2"))
    c = env.db.get_config(cid)
    assert c["ex"]["hyperliquid2"]["open"] and not c["ex"]["hyperliquid"]["open"] and not c["auto"]["open"]
    env.handle_update(cb(f"ab:{cid}:h2"))
    assert env.db.get_config(cid)["ex"]["hyperliquid2"]["base"] == "account" and env.db.get_config(cid)["ex"]["hyperliquid"]["base"] == "config"
    env.handle_update(cb(f"szp:{cid}:10:h2"))
    env.handle_update(cb(f"szl:{cid}:3:h2"))
    assert env.db.get_config(cid)["ex"]["hyperliquid2"]["over"] == {"trade_pct": 10.0, "leverage": 3.0} and env.db.get_config(cid)["ex"]["hyperliquid"]["over"] == {}
    assert "Hyperliquid 2" in last_edit(env)[2] and f"/ex {cid} hl2 trade_pct" in last_edit(env)[2]
    env.handle_update(cb(f"cx:{cid}"))
    assert "Hyperliquid 2" in last_edit(env)[2] and "Hyperliquid 3" not in last_edit(env)[2] and f"sz:{cid}:h2" in kb_data(last_edit(env)[3]) and "hl|hl2|bybit|upscale" in last_edit(env)[2]
    env.handle_update(tmsg(f"/ex {cid} hl2 trade_pct=7"))
    assert env.db.get_config(cid)["ex"]["hyperliquid2"]["over"]["trade_pct"] == 7.0 and "Hyperliquid 2" in env.tg.texts(CHAT)[-1]
    env.handle_update(tmsg(f"/ex {cid} hl3 trade_pct=1"))                                      # слот есть (HL_MAX_ACCOUNTS=3), хотя счёт не подключён — настройки хранятся
    assert env.db.get_config(cid)["ex"]["hyperliquid3"]["over"] == {"trade_pct": 1.0}
    env.handle_update(tmsg(f"/ex {cid} hl4 trade_pct=1"))
    assert "hl2" in env.tg.texts(CHAT)[-1] and "Биржа" in env.tg.texts(CHAT)[-1]
    env.handle_update(cb("m"))
    assert {"x", "x:2", "xb", "xu"} <= set(kb_data(last_edit(env)[3])) and "x:3" not in kb_data(last_edit(env)[3])
    env.handle_update(cb("x:2"))
    ex_body, ex_kb = last_edit(env)[2], last_edit(env)[3]
    assert "Hyperliquid 2" in ex_body and "Капитал" in ex_body and {"cdx:2", "t:h2", "x", "x:3"} <= set(kb_data(ex_kb))              # «➕» ведёт на свободный третий слот
    env.handle_update(cb("x"))
    assert "x:2" in kb_data(last_edit(env)[3]) and "x:3" in kb_data(last_edit(env)[3])


def test_signal_opens_on_both_accounts_with_own_sizes_and_labelled_messages(env, fake_verify, monkeypatch):
    books = _connect_two(env, fake_verify, monkeypatch, eq1=1000.0, eq2=500.0)
    cid = add_cfg(env, "Сигнал")
    for code in ("", ":h2"):
        env.handle_update(cb(f"ay:{cid}:o{code}"))
        env.handle_update(cb(f"ab:{cid}{code}"))
    env.handle_update(tmsg(f"/ex {cid} hl2 trade_pct=5 leverage=3"))
    _signal_cycle(env)
    h1, h2 = books["hyperliquid"], books["hyperliquid2"]
    assert len(h1.opened) == 1 and len(h2.opened) == 1
    _, side1, notional1, lev1, stop1, takes1 = h1.opened[0]
    _, side2, notional2, lev2, stop2, takes2 = h2.opened[0]
    assert side1 == side2 == "long" and abs(notional1 - 400.0) < 1e-6 and abs(lev1 - 2.0) < 1e-9          # 20% × 1000 $ × 2×
    assert abs(notional2 - 75.0) < 1e-6 and abs(lev2 - 3.0) < 1e-9 and stop1 == stop2 and takes1 == takes2   # 5% × 500 $ × 3×; стоп и тейки общие
    opens = [x for x in env.db.list_auto_log(env.alice["id"], 20) if x["kind"] == "open"]
    assert {x["details"]["target"] for x in opens} == {"hyperliquid", "hyperliquid2"} and {x["details"]["exchange"] for x in opens} == {"hyperliquid", "hyperliquid2"} and all(x["status"] == "sent" for x in opens)
    env.cycle()
    assert len(h1.opened) == 1 and len(h2.opened) == 1                                            # повторного входа нет


def test_second_account_alone_trades_without_touching_the_first(env, fake_verify, monkeypatch):
    books = _connect_two(env, fake_verify, monkeypatch)
    cid = add_cfg(env, "Только второй")
    env.handle_update(cb(f"ay:{cid}:o:h2"))
    env.handle_update(cb(f"ab:{cid}:h2"))
    _signal_cycle(env)
    assert len(books["hyperliquid2"].opened) == 1 and books["hyperliquid"].opened == []


def test_messages_name_the_account_only_when_it_is_not_the_only_one(env, fake_verify, monkeypatch):
    books = _connect_two(env, fake_verify, monkeypatch)
    cid = add_cfg(env, "Подписи")
    env.handle_update(cb(f"ay:{cid}:o:h2"))                                                    # база — депозит конфига (5000 $): свободной маржи на втором счёте (450 $) не хватает -> пропуск входа
    _signal_cycle(env)
    skip = [t for t in env.tg.texts(CHAT) if "не хватает свободной маржи" in t]
    assert len(skip) == 1 and "[Hyperliquid 2]" in skip[0] and books["hyperliquid2"].opened == []
    cid2 = add_cfg(env, "Первый один")
    env.handle_update(cb(f"ay:{cid2}:o"))                                                      # только первый аккаунт: подпись счёта не нужна, как раньше
    env.now["t"] += 3600
    _signal_cycle(env)
    only = [t for t in env.tg.texts(CHAT) if "Первый один" in t and "свободной маржи" in t]
    assert only and all("[Hyperliquid" not in t for t in only)


def test_disconnecting_one_account_keeps_the_other_with_its_automode(env, fake_verify, monkeypatch):
    _connect_two(env, fake_verify, monkeypatch)
    cid = add_cfg(env, "Отключение")
    for code in ("", ":h2"):
        env.handle_update(cb(f"ay:{cid}:o{code}"))
    env.handle_update(cb("x:2"))
    env.handle_update(cb("cdx:2"))
    body, kb = last_edit(env)[2], last_edit(env)[3]
    assert "Hyperliquid 2" in body and kb_data(kb) == ["dxy:2", "x:2"]
    env.handle_update(cb("dxy:2"))
    c = env.db.get_config(cid)
    assert env.db.get_exchange_account(env.alice["id"], "hyperliquid2") is None and env.db.get_exchange_account(env.alice["id"])["wallet"] == WALLET
    assert not c["ex"]["hyperliquid2"]["open"] and c["ex"]["hyperliquid"]["open"]
    assert "не подключён" in last_edit(env)[2] and "Hyperliquid 2" in last_edit(env)[2]
    env.handle_update(cb("cdx"))
    assert kb_data(last_edit(env)[3]) == ["dxy", "x"]                                           # первый аккаунт — прежние кнопки без номера


def test_panic_closes_every_hl_account(env, fake_verify, monkeypatch):
    books = _connect_two(env, fake_verify, monkeypatch)
    cid = add_cfg(env, "Паника")
    for code in ("", ":h2"):
        env.handle_update(cb(f"ay:{cid}:o{code}"))
    env.handle_update(cb("cpn"))
    assert "Hyperliquid" in last_edit(env)[2] and "Hyperliquid 2" in last_edit(env)[2]
    env.handle_update(cb("pny"))
    assert books["hyperliquid"].closed_all == 1 and books["hyperliquid2"].closed_all == 1 and not env.db.get_config(cid)["any_open"]


def test_test_screen_buttons_follow_the_exchange_code(env, fake_verify, monkeypatch):
    _connect_two(env, fake_verify, monkeypatch)
    env.handle_update(cb("t:h2"))
    assert "Hyperliquid 2" in last_edit(env)[2] and {"cty:long:h2", "cty:short:h2", "ctc:h2"} <= set(kb_data(last_edit(env)[3]))
    env.handle_update(cb("cty:long:h2"))
    assert "Hyperliquid 2" in last_edit(env)[2] and kb_data(last_edit(env)[3]) == ["tgo:long:h2", "t:h2"]
    env.handle_update(cb("t:u"))                                                                # у Upscale раньше кнопки вели на Bybit (:b) — теперь на Upscale
    assert {"cty:long:u", "cty:short:u", "ctc:u"} <= set(kb_data(last_edit(env)[3]))
