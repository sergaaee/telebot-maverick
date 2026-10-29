"""Bybit (USDT-фьючерсы) в боте и настройки конфига отдельно для каждой биржи: подключение, переключатели, вход на каждой бирже со своим размером и своей лимиткой, повторы, управление, паника."""
import pytest

from conftest import *  # noqa: F401,F403
from test_monitor_ui import (CHAT, KEY, WALLET, _fresh_monitor_cache, _restart, _signal_cycle, add_cfg, cb, fake_verify, kb_data, last_edit, texts_all, tmsg)  # noqa: F401
from test_signalbot import H, LONG, T0, append_reports, env  # noqa: F401

BYKEY = "AbCdEfGh12345"
BYSECRET = "s3cr3tS3cr3tS3cr3tS3cr3t"


@pytest.fixture()
def fake_bybit(monkeypatch):
    calls = []

    def fake(key, secret, net="testnet", exchange=None, user_id=None):
        calls.append((key, secret, net))
        if secret.endswith("BAD"):
            raise ValueError("У ключа есть право вывода средств — такой ключ не принимается.")
        return {"account": {"equity": 500.0, "free": 450.0, "used": 50.0}, "permissions": {}}
    monkeypatch.setattr("sigbot.bot.verify_bybit", fake)
    return calls


class Book:
    """Что бот сделал на одной бирже (имитация адаптера)."""
    def __init__(self, equity):
        self.equity, self.opened, self.placed, self.protected, self.cancelled, self.stops, self.closed, self.closed_all = equity, [], [], [], [], [], [], 0
        self.positions = []
        self.state = {"status": "open", "filled": 0.0, "amount": 1.0, "avg": None}
        self.fail_open = False


def _patch(monkeypatch, cls, book):
    from app import trading

    def open_market(self, sym, side, notional, lev=1.0, stop=None, takes=None):
        if book.fail_open:
            raise trading.ExchangeError("биржа отклонила ордер")
        book.opened.append((sym, side, notional, lev, stop, takes))
        return {"status": "sent"}

    monkeypatch.setattr(cls, "positions", lambda s: book.positions)
    monkeypatch.setattr(cls, "account", lambda s: {"equity": book.equity, "free": book.equity * 0.9, "used": book.equity * 0.1})
    monkeypatch.setattr(cls, "open_market", open_market)
    monkeypatch.setattr(cls, "place_limit_entry", lambda s, sym, side, notional, price, lev=1.0: book.placed.append((sym, side, notional, price, lev)) or {"status": "sent", "order_id": f"L{len(book.placed)}", "price": price})
    monkeypatch.setattr(cls, "order_state", lambda s, oid, sym: dict(book.state))
    monkeypatch.setattr(cls, "cancel_order_id", lambda s, oid, sym: book.cancelled.append(oid) or {"status": "sent"})
    monkeypatch.setattr(cls, "protect", lambda s, sym, side, amount, stop=None, takes=None: book.protected.append((sym, side, amount, stop, takes)) or {"status": "sent", "orders": []})
    monkeypatch.setattr(cls, "set_stop", lambda s, sym, side, price: book.stops.append((sym, side, price)) or {"status": "sent"})
    monkeypatch.setattr(cls, "close_market", lambda s, sym, side, share=1.0: book.closed.append((sym, side, share)) or {"status": "sent"})
    monkeypatch.setattr(cls, "close_all", lambda s: (setattr(book, "closed_all", book.closed_all + 1), {"closed": [{"symbol": "BTC"}], "cancelled": 1, "errors": []})[1])


def _connect_both(env, fake_verify, fake_bybit, monkeypatch, hl_equity=1000.0, by_equity=500.0):
    from app import trading
    env.handle_update(cb("hl:t"))
    env.handle_update(tmsg(f"{WALLET} {KEY}", mid=840))
    env.handle_update(cb("by:t"))
    env.handle_update(tmsg(f"{BYKEY} {BYSECRET}", mid=841))
    hl, by = Book(hl_equity), Book(by_equity)
    _patch(monkeypatch, trading.HyperliquidAdapter, hl)
    _patch(monkeypatch, trading.BybitAdapter, by)
    return hl, by


