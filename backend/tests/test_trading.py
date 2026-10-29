import pytest

from app.trading import DryRunAdapter, ExchangeError, HyperliquidAdapter, NotConfigured, adapter_for, mask_address, verify_hyperliquid


class FakeEx:
    """Имитация ccxt.hyperliquid: запоминает вызовы и отдаёт заданные позиции/ордера."""
    def __init__(self, price=100.0, position=None, open_orders=None, fail_on=None):
        self.calls, self.price, self.position, self.orders, self.fail_on = [], price, position, open_orders or [], fail_on
        self._id = 0

    def _rec(self, name, *a):
        self.calls.append((name, *a))
        if self.fail_on == name:
            raise RuntimeError(f"сбой {name}")

    def amount_to_precision(self, sym, v):
        return f"{v:.4f}"

    def fetch_ticker(self, sym):
        self._rec("ticker", sym)
        return {"last": self.price}

    def set_leverage(self, lev, sym, params=None):
        self._rec("leverage", lev, sym, params)

    def create_order(self, sym, typ, side, amount, price=None, params=None):
        self._rec("order", sym, typ, side, round(amount, 6), price, params or {})
        self._id += 1
        return {"id": f"o{self._id}"}

    def fetch_positions(self, symbols=None):
        self._rec("positions", symbols)
        return [self.position] if self.position else []

    def fetch_open_orders(self, sym=None):
        self._rec("open_orders", sym)
        return self.orders

    def cancel_order(self, oid, sym=None):
        self._rec("cancel", oid)

    def fetch_balance(self):
        return {"USDC": {"total": 123.0, "free": 100.0, "used": 23.0}}


def orders(fx):
    return [c for c in fx.calls if c[0] == "order"]


def test_open_market_sets_isolated_leverage_entry_stop_and_takes():
    fx = FakeEx(price=100.0)
    a = HyperliquidAdapter(exchange=fx, max_usd=1000)
    r = a.open_market("BTC/USDT:USDT", "long", 500.0, 5.0, stop=98.0, takes=[(104.0, 50), (106.0, 50)])
    assert r["status"] == "sent" and r["symbol"] == "BTC/USDC:USDC" and abs(r["amount"] - 5.0) < 1e-9 and len(r["orders"]) == 3
    assert ("leverage", 5, "BTC/USDC:USDC", {"marginMode": "isolated"}) in fx.calls
    o = orders(fx)
    assert o[0][1:5] == ("BTC/USDC:USDC", "market", "buy", 5.0) and o[0][5] == 100.0                                       # вход по рынку, цена нужна для проскальзывания
    assert o[1][2:4] == ("market", "sell") and o[1][5] == 98.0 and o[1][6] == {"stopLossPrice": 98.0, "reduceOnly": True}   # стоп — reduce-only, в обратную сторону
    assert o[2][2:4] == ("limit", "sell") and o[2][4] == 2.5 and o[2][5] == 104.0 and o[2][6] == {"reduceOnly": True}
    assert o[3][4] == 2.5 and o[3][5] == 106.0
    s = HyperliquidAdapter(exchange=FakeEx(), max_usd=1000).open_market("ETH/USDT:USDT", "short", 100.0, 2.0, stop=105.0, takes=[(95.0, 100)])
    assert s["symbol"] == "ETH/USDC:USDC"


def test_safety_limits_min_notional_and_unprotected_position_is_closed():
    a = HyperliquidAdapter(exchange=FakeEx(), max_usd=100)
    with pytest.raises(ExchangeError, match="TRADE_MAX_USD"):
        a.open_market("BTC/USDT:USDT", "long", 150.0, 2.0)
    with pytest.raises(ExchangeError, match="минимального ордера"):
        a.open_market("BTC/USDT:USDT", "long", 5.0, 1.0)
    # стоп не поставился после входа: позиция не должна остаться без защиты — закрывается по рынку
    fx = FakeEx(position={"contracts": 1.0, "side": "long"}, fail_on=None)
    n = {"k": 0}
    orig = fx.create_order

    def flaky(sym, typ, side, amount, price=None, params=None):
        n["k"] += 1
        if n["k"] == 2:                                           # второй ордер — стоп
            raise RuntimeError("биржа отклонила стоп")
        return orig(sym, typ, side, amount, price, params)
    fx.create_order = flaky
    with pytest.raises(ExchangeError, match="закрыта по рынку"):
        HyperliquidAdapter(exchange=fx, max_usd=1000).open_market("BTC/USDT:USDT", "long", 100.0, 2.0, stop=95.0)
    assert any(c[0] == "order" and c[6].get("reduceOnly") and c[3] == "sell" for c in fx.calls)                          # закрывающий reduce-only ордер ушёл


