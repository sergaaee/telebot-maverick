"""Bybit (только USDT-фьючерсы) и настройки конфига отдельно для каждой биржи: адаптер на имитации ccxt, проверка ключа, база (в том числе миграция старой), manage, API."""
import json
import sqlite3

import pytest

from app import manage
from app.config import load_settings
from app.db import EXCHANGE_OVERRIDES, EXCHANGES, Database, DbError
from app.main import create_app
from app.trading import BybitAdapter, DryRunAdapter, ExchangeError, HyperliquidAdapter, NotConfigured, adapter_for, adapters_for, forget_adapter, verify_bybit
from test_auth import H_, auth_env, client, login  # noqa: F401
from test_trading import FakeEx

KEY = "AbCdEfGh12345"
SECRET = "s3cr3tS3cr3tS3cr3tS3cr3t"


class FakeBybit(FakeEx):
    """Имитация ccxt.bybit: условные ордера (стопы) отдаются отдельной выборкой, плечо «не изменилось» — ошибка 110043, баланс в USDT."""
    def __init__(self, *a, perm=None, **k):
        super().__init__(*a, **k)
        self.stops, self.perm = [], perm
        self.leverage_error = None

    def market(self, sym):
        return {"limits": {"amount": {"min": 0.001}}}

    def set_margin_mode(self, mode, sym, params=None):
        self._rec("margin_mode", mode, sym, params)
        raise RuntimeError("bybit 110026 cross margin mode is not supported in unified account")

    def set_leverage(self, lev, sym, params=None):
        self._rec("leverage", lev, sym, params)
        if self.leverage_error:
            raise RuntimeError(self.leverage_error)

    def fetch_positions(self, symbols=None, params=None):
        self._rec("positions", symbols, params)
        return [self.position] if self.position else []

    def fetch_open_orders(self, sym=None, since=None, limit=None, params=None):
        self._rec("open_orders", sym, params)
        return list(self.stops) if (params or {}).get("trigger") else list(self.orders)

    def cancel_order(self, oid, sym=None, params=None):
        self._rec("cancel", oid, params)

    def fetch_balance(self):
        return {"USDT": {"total": 321.0, "free": 300.0, "used": 21.0}, "USDC": {"total": 5.0, "free": 5.0, "used": 0.0}}

    def private_get_v5_user_query_api(self):
        return {"retCode": 0, "result": self.perm}


def test_bybit_adapter_is_linear_usdt_only_and_keeps_the_symbol():
    fx = FakeBybit(price=100.0)
    fx.leverage_error = "bybit 110043 leverage not modified"                               # плечо уже такое — не ошибка
    a = BybitAdapter(exchange=fx, max_usd=1000)
    r = a.open_market("BTC/USDT:USDT", "long", 500.0, 5.0, stop=98.0, takes=[(104.0, 50), (106.0, 50)])
    assert r["status"] == "sent" and r["symbol"] == "BTC/USDT:USDT" and abs(r["amount"] - 5.0) < 1e-9 and len(r["orders"]) == 3
    assert ("leverage", 5, "BTC/USDT:USDT", None) in fx.calls and any(c[0] == "margin_mode" for c in fx.calls)         # режим маржи не прошёл — это не мешает
    o = [c for c in fx.calls if c[0] == "order"]
    assert o[0][2:4] == ("market", "buy") and o[1][6] == {"stopLossPrice": 98.0, "reduceOnly": True} and o[2][2:4] == ("limit", "sell")
    d = a.describe()
    assert d["name"] == "bybit" and d["label"] == "Bybit" and d["real"] and d["min_notional"] == 5.0 and d["net"] == "testnet" and d["testnet"] is True
    assert BybitAdapter(net="live").describe()["testnet"] is False and BybitAdapter(net="demo").describe()["net"] == "demo" and BybitAdapter(net="что-то").net == "testnet"
    with pytest.raises(ExchangeError, match="минимального ордера"):
        a.open_market("BTC/USDT:USDT", "long", 4.0, 1.0)
    fx.leverage_error = "bybit 10001 params error"
    with pytest.raises(ExchangeError, match="плечо"):
        a.open_market("BTC/USDT:USDT", "long", 100.0, 5.0)
    with pytest.raises(NotConfigured, match="/bybit"):
        BybitAdapter(api_key="x", secret="").positions()


def test_bybit_minimum_quantity_is_checked_against_the_market():
    fx = FakeBybit(price=50000.0)
    a = BybitAdapter(exchange=fx, max_usd=100000)
    with pytest.raises(ExchangeError, match="минимального"):
        a.open_market("BTC/USDT:USDT", "long", 10.0, 1.0)                                  # 10 $ / 50000 = 0.0002 < минимума 0.001
    assert a.min_order_value("BTC/USDT:USDT", 50000.0) == 50.0