def _both_on(env, cid, base="account"):
    for code in ("", ":b"):
        env.handle_update(cb(f"ay:{cid}:o{code}"))
        if base == "account":
            env.handle_update(cb(f"ab:{cid}{code}"))


def test_connect_bybit_flow_stores_encrypted_secret_and_deletes_messages(env, fake_bybit):
    env.handle_update(cb("xb"))
    assert "Bybit" in last_edit(env)[2] and "не подключён" in last_edit(env)[2] and "USDT-фьючерсы" in last_edit(env)[2] and {"by:t", "by:d", "cby", "x"} <= set(kb_data(last_edit(env)[3]))
    env.handle_update(cb("by:t"))
    t = env.tg.texts(CHAT)[-1]
    assert "Шаг 1 из 2" in t and "БЕЗ права вывода" in t and "USDT-фьючерсы" in t and "byx" in kb_data(env.tg.keyboards[-1][1])
    env.handle_update(tmsg(BYKEY, mid=801))
    assert (CHAT, 801) in env.tg.deleted and "Шаг 2 из 2" in env.tg.texts(CHAT)[-1]
    env.handle_update(tmsg(BYSECRET, mid=802))
    assert (CHAT, 802) in env.tg.deleted
    acc = env.db.get_exchange_account(env.alice["id"], "bybit")
    assert acc and acc["wallet"] == BYKEY and acc["net"] == "testnet" and acc["testnet"] is True and BYSECRET not in acc["key_enc"] and acc["key_enc"].startswith("gAAAA")
    assert env.db.get_exchange_account(env.alice["id"]) is None and fake_bybit == [(BYKEY, BYSECRET, "testnet")]             # Hyperliquid не затронут
    ok = env.tg.texts(CHAT)[-1]
    assert "Bybit подключён" in ok and "тестовая сеть" in ok and "500" in ok and "AbCdEf…2345" in ok and "удалены из чата" in ok
    assert BYSECRET not in texts_all(env) and BYKEY not in texts_all(env) and env.alice["id"] not in env.ui.svc._by
    from app.trading import BybitAdapter, DryRunAdapter, adapter_for
    ad = adapter_for(env.db, env.alice["id"], "bybit")
    assert isinstance(ad, BybitAdapter) and ad.key == BYSECRET and isinstance(adapter_for(env.db, env.bob["id"], "bybit"), DryRunAdapter)
    env.handle_update(cb("xb"))
    assert "Bybit" in last_edit(env)[2] and "тестовая сеть" in last_edit(env)[2] and "AbCdEf…2345" in last_edit(env)[2] and "cdb" in kb_data(last_edit(env)[3])


def test_connect_bybit_errors_attempts_cancel_demo_and_live(env, fake_bybit):
    env.handle_update(cb("by:d"))
    assert "демо" in env.tg.texts(CHAT)[-1].lower()
    env.handle_update(tmsg("коротко", mid=810))                                                 # не похоже на ключ
    assert "10–40" in env.tg.texts(CHAT)[-1] and (CHAT, 810) in env.tg.deleted and not fake_bybit
    env.handle_update(tmsg(f"{BYKEY} {BYSECRET}BAD", mid=811))                                  # биржа/проверка отказала
    assert "❌" in env.tg.texts(CHAT)[-1] and "вывод" in env.tg.texts(CHAT)[-1] and env.db.get_exchange_account(env.alice["id"], "bybit") is None
    assert env.ui.svc._by[env.alice["id"]]["step"] == "secret"                                  # ключ принят, ждём верный секрет
    env.handle_update(tmsg(BYSECRET, mid=812))
    acc = env.db.get_exchange_account(env.alice["id"], "bybit")
    assert acc["net"] == "demo" and acc["testnet"] is True and "демо" in env.tg.texts(CHAT)[-1].lower()
    # отмена и пять неудачных попыток
    env.handle_update(cb("by:t"))
    env.handle_update(cb("byx"))
    assert env.alice["id"] not in env.ui.svc._by
    env.handle_update(tmsg(f"{BYKEY} {BYSECRET}", mid=813))                                     # после отмены это обычный текст, а не сценарий
    assert env.db.get_exchange_account(env.alice["id"], "bybit")["net"] == "demo"
    env.handle_update(cb("by:t"))
    for i in range(5):
        env.handle_update(tmsg(f"{BYKEY} {BYSECRET}BAD", mid=820 + i))
    assert env.alice["id"] not in env.ui.svc._by and "Слишком много попыток" in env.tg.texts(CHAT)[-1]
    # боевая сеть — через подтверждение
    env.handle_update(cb("cby"))
    assert "БОЕВОЙ" in last_edit(env)[2] and "by:m" in kb_data(last_edit(env)[3])
    env.handle_update(cb("by:m"))
    env.handle_update(tmsg(f"{BYKEY} {BYSECRET}", mid=830))
    acc = env.db.get_exchange_account(env.alice["id"], "bybit")
    assert acc["net"] == "live" and acc["testnet"] is False and "БОЕВАЯ" in env.tg.texts(CHAT)[-1]
    # команда /cancel посреди подключения
    env.handle_update(tmsg("/bybit", mid=831))
    env.handle_update(cb("by:t"))
    env.handle_update(tmsg("/cancel", mid=832))
    assert env.alice["id"] not in env.ui.svc._by and "отменено" in env.tg.texts(CHAT)[-1]


