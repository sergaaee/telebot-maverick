"""Upscale в боте: подключение (ключ -> выбор счёта -> подтверждение платного), экран биржи, автомод на Upscale со своим размером."""
import re

import pytest

from conftest import *  # noqa: F401,F403
from test_monitor_ui import _fresh_monitor_cache, _signal_cycle, add_cfg, cb, fake_verify, kb_data, last_edit, texts_all, tmsg  # noqa: F401
from test_bybit_bot import Book, _patch, _both_on  # noqa: F401
from test_signalbot import CHAT, env  # noqa: F401

UPKEY = "usk_" + "a1B2c3D4" * 4
ACC1 = "11111111-1111-4111-8111-111111111111"
ACC2 = "22222222-2222-4222-8222-222222222222"


def flat(text):
    """Пробелы-разделители тысяч в сообщениях бота бывают неразрывными: сводим к обычному пробелу."""
    return re.sub(r"\s", " ", text)


def account(aid, typ, equity, phase="active_evaluation"):
    return {"accountId": aid, "type": typ, "phase": phase, "status": "active", "apiTrading": True, "equity": equity, "balance": equity, "markets": "crypto"}


@pytest.fixture()
def fake_upscale(monkeypatch):
    state = {"accounts": [account(ACC1, "demo", 1000.0)], "calls": []}

    def fake(key, account_id=None, base_url=None, transport=None, user_id=None):
        state["calls"].append(key)
        if key.endswith("BAD"):
            raise ValueError("Ключ недействителен, отозван или истёк")
        return {"accounts": state["accounts"], "account": state["accounts"][0] if len(state["accounts"]) == 1 else None}
    monkeypatch.setattr("sigbot.bot.verify_upscale", fake)
    return state


def test_connect_demo_account_flow_stores_encrypted_key_and_deletes_the_message(env, fake_upscale):
    env.handle_update(cb("xu"))
    assert "Upscale" in last_edit(env)[2] and "не подключён" in last_edit(env)[2] and "демо-счёта" in last_edit(env)[2] and {"up", "x", "xb"} <= set(kb_data(last_edit(env)[3]))
    env.handle_update(cb("up"))
    t = env.tg.texts(CHAT)[-1]
    assert "usk_" in t and "демо-счёта" in t and "upx" in kb_data(env.tg.keyboards[-1][1])
    env.handle_update(tmsg(UPKEY, mid=901))
    assert (CHAT, 901) in env.tg.deleted and fake_upscale["calls"] == [UPKEY]
    pick = flat(env.tg.texts(CHAT)[-1])
    assert "Ключ принят" in pick and "демо-счёт" in pick and "1 000" in pick and "upa:0" in kb_data(env.tg.keyboards[-1][1]) and UPKEY not in pick
    assert env.db.get_exchange_account(env.alice["id"], "upscale") is None                                      # пока счёт не выбран, ничего не сохранено
    env.handle_update(cb("upa:0"))                                                                              # демо — без подтверждения
    acc = env.db.get_exchange_account(env.alice["id"], "upscale")
    assert acc and acc["wallet"] == ACC1 and acc["net"] == "demo" and acc["testnet"] is True and UPKEY not in acc["key_enc"] and acc["key_enc"].startswith("gAAAA")
    assert env.db.get_exchange_account(env.alice["id"]) is None and env.db.get_exchange_account(env.alice["id"], "bybit") is None
    ok = flat(env.tg.texts(CHAT)[-1])
    assert "Upscale подключён" in ok and "демо-счёт" in ok and "111111…1111" in ok and "Не демо" not in ok and UPKEY not in texts_all(env) and ACC1 not in texts_all(env)
    assert env.alice["id"] not in env.ui.svc._up
    from app.trading import adapter_for
    from app.upscale import UpscaleAdapter
    ad = adapter_for(env.db, env.alice["id"], "upscale")
    assert isinstance(ad, UpscaleAdapter) and ad.api_key == UPKEY and ad.account_id == ACC1 and ad.testnet is True


def test_paid_account_needs_confirmation_and_several_accounts_are_chosen_by_button(env, fake_upscale):
    fake_upscale["accounts"] = [account(ACC1, "demo", 1000.0), account(ACC2, "real", 50000.0, "funded")]
    env.handle_update(cb("up"))
    env.handle_update(tmsg(UPKEY, mid=910))
    kb = kb_data(env.tg.keyboards[-1][1])
    assert {"upa:0", "upa:1", "upx"} <= set(kb) and "платный счёт" in env.tg.texts(CHAT)[-1] and "funded" in env.tg.texts(CHAT)[-1]
    env.handle_update(tmsg("просто текст", mid=911))                                                           # пока ждём выбор счёта, сообщения не принимаются
    assert "Выберите счёт кнопкой" in env.tg.texts(CHAT)[-1] and env.db.get_exchange_account(env.alice["id"], "upscale") is None
    env.handle_update(cb("upa:1"))                                                                              # платный — сначала подтверждение
    assert "платный счёт" in last_edit(env)[2] and "провалить" in last_edit(env)[2] and "upa:1:y" in kb_data(last_edit(env)[3]) and env.db.get_exchange_account(env.alice["id"], "upscale") is None
    env.handle_update(cb("upa:1:y"))
    acc = env.db.get_exchange_account(env.alice["id"], "upscale")
    assert acc["wallet"] == ACC2 and acc["net"] == "real" and acc["testnet"] is False and "не демо-счёт" in env.tg.texts(CHAT)[-1]
    env.handle_update(cb("upa:0"))                                                                              # состояние сброшено: старая кнопка ничего не делает
    assert env.db.get_exchange_account(env.alice["id"], "upscale")["wallet"] == ACC2 and env.tg.answers[-1][2] is True