def test_bybit_orders_merge_conditional_and_cancel_with_trigger_flag():
    fx = FakeBybit(open_orders=[{"id": "L1", "symbol": "BTC/USDT:USDT", "side": "buy", "type": "limit", "amount": 1, "price": 95.0}])
    fx.stops = [{"id": "S1", "symbol": "BTC/USDT:USDT", "side": "sell", "type": "market", "amount": 1, "triggerPrice": 90.0, "reduceOnly": True}]
    a = BybitAdapter(exchange=fx)
    ords = a.open_orders()
    assert [o["trigger"] for o in ords] == [None, 90.0] and ords[1]["reduce_only"]
    assert ("open_orders", None, {"settleCoin": "USDT"}) in fx.calls                         # без символа Bybit требует settleCoin
    a.cancel_order_id("L1", "BTC/USDT:USDT")
    r = a.close_all()
    assert ("cancel", "S1", {"trigger": True}) in fx.calls and ("cancel", "L1", None) in fx.calls and r["errors"] == []
    # стоп переставляется: старый условный ордер снимается с признаком trigger
    fx2 = FakeBybit(position={"contracts": 2.0, "side": "long"})
    fx2.stops = [{"id": "S9", "triggerPrice": 90.0, "reduceOnly": True, "side": "sell"}]
    res = BybitAdapter(exchange=fx2).set_stop("BTC/USDT:USDT", "long", 100.0)
    assert res["removed_old"] == 1 and ("cancel", "S9", {"trigger": True}) in fx2.calls


def test_bybit_positions_account_use_usdt_balance_and_settle_coin():
    fx = FakeBybit(position={"contracts": 0.5, "side": "long", "symbol": "BTC/USDT:USDT", "entryPrice": 100.0, "markPrice": 101.0, "liquidationPrice": 60.0, "unrealizedPnl": 0.5,
                             "leverage": 5, "initialMargin": 10.0, "notional": 50.5})
    a = BybitAdapter(exchange=fx)
    assert a.account() == {"equity": 321.0, "free": 300.0, "used": 21.0}                    # баланс в USDT, а не в USDC
    assert a.positions()[0]["size"] == 0.5 and ("positions", None, {"settleCoin": "USDT"}) in fx.calls


def test_verify_bybit_rejects_withdraw_readonly_and_missing_contract_permission(monkeypatch):
    def run(perm, net="testnet", **env):
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        fx = FakeBybit(perm=perm)
        monkeypatch.setattr(BybitAdapter, "_x", lambda self: fx)
        return verify_bybit(KEY, SECRET, net)

    good = {"readOnly": 0, "permissions": {"ContractTrade": ["Order", "Position"], "Spot": [], "Wallet": ["AccountTransfer"]}}
    res = run(good)
    assert res["account"]["equity"] == 321.0
    with pytest.raises(ValueError, match="вывод"):
        run({"readOnly": 0, "permissions": {"ContractTrade": ["Order", "Position"], "Wallet": ["AccountTransfer", "Withdraw"]}})
    with pytest.raises(ValueError, match="только для чтения"):
        run({"readOnly": 1, "permissions": {"ContractTrade": ["Order"]}})
    with pytest.raises(ValueError, match="торговли контрактами"):
        run({"readOnly": 0, "permissions": {"ContractTrade": [], "Derivatives": [], "Spot": ["SpotTrade"]}})        # спот не нужен и не заменяет фьючерсы
    with pytest.raises(ValueError, match="права ключа"):
        run(None)                                                                          # ответ без прав — не принимаем вслепую
    assert run(None, net="demo")["account"]["equity"] == 321.0                              # в демо вывода нет — проверка прав необязательна
    assert run(None, BYBIT_SKIP_PERM_CHECK="true")["permissions"] is None                    # аварийный обход


def test_verify_bybit_validates_formats_and_net(monkeypatch):
    with pytest.raises(ValueError, match="API-ключ"):
        verify_bybit("short", SECRET)
    with pytest.raises(ValueError, match="Секрет"):
        verify_bybit(KEY, "коротко")
    with pytest.raises(ValueError, match="Сеть"):
        verify_bybit(KEY, SECRET, "mainnet")


