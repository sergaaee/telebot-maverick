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


def test_sizing_per_config_via_command_and_buttons(env):
    cid = add_cfg(env, "Размер конфига")                                                       # в конфиге: 20% и плечо 2×
    env.handle_update(cb(f"c:{cid}"))
    assert "📐 20% × 2×" in kb_str(last_edit(env)[3])
    env.handle_update(cb(f"sz:{cid}"))
    body = last_edit(env)[2]
    assert "размер входа" in body and "20%" in body and "позиции, уже открытые на бирже, не меняются" in body
    env.handle_update(cb(f"szp:{cid}:10"))
    env.handle_update(cb(f"szl:{cid}:5"))
    p = env.db.get_config(cid)["params"]
    assert p["trade_pct"] == 10 and p["leverage"] == 5 and p["sl_pct"] == 40                  # остальное не тронуто
    assert "✅10%" in kb_str(last_edit(env)[3]) and "✅5×" in kb_str(last_edit(env)[3])
    env.handle_update(tmsg(f"/size {cid} 15 3"))
    assert env.db.get_config(cid)["params"]["trade_pct"] == 15 and env.db.get_config(cid)["params"]["leverage"] == 3 and "15% депозита × 3×" in env.tg.texts(CHAT)[-1]
    env.handle_update(tmsg(f"/size {cid} 30%"))                                                # только доля
    assert env.db.get_config(cid)["params"]["trade_pct"] == 30 and env.db.get_config(cid)["params"]["leverage"] == 3
    env.handle_update(tmsg(f"/size {cid} 0 5"))
    assert env.db.get_config(cid)["params"]["trade_pct"] == 30 and "trade_pct" in env.tg.texts(CHAT)[-1]       # 0% не принимается, значения прежние
    env.handle_update(tmsg("/size"))
    assert "/size № [доля" in env.tg.texts(CHAT)[-1]
    env.handle_update(tmsg("/size 999 10 5"))
    assert "не найден" in env.tg.texts(CHAT)[-1].lower()
    env.handle_update(tmsg(f"/size {cid}"))
    assert "размер входа" in env.tg.texts(CHAT)[-1]


def _restart(env):
    """Новый экземпляр бота на той же базе: как перезапуск процесса."""
    return type(env)(env.s, env.tg, env.db, env.engine, env.b, clock=lambda: env.now["t"])


def test_failed_entry_is_retried_after_restart(env, fake_verify, monkeypatch):
    from app import trading
    sent = _real_account(env, fake_verify, monkeypatch, equity=1000.0, free=900.0)
    state = {"fail": True}

    def open_market(self, *a, **k):
        if state["fail"]:
            raise trading.ExchangeError("Объём 1000.84 $ больше лимита TRADE_MAX_USD=100 $ — ордер не отправлен.")
        sent.append((a, k))
        return {"status": "sent"}

    monkeypatch.setattr(trading.HyperliquidAdapter, "open_market", open_market)
    cid = add_cfg(env, "Повтор")
    env.handle_update(cb(f"ay:{cid}:o"))
    env.handle_update(cb(f"ab:{cid}"))                                                       # размер от баланса счёта: маржи хватает
    _signal_cycle(env)
    assert not sent and [x["status"] for x in env.db.list_auto_log(env.alice["id"], 5) if x["kind"] == "open"] == ["error"]
    assert [r["config_id"] for r in env.db.list_retries()] == [cid]
    assert any("не выполнен" in t and "после перезапуска" in t for t in env.tg.texts(CHAT))
    env.cycle()                                                                              # без перезапуска бот ничего не повторяет (не долбит биржу каждую минуту)
    assert not sent
    # перезапуск: лимит поправили -> вход повторяется один раз
    state["fail"] = False
    env2 = _restart(env)
    assert env2._retry_ids == {(cid, "hyperliquid")}
    env2.cycle()
    assert len(sent) == 1 and sent[0][0][1] == "long" and env.db.list_retries() == []
    assert any("повторяю вход" in t for t in env.tg.texts(CHAT))
    assert [x["status"] for x in env.db.list_auto_log(env.alice["id"], 10) if x["kind"] == "open"][:1] == ["sent"]
    env3 = _restart(env)
    env3.cycle()
    assert len(sent) == 1                                                                    # ещё один перезапуск ничего не повторяет


