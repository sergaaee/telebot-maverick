"""Ручное закрытие любой позиции на бирже из Telegram-бота: /close и кнопки «❌ Закрыть позицию» — список позиций, выбор доли, закрытие по рынку."""
import pytest

from conftest import *  # noqa: F401,F403
from test_monitor_ui import CHAT, KEY, WALLET, cb, fake_verify, kb_data, last_edit, tmsg  # noqa: F401
from test_signalbot import env  # noqa: F401

WALLET2 = "0x" + "cd" * 20
KEY2 = "22" * 32


class Acct:
    """Позиции и закрытия одного счёта."""
    def __init__(self, positions):
        self.positions, self.closed, self.fail = positions, [], None


def _connect(env, fake_verify, monkeypatch, pos1, pos2=None):
    from app import trading
    env.handle_update(cb("hl:t"))
    env.handle_update(tmsg(f"{WALLET} {KEY}", mid=840))
    accts = {"hyperliquid": Acct(pos1)}
    if pos2 is not None:
        env.handle_update(cb("hl:t:2"))
        env.handle_update(tmsg(f"{WALLET2} {KEY2}", mid=841))
        accts["hyperliquid2"] = Acct(pos2)

    def close(self, symbol, side, share=1.0):
        a = accts[self.name]
        if a.fail:
            raise trading.ExchangeError(a.fail)
        a.closed.append((symbol, side, share))
        return {"status": "sent", "symbol": symbol, "closed": share}
    monkeypatch.setattr(trading.HyperliquidAdapter, "positions", lambda self: [dict(p) for p in accts[self.name].positions])
    monkeypatch.setattr(trading.HyperliquidAdapter, "close_market", close)
    monkeypatch.setattr(trading.HyperliquidAdapter, "account", lambda self: {"equity": 1000.0, "free": 900.0, "used": 100.0})
    monkeypatch.setattr(trading.HyperliquidAdapter, "open_orders", lambda self: [])
    monkeypatch.setattr(trading.HyperliquidAdapter, "fills", lambda self, limit=20: [])
    return accts


BTC = {"symbol": "BTC/USDC:USDC", "side": "long", "size": 0.5, "entry": 100.0, "mark": 101.0, "liq": 60.0, "leverage": 5, "pnl_usd": 12.0}
PEPE = {"symbol": "kPEPE/USDC:USDC", "side": "short", "size": 1000.0, "entry": 0.01, "mark": 0.0103, "liq": 0.02, "leverage": 3, "pnl_usd": -3.0}
ETH = {"symbol": "ETH/USDC:USDC", "side": "long", "size": 2.0, "entry": 50.0, "mark": 50.5, "liq": 30.0, "leverage": 2, "pnl_usd": 1.0}


def test_close_command_lists_positions_of_every_account_with_a_button_for_each(env, fake_verify, monkeypatch):
    _connect(env, fake_verify, monkeypatch, [BTC, PEPE], [ETH])
    env.handle_update(tmsg("/close", mid=900))
    body, kb = env.tg.texts(CHAT)[-1], env.tg.keyboards[-1][1]
    assert "Закрыть позицию" in body and "BTC лонг" in body and "kPEPE шорт" in body and "ETH лонг" in body and "Hyperliquid 2" in body
    assert {"clc:h:BTC:l", "clc:h:kPEPE:s", "clc:h2:ETH:l", "cl", "m"} <= set(kb_data(kb))
    env.handle_update(tmsg("/close hl2", mid=901))
    body, kb = env.tg.texts(CHAT)[-1], env.tg.keyboards[-1][1]
    assert "ETH лонг" in body and "BTC лонг" not in body and "clc:h:BTC:l" not in kb_data(kb) and "cl:h2" in kb_data(kb)                        # только второй аккаунт
    env.handle_update(tmsg("/close kraken", mid=902))
    assert "Биржа:" in env.tg.texts(CHAT)[-1]