def test_set_stop_replaces_only_trigger_stops_and_close_cancels_leftovers():
    pos = {"contracts": 2.0, "side": "long"}
    fx = FakeEx(position=pos, open_orders=[{"id": "stop1", "triggerPrice": 95.0, "reduceOnly": True, "side": "sell"}, {"id": "tp1", "reduceOnly": True, "side": "sell"}])
    r = HyperliquidAdapter(exchange=fx).set_stop("BTC/USDT:USDT", "long", 100.0)
    assert r["status"] == "sent" and r["removed_old"] == 1 and ("cancel", "stop1") in fx.calls and ("cancel", "tp1") not in fx.calls          # тейк-лимитку не трогаем
    o = orders(fx)[-1]
    assert o[3] == "sell" and o[4] == 2.0 and o[6] == {"stopLossPrice": 100.0, "reduceOnly": True}
    fx2 = FakeEx(position=pos, open_orders=[{"id": "tp1", "reduceOnly": True, "side": "sell"}])
    c = HyperliquidAdapter(exchange=fx2).close_market("BTC/USDT:USDT", "long", 1.0)
    assert c["status"] == "sent" and orders(fx2)[-1][3] == "sell" and orders(fx2)[-1][4] == 2.0 and orders(fx2)[-1][6] == {"reduceOnly": True} and ("cancel", "tp1") in fx2.calls
    half = HyperliquidAdapter(exchange=FakeEx(position=pos)).close_market("BTC/USDT:USDT", "long", 0.5)
    assert half["closed"] == 1.0
    none = HyperliquidAdapter(exchange=FakeEx()).close_market("BTC/USDT:USDT", "long")
    assert none["status"] == "skipped" and HyperliquidAdapter(exchange=FakeEx()).set_stop("BTC/USDT:USDT", "long", 1.0)["status"] == "skipped"


def test_positions_account_orders_fills_and_configuration():
    fx = FakeEx(position={"contracts": -0.5, "side": "short", "symbol": "BTC/USDC:USDC", "entryPrice": 100.0, "markPrice": 99.0, "liquidationPrice": 150.0, "unrealizedPnl": 0.5,
                          "leverage": 5, "initialMargin": 10.0, "notional": 49.5},
                open_orders=[{"symbol": "BTC/USDC:USDC", "side": "buy", "type": "limit", "amount": 0.5, "price": 95.0, "reduceOnly": True, "timestamp": 1}])
    fx.fetch_my_trades = lambda sym=None, since=None, limit=None: [{"symbol": "BTC/USDC:USDC", "side": "sell", "amount": 0.5, "price": 100.0, "fee": {"cost": 0.01}, "info": {"closedPnl": "1.5"}, "timestamp": 5},
                                                                  {"symbol": "BTC/USDC:USDC", "side": "buy", "amount": 0.5, "price": 99.0, "fee": {"cost": 0.01}, "info": {}, "timestamp": 9}]
    a = HyperliquidAdapter(exchange=fx)
    assert a.positions() == [{"symbol": "BTC/USDC:USDC", "side": "short", "size": 0.5, "entry": 100.0, "mark": 99.0, "liq": 150.0, "pnl_usd": 0.5, "leverage": 5, "margin": 10.0, "notional": 49.5}]
    assert a.account() == {"equity": 123.0, "free": 100.0, "used": 23.0}
    assert a.open_orders() == [{"symbol": "BTC/USDC:USDC", "side": "buy", "type": "limit", "amount": 0.5, "price": 95.0, "trigger": None, "reduce_only": True, "ts": 1}]
    f = a.fills(20)
    assert [x["ts"] for x in f] == [9, 5] and f[1]["pnl"] == "1.5" and f[0]["fee"] == 0.01                                   # последние сверху
    with pytest.raises(NotConfigured, match="/hl"):
        HyperliquidAdapter(wallet="0xabc", key="").positions()
    assert HyperliquidAdapter(wallet="a", key="b").testnet is True                                                           # тестовая сеть, если не сказано иное
    d = HyperliquidAdapter(wallet="a", key="b", testnet=True).describe()
    assert d == {"name": "hyperliquid", "real": True, "testnet": True, "min_notional": 10.0} and DryRunAdapter().describe()["real"] is False
    assert DryRunAdapter().open_orders() == [] and DryRunAdapter().fills() == []
    assert mask_address("0x1234567890abcdef1234567890abcdef12345678") == "0x1234…5678" and mask_address(None) == ""


WALLET = "0x" + "ab" * 20
AGENT_KEY = "0x" + "11" * 32