def test_retry_is_dropped_when_signal_is_stale_or_already_closed_or_position_exists(env, fake_verify, monkeypatch):
    from app import trading
    sent = _real_account(env, fake_verify, monkeypatch, equity=1000.0, free=900.0)
    cid = add_cfg(env, "Не повторять")
    env.handle_update(cb(f"ay:{cid}:o"))
    env.handle_update(cb(f"ab:{cid}"))
    _signal_cycle(env)
    # 1) сигнал устарел
    env.db.set_retry(cid, (env.now["t"] - 4 * 3600) * 1000, "long", False, 1, "ошибка")
    env2 = _restart(env)
    env2.cycle()
    assert len(sent) == 1 and "устарел" in env.tg.texts(CHAT)[-1] and env.db.list_retries() == []           # (одна отправка — исходный вход)
    # 2) на бирже уже есть позиция (первая попытка на самом деле прошла) -> дубль не открывается
    monkeypatch.setattr(trading.HyperliquidAdapter, "positions", lambda self: [{"symbol": "BTC/USDC:USDC", "side": "long", "size": 0.01}])
    env.db.set_retry(cid, (env.now["t"] - 600) * 1000, "long", False, 1, "ответ потерялся")
    env3 = _restart(env)
    env3.cycle()
    assert len(sent) == 1 and "уже есть позиция" in env.tg.texts(CHAT)[-1] and env.db.list_retries() == []
    # 3) автомод «открывать» выключен — повтор молча снимается
    env.handle_update(cb(f"a:{cid}:o"))
    env.db.set_retry(cid, (env.now["t"] - 600) * 1000, "long", False, 1, "x")
    _restart(env).cycle()
    assert len(sent) == 1 and env.db.list_retries() == []


def test_temporary_skip_is_remembered_for_retry_but_permanent_is_not(env, fake_verify, monkeypatch):
    from app import trading
    sent = _real_account(env, fake_verify, monkeypatch, equity=1000.0, free=50.0)             # свободной маржи мало
    cid = add_cfg(env, "Маржа")
    env.handle_update(cb(f"ay:{cid}:o"))
    env.handle_update(cb(f"ab:{cid}"))
    _signal_cycle(env)
    assert not sent and [r["config_id"] for r in env.db.list_retries()] == [cid]               # нехватка маржи — временная причина
    assert any("не хватает свободной маржи" in t and "после перезапуска" in t for t in env.tg.texts(CHAT))
    monkeypatch.setattr(trading.HyperliquidAdapter, "account", lambda self: {"equity": 1000.0, "free": 900.0, "used": 100.0})        # пополнили, перезапустили
    _restart(env).cycle()
    assert len(sent) == 1 and env.db.list_retries() == []


def test_failed_reversal_close_does_not_open_on_top_of_old_position(env, fake_verify, monkeypatch):
    from app import trading
    sent = _real_account(env, fake_verify, monkeypatch, equity=1000.0, free=900.0)
    monkeypatch.setattr(trading.HyperliquidAdapter, "positions", lambda self: [{"symbol": "BTC/USDC:USDC", "side": "short", "size": 0.01}])           # на бирже шорт, сигнал — лонг
    monkeypatch.setattr(trading.HyperliquidAdapter, "close_market", lambda self, *a, **k: (_ for _ in ()).throw(trading.ExchangeError("таймаут")))
    cid = add_cfg(env, "Разворот")
    env.handle_update(cb(f"ay:{cid}:o"))
    env.handle_update(cb(f"ab:{cid}"))
    _signal_cycle(env)
    assert not sent                                                                        # новую позицию поверх нерешённой старой не открываем
    assert [r["config_id"] for r in env.db.list_retries()] == [cid] and any("не удалось закрыть встречную позицию" in t for t in env.tg.texts(CHAT))


