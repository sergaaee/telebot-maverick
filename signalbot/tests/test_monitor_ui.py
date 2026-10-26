"""Интерфейс бота: мониторинг с кнопками, автомод, пауза, live, подключение счёта Hyperliquid через сообщения (с удалением ключа из чата)."""
import json

import pytest

from conftest import *  # noqa: F401,F403
from test_signalbot import BOB, CHAT, H, LONG, T0, UI_CONFIG, append_reports, env, msg  # noqa: F401

WALLET = "0x" + "ab" * 20
AGENT = "0x" + "cd" * 20
KEY = "11" * 32


@pytest.fixture(autouse=True)
def _fresh_monitor_cache():
    from app import monitor
    monitor._cache.clear()                                       # расчёт мониторинга кэшируется на 20 с — между тестами и после изменения данных сбрасываем
    yield
    monitor._cache.clear()


def open_position(env):
    """Лонговые отчёты за пару часов до конца свечей: позиция откроется и до конца данных останется открытой (тейки и стоп далеко, цена за это время не успевает дойти)."""
    start = T0 + 38 * H
    append_reports(env.hist, LONG(start, 3), 11)
    env.now["t"] = (start + 3 * H // 2) / 1000
    from app import monitor
    monitor._cache.clear()


def cb(data, chat=CHAT, mid=500, uid=None, cid="cb1"):
    return {"update_id": 2, "callback_query": {"id": cid, "from": {"id": uid or chat}, "data": data, "message": {"chat": {"id": chat}, "message_id": mid}}}


def tmsg(text, mid=700, chat=CHAT):
    u = msg(text, chat=chat)
    u["message"]["message_id"] = mid
    return u


def open_cfg(**extra):
    p = json.loads(json.dumps(UI_CONFIG["params"]))
    p.pop("bogus")
    p |= {"tps": [{"pct": 50, "share": 50}, {"pct": 80, "share": 50}], "sl_pct": 40, "confirm": 2} | extra
    return p


def add_cfg(env, name, **extra):
    from sigbot.configs import validate
    c, _ = env.db.add_config(env.alice["id"], name, validate(open_cfg(**extra), env.s.defaults)[0])        # как при приёме конфига ботом: все поля, в том числе source
    env.db.update_config(c["id"], baseline=True)
    return c["id"]


def last_edit(env):
    return env.tg.edits[-1]


def kb_data(kb):
    return [d for row in (kb or []) for _t, d in row]


def test_unlinked_user_cannot_use_monitor_or_buttons(env):
    env.handle_update(tmsg("/monitor", chat=999))
    assert "не привязан" in env.tg.texts(999)[-1]
    env.handle_update(cb("m", chat=999, uid=999))
    assert env.tg.answers[-1][2] is True and "/link" in env.tg.answers[-1][1] and env.tg.edits == []
    env.handle_update(tmsg("/hl", chat=999))
    assert "не привязан" in env.tg.texts(999)[-1] and env.db.get_exchange_account(env.alice["id"]) is None


def test_monitor_list_and_card_show_position_levels_and_status(env):
    cid = add_cfg(env, "Открытая")
    add_cfg(env, "Ждёт", cool_drop=5.0)
    open_position(env)
    env.handle_update(tmsg("/monitor"))
    text, kb = env.tg.texts(CHAT)[-1], env.tg.keyboards[-1][1]
    assert "Мониторинг" in text and "Открытая" in text and "симуляции" in text and "расчёт" in text and "в лонге" in text and "ждёт: откат" in text and "с позицией: 1" in text
    assert f"c:{cid}" in kb_data(kb) and "lv:m" in kb_data(kb) and "x" in kb_data(kb) and "t" in kb_data(kb) and "coff" in kb_data(kb)
    env.handle_update(cb(f"c:{cid}"))
    _, mid, card, kb = last_edit(env)
    assert mid == 500 and "#%d Открытая" % cid in card and "Статус" in card and "в лонге" in card and "TP1" in card and "ликвид" in card and "SL" in card and "ЛОНГ" in card
    assert "Автомод" in card and f"a:{cid}:o" in kb_data(kb) and f"p:{cid}" in kb_data(kb) and f"v:{cid}:stats" in kb_data(kb) and "m" in kb_data(kb)
    assert env.tg.answers[-1][0] == "cb1"                                                      # на каждую кнопку — ответ, «часики» не висят


def test_auto_toggle_needs_confirmation_and_off_is_immediate(env):
    cid = add_cfg(env, "Автомод")
    env.handle_update(cb(f"a:{cid}:o"))
    assert "Включить автомод" in last_edit(env)[2] and f"ay:{cid}:o" in kb_data(last_edit(env)[3])
    assert env.db.get_config(cid)["auto"] == {"open": False, "manage": False, "base": "config"}                   # пока не подтвердили — выключен
    env.handle_update(cb(f"ay:{cid}:o"))
    assert env.db.get_config(cid)["auto"] == {"open": True, "manage": False, "base": "config"} and "✅ Открывать" in kb_str(last_edit(env)[3]) and "симуляция" in env.tg.answers[-1][1]
    env.handle_update(cb(f"a:{cid}:m"))
    env.handle_update(cb(f"ay:{cid}:m"))
    assert env.db.get_config(cid)["auto"] == {"open": True, "manage": True, "base": "config"}
    env.handle_update(cb(f"a:{cid}:o"))                                                         # включённый выключается сразу, без вопросов
    assert env.db.get_config(cid)["auto"] == {"open": False, "manage": True, "base": "config"} and "выключен" in env.tg.answers[-1][1]
    assert any(x["kind"] == "auto_toggle" for x in env.db.list_auto_log(env.alice["id"]))
    env.handle_update(cb("coff"))
    assert "ВСЕХ" in last_edit(env)[2] and "offy" in kb_data(last_edit(env)[3])
    env.handle_update(cb("offy"))
    assert env.db.get_config(cid)["auto"] == {"open": False, "manage": False, "base": "config"} and "Мониторинг" in last_edit(env)[2]


def kb_str(kb):
    return " ".join(t for row in kb for t, _d in row)


def test_pause_resume_delete_and_foreign_configs(env):
    cid = add_cfg(env, "Для паузы")
    env.db.set_auto(env.alice["id"], cid, True, True)
    env.handle_update(cb(f"p:{cid}"))
    c = env.db.get_config(cid)
    assert c["paused"] and c["auto"] == {"open": False, "manage": False, "base": "config"} and "паузе" in last_edit(env)[2] and "Возобновить" in kb_str(last_edit(env)[3])
    env.handle_update(cb(f"a:{cid}:o"))                                                         # на паузе автомод не включить
    assert env.tg.answers[-1][2] is True and "паузе" in env.tg.answers[-1][1] and not env.db.get_config(cid)["auto"]["open"]
    env.handle_update(cb(f"p:{cid}"))
    assert not env.db.get_config(cid)["paused"] and not env.db.get_config(cid)["baseline"]
    env.handle_update(cb(f"cd:{cid}"))
    assert "Удалить" in last_edit(env)[2] and env.db.get_config(cid)
    env.handle_update(cb(f"dy:{cid}"))
    assert env.db.get_config(cid) is None and "Мониторинг" in last_edit(env)[2]
    other, _ = env.db.add_config(env.bob["id"], "Чужой", open_cfg())
    for data in (f"c:{other['id']}", f"p:{other['id']}", f"dy:{other['id']}", f"ay:{other['id']}:o", f"e:{other['id']}"):
        env.tg.answers.clear()
        env.handle_update(cb(data))
        assert env.tg.answers[-1][2] is True and "не найден" in env.tg.answers[-1][1], data
    assert env.db.get_config(other["id"]) is not None and not env.db.get_config(other["id"])["auto"]["open"]


def test_tabs_and_config_export(env):
    cid = add_cfg(env, "Вкладки")
    for tab, word in (("stats", "Сделок"), ("hist", "акрытых"), ("params", "Депозит"), ("log", "журнал")):
        env.handle_update(cb(f"v:{cid}:{tab}"))
        assert word in last_edit(env)[2], tab
        assert f"c:{cid}" in kb_data(last_edit(env)[3])
    env.handle_update(cb(f"e:{cid}"))
    assert env.tg.docs and env.tg.docs[-1][1].startswith("maverick-config_") and json.loads(env.tg.docs[-1][2])["params"]["sl_pct"] == 40


def test_live_edits_only_on_change_and_expires(env):
    cid = add_cfg(env, "Лайв")
    env.handle_update(cb("lv:m"))
    assert (CHAT, 500) in env.ui.live and "Live" in env.tg.answers[-1][1] and "LIVE" in last_edit(env)[2]
    n = len(env.tg.edits)
    env.refresh_live()
    assert len(env.tg.edits) == n                                                               # ничего не изменилось — сообщение не трогаем
    env.db.update_config(cid, paused=True)
    env.refresh_live()
    assert len(env.tg.edits) == n + 1 and "паузе" in last_edit(env)[2]                           # изменилось — перерисовали на месте
    env.handle_update(cb(f"c:{cid}"))                                                           # навигация внутри live-сообщения: подписка следует за экраном
    assert env.ui.live[(CHAT, 500)]["screen"] == f"c:{cid}"
    env.handle_update(cb("coff"))
    assert (CHAT, 500) not in env.ui.live                                                       # на подтверждении live снимается
    env.handle_update(cb("lv:m"))
    env.handle_update(cb("lv:m"))
    assert (CHAT, 500) not in env.ui.live and "остановлен" in env.tg.answers[-1][1]
    env.handle_update(cb("lv:m"))
    env.ui.live[(CHAT, 500)]["until"] = 0
    env.refresh_live()
    assert (CHAT, 500) not in env.ui.live and "LIVE" not in last_edit(env)[2]                    # по истечении — финальная перерисовка без live
    env.handle_update(cb("lv:m"))
    env.tg.edit_message = lambda *a, **k: (_ for _ in ()).throw(TelegramError("message to edit not found"))
    env.ui.live[(CHAT, 500)]["sig"] = "x"
    env.refresh_live()
    assert (CHAT, 500) not in env.ui.live                                                       # сообщение удалили — подписка снимается сама


from sigbot.transport import TelegramError  # noqa: E402


@pytest.fixture()
def fake_verify(monkeypatch):
    calls = []

    def fake(wallet, key, testnet=True, exchange=None, user_id=None):
        calls.append((wallet, key, testnet))
        if key.endswith("00" * 4):
            raise ValueError("Этот ключ не принадлежит API-кошельку")
        return {"agent": AGENT, "account": {"equity": 123.0, "free": 100.0, "used": 23.0}}
    monkeypatch.setattr("sigbot.bot.verify_hyperliquid", fake)
    return calls


def texts_all(env):
    return " ".join(t for _c, t in env.tg.sent)


def test_connect_hyperliquid_flow_stores_encrypted_key_and_deletes_messages(env, fake_verify):
    env.handle_update(cb("x"))
    assert "не подключён" in last_edit(env)[2] and "hl:t" in kb_data(last_edit(env)[3]) and "chl" in kb_data(last_edit(env)[3])
    env.handle_update(cb("hl:t"))
    assert "Шаг 1 из 2" in env.tg.texts(CHAT)[-1] and "hlx" in kb_data(env.tg.keyboards[-1][1])
    env.handle_update(tmsg(WALLET, mid=701))
    assert (CHAT, 701) in env.tg.deleted and "Шаг 2 из 2" in env.tg.texts(CHAT)[-1]
    env.handle_update(tmsg(KEY, mid=702))
    assert (CHAT, 702) in env.tg.deleted                                                         # сообщение с ключом удалено
    acc = env.db.get_exchange_account(env.alice["id"])
    assert acc and acc["wallet"] == WALLET and acc["testnet"] is True and acc["agent_address"] == AGENT and KEY not in acc["key_enc"] and acc["key_enc"].startswith("gAAAA")
    assert fake_verify == [(WALLET, KEY, True)]
    ok = env.tg.texts(CHAT)[-1]
    assert "подключён" in ok and "тестовая сеть" in ok and "удалены из чата" in ok and "0xabab…abab" in ok and "123" in ok
    assert KEY not in texts_all(env) and WALLET not in " ".join(ok.split())                      # ни ключ, ни полный адрес в ответах не повторяются
    assert env.alice["id"] not in env.ui.svc._hl
    from app.trading import adapter_for
    assert adapter_for(env.db, env.alice["id"]).key == "0x" + KEY                                 # расшифровывается в адаптер
    # чужой пользователь счёта не видит
    from app.trading import DryRunAdapter
    assert isinstance(adapter_for(env.db, env.bob["id"]), DryRunAdapter)


def test_connect_single_message_invalid_key_attempts_and_cancel(env, fake_verify):
    env.handle_update(cb("hl:t"))
    env.handle_update(tmsg("0x123", mid=710))
    assert "42 символа" in env.tg.texts(CHAT)[-1] and env.db.get_exchange_account(env.alice["id"]) is None
    env.handle_update(tmsg(f"{WALLET} {KEY[:-8]}00000000", mid=711))                              # адрес и ключ одним сообщением; ключ «плохой»
    assert (CHAT, 711) in env.tg.deleted and "не принадлежит" in env.tg.texts(CHAT)[-1] and "ещё раз" in env.tg.texts(CHAT)[-1]
    assert env.db.get_exchange_account(env.alice["id"]) is None and KEY[:-8] not in texts_all(env)
    env.handle_update(tmsg(KEY[:-8] + "00000000", mid=712))                                        # повтор — отдельным сообщением (адрес уже запомнен)
    assert (CHAT, 712) in env.tg.deleted
    env.handle_update(tmsg("/cancel"))
    assert "отменено" in env.tg.texts(CHAT)[-1] and env.alice["id"] not in env.ui.svc._hl
    env.handle_update(tmsg(KEY, mid=713))                                                           # вне сценария ключ не принимается как данные счёта
    assert env.db.get_exchange_account(env.alice["id"]) is None
    # пять неудач подряд — отмена
    env.handle_update(cb("hl:t"))
    for i in range(5):
        env.handle_update(tmsg(f"{WALLET} {KEY[:-8]}00000000", mid=720 + i))
    assert "Слишком много попыток" in env.tg.texts(CHAT)[-1] and env.alice["id"] not in env.ui.svc._hl
    # любая другая команда во время сценария его отменяет
    env.handle_update(cb("hl:t"))
    env.handle_update(tmsg("/list"))
    assert env.alice["id"] not in env.ui.svc._hl
    env.handle_update(tmsg(f"{WALLET} {KEY}", mid=740))
    assert env.db.get_exchange_account(env.alice["id"]) is None


def test_mainnet_needs_confirmation_and_disconnect_clears_key_and_auto(env, fake_verify):
    env.handle_update(cb("chl"))
    assert "БОЕВУЮ" in last_edit(env)[2] and "hl:m" in kb_data(last_edit(env)[3])
    env.handle_update(cb("hl:m"))
    env.handle_update(tmsg(f"{WALLET} {KEY}", mid=750))
    assert env.db.get_exchange_account(env.alice["id"])["testnet"] is False and "БОЕВАЯ" in env.tg.texts(CHAT)[-1]
    cid = add_cfg(env, "С автомодом")
    env.db.set_auto(env.alice["id"], cid, True, True)
    env.handle_update(cb("cdx"))
    assert "Отключить" in last_edit(env)[2] and "dxy" in kb_data(last_edit(env)[3]) and env.db.get_exchange_account(env.alice["id"])
    env.handle_update(cb("dxy"))
    assert env.db.get_exchange_account(env.alice["id"]) is None and env.db.get_config(cid)["auto"] == {"open": False, "manage": False, "base": "config"} and "не подключён" in last_edit(env)[2]


def test_exchange_screen_shows_account_positions_and_errors(env, monkeypatch):
    from app import trading

    class Fx:
        def fetch_positions(self, syms=None):
            return [{"contracts": 0.1, "side": "long", "symbol": "BTC/USDC:USDC", "entryPrice": 100.0, "markPrice": 101.0, "liquidationPrice": 80.0, "unrealizedPnl": 0.1, "leverage": 5, "initialMargin": 2.0, "notional": 10.1}]

        def fetch_balance(self):
            return {"USDC": {"total": 50.0, "free": 40.0, "used": 10.0}}

        def fetch_open_orders(self, sym=None):
            return [{"symbol": "BTC/USDC:USDC", "side": "sell", "type": "limit", "amount": 0.1, "price": 110.0, "reduceOnly": True, "timestamp": 1}]

        def fetch_my_trades(self, sym=None, since=None, limit=None):
            return [{"symbol": "BTC/USDC:USDC", "side": "buy", "amount": 0.1, "price": 100.0, "fee": {"cost": 0.0}, "info": {}, "timestamp": 1_700_000_000_000}]
    monkeypatch.setattr(trading.HyperliquidAdapter, "_x", lambda self: Fx())
    from app.secrets_store import encrypt
    env.db.set_exchange_account(env.alice["id"], "hyperliquid", WALLET, encrypt("0x" + KEY, env.db.path), True, AGENT)
    env.handle_update(cb("x"))
    t = last_edit(env)[2]
    assert "Hyperliquid" in t and "тестовая сеть" in t and "Капитал" in t and "50.00" in t and "BTC/USDC:USDC" in t and "Открытых ордеров: 1" in t and "0xabab…abab" in t
    assert KEY not in t and "dxy" not in t and "cdx" in kb_data(last_edit(env)[3])
    monkeypatch.setattr(trading.HyperliquidAdapter, "_x", lambda self: (_ for _ in ()).throw(trading.ExchangeError("Hyperliquid недоступен: таймаут")))
    env.handle_update(cb("x"))
    assert "недоступен" in last_edit(env)[2] and "cdx" in kb_data(last_edit(env)[3])                # сбой биржи не прячет подключение и кнопку отключения
    env.handle_update(cb("t"))
    assert "реальный" in last_edit(env)[2] and "тестовая сеть" in last_edit(env)[2]


def test_test_position_buttons_dry_run(env):
    env.handle_update(cb("t"))
    assert "симуляция" in last_edit(env)[2].lower() and "cty:long" in kb_data(last_edit(env)[3])
    env.handle_update(cb("cty:long"))
    assert "tgo:long" in kb_data(last_edit(env)[3])
    env.handle_update(cb("tgo:long"))
    assert "Симуляция" in env.tg.answers[-1][1] and env.db.list_auto_log(env.alice["id"])[0]["kind"] == "test_position"
    env.handle_update(cb("ctc"))
    env.handle_update(cb("tcgo"))
    assert env.db.list_auto_log(env.alice["id"])[0]["kind"] == "test_close"


def log_records(env):
    from app.tradelog import log_path
    p = log_path(env.db.path)
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


def test_trading_log_records_whole_auto_flow_without_secrets(env, fake_verify, monkeypatch):
    # подключение счёта: события есть, секретов нет
    env.handle_update(cb("hl:t"))
    env.handle_update(tmsg(f"{WALLET} {KEY}", mid=801))
    ev = [r["event"] for r in log_records(env)]
    assert "account_connected" in ev
    raw = open(__import__("app.tradelog", fromlist=["x"]).log_path(env.db.path), encoding="utf-8").read()
    assert KEY not in raw and WALLET not in raw and "0xabab…abab" in raw
    # автомод: решение → результат (ошибка биржи фиксируется со стеком, цикл бота жив)
    cid = add_cfg(env, "Для лога")
    env.handle_update(cb(f"ay:{cid}:o"))
    from app import trading
    monkeypatch.setattr(trading.HyperliquidAdapter, "_x", lambda self: (_ for _ in ()).throw(trading.ExchangeError("Hyperliquid недоступен: таймаут")))
    monkeypatch.setattr(trading.HyperliquidAdapter, "positions", lambda self: [])              # сверка с биржей проходит (позиций нет), падает уже сам ордер
    start = T0 + 38 * H
    append_reports(env.hist, LONG(start, 3), 11)
    env.now["t"] = (start + 3 * H // 2) / 1000
    from app import monitor
    monitor._cache.clear()
    env.cycle()
    recs = log_records(env)
    kinds = [r["event"] for r in recs]
    assert "auto_toggle" in kinds and "auto_decision" in kinds and "auto_fail" in kinds
    d = next(r for r in recs if r["event"] == "auto_decision")
    assert d["user"] == env.alice["id"] and d["config"] == cid and d["kind"] == "open" and d["symbol"] == "BTC/USDT:USDT" and d["notional"] > 0 and d["exchange"] == "hyperliquid"
    f = next(r for r in recs if r["event"] == "auto_fail")
    assert "таймаут" in f["error"] and f["error_type"] == "ExchangeError" and "Traceback" in f["traceback"] and f["level"] == "error"
    assert any(r["event"] == "exchange_fail" or r["event"] == "auto_fail" for r in recs)
    # /tradelog: свои записи файлом, чужие не видны
    env.handle_update(tmsg("/tradelog 100"))
    name, body = env.tg.docs[-1][1], env.tg.docs[-1][2].decode()
    assert name == "trading-log.jsonl" and "auto_fail" in body and all(json.loads(x)["user"] == env.alice["id"] for x in body.splitlines())
    env.handle_update(tmsg("/tradelog", chat=BOB))
    assert "пуст" in env.tg.texts(BOB)[-1] or env.tg.docs[-1][0] == BOB


def test_failed_verification_is_logged_with_reason(env):
    env.handle_update(cb("hl:t"))
    env.handle_update(tmsg(f"{WALLET} {KEY}", mid=810))                                           # настоящая проверка: адаптер не может подключиться / ключ не опознан
    recs = log_records(env)
    assert any(r["event"] == "verify_start" for r in recs) and any(r["event"] == "verify_fail" for r in recs)
    raw = open(__import__("app.tradelog", fromlist=["x"]).log_path(env.db.path), encoding="utf-8").read()
    assert KEY not in raw


def test_auto_open_is_checked_against_real_exchange_positions(env, fake_verify, monkeypatch):
    from app import trading
    env.handle_update(cb("hl:t"))
    env.handle_update(tmsg(f"{WALLET} {KEY}", mid=820))
    cid = add_cfg(env, "Сверка")
    env.handle_update(cb(f"ay:{cid}:o"))
    sent, closed, state = [], [], {"pos": [{"symbol": "BTC/USDC:USDC", "side": "long", "size": 0.001}]}
    monkeypatch.setattr(trading.HyperliquidAdapter, "positions", lambda self: state["pos"])
    monkeypatch.setattr(trading.HyperliquidAdapter, "open_market", lambda self, *a, **k: sent.append(a) or {"status": "sent"})
    monkeypatch.setattr(trading.HyperliquidAdapter, "close_market", lambda self, *a, **k: closed.append(a) or {"status": "sent"})
    start = T0 + 38 * H
    append_reports(env.hist, LONG(start, 3), 11)
    env.now["t"] = (start + 3 * H // 2) / 1000
    from app import monitor
    monitor._cache.clear()
    env.cycle()
    assert not sent and not closed                                                            # на бирже уже есть лонг — повторный вход пропущен
    assert any("уже есть позиция" in (x["details"].get("note") or "") for x in env.db.list_auto_log(env.alice["id"], 20) if x["status"] == "skipped")
    assert any("уже есть позиция" in t for t in env.tg.texts(CHAT))


def test_panic_closes_everything_and_turns_auto_off(env, fake_verify, monkeypatch):
    from app import trading
    env.handle_update(cb("hl:t"))
    env.handle_update(tmsg(f"{WALLET} {KEY}", mid=830))
    cid = add_cfg(env, "Паника")
    env.handle_update(cb(f"ay:{cid}:o"))
    called = []
    monkeypatch.setattr(trading.HyperliquidAdapter, "close_all", lambda self: called.append(1) or {"closed": [{"symbol": "BTC/USDC:USDC"}], "cancelled": 2, "errors": []})
    env.handle_update(tmsg("/panic"))
    assert not called and "СТОП ВСЁ" in env.tg.texts(CHAT)[-1]                                # сначала подтверждение
    env.handle_update(cb("pny"))
    assert called and env.db.get_config(cid)["auto"] == {"open": False, "manage": False, "base": "config"}
    assert any(x["kind"] == "panic" for x in env.db.list_auto_log(env.alice["id"], 10))


def test_digest_and_export_commands(env):
    env.handle_update(tmsg("/digest"))
    assert "сейчас: <b>off</b>" in env.tg.texts(CHAT)[-1]
    env.handle_update(tmsg("/digest daily"))
    assert env.db.get_user(env.alice["id"])["digest"] == "daily" and "каждый день" in env.tg.texts(CHAT)[-1]
    cid = add_cfg(env, "Для сводки")
    env.handle_update(tmsg("/digest now"))
    assert "Дневная сводка" in env.tg.texts(CHAT)[-1] and "Для сводки" in env.tg.texts(CHAT)[-1]
    # автоматическая отправка по расписанию: один раз за слот
    env.db.mark_digest_sent(env.alice["id"], 1.0)
    env.now["t"] = 1_700_000_000 + 3600 * 12
    before = len(env.tg.texts(CHAT))
    env._send_digests()
    env._send_digests()
    sent = [t for t in env.tg.texts(CHAT)[before:] if "сводка" in t]
    assert len(sent) == 1
    env.handle_update(tmsg("/digest off"))
    assert env.db.get_user(env.alice["id"])["digest"] == "off"
    # выгрузки файлами
    env.handle_update(tmsg("/export trades"))
    name, body = env.tg.docs[-1][1], env.tg.docs[-1][2].decode("utf-8")
    assert name == "maverick-trades.csv" and body.lstrip("﻿").startswith("strategy,symbol")
    env.handle_update(tmsg("/export log"))
    assert env.tg.docs[-1][1] == "maverick-autolog.csv"
    env.handle_update(tmsg("/export fills"))
    assert "не подключён" in env.tg.texts(CHAT)[-1]
    env.handle_update(tmsg("/export"))
    assert "/export trades" in env.tg.texts(CHAT)[-1]


def _real_account(env, fake_verify, monkeypatch, equity=1000.0, free=900.0):
    from app import trading
    env.handle_update(cb("hl:t"))
    env.handle_update(tmsg(f"{WALLET} {KEY}", mid=840))
    sent = []
    monkeypatch.setattr(trading.HyperliquidAdapter, "positions", lambda self: [])
    monkeypatch.setattr(trading.HyperliquidAdapter, "account", lambda self: {"equity": equity, "free": free, "used": equity - free})
    monkeypatch.setattr(trading.HyperliquidAdapter, "open_market", lambda self, *a, **k: sent.append((a, k)) or {"status": "sent"})
    return sent


def _signal_cycle(env):
    start = T0 + 38 * H
    append_reports(env.hist, LONG(start, 3), 11)
    env.now["t"] = (start + 3 * H // 2) / 1000
    from app import monitor
    monitor._cache.clear()
    env.cycle()


def test_size_from_config_deposit_vs_real_account_balance(env, fake_verify, monkeypatch):
    sent = _real_account(env, fake_verify, monkeypatch, equity=1000.0, free=900.0)
    cid = add_cfg(env, "Размер")                                                              # депозит конфига 5000, 20%, плечо 2 -> маржа 1000, объём 2000
    env.handle_update(cb(f"ay:{cid}:o"))
    # по умолчанию размер от депозита конфига: 5000 * 20% = 1000 $ маржи, а свободно 900 -> ордер не уходит, причина понятна
    _signal_cycle(env)
    assert not sent
    skipped = [x for x in env.db.list_auto_log(env.alice["id"], 20) if x["status"] == "skipped"]
    assert skipped and "не хватает свободной маржи" in skipped[0]["details"]["note"] and "баланс счёта" in skipped[0]["details"]["note"]
    assert any("не хватает свободной маржи" in t for t in env.tg.texts(CHAT))
    # база «баланс счёта»: маржа = 20% от 1000 = 200 $, объём 400 $ при том же плече 2×
    env.handle_update(cb(f"ab:{cid}"))
    assert env.db.get_config(cid)["auto"]["base"] == "account" and "баланса счёта" in env.tg.answers[-1][1]
    env.db.update_config(cid, last_ts=0)
    env.forget(cid)
    _signal_cycle(env)
    assert len(sent) == 1
    assert abs(sent[0][0][2] - 400.0) < 1e-6 and abs(sent[0][0][3] - 2.0) < 1e-9
    log = [x for x in env.db.list_auto_log(env.alice["id"], 20) if x["kind"] == "open" and x["status"] == "sent"][0]["details"]
    assert log["size_base"] == "account" and "1 000" in log["size_note"]
    # назад к депозиту конфига
    env.handle_update(cb(f"ab:{cid}"))
    assert env.db.get_config(cid)["auto"]["base"] == "config"


def test_card_warns_when_config_deposit_far_above_real_capital(env, fake_verify, monkeypatch):
    _real_account(env, fake_verify, monkeypatch, equity=1000.0, free=900.0)
    cid = add_cfg(env, "Депозит 200к", deposit=200000)
    env.handle_update(cb(f"ay:{cid}:o"))
    env.handle_update(cb(f"c:{cid}"))
    body = last_edit(env)[2]
    assert "База размера входа" in body and "депозит конфига" in body and "намного больше капитала на бирже" in body
    env.handle_update(cb(f"ab:{cid}"))
    assert "намного больше" not in last_edit(env)[2] and "баланс счёта" in last_edit(env)[2]