class VerifyEx(FakeEx):
    def __init__(self, agent="0x" + "cd" * 20, role=None, **kw):
        super().__init__(**kw)
        self.agent, self.role = agent, role

    def eth_get_address_from_private_key(self, key):
        return self.agent

    def public_post_info(self, req):
        self.calls.append(("info", req))
        return self.role if self.role is not None else {"role": "agent", "data": {"user": WALLET}}


def test_verify_hyperliquid_accepts_only_registered_api_wallet_keys():
    r = verify_hyperliquid(WALLET, AGENT_KEY, True, exchange=VerifyEx())
    assert r["agent"] == "0x" + "cd" * 20 and r["account"]["equity"] == 123.0
    for bad_wallet in ("", "0x123", "ab" * 20, "0x" + "zz" * 20):
        with pytest.raises(ValueError, match="Адрес кошелька"):
            verify_hyperliquid(bad_wallet, AGENT_KEY, True, exchange=VerifyEx())
    for bad_key in ("", "0x12", "xyz" * 22):
        with pytest.raises(ValueError, match="Ключ"):
            verify_hyperliquid(WALLET, bad_key, True, exchange=VerifyEx())
    with pytest.raises(ValueError, match="выводить средства"):                                                               # ключ основного кошелька (может выводить средства) не принимаем
        verify_hyperliquid(WALLET, AGENT_KEY, True, exchange=VerifyEx(agent=WALLET, role={"role": "missing"}))
    other = "0x" + "ee" * 20
    with pytest.raises(ValueError, match="адрес самого API-кошелька"):                                                       # прислали адрес агента вместо основного
        verify_hyperliquid(WALLET, AGENT_KEY, True, exchange=VerifyEx(agent=WALLET, role={"role": "agent", "data": {"user": other}}))
    with pytest.raises(ValueError, match="не принадлежит API-кошельку"):
        verify_hyperliquid(WALLET, AGENT_KEY, True, exchange=VerifyEx(role={"role": "missing"}))
    with pytest.raises(ValueError, match="не принадлежит API-кошельку"):                                                      # агент зарегистрирован, но на ЧУЖОЙ адрес
        verify_hyperliquid(WALLET, AGENT_KEY, True, exchange=VerifyEx(role={"role": "agent", "data": {"user": "0x" + "ee" * 20}}))
    ok = verify_hyperliquid(WALLET, "11" * 32, True, exchange=VerifyEx())                                                    # ключ без 0x тоже принимается
    assert ok["agent"]


def test_adapter_for_is_per_user_and_encrypted_account_roundtrip(tmp_path, monkeypatch):
    from app.db import Database
    from app.secrets_store import decrypt, encrypt
    monkeypatch.setenv("SECRET_KEY", "")                                                                                     # ключ-файл рядом с базой
    monkeypatch.delenv("TRADE_EXCHANGE", raising=False)
    db = Database(tmp_path / "u.db")
    a, b = db.add_user("alice", "alice-password"), db.add_user("bob", "bob-password-1")
    assert isinstance(adapter_for(db, a["id"]), DryRunAdapter)
    enc = encrypt(AGENT_KEY, db.path)
    assert AGENT_KEY not in enc and decrypt(enc, db.path) == AGENT_KEY and (tmp_path / "secret.key").exists()
    assert oct((tmp_path / "secret.key").stat().st_mode & 0o777) == "0o600"
    db.set_exchange_account(a["id"], "hyperliquid", WALLET, enc, True, "0x" + "cd" * 20)
    ad = adapter_for(db, a["id"])
    assert isinstance(ad, HyperliquidAdapter) and ad.wallet == WALLET and ad.key == AGENT_KEY and ad.testnet is True
    assert adapter_for(db, a["id"]) is ad                                                                                    # кэш
    assert isinstance(adapter_for(db, b["id"]), DryRunAdapter)                                                               # у другого пользователя счёта нет
    raw = (tmp_path / "u.db").read_bytes()
    assert AGENT_KEY.encode() not in raw and b"1111111111111111" not in raw                                                  # в файле базы ключа в открытом виде нет
    monkeypatch.setenv("TRADE_EXCHANGE", "dry-run")
    assert isinstance(adapter_for(db, a["id"]), DryRunAdapter)                                                               # принудительная симуляция
    monkeypatch.delenv("TRADE_EXCHANGE")
    (tmp_path / "secret.key").unlink()                                                                                       # мастер-ключ потерян: адаптер есть, но выдаёт «подключите заново»
    from app.trading import forget_adapter
    forget_adapter(a["id"])
    broken = adapter_for(db, a["id"])
    with pytest.raises(NotConfigured):
        broken.positions()
    assert db.delete_exchange_account(a["id"]) and db.get_exchange_account(a["id"]) is None