def test_each_exchange_has_its_own_toggles_and_confirmation(env, fake_verify, fake_bybit, monkeypatch):
    _connect_both(env, fake_verify, fake_bybit, monkeypatch)
    cid = add_cfg(env, "Две биржи")
    env.handle_update(cb(f"c:{cid}"))
    body, kb = last_edit(env)[2], last_edit(env)[3]
    assert "Hyperliquid" in body and "Bybit" in body and {f"a:{cid}:o", f"a:{cid}:o:b", f"a:{cid}:m:b", f"ab:{cid}:b", f"sz:{cid}:b", f"sz:{cid}:h", f"cx:{cid}"} <= set(kb_data(kb))
    env.handle_update(cb(f"a:{cid}:o:b"))                                                       # включение — через подтверждение, и оно про Bybit
    assert "Bybit" in last_edit(env)[2] and f"ay:{cid}:o:b" in kb_data(last_edit(env)[3]) and not env.db.get_config(cid)["ex"]["bybit"]["open"]
    env.handle_update(cb(f"ay:{cid}:o:b"))
    c = env.db.get_config(cid)
    assert c["ex"]["bybit"]["open"] and not c["ex"]["hyperliquid"]["open"] and not c["auto"]["open"]
    env.handle_update(cb(f"a:{cid}:o:b"))                                                       # выключение сразу
    assert not env.db.get_config(cid)["ex"]["bybit"]["open"]
    env.handle_update(cb(f"ab:{cid}:b"))
    assert env.db.get_config(cid)["ex"]["bybit"]["base"] == "account" and env.db.get_config(cid)["ex"]["hyperliquid"]["base"] == "config"
    env.handle_update(cb(f"cx:{cid}"))
    assert "по биржам" in last_edit(env)[2] and "Bybit" in last_edit(env)[2] and "/ex" in last_edit(env)[2]
    env.handle_update(cb("m"))
    assert "Hyperliquid" in last_edit(env)[2] and "Bybit" in last_edit(env)[2] and "xb" in kb_data(last_edit(env)[3])