def test_choose_the_share_and_close_at_market(env, fake_verify, monkeypatch):
    accts = _connect(env, fake_verify, monkeypatch, [BTC, PEPE])
    env.handle_update(cb("clc:h:BTC:l"))
    body, kb = last_edit(env)[2], last_edit(env)[3]
    assert "Закрыть по рынку" in body and "BTC лонг" in body and "0.5" in body and kb_data(kb) == ["cly:h:BTC:l:25", "cly:h:BTC:l:50", "cly:h:BTC:l:75", "cly:h:BTC:l:100", "cl:h"]
    env.handle_update(cb("cly:h:BTC:l:50"))
    assert accts["hyperliquid"].closed == [("BTC/USDC:USDC", "long", 0.5)]
    assert "закрыто 50%" in env.tg.answers[-1][1] and not env.tg.answers[-1][2]
    log = [x for x in env.db.list_auto_log(env.alice["id"], 10) if x["kind"] == "manual_close"]
    assert len(log) == 1 and log[0]["status"] == "sent" and log[0]["details"]["share"] == 0.5
    env.handle_update(cb("cly:h:kPEPE:s:100"))                                                         # монета с маленькой буквой и шорт
    assert accts["hyperliquid"].closed[-1] == ("kPEPE/USDC:USDC", "short", 1.0) and "закрыто 100%" in env.tg.answers[-1][1]


def test_second_account_is_closed_separately(env, fake_verify, monkeypatch):
    accts = _connect(env, fake_verify, monkeypatch, [BTC], [ETH])
    env.handle_update(cb("cly:h2:ETH:l:100"))
    assert accts["hyperliquid2"].closed == [("ETH/USDC:USDC", "long", 1.0)] and accts["hyperliquid"].closed == []
    assert "Hyperliquid 2" in env.tg.answers[-1][1]


def test_vanished_position_and_exchange_errors_are_shown_not_sent(env, fake_verify, monkeypatch):
    accts = _connect(env, fake_verify, monkeypatch, [BTC])
    env.handle_update(cb("cly:h:ETH:l:100"))                                                           # такой позиции нет
    assert accts["hyperliquid"].closed == [] and env.tg.answers[-1][2] and "нет позиции" in env.tg.answers[-1][1]
    env.handle_update(cb("clc:h:ETH:l"))
    assert "уже нет" in last_edit(env)[2]
    accts["hyperliquid"].fail = "биржа не отвечает"
    env.handle_update(cb("cly:h:BTC:l:100"))
    assert env.tg.answers[-1][2] and "биржа не отвечает" in env.tg.answers[-1][1] and accts["hyperliquid"].closed == []
    assert [x["status"] for x in env.db.list_auto_log(env.alice["id"], 5) if x["kind"] == "manual_close"] == ["error"]
    env.handle_update(cb("cly:h:BTC:l:abc"))
    assert env.tg.answers[-1][2] and "Неверная доля" in env.tg.answers[-1][1]


def test_entry_points_without_an_account_and_buttons_on_other_screens(env, fake_verify, monkeypatch):
    env.handle_update(tmsg("/close", mid=900))
    assert "Биржа не подключена" in env.tg.texts(CHAT)[-1]
    env.handle_update(cb("cly:h:BTC:l:100"))
    assert env.tg.answers[-1][2] and "не подключён" in env.tg.answers[-1][1]
    _connect(env, fake_verify, monkeypatch, [BTC])
    env.handle_update(cb("m"))
    assert "cl" in kb_data(last_edit(env)[3])
    env.handle_update(cb("x"))
    assert "cl:h" in kb_data(last_edit(env)[3])
    from app import trading
    monkeypatch.setattr(trading.HyperliquidAdapter, "positions", lambda self: (_ for _ in ()).throw(trading.ExchangeError("нет связи")))
    env.handle_update(cb("cl"))
    assert "нет связи" in last_edit(env)[2]