def test_bad_key_attempts_cancel_and_timeout(env, fake_upscale, monkeypatch):
    env.handle_update(cb("up"))
    env.handle_update(tmsg(UPKEY + "BAD", mid=920))
    assert "❌" in env.tg.texts(CHAT)[-1] and "недействителен" in env.tg.texts(CHAT)[-1] and (CHAT, 920) in env.tg.deleted and env.db.get_exchange_account(env.alice["id"], "upscale") is None
    env.handle_update(cb("upx"))
    assert env.alice["id"] not in env.ui.svc._up
    env.handle_update(tmsg(UPKEY, mid=921))                                                                     # после отмены это обычный текст
    assert env.db.get_exchange_account(env.alice["id"], "upscale") is None and not fake_upscale["calls"][1:]
    env.handle_update(cb("up"))
    for i in range(5):
        env.handle_update(tmsg(UPKEY + "BAD", mid=930 + i))
    assert env.alice["id"] not in env.ui.svc._up and "Слишком много попыток" in env.tg.texts(CHAT)[-1]
    env.handle_update(cb("up"))
    env.handle_update(tmsg("/cancel", mid=940))
    assert env.alice["id"] not in env.ui.svc._up and "отменено" in env.tg.texts(CHAT)[-1]
    env.handle_update(cb("up"))
    import time as _t
    real = _t.time
    monkeypatch.setattr("sigbot.bot.time.time", lambda: real() + 3600)                                         # прошло больше 10 минут
    env.handle_update(tmsg(UPKEY, mid=941))
    assert any("Время подключения вышло" in t for t in env.tg.texts(CHAT)) and env.db.get_exchange_account(env.alice["id"], "upscale") is None


def test_exchange_screen_shows_account_without_invented_free_balance_and_disconnect_works(env, fake_upscale, monkeypatch):
    from app import trading  # noqa: F401
    from app.upscale import UpscaleAdapter
    env.handle_update(cb("up"))
    env.handle_update(tmsg(UPKEY, mid=950))
    env.handle_update(cb("upa:0"))
    monkeypatch.setattr(UpscaleAdapter, "account", lambda s: {"equity": 1234.5, "free": None, "used": 100.0})
    monkeypatch.setattr(UpscaleAdapter, "positions", lambda s: [{"symbol": "BTC/USD", "side": "long", "size": 0.01, "entry": 100000.0, "mark": 101000.0, "liq": None, "pnl_usd": 10.0, "leverage": 5.0, "margin": 200.0, "notional": 1000.0}])
    monkeypatch.setattr(UpscaleAdapter, "open_orders", lambda s: [])
    monkeypatch.setattr(UpscaleAdapter, "fills", lambda s, limit=20: [])
    env.handle_update(cb("xu"))
    body, kb = last_edit(env)[2], last_edit(env)[3]
    body = flat(body)
    assert "Upscale" in body and "демо-счёт" in body and "1 234.50" in body
    assert "свободно" not in body and "BTC/USD" in body and {"cdu", "t:u", "x", "xb"} <= set(kb_data(kb))
    env.handle_update(cb("cdu"))
    assert "Отключить счёт Upscale" in last_edit(env)[2] and "dxu" in kb_data(last_edit(env)[3])
    env.handle_update(cb("dxu"))
    assert env.db.get_exchange_account(env.alice["id"], "upscale") is None and "не подключён" in last_edit(env)[2]
    add_cfg(env, "Для списка")
    env.handle_update(cb("m"))
    assert "xu" in kb_data(last_edit(env)[3])


def test_signal_opens_on_upscale_with_its_own_size_and_ex_command_knows_the_exchange(env, fake_upscale, fake_verify, monkeypatch):
    from app.upscale import UpscaleAdapter
    env.handle_update(cb("up"))
    env.handle_update(tmsg(UPKEY, mid=960))
    env.handle_update(cb("upa:0"))
    book = Book(500.0)
    _patch(monkeypatch, UpscaleAdapter, book)
    cid = add_cfg(env, "На Upscale")
    env.handle_update(cb(f"c:{cid}"))
    assert {f"a:{cid}:o:u", f"a:{cid}:m:u", f"ab:{cid}:u", f"sz:{cid}:u"} <= set(kb_data(last_edit(env)[3])) and "Upscale" in last_edit(env)[2]
    env.handle_update(cb(f"ay:{cid}:o:u"))
    env.handle_update(cb(f"ab:{cid}:u"))
    env.handle_update(tmsg(f"/ex {cid} upscale trade_pct=5 leverage=3"))
    c = env.db.get_config(cid)
    assert c["ex"]["upscale"]["open"] and c["ex"]["upscale"]["over"] == {"trade_pct": 5.0, "leverage": 3.0} and not c["ex"]["hyperliquid"]["open"] and "Upscale" in env.tg.texts(CHAT)[-1]
    _signal_cycle(env)
    assert len(book.opened) == 1 and book.opened[0][0] == "BTC/USDT:USDT" and abs(book.opened[0][2] - 75.0) < 1e-6 and abs(book.opened[0][3] - 3.0) < 1e-9          # 5% × 500 $ × 3×
    assert any(x["kind"] == "open" and x["details"]["target"] == "upscale" for x in env.db.list_auto_log(env.alice["id"], 10))
    env.handle_update(cb("pny"))                                                                                # СТОП ВСЁ закрывает и на Upscale
    assert book.closed_all == 1 and not env.db.get_config(cid)["any_open"]
    env.handle_update(tmsg("/export fills kraken"))
    assert "upscale" in env.tg.texts(CHAT)[-1]