def test_signal_opens_on_each_exchange_with_its_own_size(env, fake_verify, fake_bybit, monkeypatch):
    hl, by = _connect_both(env, fake_verify, fake_bybit, monkeypatch, hl_equity=1000.0, by_equity=500.0)
    cid = add_cfg(env, "Размеры")
    _both_on(env, cid)
    env.handle_update(tmsg(f"/ex {cid} bybit trade_pct=5 leverage=3"))                           # на Bybit — 5% × 3×, на Hyperliquid — как в конфиге (20% × 2×)
    assert env.db.get_config(cid)["params"]["trade_pct"] == 20 and env.db.get_config(cid)["ex"]["bybit"]["over"] == {"trade_pct": 5.0, "leverage": 3.0}
    assert "доля 5%" in env.tg.texts(CHAT)[-1] and "Bybit" in env.tg.texts(CHAT)[-1]
    _signal_cycle(env)
    assert len(hl.opened) == 1 and len(by.opened) == 1
    sym, side, notional, lev, stop, takes = hl.opened[0]
    assert side == "long" and abs(notional - 400.0) < 1e-6 and abs(lev - 2.0) < 1e-9                  # 20% × 1000 $ × 2×
    bsym, bside, bnot, blev, bstop, btakes = by.opened[0]
    assert bsym == "BTC/USDT:USDT" and bside == "long" and abs(bnot - 75.0) < 1e-6 and abs(blev - 3.0) < 1e-9       # 5% × 500 $ × 3×
    assert stop == bstop and takes == btakes                                                      # стоп и тейки общие — из конфига
    opens = [x for x in env.db.list_auto_log(env.alice["id"], 20) if x["kind"] == "open"]
    assert {x["details"]["target"] for x in opens} == {"hyperliquid", "bybit"} and all(x["status"] == "sent" for x in opens)
    env.cycle()
    assert len(hl.opened) == 1 and len(by.opened) == 1                                           # повторного входа нет


def test_exchange_without_toggle_does_not_trade_and_simulation_is_per_exchange(env, fake_verify, fake_bybit, monkeypatch):
    hl, by = _connect_both(env, fake_verify, fake_bybit, monkeypatch)
    cid = add_cfg(env, "Только Bybit")
    env.handle_update(cb(f"ay:{cid}:o:b"))
    env.handle_update(cb(f"ab:{cid}:b"))
    _signal_cycle(env)
    assert len(by.opened) == 1 and hl.opened == [] and env.db.get_pending(cid) is None


def test_limit_entry_only_on_the_exchange_that_asks_for_it(env, fake_verify, fake_bybit, monkeypatch):
    hl, by = _connect_both(env, fake_verify, fake_bybit, monkeypatch)
    cid = add_cfg(env, "Лимитка на Bybit")                                                        # в конфиге вход по рынку
    _both_on(env, cid)
    env.handle_update(tmsg(f"/ex {cid} bybit entry_limit_pct=0.5 entry_limit_dir=pullback entry_limit_ttl_h=2"))
    _signal_cycle(env)
    assert len(hl.opened) == 1 and hl.placed == []                                                # Hyperliquid — по рынку
    assert by.opened == [] and len(by.placed) == 1 and abs(by.placed[0][3] - 99.5) < 1e-9          # Bybit — лимитка на откат 0.5% от цены сигнала 100
    assert env.db.get_pending(cid) is None and env.db.get_pending(cid, "bybit")["order_id"] == "L1"
    assert any("[Bybit]" in t and "лимитку" in t for t in env.tg.texts(CHAT))
    by.state = {"status": "filled", "filled": 1.0, "amount": 1.0, "avg": 99.5}
    by.positions = [{"symbol": "BTC/USDT:USDT", "side": "long", "size": 1.0, "entry": 99.4}]
    env.cycle()
    assert env.db.get_pending(cid, "bybit") is None and len(by.protected) == 1 and hl.protected == []
    assert abs(by.protected[0][3] - 99.4 * 0.6) < 1e-6                                            # стоп от цены исполнения (SL 40% в тестовом конфиге)
    # лимитка в конфиге, а на Hyperliquid — «по рынку» (0)
    env2cid = add_cfg(env, "Рынок на HL", entry_limit_pct=0.5, entry_limit_dir="pullback", entry_limit_ttl_h=2)
    assert env2cid != cid
    env.handle_update(tmsg(f"/ex {env2cid} hl entry_limit_pct=0"))
    assert env.db.get_config(env2cid)["ex"]["hyperliquid"]["over"] == {"entry_limit_pct": 0.0}