def _signal_while_auto_off(env, fake_verify, monkeypatch, name, **extra):
    """Сигнал приходит, пока автомод выключен: уведомление есть, ордера нет, last_ts сдвинут (обычное поведение)."""
    sent = _real_account(env, fake_verify, monkeypatch, equity=1000.0, free=900.0)
    cid = add_cfg(env, name, **extra)
    env.handle_update(cb(f"ab:{cid}"))                                                       # размер от баланса счёта — маржи хватает
    _signal_cycle(env)
    assert not sent
    return sent, cid


def test_catchup_enters_on_fresh_signal_when_auto_is_switched_on(env, fake_verify, monkeypatch):
    sent, cid = _signal_while_auto_off(env, fake_verify, monkeypatch, "Догнать")
    assert env.db.get_config(cid)["catchup"]["on"] is False
    env.handle_update(cb(f"ay:{cid}:o"))                                                     # опция выключена (по умолчанию): включение автомода ничего не догоняет
    env.cycle()
    assert not sent and not any("догоняю" in t for t in env.tg.texts(CHAT))
    env.handle_update(cb(f"a:{cid}:o"))                                                      # снова выключаем автомод
    env.handle_update(tmsg(f"/catchup {cid} on 120 0.1"))
    assert env.db.get_config(cid)["catchup"] == {"on": True, "age_min": 120.0, "move_pct": 0.1, "armed": False}
    assert "ВКЛ" in env.tg.texts(CHAT)[-1] and "при включении автомода" in env.tg.texts(CHAT)[-1]
    env.handle_update(cb(f"ay:{cid}:o"))                                                     # включили автомод -> взведено
    assert env.db.get_config(cid)["catchup"]["armed"] is True
    env.cycle()
    assert len(sent) == 1 and sent[0][0][1] == "long"
    assert any("догоняю сигнал" in t for t in env.tg.texts(CHAT)) and env.db.get_config(cid)["catchup"]["armed"] is False
    env.cycle()
    assert len(sent) == 1                                                                    # один заход, повторов нет


def test_catchup_refuses_old_signal_or_price_already_in_profit(env, fake_verify, monkeypatch):
    sent, cid = _signal_while_auto_off(env, fake_verify, monkeypatch, "Не догонять")
    env.handle_update(tmsg(f"/catchup {cid} on 10 0.1"))                                    # сигнал старше 10 минут
    env.handle_update(cb(f"ay:{cid}:o"))
    env.cycle()
    assert not sent and any("мин назад, а допустимо не больше 10" in t for t in env.tg.texts(CHAT))
    assert any(x["kind"] == "catchup" and x["status"] == "skipped" for x in env.db.list_auto_log(env.alice["id"], 10))
    # свежесть позволяет, но цена ушла в сторону прибыли больше чем на 0.1% от цены сигнала
    env.handle_update(cb(f"a:{cid}:o"))
    env.handle_update(tmsg(f"/catchup {cid} on 240 0.1"))
    env.engine.price_fn = lambda sym: 10_000.0
    env.handle_update(cb(f"ay:{cid}:o"))
    env.cycle()
    assert not sent and any("ушла в сторону прибыли" in t for t in env.tg.texts(CHAT))
    # цена ниже цены сигнала (для лонга это «в сторону убытка») — вход не хуже сигнального, догоняем
    env.handle_update(cb(f"a:{cid}:o"))
    env.engine.price_fn = lambda sym: 50.0
    env.handle_update(cb(f"cu:{cid}"))                                                       # выключить опцию кнопкой
    env.handle_update(cb(f"cu:{cid}"))                                                       # и включить снова (автомод выключен -> не взводится)
    assert env.db.get_config(cid)["catchup"]["on"] and not env.db.get_config(cid)["catchup"]["armed"]
    env.handle_update(cb(f"ay:{cid}:o"))
    env.cycle()
    assert len(sent) == 1


