import pytest

from app.trading import DryRunAdapter, ExchangeError, HyperliquidAdapter, NotConfigured, get_adapter


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


def test_positions_account_and_configuration(monkeypatch):
    fx = FakeEx(position={"contracts": -0.5, "side": "short", "symbol": "BTC/USDC:USDC", "entryPrice": 100.0, "markPrice": 99.0, "liquidationPrice": 150.0, "unrealizedPnl": 0.5,
                          "leverage": 5, "initialMargin": 10.0, "notional": 49.5})
    a = HyperliquidAdapter(exchange=fx)
    p = a.positions()
    assert p == [{"symbol": "BTC/USDC:USDC", "side": "short", "size": 0.5, "entry": 100.0, "mark": 99.0, "liq": 150.0, "pnl_usd": 0.5, "leverage": 5, "margin": 10.0, "notional": 49.5}]
    assert a.account() == {"equity": 123.0, "free": 100.0, "used": 23.0}
    with pytest.raises(NotConfigured, match="HL_PRIVATE_KEY"):
        HyperliquidAdapter(wallet="0xabc", key="").positions()
    monkeypatch.delenv("HL_TESTNET", raising=False)
    assert HyperliquidAdapter(wallet="a", key="b").testnet is True                                       # по умолчанию тестовая сеть
    monkeypatch.setenv("HL_TESTNET", "false")
    assert HyperliquidAdapter(wallet="a", key="b").testnet is False
    monkeypatch.setenv("TRADE_EXCHANGE", "hyperliquid")
    assert isinstance(get_adapter(), HyperliquidAdapter) and isinstance(get_adapter(""), DryRunAdapter)
    d = HyperliquidAdapter(wallet="a", key="b", testnet=True).describe()
    assert d == {"name": "hyperliquid", "real": True, "testnet": True, "min_notional": 10.0}
    assert DryRunAdapter().describe()["real"] is False


def test_exchange_failures_are_wrapped():
    with pytest.raises(ExchangeError, match="сбой ticker"):
        HyperliquidAdapter(exchange=FakeEx(fail_on="ticker")).open_market("BTC/USDT:USDT", "long", 20.0, 2.0)