def test_pending_orders_are_tracked_and_cancelled_per_exchange(env, fake_verify, fake_bybit, monkeypatch):
    hl, by = _connect_both(env, fake_verify, fake_bybit, monkeypatch)
    cid = add_cfg(env, "Две лимитки")
    _both_on(env, cid)
    for ex in ("hyperliquid", "bybit"):
        env.db.set_pending(cid, env.alice["id"], 1, "BTC/USDT:USDT", "long", "pullback", 99.5, 100.0, 400.0, 200.0, 2.0, f"ord-{ex}", env.now["t"] + 3600, exchange=ex)
    env.handle_update(cb(f"a:{cid}:o:b"))                                                       # выключили «открывать» только на Bybit
    env.cycle()
    assert by.cancelled == ["ord-bybit"] and hl.cancelled == [] and env.db.get_pending(cid, "bybit") is None and env.db.get_pending(cid)["order_id"] == "ord-hyperliquid"
    assert any("[Bybit]" in t and "снят" in t for t in env.tg.texts(CHAT))
    # удаление конфига снимает лимитки на всех биржах
    env.db.set_pending(cid, env.alice["id"], 1, "BTC/USDT:USDT", "long", "pullback", 99.5, 100.0, 400.0, 200.0, 2.0, "ord-b2", env.now["t"] + 3600, exchange="bybit")
    env.handle_update(cb(f"dy:{cid}"))
    assert set(by.cancelled) == {"ord-bybit", "ord-b2"} and "ord-hyperliquid" in hl.cancelled and env.db.get_config(cid) is None


def test_failed_entry_on_one_exchange_is_retried_only_there(env, fake_verify, fake_bybit, monkeypatch):
    hl, by = _connect_both(env, fake_verify, fake_bybit, monkeypatch)
    by.fail_open = True
    cid = add_cfg(env, "Повтор на Bybit")
    _both_on(env, cid)
    _signal_cycle(env)
    assert len(hl.opened) == 1 and by.opened == []
    assert [(r["config_id"], r["exchange"]) for r in env.db.list_retries()] == [(cid, "bybit")]
    assert any("[Bybit]" in t and "не выполнен" in t for t in env.tg.texts(CHAT))
    by.fail_open = False
    env2 = _restart(env)
    assert env2._retry_ids == {(cid, "bybit")}
    env2.cycle()
    assert len(by.opened) == 1 and len(hl.opened) == 1 and env.db.list_retries() == []             # Hyperliquid второй раз не входит
    assert any("повторяю" in t and "[Bybit]" in t for t in env.tg.texts(CHAT))


def test_manage_acts_only_on_exchanges_with_manage_on(env, fake_verify, fake_bybit, monkeypatch):
    hl, by = _connect_both(env, fake_verify, fake_bybit, monkeypatch)
    cid = add_cfg(env, "Управление")
    env.db.set_auto(env.alice["id"], cid, None, True, exchange="bybit")
    cfg = env.db.get_config(cid)
    res = {"params": {"symbol": "BTC/USDT:USDT"}}
    env._auto_fill(cfg, res, {"tp1_be": True, "entry": 100.0, "label": "TP1", "side": "long", "ts": 1, "share": 0.5})
    assert by.stops == [("BTC/USDT:USDT", "long", 100.0)] and hl.stops == []
    env._auto_fill(cfg, res, {"label": "WKND", "side": "long", "ts": 2, "share": 1.0})
    assert by.closed == [("BTC/USDT:USDT", "long", 1.0)] and hl.closed == []
    env.db.set_auto(env.alice["id"], cid, None, True)                                           # включили и на Hyperliquid
    env._auto_fill(env.db.get_config(cid), res, {"label": "REV", "side": "long", "ts": 3, "share": 1.0})
    assert len(by.closed) == 2 and len(hl.closed) == 1