def test_catchup_toggled_on_while_auto_is_already_enabled_fires_immediately(env, fake_verify, monkeypatch):
    sent, cid = _signal_while_auto_off(env, fake_verify, monkeypatch, "Позже")
    env.handle_update(cb(f"ay:{cid}:o"))
    env.cycle()
    assert not sent
    env.handle_update(tmsg(f"/catchup {cid} on 120 0.1"))                                    # автомод уже включён -> взводится сразу
    assert env.db.get_config(cid)["catchup"]["armed"] is True and "проверяю сигнал" in env.tg.texts(CHAT)[-1]
    env.cycle()
    assert len(sent) == 1
    env.handle_update(tmsg("/catchup"))
    assert "Использование" in env.tg.texts(CHAT)[-1]
    env.handle_update(tmsg(f"/catchup {cid} on 0 0.1"))
    assert "от 1 до 1440" in env.tg.texts(CHAT)[-1] and env.db.get_config(cid)["catchup"]["age_min"] == 120.0


class LimitBook:
    """Имитация лимитных ордеров на бирже для автомода."""
    def __init__(self, monkeypatch, positions=None):
        from app import trading
        self.placed, self.cancelled, self.protected, self.opened = [], [], [], []
        self.state = {"status": "open", "filled": 0.0, "amount": 4.0, "avg": None}
        self.positions = positions if positions is not None else []
        H = trading.HyperliquidAdapter
        monkeypatch.setattr(H, "positions", lambda s: self.positions)
        monkeypatch.setattr(H, "place_limit_entry", lambda s, sym, side, notional, price, lev=1.0: self.placed.append((sym, side, notional, price, lev)) or {"status": "sent", "order_id": f"L{len(self.placed)}", "price": price})
        monkeypatch.setattr(H, "order_state", lambda s, oid, sym: dict(self.state))
        monkeypatch.setattr(H, "cancel_order_id", lambda s, oid, sym: self.cancelled.append(oid) or {"status": "sent"})
        monkeypatch.setattr(H, "protect", lambda s, sym, side, amount, stop=None, takes=None: self.protected.append((sym, side, amount, stop, takes)) or {"status": "sent", "orders": []})
        monkeypatch.setattr(H, "open_market", lambda s, sym, side, notional, lev=1.0, stop=None, takes=None: self.opened.append((sym, side, notional, lev, stop, takes)) or {"status": "sent"})


def _limit_env(env, fake_verify, monkeypatch, name, ttl=2.0, **extra):
    _real_account(env, fake_verify, monkeypatch, equity=1000.0, free=900.0)
    book = LimitBook(monkeypatch)
    cid = add_cfg(env, name, entry_limit_pct=0.5, entry_limit_dir="pullback", entry_limit_ttl_h=ttl, **extra)
    env.handle_update(cb(f"ay:{cid}:o"))
    env.handle_update(cb(f"ab:{cid}"))
    return book, cid


def test_limit_entry_places_order_then_protects_after_fill(env, fake_verify, monkeypatch):
    book, cid = _limit_env(env, fake_verify, monkeypatch, "Лимитка")
    _signal_cycle(env)
    assert not book.opened and len(book.placed) == 1                                           # по рынку не входим — стоит лимитка
    sym, side, notional, price, lev = book.placed[0]
    assert side == "long" and abs(price - 99.5) < 1e-9 and abs(notional - 400.0) < 1e-6 and lev == 2.0                          # цена сигнала 100, откат 0.5%
    pend = env.db.get_pending(cid)
    assert pend["order_id"] == "L1" and pend["mode"] == "pullback" and abs(pend["level"] - 99.5) < 1e-9 and pend["expires"] > env.now["t"]
    assert any("поставил <b>лимитку" in t for t in env.tg.texts(CHAT))
    env.cycle()                                                                                # ордер ещё открыт: ничего не меняется, повторов нет
    assert len(book.placed) == 1 and not book.protected and env.db.get_pending(cid)
    book.state = {"status": "filled", "filled": 4.0, "amount": 4.0, "avg": 99.5}
    book.positions = [{"symbol": "BTC/USDC:USDC", "side": "long", "size": 4.0, "entry": 99.4}]
    env.cycle()
    assert env.db.get_pending(cid) is None and len(book.protected) == 1
    sym, side, amount, stop, takes = book.protected[0]
    assert amount == 4.0 and abs(stop - 99.4 * (1 - 0.40)) < 1e-6 and len(takes) == 2 and abs(takes[0][0] - 99.4 * 1.5) < 1e-6        # SL 40%, TP 50%/80% — от цены исполнения 99.4
    assert any("лимитка исполнена" in t for t in env.tg.texts(CHAT))
    assert [x["kind"] for x in env.db.list_auto_log(env.alice["id"], 10) if x["kind"] in ("limit_entry", "protect")] == ["protect", "limit_entry"]