def test_exchange_failures_are_wrapped():
    with pytest.raises(ExchangeError, match="сбой ticker"):
        HyperliquidAdapter(exchange=FakeEx(fail_on="ticker")).open_market("BTC/USDT:USDT", "long", 20.0, 2.0)


def test_default_order_limit_is_5000_and_env_overrides(monkeypatch):
    from app.trading import HyperliquidAdapter
    monkeypatch.delenv("TRADE_MAX_USD", raising=False)
    assert HyperliquidAdapter("0x" + "ab" * 20, "0x" + "11" * 32).max_usd == 5000.0
    monkeypatch.setenv("TRADE_MAX_USD", "100")                                                   # старое значение из .env продолжает действовать — его нужно поменять там
    assert HyperliquidAdapter("0x" + "ab" * 20, "0x" + "11" * 32).max_usd == 100.0


class LimitEx(FakeEx):
    def __init__(self, order=None, **kw):
        super().__init__(**kw)
        self.order = order

    def price_to_precision(self, sym, v):
        return f"{v:.2f}"

    def fetch_order(self, oid, sym=None):
        self._rec("fetch_order", oid)
        if self.order is None:
            raise RuntimeError("fetch_order не поддерживается")
        return self.order


def test_limit_entry_places_only_the_entry_order_and_protect_adds_stop_and_takes_later():
    fx = LimitEx(price=100.0)
    a = HyperliquidAdapter(exchange=fx, max_usd=1000)
    r = a.place_limit_entry("BTC/USDT:USDT", "long", 500.0, 99.5, 5.0)
    assert r["status"] == "sent" and r["order_id"] == "o1" and abs(r["price"] - 99.5) < 1e-9 and abs(r["amount"] - 500 / 99.5) < 1e-3
    o = orders(fx)
    assert len(o) == 1 and o[0][2:4] == ("limit", "buy") and o[0][5] == 99.5 and ("leverage", 5, "BTC/USDC:USDC", {"marginMode": "isolated"}) in fx.calls            # стопа и тейков пока нет
    p = a.protect("BTC/USDT:USDT", "long", 5.0, stop=98.0, takes=[(104.0, 50), (106.0, 50)])
    assert [x["kind"] for x in p["orders"]] == ["stop", "take", "take"]
    o = orders(fx)
    assert o[1][2:4] == ("market", "sell") and o[1][6] == {"stopLossPrice": 98.0, "reduceOnly": True} and o[2][2:4] == ("limit", "sell") and o[2][4] == 2.5
    with pytest.raises(ExchangeError, match="больше лимита"):
        a.place_limit_entry("BTC/USDT:USDT", "long", 5000.0, 99.5, 5.0)
    with pytest.raises(ExchangeError, match="меньше минимального"):
        a.place_limit_entry("BTC/USDT:USDT", "short", 5.0, 100.5, 1.0)
    s = a.place_limit_entry("BTC/USDT:USDT", "short", 500.0, 100.5, 2.0)
    assert orders(fx)[-1][2:4] == ("limit", "sell") and s["side"] == "short"


def test_protect_failure_closes_the_position_and_order_state_maps_statuses():
    fx = LimitEx(price=100.0, position={"contracts": 5.0, "side": "long"}, fail_on="order")
    a = HyperliquidAdapter(exchange=fx, max_usd=1000)
    with pytest.raises(ExchangeError, match="закрыта|закрыть не удалось"):
        a.protect("BTC/USDT:USDT", "long", 5.0, stop=98.0)
    for raw, want in (("open", "open"), ("closed", "filled"), ("canceled", "canceled"), ("expired", "canceled"), ("weird", "unknown")):
        st = HyperliquidAdapter(exchange=LimitEx(order={"status": raw, "filled": 2.0, "amount": 5.0, "average": 99.4})).order_state("o1", "BTC/USDT:USDT")
        assert st["status"] == want and st["filled"] == 2.0 and st["avg"] == 99.4
    fb = HyperliquidAdapter(exchange=LimitEx(order=None, open_orders=[{"id": "o1"}]))                                      # биржа не отдаёт ордер по id -> смотрим открытые
    assert fb.order_state("o1", "BTC/USDT:USDT")["status"] == "open"
    assert HyperliquidAdapter(exchange=LimitEx(order=None, open_orders=[])).order_state("o1", "BTC/USDT:USDT")["status"] == "unknown"
    ex = LimitEx()
    HyperliquidAdapter(exchange=ex).cancel_order_id("o9", "BTC/USDT:USDT")
    assert ("cancel", "o9") in ex.calls
