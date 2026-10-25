"""Торговля: слой исполнения действий (открыть позицию, переставить стоп, закрыть) за единым интерфейсом.

Данные (свечи, отчёты) идут с Binance как раньше, а торговать можно на Hyperliquid (адаптер на ccxt) — либо в режиме симуляции (по умолчанию): действие записывается в журнал,
ордера не отправляются. Выбор — переменная TRADE_EXCHANGE (пусто = симуляция, hyperliquid = биржа).

ВАЖНО про Hyperliquid:
 * по умолчанию — ТЕСТОВАЯ сеть (HL_TESTNET=true); боевая включается только явным HL_TESTNET=false;
 * ключ — ключ API-кошелька («агента»), а не основного кошелька: он умеет торговать, но не выводить средства;
 * каждый ордер ограничен TRADE_MAX_USD (по умолчанию 100 $ объёма) — предохранитель от слишком большого размера по конфигу;
 * код проверен только на имитации биржи (тесты), на живой сети он не запускался: сначала прогоните его на testnet.
"""
import math
import os

TEST_MAX_USD = 5.0                      # потолок тестовой позиции (маржа): даже если подменить запрос, больше не откроется


class NotConfigured(RuntimeError):
    """Нет ключей / адаптер не настроен."""


class ExchangeError(RuntimeError):
    """Биржа отклонила действие или недоступна."""


class ExchangeAdapter:
    name = "base"
    real = False
    testnet = False
    min_notional = 0.0                  # минимальный объём ордера на бирже, $

    def describe(self):
        return {"name": self.name, "real": self.real, "testnet": self.testnet, "min_notional": self.min_notional}

    def open_market(self, symbol, side, notional, leverage=1.0, stop=None, takes=None):
        """Вход по рынку на объём notional ($) с плечом leverage; stop — цена стопа; takes — [(цена, доля %)] тейков (reduce-only)."""
        raise NotImplementedError

    def set_stop(self, symbol, side, price):
        raise NotImplementedError

    def close_market(self, symbol, side, share=1.0):
        raise NotImplementedError

    def positions(self):
        return []

    def account(self):
        return {}


class DryRunAdapter(ExchangeAdapter):
    """Ничего не отправляет: возвращает то, что было бы сделано."""
    name = "dry-run"

    def open_market(self, symbol, side, notional, leverage=1.0, stop=None, takes=None):
        return {"status": "dry_run", "note": "симуляция: ордер не отправлялся", "symbol": symbol, "side": side, "notional": notional, "leverage": leverage, "stop": stop, "takes": takes}

    def set_stop(self, symbol, side, price):
        return {"status": "dry_run", "note": "симуляция: стоп не переставлялся", "symbol": symbol, "side": side, "stop": price}

    def close_market(self, symbol, side, share=1.0):
        return {"status": "dry_run", "note": "симуляция: позиция не закрывалась", "symbol": symbol, "side": side, "share": share}