def test_adapter_for_is_per_exchange_and_forget_is_selective(auth_env, monkeypatch):
    from app.secrets_store import encrypt
    db = auth_env
    uid = db.check_login("alice", "alice-password")["id"]
    db.set_exchange_account(uid, "hyperliquid", "0x" + "ab" * 20, encrypt("0x" + "11" * 32, db.path), True, "0x" + "cd" * 20)
    db.set_exchange_account(uid, "bybit", KEY, encrypt(SECRET, db.path), False, None, net="demo")
    hl, by = adapter_for(db, uid), adapter_for(db, uid, "bybit")
    assert type(hl) is HyperliquidAdapter and type(by) is BybitAdapter and by.key == SECRET and by.wallet == KEY and by.net == "demo" and by.user_id == uid
    assert adapter_for(db, uid, "bybit") is by and adapter_for(db, uid) is hl                  # кеш по (пользователь, биржа)
    forget_adapter(uid, "bybit")
    assert adapter_for(db, uid) is hl and adapter_for(db, uid, "bybit") is not by              # забыли только Bybit
    assert set(adapters_for(db, uid)) == {"hyperliquid", "bybit"} and isinstance(adapter_for(db, uid, "binance"), DryRunAdapter)
    assert isinstance(adapter_for(db, db.check_login("bob", "bob-password-1")["id"], "bybit"), DryRunAdapter)         # у другого пользователя счёта нет
    monkeypatch.setenv("TRADE_EXCHANGE", "dry-run")
    assert isinstance(adapter_for(db, uid, "bybit"), DryRunAdapter)
    monkeypatch.delenv("TRADE_EXCHANGE")
    # счёт Bybit в базе без net (не должно быть, но) — сеть выводится из флага testnet
    db.set_exchange_account(uid, "bybit", KEY, encrypt(SECRET, db.path), True, None)
    forget_adapter(uid)
    assert adapter_for(db, uid, "bybit").net == "testnet"


# ---------------------------------------------------------------- база: счета, настройки по биржам, миграция

def test_two_accounts_per_user_and_pending_retry_per_exchange(auth_env):
    db = auth_env
    uid = db.check_login("alice", "alice-password")["id"]
    db.set_exchange_account(uid, "hyperliquid", "0xaa", "enc1", True, "0xbb")
    db.set_exchange_account(uid, "bybit", KEY, "enc2", False, None, net="live")
    assert [a["exchange"] for a in db.list_exchange_accounts(uid)] == ["bybit", "hyperliquid"]
    assert db.get_exchange_account(uid)["wallet"] == "0xaa" and db.get_exchange_account(uid, "bybit")["net"] == "live"
    db.set_exchange_account(uid, "bybit", KEY, "enc3", True, None, net="demo")             # повторное подключение заменяет, а не плодит
    assert db.get_exchange_account(uid, "bybit")["key_enc"] == "enc3" and len(db.list_exchange_accounts(uid)) == 2
    assert db.delete_exchange_account(uid, "bybit") and db.get_exchange_account(uid) and db.get_exchange_account(uid, "bybit") is None
    cid = db.add_config(uid, "К", {"symbol": "BTC/USDT:USDT"})[0]["id"]
    for ex in ("hyperliquid", "bybit"):
        db.set_pending(cid, uid, 1, "BTC/USDT:USDT", "long", "pullback", 99.0, 100.0, 400.0, 200.0, 2.0, f"o-{ex}", 9e9, exchange=ex)
        db.set_retry(cid, 1, "long", reason=ex, exchange=ex)
    assert db.get_pending(cid)["order_id"] == "o-hyperliquid" and db.get_pending(cid, "bybit")["order_id"] == "o-bybit" and len(db.pending_of(cid)) == 2
    db.clear_pending(cid, "bybit")
    db.clear_retry(cid, "hyperliquid")
    assert [r["exchange"] for r in db.pending_of(cid)] == ["hyperliquid"] and [r["exchange"] for r in db.list_retries()] == ["bybit"]
    db.clear_pending(cid)
    assert db.pending_of(cid) == []