def test_ex_command_sets_clears_and_validates(env, fake_verify, fake_bybit, monkeypatch):
    cid = add_cfg(env, "Команда")
    env.handle_update(tmsg("/ex"))
    assert "/ex №" in env.tg.texts(CHAT)[-1] and "trade_pct" in env.tg.texts(CHAT)[-1]
    env.handle_update(tmsg(f"/ex {cid}"))
    assert "по биржам" in env.tg.texts(CHAT)[-1]                                                  # открылся экран «настройки по биржам»
    over = lambda ex: env.db.get_config(cid)["ex"][ex]["over"]  # noqa: E731
    env.handle_update(tmsg(f"/ex {cid} by trade_pct=7,5 leverage=4 entry_limit_pct=0.3 entry_limit_dir=breakout entry_limit_ttl_h=3"))
    assert over("bybit") == {"trade_pct": 7.5, "leverage": 4.0, "entry_limit_pct": 0.3, "entry_limit_dir": "breakout", "entry_limit_ttl_h": 3.0} and over("hyperliquid") == {}
    assert "по ходу" in env.tg.texts(CHAT)[-1] and "7.5" in env.tg.texts(CHAT)[-1]
    env.handle_update(tmsg(f"/ex {cid} bybit trade_pct=-"))                                       # «-» убирает одну настройку
    assert "trade_pct" not in over("bybit") and over("bybit")["leverage"] == 4.0
    env.handle_update(tmsg(f"/ex {cid} hl leverage=9"))
    assert over("hyperliquid") == {"leverage": 9.0}
    env.handle_update(tmsg(f"/ex {cid} bybit reset"))
    assert over("bybit") == {} and over("hyperliquid") == {"leverage": 9.0} and "берётся из конфига" in env.tg.texts(CHAT)[-1]
    for bad, expect in ((f"/ex {cid} kraken leverage=2", "Биржа"), (f"/ex {cid} bybit foo=1", "Не понял"), (f"/ex {cid} bybit leverage=abc", "нужно число"),
                        (f"/ex {cid} bybit leverage=500", "⚠️"), (f"/ex {cid} bybit entry_limit_dir=вбок", "⚠️"), (f"/ex {cid} bybit sl_pct=3", "Не понял")):
        env.handle_update(tmsg(bad))
        assert expect in env.tg.texts(CHAT)[-1], bad
    assert over("bybit") == {}                                                                    # ошибки ничего не записали
    n = len(env.tg.texts(CHAT))
    env.handle_update(tmsg("/ex 999 bybit leverage=2"))
    assert len(env.tg.texts(CHAT)) == n + 1 and over("bybit") == {}                               # бот ответил (конфига нет) и ничего не записал


def test_sizing_screens_for_exchange_do_not_touch_the_config(env, fake_verify, fake_bybit, monkeypatch):
    _connect_both(env, fake_verify, fake_bybit, monkeypatch)
    cid = add_cfg(env, "Размер по биржам")
    env.handle_update(cb(f"sz:{cid}:b"))
    body, kb = last_edit(env)[2], last_edit(env)[3]
    assert "Bybit" in body and "Только для Bybit" in body and "20%" in body and {f"szp:{cid}:10:b", f"szl:{cid}:5:b", f"szr:{cid}:b"} <= set(kb_data(kb))
    env.handle_update(cb(f"szp:{cid}:10:b"))
    env.handle_update(cb(f"szl:{cid}:5:b"))
    c = env.db.get_config(cid)
    assert c["ex"]["bybit"]["over"] == {"trade_pct": 10.0, "leverage": 5.0} and c["params"]["trade_pct"] == 20 and c["params"]["leverage"] == 2 and c["ex"]["hyperliquid"]["over"] == {}
    assert "(своя)" in last_edit(env)[2] and "10%" in last_edit(env)[2]
    env.handle_update(cb(f"szp:{cid}:15:h"))                                                    # Hyperliquid — своя настройка, конфиг тоже не меняется
    assert env.db.get_config(cid)["ex"]["hyperliquid"]["over"] == {"trade_pct": 15.0} and env.db.get_config(cid)["params"]["trade_pct"] == 20
    env.handle_update(cb(f"szr:{cid}:b"))
    assert env.db.get_config(cid)["ex"]["bybit"]["over"] == {} and env.db.get_config(cid)["ex"]["hyperliquid"]["over"] == {"trade_pct": 15.0}
    env.handle_update(cb(f"szp:{cid}:30"))                                                      # без биржи — как раньше, меняется сам конфиг
    assert env.db.get_config(cid)["params"]["trade_pct"] == 30