def test_limit_expires_or_is_cancelled_by_user_actions(env, fake_verify, monkeypatch):
    book, cid = _limit_env(env, fake_verify, monkeypatch, "Истечёт")
    _signal_cycle(env)
    assert env.db.get_pending(cid)
    env.now["t"] += 3 * 3600                                                                   # прошло больше срока жизни (2 ч)
    env.cycle()
    assert book.cancelled == ["L1"] and env.db.get_pending(cid) is None and not book.protected
    assert any("не исполнилась за срок жизни" in t for t in env.tg.texts(CHAT))
    # автомод «открывать» выключили, пока ордер стоит -> ордер снимается
    env.db.set_pending(cid, env.alice["id"], 1, "BTC/USDT:USDT", "long", "pullback", 99.5, 100.0, 400.0, 200.0, 2.0, "L7", env.now["t"] + 3600)
    env.handle_update(cb(f"a:{cid}:o"))
    env.cycle()
    assert "L7" in book.cancelled and env.db.get_pending(cid) is None
    # частичное исполнение при отмене по сроку: набранное защищаем
    env.handle_update(cb(f"ay:{cid}:o"))
    env.db.set_pending(cid, env.alice["id"], 1, "BTC/USDT:USDT", "long", "pullback", 99.5, 100.0, 400.0, 200.0, 2.0, "L8", env.now["t"] - 10)
    book.state = {"status": "open", "filled": 1.0, "amount": 4.0, "avg": 99.5}
    book.positions = [{"symbol": "BTC/USDC:USDC", "side": "long", "size": 1.0, "entry": 99.5}]
    env.cycle()
    assert "L8" in book.cancelled and len(book.protected) == 1 and book.protected[0][2] == 1.0 and any("частично" in t for t in env.tg.texts(CHAT))


def test_panic_removes_pending_entries(env, fake_verify, monkeypatch):
    book, cid = _limit_env(env, fake_verify, monkeypatch, "Паника лимитки")
    _signal_cycle(env)
    assert env.db.get_pending(cid)
    from app import trading
    monkeypatch.setattr(trading.HyperliquidAdapter, "close_all", lambda self: {"closed": [], "cancelled": 1, "errors": []})
    env.handle_update(cb("pny"))
    assert env.db.get_pending(cid) is None


def test_breakout_entry_waits_for_price_then_enters_at_market(env, fake_verify, monkeypatch):
    _real_account(env, fake_verify, monkeypatch, equity=1000.0, free=900.0)
    book = LimitBook(monkeypatch)
    cid = add_cfg(env, "По ходу", entry_limit_pct=0.5, entry_limit_dir="breakout", entry_limit_ttl_h=2.0)
    env.handle_update(cb(f"ay:{cid}:o"))
    env.handle_update(cb(f"ab:{cid}"))
    _signal_cycle(env)
    pend = env.db.get_pending(cid)
    assert not book.placed and not book.opened and pend["mode"] == "breakout" and pend["order_id"] is None and abs(pend["level"] - 100.5) < 1e-9       # лонг по ходу — на 0.5% выше
    assert any("жду движения цены" in t for t in env.tg.texts(CHAT))
    env.engine.price_fn = lambda sym: 100.4
    env.cycle()
    assert not book.opened and env.db.get_pending(cid)                                          # цена ещё не дошла
    env.engine.price_fn = lambda sym: 100.6
    env.cycle()
    assert len(book.opened) == 1 and book.opened[0][1] == "long" and abs(book.opened[0][4] - 100.6 * 0.6) < 1e-6 and env.db.get_pending(cid) is None
    assert any("вхожу по рынку" in t for t in env.tg.texts(CHAT))