def test_exchange_settings_are_separate_and_default_to_off(auth_env):
    db = auth_env
    uid = db.check_login("alice", "alice-password")["id"]
    cid = db.add_config(uid, "К", {"symbol": "BTC/USDT:USDT"})[0]["id"]
    c = db.get_config(cid)
    assert set(c["ex"]) == set(EXCHANGES) and not c["any_open"] and not c["any_manage"] and all(not e["open"] and not e["manage"] and e["over"] == {} for e in c["ex"].values())
    db.set_auto(uid, cid, True, None, exchange="bybit")
    db.set_auto(uid, cid, None, True)                                                      # Hyperliquid — прежняя колонка auto
    c = db.get_config(cid)
    assert c["ex"]["bybit"] == {"open": True, "manage": False, "base": "config", "over": {}} and c["ex"]["hyperliquid"]["manage"] and c["auto"]["manage"]
    assert c["auto"]["open"] is False and c["any_open"] and c["any_manage"]
    db.set_auto(uid, cid, base="account", exchange="bybit")
    assert db.get_config(cid)["ex"]["bybit"]["base"] == "account" and db.get_config(cid)["ex"]["hyperliquid"]["base"] == "config"
    assert db.set_ex_over(uid, cid, "bybit", {"trade_pct": 5, "leverage": 3}) == {"trade_pct": 5, "leverage": 3}
    assert db.set_ex_over(uid, cid, "bybit", {"leverage": None, "entry_limit_pct": 0.3}) == {"trade_pct": 5, "entry_limit_pct": 0.3}
    assert db.get_config(cid)["ex"]["hyperliquid"]["over"] == {} and db.get_config(cid)["ex"]["bybit"]["open"]                   # настройки одной биржи не трогают другую и переключатели
    with pytest.raises(DbError):
        db.set_ex_over(uid, cid, "bybit", {"sl_pct": 1})                                   # стоп и тейки — общие, у биржи их менять нельзя
    with pytest.raises(DbError):
        db.set_ex_over(uid, cid, "kraken", {"trade_pct": 1})
    assert db.set_ex_over(uid, 999, "bybit", {"trade_pct": 1}) is None and set(EXCHANGE_OVERRIDES) == {"trade_pct", "leverage", "pyramid_pct", "pyramid_leverage", "entry_limit_pct", "entry_limit_dir", "entry_limit_ttl_h"}
    # выключить на одной бирже: вторая остаётся
    assert db.disable_all_auto(uid, "bybit") == 1
    c = db.get_config(cid)
    assert not c["ex"]["bybit"]["open"] and c["ex"]["hyperliquid"]["manage"] and c["ex"]["bybit"]["over"]["trade_pct"] == 5 and c["ex"]["bybit"]["base"] == "account"
    assert db.disable_all_auto(uid) == 1 and not db.get_config(cid)["any_manage"]
    # повторный импорт конфига с тем же именем сбрасывает всё, в том числе настройки бирж (как и автомод)
    db.add_config(uid, "К", {"symbol": "BTC/USDT:USDT"})
    assert db.get_config(cid)["ex"]["bybit"]["over"] == {}