class HyperliquidAdapter(ExchangeAdapter):
    """Hyperliquid (бессрочные контракты, расчёт в USDC) через ccxt. Символ стратегии «BTC/USDT:USDT» превращается в «BTC/USDC:USDC»."""
    name = "hyperliquid"
    real = True
    min_notional = 10.0                 # минимальный ордер на Hyperliquid — 10 $ объёма (по документации биржи; уточните при смене правил)

    def __init__(self, wallet=None, key=None, testnet=None, max_usd=None, exchange=None):
        self.wallet = wallet if wallet is not None else os.getenv("HL_WALLET_ADDRESS", "")
        self.key = key if key is not None else os.getenv("HL_PRIVATE_KEY", "")
        self.testnet = testnet if testnet is not None else os.getenv("HL_TESTNET", "true").strip().lower() != "false"
        self.max_usd = float(max_usd if max_usd is not None else (os.getenv("TRADE_MAX_USD") or 100))
        self._ex = exchange

    # ---------- соединение ----------
    def _x(self):
        if self._ex is None:
            if not (self.wallet and self.key):
                raise NotConfigured("Hyperliquid: укажите в .env HL_WALLET_ADDRESS (адрес основного кошелька) и HL_PRIVATE_KEY (ключ API-кошелька).")
            import ccxt
            ex = ccxt.hyperliquid({"walletAddress": self.wallet, "privateKey": self.key, "enableRateLimit": True})
            if self.testnet:
                ex.set_sandbox_mode(True)
            try:
                ex.load_markets()
            except Exception as e:
                raise ExchangeError(f"Hyperliquid недоступен: {e}") from e
            self._ex = ex
        return self._ex

    @staticmethod
    def hl_symbol(symbol):
        return f"{symbol.split('/')[0]}/USDC:USDC"

    def _call(self, what, fn, *a, **k):
        try:
            return fn(*a, **k)
        except (NotConfigured, ExchangeError):
            raise
        except Exception as e:
            raise ExchangeError(f"Hyperliquid, {what}: {e}") from e

    def _price(self, ex, sym):
        t = self._call("цена", ex.fetch_ticker, sym)
        p = t.get("last") or t.get("close") or t.get("mark")
        if not p:
            raise ExchangeError("Hyperliquid: не удалось получить цену")
        return float(p)

    def _position(self, ex, sym):
        """Текущая позиция по символу: (размер в базовом активе > 0, сторона 'long'/'short') или (0, None)."""
        for p in self._call("позиции", ex.fetch_positions, [sym]) or []:
            size = abs(float(p.get("contracts") or 0))
            if size > 0:
                return size, (p.get("side") or ("long" if float(p.get("contracts") or 0) > 0 else "short"))
        return 0.0, None

    # ---------- действия ----------
    def open_market(self, symbol, side, notional, leverage=1.0, stop=None, takes=None):
        ex = self._x()
        sym = self.hl_symbol(symbol)
        if notional > self.max_usd + 1e-9:
            raise ExchangeError(f"Объём {notional:.2f} $ больше лимита TRADE_MAX_USD={self.max_usd:g} $ — ордер не отправлен.")
        if notional < self.min_notional - 1e-9:
            raise ExchangeError(f"Объём {notional:.2f} $ меньше минимального ордера Hyperliquid ({self.min_notional:g} $).")
        price = self._price(ex, sym)
        amount = float(ex.amount_to_precision(sym, notional / price))
        if amount <= 0:
            raise ExchangeError("Размер позиции после округления равен нулю.")
        buy = side == "long"
        entry_side, exit_side = ("buy", "sell") if buy else ("sell", "buy")
        self._call("плечо", ex.set_leverage, int(max(1, round(leverage))), sym, {"marginMode": "isolated"})
        order = self._call("вход", ex.create_order, sym, "market", entry_side, amount, price)
        res = {"status": "sent", "entry_order": order.get("id") if isinstance(order, dict) else None, "symbol": sym, "side": side, "amount": amount, "price": price,
               "notional": amount * price, "leverage": leverage, "orders": []}
        try:
            if stop:
                o = ex.create_order(sym, "market", exit_side, amount, float(stop), {"stopLossPrice": float(stop), "reduceOnly": True})
                res["orders"].append({"kind": "stop", "id": o.get("id"), "price": stop})
            for tp_price, share in (takes or []):
                part = float(ex.amount_to_precision(sym, amount * float(share) / 100))
                if part > 0:
                    o = ex.create_order(sym, "limit", exit_side, part, float(tp_price), {"reduceOnly": True})
                    res["orders"].append({"kind": "take", "id": o.get("id"), "price": tp_price, "share": share})
        except Exception as e:                              # позиция открыта, а стоп/тейк не встали — без защиты её оставлять нельзя: закрываем
            closed = None
            try:
                self.close_market(symbol, side, 1.0)
                closed = "позиция закрыта по рынку"
            except Exception as e2:
                closed = f"закрыть не удалось: {e2}"
            raise ExchangeError(f"Вход выполнен, но стоп/тейки не поставлены ({e}); {closed}.") from e
        return res

    def set_stop(self, symbol, side, price):
        """Переставляет стоп на остаток позиции: старые стопы (триггерные reduce-only) снимаются, ставится новый. Тейки-лимитки не трогаются."""
        ex = self._x()
        sym = self.hl_symbol(symbol)
        size, pos_side = self._position(ex, sym)
        if size <= 0:
            return {"status": "skipped", "note": "позиции на бирже нет — стоп не переставлялся", "symbol": sym}
        exit_side = "sell" if pos_side == "long" else "buy"
        removed = 0
        for o in self._call("ордера", ex.fetch_open_orders, sym) or []:
            if o.get("triggerPrice") and o.get("reduceOnly") and (o.get("side") == exit_side):
                self._call("снятие стопа", ex.cancel_order, o["id"], sym)
                removed += 1
        o = self._call("новый стоп", ex.create_order, sym, "market", exit_side, size, float(price), {"stopLossPrice": float(price), "reduceOnly": True})
        return {"status": "sent", "symbol": sym, "stop": price, "removed_old": removed, "order": o.get("id") if isinstance(o, dict) else None}

    def close_market(self, symbol, side, share=1.0):
        ex = self._x()
        sym = self.hl_symbol(symbol)
        size, pos_side = self._position(ex, sym)
        if size <= 0:
            return {"status": "skipped", "note": "позиции на бирже нет — закрывать нечего", "symbol": sym}
        exit_side = "sell" if pos_side == "long" else "buy"
        part = min(1.0, max(0.0, float(share)))
        amount = size if part >= 0.999 else float(ex.amount_to_precision(sym, size * part))
        price = self._price(ex, sym)
        o = self._call("закрытие", ex.create_order, sym, "market", exit_side, amount, price, {"reduceOnly": True})
        if part >= 0.999:                                     # позиция закрыта целиком — висящие стопы и тейки больше не нужны
            for x in self._call("ордера", ex.fetch_open_orders, sym) or []:
                if x.get("reduceOnly"):
                    try:
                        ex.cancel_order(x["id"], sym)
                    except Exception:
                        pass
        return {"status": "sent", "symbol": sym, "closed": amount, "order": o.get("id") if isinstance(o, dict) else None}

    # ---------- состояние биржи ----------
    def positions(self):
        ex = self._x()
        out = []
        for p in self._call("позиции", ex.fetch_positions) or []:
            size = abs(float(p.get("contracts") or 0))
            if size <= 0:
                continue
            out.append({"symbol": p.get("symbol"), "side": p.get("side"), "size": size, "entry": p.get("entryPrice"), "mark": p.get("markPrice"), "liq": p.get("liquidationPrice"),
                        "pnl_usd": p.get("unrealizedPnl"), "leverage": p.get("leverage"), "margin": p.get("initialMargin"), "notional": p.get("notional")})
        return out

    def account(self):
        ex = self._x()
        b = self._call("баланс", ex.fetch_balance) or {}
        usdc = b.get("USDC") or {}
        return {"equity": usdc.get("total"), "free": usdc.get("free"), "used": usdc.get("used")}


def get_adapter(name=None):
    name = (name if name is not None else os.getenv("TRADE_EXCHANGE", "")).strip().lower()
    if name in ("hyperliquid", "hl"):
        return HyperliquidAdapter()
    return DryRunAdapter()