def test_panic_disconnect_and_tests_on_bybit(env, fake_verify, fake_bybit, monkeypatch):
    hl, by = _connect_both(env, fake_verify, fake_bybit, monkeypatch)
    cid = add_cfg(env, "Паника на двух")
    _both_on(env, cid, base="config")
    env.handle_update(cb("cpn"))
    assert "Hyperliquid" in last_edit(env)[2] and "Bybit" in last_edit(env)[2]
    env.handle_update(cb("pny"))
    assert hl.closed_all == 1 and by.closed_all == 1 and not env.db.get_config(cid)["any_open"]
    # тестовая позиция на Bybit
    env.handle_update(cb("t:b"))
    assert "Bybit" in last_edit(env)[2] and "cty:long:b" in kb_data(last_edit(env)[3]) and "ctc:b" in kb_data(last_edit(env)[3])
    env.handle_update(cb("cty:long:b"))
    assert "Bybit" in last_edit(env)[2] and "tgo:long:b" in kb_data(last_edit(env)[3])
    env.handle_update(cb("tgo:long:b"))
    assert by.opened and by.opened[0][0] == "BTC/USDT:USDT" and abs(by.opened[0][2] - 6.0) < 1e-6 and hl.opened == []         # маржа 1 $ × 6× (минимум Bybit 5 $ с запасом 10%)
    env.handle_update(cb("tcgo:b"))
    assert by.closed and hl.closed == []
    # отключение Bybit не трогает Hyperliquid
    _both_on(env, cid, base="config")
    env.handle_update(cb("cdb"))
    assert "Bybit" in last_edit(env)[2] and "dxb" in kb_data(last_edit(env)[3])
    env.handle_update(cb("dxb"))
    c = env.db.get_config(cid)
    assert env.db.get_exchange_account(env.alice["id"], "bybit") is None and env.db.get_exchange_account(env.alice["id"]) and c["ex"]["hyperliquid"]["open"] and not c["ex"]["bybit"]["open"]
    assert "не подключён" in last_edit(env)[2]


def test_export_fills_per_exchange_and_digest_lists_both(env, fake_verify, fake_bybit, monkeypatch):
    from app import trading
    hl, by = _connect_both(env, fake_verify, fake_bybit, monkeypatch)
    monkeypatch.setattr(trading.BybitAdapter, "fills", lambda self, limit=20: [{"symbol": "BTC/USDT:USDT", "side": "buy", "amount": 1.0, "price": 100.0, "fee": 0.1, "pnl": None, "ts": 1_700_000_000_000}])
    monkeypatch.setattr(trading.HyperliquidAdapter, "fills", lambda self, limit=20: [])
    env.handle_update(tmsg("/export fills bybit"))
    assert env.tg.docs[-1][1] == "maverick-fills.csv" and "BTC/USDT:USDT" in env.tg.docs[-1][2].decode("utf-8")
    env.handle_update(tmsg("/export fills kraken"))
    assert "hl" in env.tg.texts(CHAT)[-1] and "bybit" in env.tg.texts(CHAT)[-1]
    add_cfg(env, "Для сводки")
    env.handle_update(tmsg("/digest now"))
    t = env.tg.texts(CHAT)[-1]
    assert "Hyperliquid" in t and "Bybit" in t and "капитал" in t
    env.handle_update(cb("cdb"))
    env.handle_update(cb("dxb"))
    env.handle_update(tmsg("/export fills bybit"))
    assert "Bybit не подключён" in env.tg.texts(CHAT)[-1] and "/bybit" in env.tg.texts(CHAT)[-1]