def test_breakout_gives_up_after_ttl_and_dry_run_only_logs(env, fake_verify, monkeypatch):
    _real_account(env, fake_verify, monkeypatch, equity=1000.0, free=900.0)
    book = LimitBook(monkeypatch)
    cid = add_cfg(env, "Не дошла", entry_limit_pct=0.5, entry_limit_dir="breakout", entry_limit_ttl_h=1.0)
    env.handle_update(cb(f"ay:{cid}:o"))
    env.handle_update(cb(f"ab:{cid}"))
    _signal_cycle(env)
    env.now["t"] += 2 * 3600
    env.cycle()
    assert env.db.get_pending(cid) is None and not book.opened and any("цена не дошла до уровня" in t for t in env.tg.texts(CHAT))
    # счёт не подключён: лимитка только в журнале, ожидающих входов нет
    from app import trading
    env.db.delete_exchange_account(env.alice["id"])
    trading.forget_adapter(env.alice["id"])
    cid2 = add_cfg(env, "Симуляция лимитки", entry_limit_pct=0.5, entry_limit_ttl_h=1.0)
    env.handle_update(cb(f"ay:{cid2}:o"))
    start = T0 + 44 * H
    append_reports(env.hist, LONG(start, 3), 31)
    env.now["t"] = (start + 3 * H // 2) / 1000
    from app import monitor
    monitor._cache.clear()
    env.cycle()
    assert env.db.get_pending(cid2) is None and any("симуляция — поставил бы лимитку" in t for t in env.tg.texts(CHAT))


def test_opposite_signal_cancels_the_standing_limit(env, fake_verify, monkeypatch):
    book, cid = _limit_env(env, fake_verify, monkeypatch, "Обратный", ttl=8.0, side="both")
    _signal_cycle(env)
    assert env.db.get_pending(cid)["side"] == "long" and env.db.get_pending(cid)["order_id"] == "L1"
    start = T0 + 38 * H + 3 * H
    append_reports(env.hist, [(start + i * H // 2, 0.9, 0.5, 0.8) for i in range(3)], 21)                    # теперь подтверждён шорт
    env.now["t"] = (start + 3 * H // 2) / 1000
    from app import monitor
    monitor._cache.clear()
    env.cycle()
    assert book.cancelled == ["L1"] and any("пришёл обратный сигнал" in t for t in env.tg.texts(CHAT))
    pend = env.db.get_pending(cid)
    assert pend["side"] == "short" and pend["order_id"] == "L2" and abs(pend["level"] - 100.5) < 1e-9 and book.placed[-1][1] == "short"          # шорт — лимитка выше цены на 0.5%


def test_pending_entry_survives_bot_restart(env, fake_verify, monkeypatch):
    book, cid = _limit_env(env, fake_verify, monkeypatch, "Рестарт")
    _signal_cycle(env)
    assert env.db.get_pending(cid)["order_id"] == "L1"
    env2 = _restart(env)                                                                       # перезапуск бота: ожидающий вход сохранён, ордер продолжает отслеживаться
    book.state = {"status": "filled", "filled": 4.0, "amount": 4.0, "avg": 99.5}
    book.positions = [{"symbol": "BTC/USDC:USDC", "side": "long", "size": 4.0, "entry": 99.5}]
    env2.cycle()
    assert env.db.get_pending(cid) is None and len(book.protected) == 1


PYR = dict(pyramid_enabled=True, pyramid_max=2, pyramid_pct=10, pyramid_gap_h=0)


def _positions_follow_orders(monkeypatch):
    """Имитация: рыночный вход создаёт позицию на бирже, а добор требует, чтобы она уже была."""
    from app import trading
    H = trading.HyperliquidAdapter
    book = {"pos": [], "opened": []}
    monkeypatch.setattr(H, "positions", lambda s: book["pos"])

    def open_market(s, sym, side, notional, lev=1.0, stop=None, takes=None):
        book["opened"].append((side, round(notional, 2), lev))
        book["pos"] = [{"symbol": "BTC/USDC:USDC", "side": side, "size": 1.0, "entry": 100.0}]
        return {"status": "sent"}

    monkeypatch.setattr(H, "open_market", open_market)
    return book


def test_catchup_when_latest_signal_is_an_add_enters_first_leg_and_then_the_add(env, fake_verify, monkeypatch):
    sent, cid = _signal_while_auto_off(env, fake_verify, monkeypatch, "Добор", **PYR)
    book = _positions_follow_orders(monkeypatch)
    env.handle_update(tmsg(f"/catchup {cid} on 120 0.1"))
    env.handle_update(cb(f"ay:{cid}:o"))
    env.cycle()
    assert [o[0] for o in book["opened"]] == ["long", "long"]
    assert book["opened"][0][1] == 400.0 and book["opened"][1][1] == 200.0                     # первая нога: 20% от 1000 × 2×; добор: 10% × 2×
    texts = " ".join(env.tg.texts(CHAT))
    assert "добор к позиции, которая по расчёту открыта" in texts and "догонять нечего" not in texts
    kinds = [x["kind"] for x in env.db.list_auto_log(env.alice["id"], 10) if x["status"] == "sent"]
    assert kinds[:2] == ["add", "open"]                                                        # журнал новыми сверху: сначала вход, потом добор


def test_catchup_of_an_add_respects_age_price_and_limit_entry(env, fake_verify, monkeypatch):
    sent, cid = _signal_while_auto_off(env, fake_verify, monkeypatch, "Добор-условия", **PYR)
    book = _positions_follow_orders(monkeypatch)
    env.handle_update(tmsg(f"/catchup {cid} on 10 0.1"))                                      # сигнал добора старше 10 минут
    env.handle_update(cb(f"ay:{cid}:o"))
    env.cycle()
    assert not book["opened"] and any("мин назад, а допустимо не больше 10" in t for t in env.tg.texts(CHAT))
    env.handle_update(cb(f"a:{cid}:o"))
    env.handle_update(tmsg(f"/catchup {cid} on 240 0.1"))
    env.engine.price_fn = lambda sym: 10_000.0                                                # цена ушла в прибыль относительно цены сигнала добора
    env.handle_update(cb(f"ay:{cid}:o"))
    env.cycle()
    assert not book["opened"] and any("ушла в сторону прибыли" in t for t in env.tg.texts(CHAT))


def test_catchup_with_limit_entry_places_the_order_for_a_signal_still_waiting_in_the_calculation(env, fake_verify, monkeypatch):
    _real_account(env, fake_verify, monkeypatch, equity=1000.0, free=900.0)
    book = LimitBook(monkeypatch)
    cid = add_cfg(env, "Лимитка ждёт", entry_limit_pct=0.5, entry_limit_dir="pullback", entry_limit_ttl_h=6.0)
    env.handle_update(cb(f"ab:{cid}"))
    _signal_cycle(env)
    assert not book.placed                                                                    # автомод был выключен — ордеров нет
    env.handle_update(tmsg(f"/catchup {cid} on 120 0.1"))
    env.handle_update(cb(f"ay:{cid}:o"))
    env.cycle()
    assert len(book.placed) == 1 and book.placed[0][1] == "long" and env.db.get_pending(cid)            # откат в расчёте ещё не случился: догоняем сигнал — ставим лимитку сейчас


def test_catchup_of_an_add_with_limit_entry_places_only_the_first_leg_order(env, fake_verify, monkeypatch):
    _real_account(env, fake_verify, monkeypatch, equity=1000.0, free=900.0)
    book = LimitBook(monkeypatch)
    cid = add_cfg(env, "Добор лимитка", entry_limit_pct=0.01, entry_limit_dir="breakout", entry_limit_ttl_h=6.0, **PYR)     # вход «по ходу» на 0.01%: в расчёте исполняется сразу, доборы — по рынку
    env.handle_update(cb(f"ab:{cid}"))
    _signal_cycle(env)
    env.handle_update(tmsg(f"/catchup {cid} on 120 0.1"))
    env.handle_update(cb(f"ay:{cid}:o"))
    env.cycle()
    pend = env.db.get_pending(cid)
    assert pend and pend["mode"] == "breakout" and not book.opened and not book.placed         # первая нога ждёт цену; добор не ставится
    assert any("добор не ставлю" in t for t in env.tg.texts(CHAT))