def test_old_database_is_migrated_with_hyperliquid_data_kept(tmp_path):
    path = str(tmp_path / "old.db")
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, login TEXT UNIQUE NOT NULL, login_lc TEXT UNIQUE, pw_hash TEXT NOT NULL, created REAL NOT NULL, disabled INTEGER NOT NULL DEFAULT 0,
            telegram_id INTEGER, telegram_name TEXT, failed INTEGER NOT NULL DEFAULT 0, locked_until REAL NOT NULL DEFAULT 0);
        INSERT INTO users(id, login, login_lc, pw_hash, created) VALUES (1, 'old', 'old', 'x', 1);
        CREATE TABLE configs (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, name TEXT NOT NULL, params TEXT NOT NULL, tf TEXT NOT NULL DEFAULT 'auto',
            paused INTEGER NOT NULL DEFAULT 0, baseline INTEGER NOT NULL DEFAULT 0, last_ts INTEGER NOT NULL DEFAULT 0, alerts INTEGER NOT NULL DEFAULT 0, error TEXT, created REAL NOT NULL, updated REAL NOT NULL, auto TEXT);
        INSERT INTO configs(id, user_id, name, params, created, updated, auto) VALUES (7, 1, 'Старый', '{}', 1, 1, '{"open": true, "manage": true, "base": "account"}');
        CREATE TABLE exchange_accounts (user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE, exchange TEXT NOT NULL, wallet TEXT NOT NULL, key_enc TEXT NOT NULL, testnet INTEGER NOT NULL,
            agent_address TEXT, created REAL NOT NULL, verified REAL NOT NULL);
        INSERT INTO exchange_accounts VALUES (1, 'hyperliquid', '0xaa', 'enc', 1, '0xbb', 5, 6);
        CREATE TABLE pending_entries (config_id INTEGER PRIMARY KEY REFERENCES configs(id) ON DELETE CASCADE, user_id INTEGER NOT NULL, signal_ts INTEGER NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
            mode TEXT NOT NULL, level REAL NOT NULL, ref REAL NOT NULL, notional REAL NOT NULL, margin REAL NOT NULL, leverage REAL NOT NULL, order_id TEXT, created REAL NOT NULL, expires REAL NOT NULL);
        INSERT INTO pending_entries VALUES (7, 1, 123, 'BTC/USDT:USDT', 'long', 'pullback', 99, 100, 400, 200, 2, 'L1', 1, 2);
        CREATE TABLE auto_retry (config_id INTEGER PRIMARY KEY REFERENCES configs(id) ON DELETE CASCADE, signal_ts INTEGER NOT NULL, side TEXT NOT NULL, pyramid INTEGER NOT NULL DEFAULT 0, leg INTEGER, reason TEXT,
            created REAL NOT NULL);
        INSERT INTO auto_retry VALUES (7, 123, 'long', 0, NULL, 'сбой', 3);
    """)
    c.commit()
    c.close()
    db = Database(path)
    db2 = Database(path)                                                                   # повторное открытие ничего не ломает
    acc = db2.get_exchange_account(1)
    assert acc["wallet"] == "0xaa" and acc["agent_address"] == "0xbb" and acc["testnet"] is True and acc["net"] is None and db.get_exchange_account(1, "bybit") is None
    assert db.get_pending(7)["order_id"] == "L1" and db.get_pending(7)["exchange"] == "hyperliquid" and db.get_pending(7, "bybit") is None
    r = db.list_retries()
    assert len(r) == 1 and r[0]["exchange"] == "hyperliquid" and r[0]["reason"] == "сбой"
    cfg = db.get_config(7)
    assert cfg["auto"] == {"open": True, "manage": True, "base": "account"} and cfg["ex"]["hyperliquid"]["open"] and not cfg["ex"]["bybit"]["open"] and cfg["any_open"]
    db.set_exchange_account(1, "bybit", KEY, "e2", True, None, net="testnet")
    assert len(db.list_exchange_accounts(1)) == 2


# ---------------------------------------------------------------- manage

def _cfg(db, name="Конфиг", **extra):
    uid = db.check_login("alice", "alice-password")["id"]
    params = {"symbol": "BTC/USDT:USDT", "trade_pct": 20, "leverage": 2, "deposit": 1000} | extra
    return uid, db.add_config(uid, name, params)[0]["id"]


def test_set_ex_over_validates_and_does_not_touch_the_config(auth_env):
    db = auth_env
    uid, cid = _cfg(db)
    ad = DryRunAdapter()
    assert manage.set_ex_over(db, ad, uid, cid, "bybit", trade_pct=5, leverage=3) == {"trade_pct": 5, "leverage": 3}
    c = db.get_config(cid)
    assert c["params"]["trade_pct"] == 20 and c["params"]["leverage"] == 2 and c["ex"]["bybit"]["over"] == {"trade_pct": 5, "leverage": 3} and c["ex"]["hyperliquid"]["over"] == {}
    assert manage.effective_params(c["params"], c["ex"]["bybit"]["over"])["leverage"] == 3 and manage.effective_params(c["params"], c["ex"]["hyperliquid"]["over"])["leverage"] == 2
    assert manage.set_ex_over(db, ad, uid, cid, "hyperliquid", entry_limit_pct=0.4, entry_limit_dir="breakout", entry_limit_ttl_h=6) == {"entry_limit_pct": 0.4, "entry_limit_dir": "breakout", "entry_limit_ttl_h": 6}
    assert manage.set_ex_over(db, ad, uid, cid, "bybit", entry_limit_pct=0) == {"trade_pct": 5, "leverage": 3, "entry_limit_pct": 0}                  # 0 — «по рынку» (не «убрать»)
    assert manage.set_ex_over(db, ad, uid, cid, "bybit", trade_pct=None, leverage=None, entry_limit_pct=None) == {}
    for bad in ({"leverage": 500}, {"trade_pct": 0}, {"trade_pct": "много"}, {"entry_limit_dir": "вбок"}, {"entry_limit_pct": 50}, {"sl_pct": 1}, {}):
        with pytest.raises(manage.ManageError):
            manage.set_ex_over(db, ad, uid, cid, "bybit", **bad)
    with pytest.raises(manage.ManageError, match="биржа"):
        manage.set_ex_over(db, ad, uid, cid, "kraken", trade_pct=1)
    with pytest.raises(manage.ManageError, match="не найден"):
        manage.set_ex_over(db, ad, uid, 999, "bybit", trade_pct=1)
    log = db.list_auto_log(uid, 10)
    assert log[0]["kind"] == "exchange_cfg" and log[0]["details"]["exchange"] == "bybit"
    # пирамидинг: у биржи не должно остаться места на добор
    uid, cid2 = _cfg(db, "Пирамида", pyramid_enabled=True, pyramid_pct=10, trade_pct=20)
    with pytest.raises(manage.ManageError, match="Пирамидинг"):
        manage.set_ex_over(db, ad, uid, cid2, "bybit", trade_pct=95)


def test_set_auto_per_exchange_arms_catchup_and_keeps_other_exchange(auth_env):
    db = auth_env
    uid, cid = _cfg(db)
    ad = DryRunAdapter()
    manage.set_catchup(db, ad, uid, cid, on=True)
    manage.set_auto(db, ad, uid, cid, open_=True, exchange="bybit")
    assert db.get_config(cid)["catchup"]["armed"] and db.get_config(cid)["ex"]["bybit"]["open"] and not db.get_config(cid)["ex"]["hyperliquid"]["open"]
    db.set_catchup(uid, cid, armed=False)
    manage.set_auto(db, ad, uid, cid, open_=True)                                          # вторая биржа: тоже взводит, это отдельный вход
    assert db.get_config(cid)["catchup"]["armed"]
    manage.set_auto(db, ad, uid, cid, open_=False, exchange="bybit")                      # осталась включённой Hyperliquid — «догнать» не снимается
    assert db.get_config(cid)["catchup"]["armed"]
    manage.set_auto(db, ad, uid, cid, open_=False)
    assert not db.get_config(cid)["catchup"]["armed"]
    with pytest.raises(manage.ManageError):
        manage.set_auto(db, ad, uid, cid, open_=True, exchange="kraken")
    manage.set_auto(db, ad, uid, cid, open_=True, manage=True, exchange="bybit")
    manage.set_paused(db, uid, cid, True)                                                  # пауза выключает автомод на всех биржах
    assert not db.get_config(cid)["any_open"] and not db.get_config(cid)["any_manage"]


class Spy(DryRunAdapter):
    real = True

    def __init__(self, name, fail=False):
        self.name, self.fail, self.cancelled, self.closed_all = name, fail, [], 0

    def cancel_order_id(self, oid, symbol):
        if self.fail:
            raise ExchangeError("биржа не отвечает")
        self.cancelled.append(oid)

    def close_all(self):
        self.closed_all += 1
        if self.fail:
            raise ExchangeError("нет связи")
        return {"closed": [{"symbol": "BTC", "side": "long", "size": 1.0, "status": "sent"}], "cancelled": 2, "errors": ["часть ордеров не снялась"] if self.name == "bybit" else []}


def test_disconnect_one_exchange_turns_off_only_its_auto(auth_env):
    db = auth_env
    uid, cid = _cfg(db)
    db.set_exchange_account(uid, "hyperliquid", "0xaa", "e", True, "0xbb")
    db.set_exchange_account(uid, "bybit", KEY, "e", True, None, net="testnet")
    manage.set_auto(db, DryRunAdapter(), uid, cid, open_=True, manage=True)
    manage.set_auto(db, DryRunAdapter(), uid, cid, open_=True, exchange="bybit")
    db.set_pending(cid, uid, 1, "BTC/USDT:USDT", "long", "pullback", 99.0, 100.0, 400.0, 200.0, 2.0, "L1", 9e9, exchange="bybit")
    db.set_retry(cid, 1, "long", reason="x", exchange="bybit")
    assert manage.disconnect_account(db, uid, "bybit") is True
    c = db.get_config(cid)
    assert db.get_exchange_account(uid, "bybit") is None and db.get_exchange_account(uid)
    assert not c["ex"]["bybit"]["open"] and c["ex"]["hyperliquid"]["open"] and c["ex"]["hyperliquid"]["manage"]
    assert db.pending_of(cid) == [] and db.list_retries() == []
    assert manage.disconnect_account(db, uid) is True and not db.get_config(cid)["any_open"]


def test_panic_and_remove_config_work_on_every_exchange(auth_env):
    db = auth_env
    uid, cid = _cfg(db)
    manage.set_auto(db, DryRunAdapter(), uid, cid, open_=True)
    manage.set_auto(db, DryRunAdapter(), uid, cid, open_=True, exchange="bybit")
    hl, by = Spy("hyperliquid"), Spy("bybit")
    out = manage.panic(db, {"hyperliquid": hl, "bybit": by}, uid)
    assert hl.closed_all == 1 and by.closed_all == 1 and len(out["closed"]) == 2 and {c["exchange"] for c in out["closed"]} == {"hyperliquid", "bybit"} and out["cancelled"] == 4
    assert out["errors"] == ["Bybit: часть ордеров не снялась"] and out["auto_off"] == 1 and not db.get_config(cid)["any_open"]
    out2 = manage.panic(db, {"hyperliquid": Spy("hyperliquid", fail=True), "bybit": by}, uid)          # у одной биржи сбой — другая всё равно закрыта
    assert by.closed_all == 2 and any("Hyperliquid" in e for e in out2["errors"]) and len(out2["closed"]) == 1
    assert manage.panic(db, hl, uid)["closed"][0]["status"] == "sent"                      # старый вызов с одним адаптером
    # удаление конфига снимает лимитки на каждой бирже
    db.set_pending(cid, uid, 1, "BTC/USDT:USDT", "long", "pullback", 99.0, 100.0, 400.0, 200.0, 2.0, "H1", 9e9)
    db.set_pending(cid, uid, 1, "BTC/USDT:USDT", "long", "pullback", 99.0, 100.0, 400.0, 200.0, 2.0, "B1", 9e9, exchange="bybit")
    bad = Spy("bybit", fail=True)
    with pytest.raises(manage.ManageError, match="Bybit"):
        manage.remove_config(db, uid, cid, {"hyperliquid": hl, "bybit": bad})
    assert db.get_config(cid)                                                              # снять не удалось — конфиг остался
    assert manage.remove_config(db, uid, cid, {"hyperliquid": hl, "bybit": by}) == "Конфиг" and "H1" in hl.cancelled and "B1" in by.cancelled and db.get_config(cid) is None


# ---------------------------------------------------------------- API

def test_api_per_exchange_auto_overrides_accounts_and_disconnect(auth_env, monkeypatch):
    manage._acc.clear()
    db = auth_env
    alice = db.check_login("alice", "alice-password")
    cid = db.add_config(alice["id"], "Для API", {"symbol": "BTC/USDT:USDT", "side": "long", "confirm": 2, "tps": [{"pct": 50, "share": 100}], "sl_pct": 50, "deposit": 10000, "trade_pct": 10, "leverage": 5})[0]["id"]
    app = create_app(load_settings())
    a = client(app)
    login(a, "alice", "alice-password")
    m = a.post("/api/monitor", json={}, headers=H_).json()
    e = m["entries"][0]
    assert set(e["ex"]) == {"hyperliquid", "bybit"} and not e["ex"]["bybit"]["open"] and e["pending_entries"] == {} and set(m["exchanges"]) == {"hyperliquid", "bybit"} and not m["exchanges"]["bybit"]["real"]
    assert e["settings"]["entry_limit_pct"] == 0 and e["settings"]["entry_limit_dir"] == "pullback"
    r = a.put(f"/api/monitor/configs/{cid}/auto", json={"open": True, "exchange": "bybit"}, headers=H_)
    assert r.status_code == 200 and r.json()["target"] == "bybit" and r.json()["auto"]["open"] is True
    c = db.get_config(cid)
    assert c["ex"]["bybit"]["open"] and not c["auto"]["open"]                              # без exchange — Hyperliquid, как раньше
    assert a.put(f"/api/monitor/configs/{cid}/auto", json={"manage": True}, headers=H_).json()["target"] == "hyperliquid" and db.get_config(cid)["auto"]["manage"]
    assert a.put(f"/api/monitor/configs/{cid}/auto", json={"open": True, "exchange": "kraken"}, headers=H_).status_code == 422
    r = a.put(f"/api/monitor/configs/{cid}/exchange/bybit", json={"trade_pct": 5, "leverage": 3, "entry_limit_pct": 0.3, "entry_limit_dir": "pullback"}, headers=H_)
    assert r.status_code == 200 and r.json()["over"] == {"trade_pct": 5, "leverage": 3, "entry_limit_pct": 0.3, "entry_limit_dir": "pullback"}
    assert db.get_config(cid)["params"]["trade_pct"] == 10                                  # сам конфиг не изменился
    assert a.put(f"/api/monitor/configs/{cid}/exchange/bybit", json={"leverage": 900}, headers=H_).status_code == 422
    assert a.put(f"/api/monitor/configs/{cid}/exchange/bybit", json={"sl_pct": 3}, headers=H_).status_code == 422                                  # лишние ключи игнорируются -> «нечего менять»
    assert a.put(f"/api/monitor/configs/{cid}/exchange/kraken", json={"leverage": 3}, headers=H_).status_code == 422
    assert a.put(f"/api/monitor/configs/{cid}/exchange/bybit", json={"trade_pct": None, "leverage": None, "entry_limit_pct": None, "entry_limit_dir": None}, headers=H_).json()["over"] == {}
    bob = client(app)
    login(bob, "bob", "bob-password-1")
    assert bob.put(f"/api/monitor/configs/{cid}/exchange/bybit", json={"leverage": 3}, headers=H_).status_code == 404            # чужой конфиг
    # счета: Bybit отдельно от Hyperliquid, секрет наружу не отдаётся
    db.set_exchange_account(alice["id"], "bybit", KEY, "не-шифртекст", True, None, net="demo")
    info = a.get("/api/exchange/account?exchange=bybit").json()
    assert info["connected"] is True and info["wallet"] == "AbCdEf…2345" and info["net"] == "demo" and info["exchange"]["name"] == "bybit" and info["exchange"]["net"] == "demo" and info["error"]
    assert "не-шифртекст" not in str(info) and a.get("/api/exchange/account").json()["connected"] is False                      # Hyperliquid не затронут
    both = a.get("/api/exchange/accounts").json()["accounts"]
    assert both["bybit"]["connected"] and not both["hyperliquid"]["connected"]
    assert a.get("/api/exchange/account?exchange=kraken").status_code == 422
    r = a.post("/api/trading/test-position", json={"confirm": True, "exchange": "bybit"}, headers=H_)
    assert r.status_code == 503 and "/bybit" in r.json()["detail"]                         # ключ не расшифровать: честная ошибка с подсказкой про /bybit
    assert a.post("/api/trading/test-position", json={"confirm": True}, headers=H_).json()["status"] == "dry_run"                            # Hyperliquid — симуляция
    assert a.put(f"/api/monitor/configs/{cid}/auto", json={"open": True, "exchange": "bybit"}, headers=H_).status_code == 200
    assert a.delete("/api/exchange/account?exchange=bybit", headers=H_).json() == {"removed": True}
    assert db.get_exchange_account(alice["id"], "bybit") is None and not db.get_config(cid)["ex"]["bybit"]["open"] and db.get_config(cid)["auto"]["manage"]     # выключился только автомод Bybit
    assert a.get("/api/exchange/account?exchange=bybit").json()["connected"] is False
    # выгрузка исполнений по бирже
    assert a.get("/api/export/fills.csv?exchange=bybit").status_code == 409 and a.get("/api/export/fills.csv?exchange=kraken").status_code == 422


def test_api_panic_and_portfolio_cover_all_exchanges(auth_env, monkeypatch):
    manage._acc.clear()
    db = auth_env
    alice = db.check_login("alice", "alice-password")
    db.add_config(alice["id"], "П", {"symbol": "BTC/USDT:USDT", "side": "long", "confirm": 2, "tps": [{"pct": 50, "share": 100}], "sl_pct": 50, "deposit": 10000, "trade_pct": 10, "leverage": 5})
    db.set_exchange_account(alice["id"], "hyperliquid", "0x" + "ab" * 20, "e", True, "0x" + "cd" * 20)
    db.set_exchange_account(alice["id"], "bybit", KEY, "e", True, None, net="testnet")
    seen = []
    for cls, eq in ((HyperliquidAdapter, 100.0), (BybitAdapter, 40.0)):
        monkeypatch.setattr(cls, "account", lambda self, eq=eq: {"equity": eq, "free": eq, "used": 0.0})
        monkeypatch.setattr(cls, "positions", lambda self: [{"symbol": "BTC", "side": "long", "size": 1.0, "notional": 50.0}])
        monkeypatch.setattr(cls, "close_all", lambda self: seen.append(self.name) or {"closed": [], "cancelled": 0, "errors": []})
    app = create_app(load_settings())
    a = client(app)
    login(a, "alice", "alice-password")
    pf = a.post("/api/portfolio", headers=H_).json()
    assert pf["exposure"]["real"]["equity"] == 140.0 and pf["exposure"]["real"]["gross"] == 100.0                      # реальные счета обеих бирж складываются
    assert a.post("/api/panic", json={"confirm": True}, headers=H_).status_code == 200 and sorted(seen) == ["bybit", "hyperliquid"]
    m = a.post("/api/monitor", json={}, headers=H_).json()
    assert m["accounts"]["bybit"] == {"equity": 40.0, "free": 40.0} and m["accounts"]["hyperliquid"]["equity"] == 100.0 and m["exchanges"]["bybit"]["real"]


def test_trading_log_hides_bybit_keys_even_inside_error_text(tmp_path, monkeypatch):
    from app import tradelog
    monkeypatch.setenv("TRADE_LOG_FILE", str(tmp_path / "t.log"))
    tradelog._logger = None
    tradelog.trade_log("auto_fail", user=1, error=f'bybit GET https://api.bybit.com/v5/x?apiKey={KEY}&sign=abcdef1234567890 {{"secret": "{SECRET}"}}', api_key=KEY, secret=SECRET, exchange="bybit")
    text = (tmp_path / "t.log").read_text(encoding="utf-8")
    assert KEY not in text and SECRET not in text and "abcdef1234567890" not in text and "bybit" in text and '"api_key": "***"' in text
    tradelog._logger = None