def test_catchup_enters_every_exchange_with_its_own_size(env, fake_verify, fake_bybit, monkeypatch):
    """Включили автомод «открывать» при включённом «догнать сигнал»: вход по свежему сигналу — на каждой включённой бирже (каждая со своим размером)."""
    hl, by = _connect_both(env, fake_verify, fake_bybit, monkeypatch)
    start = T0 + 38 * H
    append_reports(env.hist, LONG(start, 3), 11)
    env.now["t"] = (start + 3 * H // 2) / 1000
    from app import monitor
    monitor._cache.clear()
    cid = add_cfg(env, "Догнать на двух")
    env.cycle()                                                                                   # сигнал пришёл, пока автомод был выключен
    assert hl.opened == [] and by.opened == []
    env.handle_update(tmsg(f"/ex {cid} bybit trade_pct=5"))
    env.handle_update(tmsg(f"/catchup {cid} on 120 5"))
    for code in ("", ":b"):
        env.handle_update(cb(f"ay:{cid}:o{code}"))
        env.handle_update(cb(f"ab:{cid}{code}"))
    env.cycle()
    assert len(hl.opened) == 1 and len(by.opened) == 1
    assert abs(hl.opened[0][2] - 400.0) < 1e-6 and abs(by.opened[0][2] - 50.0) < 1e-6 and abs(by.opened[0][3] - 2.0) < 1e-9           # Bybit: 5% × 500 $ × 2× (плечо из конфига)
    assert not env.db.get_config(cid)["catchup"]["armed"]


def test_order_size_applies_exchange_overrides():
    from app import manage
    from app.trading import DryRunAdapter
    from sigbot.engine import Engine
    res = {"params": {"deposit": 1000.0, "compound": False, "trade_pct": 20, "leverage": 2, "pyramid_pct": 10, "pyramid_leverage": None}, "stats": {"total": {"total_usd": 0.0}}}
    assert Engine.order_size(res) == (400.0, 200.0)
    assert Engine.order_size(res, over={"trade_pct": 5, "leverage": 3}) == (150.0, 50.0)
    assert Engine.order_size(res, add=True, over={"pyramid_pct": 4, "pyramid_leverage": 10}) == (400.0, 40.0)
    assert Engine.order_size(res, equity=500.0, over={"trade_pct": 10}) == (100.0, 50.0)
    assert Engine.order_size(res, over={"entry_limit_pct": 1.0}) == (400.0, 200.0)                  # лишние ключи на размер не влияют
    sz = manage.size_order(Engine.order_size, res, False, DryRunAdapter(), "config", None, {"trade_pct": 5, "leverage": 3})
    assert sz["notional"] == 150.0 and sz["margin"] == 50.0 and manage.size_order(Engine.order_size, res, False, DryRunAdapter(), "config", None)["notional"] == 400.0


def test_catchup_of_an_add_is_done_per_exchange_limit_exchange_gets_only_the_first_leg(env, fake_verify, fake_bybit, monkeypatch):
    """Последний сигнал — добор. Hyperliquid (по рынку) догоняет позицию целиком: первая нога + добор. Bybit (свой вход лимиткой) ставит только первую ногу: добирать можно лишь в открытую позицию."""
    from app import trading
    from test_monitor_ui import PYR, _positions_follow_orders, _real_account
    _real_account(env, fake_verify, monkeypatch, equity=1000.0, free=900.0)
    hl_book = _positions_follow_orders(monkeypatch)                                              # Hyperliquid: вход создаёт позицию, добор её требует
    by = Book(500.0)
    _patch(monkeypatch, trading.BybitAdapter, by)
    env.handle_update(cb("by:t"))
    env.handle_update(tmsg(f"{BYKEY} {BYSECRET}", mid=860))
    cid = add_cfg(env, "Добор на двух", **PYR)
    env.handle_update(cb(f"ab:{cid}"))
    env.handle_update(cb(f"ab:{cid}:b"))
    _signal_cycle(env)
    assert not hl_book["opened"] and not by.opened and not by.placed                              # автомод был выключен
    env.handle_update(tmsg(f"/ex {cid} bybit entry_limit_pct=0.5 entry_limit_dir=pullback entry_limit_ttl_h=3"))
    env.handle_update(tmsg(f"/catchup {cid} on 120 0.1"))
    env.handle_update(cb(f"ay:{cid}:o"))
    env.handle_update(cb(f"ay:{cid}:o:b"))
    env.cycle()
    assert [o[0] for o in hl_book["opened"]] == ["long", "long"] and hl_book["opened"][1][1] == 200.0           # Hyperliquid: нога и добор
    assert len(by.placed) == 1 and by.opened == [] and env.db.get_pending(cid, "bybit") and env.db.get_pending(cid) is None
    assert any("добор не ставлю" in t and "Bybit" in t for t in env.tg.texts(CHAT))
