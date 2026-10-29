"""Торговля: слой исполнения действий (открыть позицию, переставить стоп, закрыть) за единым интерфейсом.

Данные (свечи, отчёты) идут с Binance как раньше, а торговать можно на Hyperliquid и на Bybit (только USDT-фьючерсы; адаптеры на ccxt). Счёт на каждой бирже у пользователя свой:
Hyperliquid — адрес кошелька и ключ API-кошелька (/hl), Bybit — API-ключ и секрет без права вывода (/bybit); они проверяются и хранятся в базе ЗАШИФРОВАННЫМИ (secrets_store.py);
пока счёт не подключён — режим симуляции: действие записывается в журнал, ордера не отправляются.
TRADE_EXCHANGE=dry-run принудительно включает симуляцию для всех.

ВАЖНО про Hyperliquid:
 * сеть (тестовая / боевая) пользователь выбирает при подключении, тестовая — по умолчанию в боте;
 * принимается только ключ API-кошелька («агента», он умеет торговать, но не выводить средства), ключ основного кошелька отклоняется;
 * каждый ордер ограничен TRADE_MAX_USD (по умолчанию 5000 $ объёма) — предохранитель от слишком большого размера по конфигу;
 * код проверен только на имитации биржи (тесты), на живой сети он не запускался: сначала прогоните его на testnet.
"""
import os
import re

from .secrets_store import SecretError, decrypt
from .tradelog import trade_error, trade_log

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

    def open_orders(self):
        return []

    def fills(self, limit=20):
        return []

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

    def close_all(self):
        """Аварийно: закрыть все позиции и снять все ордера. -> {"closed": [...], "cancelled": n, "errors": [...]}"""
        return {"closed": [], "cancelled": 0, "errors": []}

    def place_limit_entry(self, symbol, side, notional, price, leverage=1.0):
        """Лимитный ордер на вход (без стопа и тейков: они ставятся после исполнения, см. protect). -> {"status", "order_id", "amount", "price", ...}"""
        raise NotImplementedError

    def order_state(self, order_id, symbol):
        """-> {"status": "open"|"filled"|"canceled"|"unknown", "filled": доля/кол-во, "avg": средняя цена | None}"""
        raise NotImplementedError

    def cancel_order_id(self, order_id, symbol):
        raise NotImplementedError

    def protect(self, symbol, side, amount, stop=None, takes=None):
        """Стоп и тейки (reduce-only) на уже открытую позицию."""
        raise NotImplementedError

    def stop_prices(self, symbol, side):
        """Цены стопов (триггерные reduce-only ордера на закрытие позиции side), стоящих на бирже сейчас."""
        return []

    def clear_stops(self, symbol, side):
        """Снимает все стопы позиции (тейки не трогает). -> сколько снято."""
        return 0


class DryRunAdapter(ExchangeAdapter):
    """Ничего не отправляет: возвращает то, что было бы сделано."""
    name = "dry-run"

    def open_market(self, symbol, side, notional, leverage=1.0, stop=None, takes=None):
        return {"status": "dry_run", "note": "симуляция: ордер не отправлялся", "symbol": symbol, "side": side, "notional": notional, "leverage": leverage, "stop": stop, "takes": takes}

    def set_stop(self, symbol, side, price):
        return {"status": "dry_run", "note": "симуляция: стоп не переставлялся", "symbol": symbol, "side": side, "stop": price}

    def close_market(self, symbol, side, share=1.0):
        return {"status": "dry_run", "note": "симуляция: позиция не закрывалась", "symbol": symbol, "side": side, "share": share}

    def close_all(self):
        return {"closed": [], "cancelled": 0, "errors": [], "note": "симуляция: счёт не подключён, закрывать нечего"}

    def place_limit_entry(self, symbol, side, notional, price, leverage=1.0):
        return {"status": "dry_run", "note": "симуляция: лимитный ордер не отправлялся", "symbol": symbol, "side": side, "notional": notional, "price": price, "leverage": leverage, "order_id": None}


class HyperliquidAdapter(ExchangeAdapter):
    """Hyperliquid (бессрочные контракты, расчёт в USDC) через ccxt. Символ стратегии «BTC/USDT:USDT» превращается в «BTC/USDC:USDC»."""
    name = "hyperliquid"
    label = "Hyperliquid"
    quote = "USDC"                      # валюта расчёта в балансе
    real = True
    min_notional = 10.0                 # минимальный ордер на Hyperliquid — 10 $ объёма (по документации биржи; уточните при смене правил)

    def __init__(self, wallet="", key="", testnet=True, max_usd=None, exchange=None):
        self.wallet, self.key, self.testnet = wallet or "", key or "", bool(testnet)
        self.max_usd = float(max_usd if max_usd is not None else (os.getenv("TRADE_MAX_USD") or 5000))
        self._ex = exchange
        self.user_id = None                 # для лога: чей счёт

    def _log(self, event, level="info", **f):
        trade_log(event, level, user=self.user_id, exchange=self.name, testnet=self.testnet, **f)

    # ---------- соединение ----------
    def _x(self):
        if self._ex is None:
            if not (self.wallet and self.key):
                self._log("not_configured", "warn", wallet=bool(self.wallet), key=bool(self.key))
                raise NotConfigured(self._not_connected())
            import ccxt
            ex = ccxt.hyperliquid({"walletAddress": self.wallet, "privateKey": self.key, "enableRateLimit": True})
            if self.testnet:
                ex.set_sandbox_mode(True)
            try:
                ex.load_markets()
            except Exception as e:
                trade_error("exchange_fail", e, user=self.user_id, what="load_markets", testnet=self.testnet)
                raise ExchangeError(f"{self.label} недоступен: {e}") from e
            self._log("exchange_connected", markets=len(getattr(ex, "markets", {}) or {}))
            self._ex = ex
        return self._ex

    def _not_connected(self):
        return "Счёт Hyperliquid не подключён — отправьте боту /hl (адрес кошелька и ключ API-кошелька)."

    @staticmethod
    def hl_symbol(symbol):
        return f"{symbol.split('/')[0]}/USDC:USDC"

    # ----- особенности бирж (Bybit переопределяет) -----
    def _set_leverage(self, ex, sym, leverage):
        self._call("плечо", ex.set_leverage, int(max(1, round(leverage))), sym, {"marginMode": "isolated"})

    def _open_orders(self, ex, sym=None):
        """Открытые ордера, включая условные (стопы)."""
        return self._call("ордера", ex.fetch_open_orders, sym) if sym else self._call("ордера", ex.fetch_open_orders)

    def _positions_raw(self, ex, symbols=None):
        return self._call("позиции", ex.fetch_positions, symbols) if symbols else self._call("позиции", ex.fetch_positions)

    def _cancel(self, ex, order, sym):
        ex.cancel_order(order["id"], sym)

    def _min_value_ok(self, ex, sym, amount, price):
        """Дополнительные ограничения биржи на размер (минимальное количество и т.п.); исключение, если ордер не пройдёт."""
        return None

    def _round(self, kind, ex, sym, value, price=None):
        """Округление количества / цены по правилам биржи. ccxt на слишком малом количестве бросает InvalidOrder («must be greater than minimum amount precision») —
        это не сбой связи, а «объём меньше минимума»: превращаем в понятную ExchangeError, а не в трейсбэк."""
        try:
            return float((ex.amount_to_precision if kind == "amount" else ex.price_to_precision)(sym, value))
        except Exception as e:
            if kind != "amount":
                raise ExchangeError(f"{self.label}: цена {value:g} не подходит под шаг цены {sym}: {e}") from e
            mn = None
            try:
                mn = float((((ex.market(sym).get("limits") or {}).get("amount") or {}).get("min")) or 0) or None
            except Exception:
                pass
            hint = f"минимальное количество {mn:g}" + (f" (≈ {mn * price:.2f} $ по текущей цене)" if price else "") if mn else f"биржа отклонила округление: {str(e)[:150]}"
            raise ExchangeError(f"Объём слишком мал для {sym} на {self.label}: количество {value:.6g} меньше минимального — {hint}. Ордер не отправлен: увеличьте долю или плечо.") from e

    def _split_takes(self, ex, sym, amount, takes):
        """Тейки (reduce-only) по долям позиции -> [(цена, доля %, количество)]. Часть, которая после округления меньше минимального количества биржи, не теряется и не роняет вход:
        её количество переходит в следующий тейк (а остаток в конце — в последний), поэтому вся позиция по-прежнему покрыта тейками."""
        out, carry = [], 0.0
        for tp_price, share in (takes or []):
            raw = amount * float(share) / 100 + carry
            try:
                part = float(ex.amount_to_precision(sym, raw))
            except Exception:
                part = 0.0
            if part > 0:
                out.append([tp_price, share, part])
                carry = 0.0
            else:
                carry = raw
        if carry > 0 and out:
            try:
                out[-1][2] = float(ex.amount_to_precision(sym, out[-1][2] + carry))
            except Exception:
                pass
        return [tuple(x) for x in out]

    def _call(self, what, fn, *a, **k):
        try:
            return fn(*a, **k)
        except (NotConfigured, ExchangeError):
            raise
        except Exception as e:
            trade_error("exchange_fail", e, user=self.user_id, what=what, testnet=self.testnet)
            raise ExchangeError(f"{self.label}, {what}: {e}") from e

    def _price(self, ex, sym):
        t = self._call("цена", ex.fetch_ticker, sym)
        p = t.get("last") or t.get("close") or t.get("mark")
        if not p:
            raise ExchangeError(f"{self.label}: не удалось получить цену")
        return float(p)

    def _position(self, ex, sym):
        """Текущая позиция по символу: (размер в базовом активе > 0, сторона 'long'/'short') или (0, None)."""
        for p in self._positions_raw(ex, [sym]) or []:
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
            raise ExchangeError(f"Объём {notional:.2f} $ меньше минимального ордера {self.label} ({self.min_notional:g} $).")
        price = self._price(ex, sym)
        amount = self._round("amount", ex, sym, notional / price, price)
        if amount * price < self.min_notional * 1.01:         # округление вниз (и проскальзывание рыночной цены) увело бы ордер под минимум биржи — берём с небольшим запасом
            amount = self._round("amount", ex, sym, self.min_notional * 1.03 / price, price)
            notional = max(notional, amount * price)
            if notional > self.max_usd + 1e-9:
                raise ExchangeError(f"Объём {notional:.2f} $ с учётом минимума биржи больше лимита TRADE_MAX_USD={self.max_usd:g} $ — ордер не отправлен.")
        if amount <= 0:
            raise ExchangeError("Размер позиции после округления равен нулю.")
        self._min_value_ok(ex, sym, amount, price)
        buy = side == "long"
        entry_side, exit_side = ("buy", "sell") if buy else ("sell", "buy")
        self._log("order_request", kind="open", symbol=sym, side=side, notional=notional, amount=amount, price=price, leverage=leverage, stop=stop, takes=takes)
        self._set_leverage(ex, sym, leverage)
        order = self._call("вход", ex.create_order, sym, "market", entry_side, amount, price)
        self._log("order_ok", kind="entry", symbol=sym, order=order if isinstance(order, dict) and len(str(order)) < 1500 else (order.get("id") if isinstance(order, dict) else str(order)[:300]))
        res = {"status": "sent", "entry_order": order.get("id") if isinstance(order, dict) else None, "symbol": sym, "side": side, "amount": amount, "price": price,
               "notional": amount * price, "leverage": leverage, "orders": []}
        res["orders"] = self._protect(ex, sym, symbol, side, amount, stop, takes)
        return res

    def _protect(self, ex, sym, symbol, side, amount, stop, takes):
        """Стоп и тейки на открытую позицию. Не встали — позицию без защиты не оставляем: закрываем по рынку и сообщаем."""
        exit_side = "sell" if side == "long" else "buy"
        placed = []
        try:
            if stop:
                o = ex.create_order(sym, "market", exit_side, amount, float(stop), {"stopLossPrice": float(stop), "reduceOnly": True})
                placed.append({"kind": "stop", "id": o.get("id"), "price": stop})
            for tp_price, share, part in self._split_takes(ex, sym, amount, takes):
                o = ex.create_order(sym, "limit", exit_side, part, float(tp_price), {"reduceOnly": True})
                placed.append({"kind": "take", "id": o.get("id"), "price": tp_price, "share": share, "amount": part})
        except Exception as e:                              # позиция открыта, а стоп/тейк не встали — без защиты её оставлять нельзя: закрываем
            closed = None
            trade_error("order_fail", e, user=self.user_id, kind="stop_or_takes", symbol=sym, side=side, note="после входа защита не встала — закрываем позицию")
            try:
                self.close_market(symbol, side, 1.0)
                closed = "позиция закрыта по рынку"
            except Exception as e2:
                closed = f"закрыть не удалось: {e2}"
            raise ExchangeError(f"Вход выполнен, но стоп/тейки не поставлены ({e}); {closed}.") from e
        return placed

    def protect(self, symbol, side, amount, stop=None, takes=None):
        ex = self._x()
        sym = self.hl_symbol(symbol)
        self._log("order_request", kind="protect", symbol=sym, side=side, amount=amount, stop=stop, takes=takes)
        placed = self._protect(ex, sym, symbol, side, amount, stop, takes)
        self._log("order_ok", kind="protect", symbol=sym, orders=placed)
        return {"status": "sent", "symbol": sym, "orders": placed}

    def place_limit_entry(self, symbol, side, notional, price, leverage=1.0):
        ex = self._x()
        sym = self.hl_symbol(symbol)
        if notional > self.max_usd + 1e-9:
            raise ExchangeError(f"Объём {notional:.2f} $ больше лимита TRADE_MAX_USD={self.max_usd:g} $ — ордер не отправлен.")
        if notional < self.min_notional - 1e-9:
            raise ExchangeError(f"Объём {notional:.2f} $ меньше минимального ордера {self.label} ({self.min_notional:g} $).")
        px = self._round("price", ex, sym, price) if hasattr(ex, "price_to_precision") else float(price)
        amount = self._round("amount", ex, sym, notional / px, px)
        if amount * px < self.min_notional * 1.01:
            amount = self._round("amount", ex, sym, self.min_notional * 1.03 / px, px)
            if amount * px > self.max_usd + 1e-9:
                raise ExchangeError(f"Объём {amount * px:.2f} $ с учётом минимума биржи больше лимита TRADE_MAX_USD={self.max_usd:g} $ — ордер не отправлен.")
        if amount <= 0:
            raise ExchangeError("Размер позиции после округления равен нулю.")
        self._min_value_ok(ex, sym, amount, px)
        self._log("order_request", kind="limit_entry", symbol=sym, side=side, notional=amount * px, amount=amount, price=px, leverage=leverage)
        self._set_leverage(ex, sym, leverage)
        order = self._call("лимитный вход", ex.create_order, sym, "limit", "buy" if side == "long" else "sell", amount, px)
        oid = order.get("id") if isinstance(order, dict) else None
        self._log("order_ok", kind="limit_entry", symbol=sym, order=oid)
        return {"status": "sent", "order_id": oid, "symbol": sym, "side": side, "amount": amount, "price": px, "notional": amount * px, "leverage": leverage}

    def order_state(self, order_id, symbol):
        """Состояние ордера. Если биржа не умеет отдавать ордер по id, судим по списку открытых: нет в списке — исполнен или отменён (позицию смотрит вызывающий)."""
        ex = self._x()
        sym = self.hl_symbol(symbol)
        try:
            o = ex.fetch_order(order_id, sym)
            st = {"open": "open", "closed": "filled", "canceled": "canceled", "cancelled": "canceled", "rejected": "canceled", "expired": "canceled"}.get(str(o.get("status")), "unknown")
            return {"status": st, "filled": float(o.get("filled") or 0), "amount": float(o.get("amount") or 0), "avg": float(o["average"]) if o.get("average") else None}
        except Exception as e:
            self._log("order_state_fallback", "warn", order=order_id, error=str(e)[:200])
        opened = self._open_orders(ex, sym) or []
        if any(str(x.get("id")) == str(order_id) for x in opened):
            return {"status": "open", "filled": 0.0, "amount": 0.0, "avg": None}
        return {"status": "unknown", "filled": 0.0, "amount": 0.0, "avg": None}

    def cancel_order_id(self, order_id, symbol):
        ex = self._x()
        self._call("отмена ордера", ex.cancel_order, order_id, self.hl_symbol(symbol))
        self._log("order_ok", kind="cancel_entry", order=order_id)
        return {"status": "sent", "order_id": order_id}

    def set_stop(self, symbol, side, price):
        """Переставляет стоп на остаток позиции: старые стопы (триггерные reduce-only) снимаются, ставится новый. Тейки-лимитки не трогаются."""
        ex = self._x()
        sym = self.hl_symbol(symbol)
        size, pos_side = self._position(ex, sym)
        if size <= 0:
            return {"status": "skipped", "note": "позиции на бирже нет — стоп не переставлялся", "symbol": sym}
        exit_side = "sell" if pos_side == "long" else "buy"
        removed = 0
        self._log("order_request", kind="move_stop", symbol=sym, side=pos_side, size=size, stop=price)
        for o in self._open_orders(ex, sym) or []:
            if o.get("triggerPrice") and o.get("reduceOnly") and (o.get("side") == exit_side):
                self._call("снятие стопа", self._cancel, ex, o, sym)
                removed += 1
        o = self._call("новый стоп", ex.create_order, sym, "market", exit_side, size, float(price), {"stopLossPrice": float(price), "reduceOnly": True})
        self._log("order_ok", kind="move_stop", symbol=sym, stop=price, removed_old=removed, order=o.get("id") if isinstance(o, dict) else None)
        return {"status": "sent", "symbol": sym, "stop": price, "removed_old": removed, "order": o.get("id") if isinstance(o, dict) else None}

    def stop_prices(self, symbol, side):
        ex = self._x()
        sym = self.hl_symbol(symbol)
        exit_side = "sell" if side == "long" else "buy"
        return sorted(float(o["triggerPrice"]) for o in (self._open_orders(ex, sym) or []) if o.get("triggerPrice") and o.get("reduceOnly") and o.get("side") == exit_side)

    def clear_stops(self, symbol, side):
        """Снимает стопы позиции (триггерные reduce-only ордера в сторону закрытия); тейки-лимитки остаются."""
        ex = self._x()
        sym = self.hl_symbol(symbol)
        exit_side = "sell" if side == "long" else "buy"
        removed = 0
        for o in self._open_orders(ex, sym) or []:
            if o.get("triggerPrice") and o.get("reduceOnly") and o.get("side") == exit_side:
                self._call("снятие стопа", self._cancel, ex, o, sym)
                removed += 1
        self._log("order_ok", kind="clear_stops", symbol=sym, removed=removed)
        return removed

    def close_market(self, symbol, side, share=1.0):
        ex = self._x()
        sym = self.hl_symbol(symbol)
        size, pos_side = self._position(ex, sym)
        if size <= 0:
            return {"status": "skipped", "note": "позиции на бирже нет — закрывать нечего", "symbol": sym}
        if side and pos_side != side:                          # на бирже позиция в другую сторону (например, уже открыта новая после разворота) — чужую не трогаем
            return {"status": "skipped", "note": f"на бирже позиция {pos_side}, а закрывать нужно {side} — не трогаю", "symbol": sym}
        exit_side = "sell" if pos_side == "long" else "buy"
        part = min(1.0, max(0.0, float(share)))
        if part >= 0.999:
            amount = size
        else:
            try:
                amount = float(ex.amount_to_precision(sym, size * part))
            except Exception as e:
                self._log("close_part_too_small", "warn", symbol=sym, size=size, share=part, error=str(e)[:200])
                return {"status": "skipped", "note": f"закрываемая часть ({size * part:.6g}) меньше минимального количества {self.label} — частичное закрытие пропущено, позиция {size:g} осталась", "symbol": sym}
        price = self._price(ex, sym)
        self._log("order_request", kind="close", symbol=sym, side=pos_side, size=size, share=part, amount=amount, price=price)
        o = self._call("закрытие", ex.create_order, sym, "market", exit_side, amount, price, {"reduceOnly": True})
        self._log("order_ok", kind="close", symbol=sym, amount=amount, order=o.get("id") if isinstance(o, dict) else None)
        if part >= 0.999:                                     # позиция закрыта целиком — висящие стопы и тейки больше не нужны
            for x in self._open_orders(ex, sym) or []:
                if x.get("reduceOnly"):
                    try:
                        self._cancel(ex, x, sym)
                    except Exception:
                        pass
        return {"status": "sent", "symbol": sym, "closed": amount, "order": o.get("id") if isinstance(o, dict) else None}

    def close_all(self):
        """Аварийное закрытие: все позиции по рынку (reduce-only) и отмена всех открытых ордеров. Ошибка по одной позиции не мешает остальным."""
        ex = self._x()
        closed, errors, cancelled = [], [], 0
        self._log("close_all_start", "warn")
        for p in self.positions():
            try:
                r = self.close_market(p["symbol"], p["side"], 1.0)
                closed.append({"symbol": p["symbol"], "side": p["side"], "size": p["size"], "status": r.get("status")})
            except Exception as e:
                errors.append(f"закрытие {p['symbol']}: {e}")
        try:
            for o in self._open_orders(ex) or []:
                try:
                    self._cancel(ex, o, o.get("symbol"))
                    cancelled += 1
                except Exception as e:
                    errors.append(f"отмена ордера {o.get('id')}: {e}")
        except Exception as e:
            errors.append(f"список ордеров: {e}")
        self._log("close_all_done", "warn", closed=len(closed), cancelled=cancelled, errors=errors)
        return {"closed": closed, "cancelled": cancelled, "errors": errors}

    # ---------- состояние биржи ----------
    def positions(self):
        ex = self._x()
        out = []
        for p in self._positions_raw(ex) or []:
            size = abs(float(p.get("contracts") or 0))
            if size <= 0:
                continue
            out.append({"symbol": p.get("symbol"), "side": p.get("side"), "size": size, "entry": p.get("entryPrice"), "mark": p.get("markPrice"), "liq": p.get("liquidationPrice"),
                        "pnl_usd": p.get("unrealizedPnl"), "leverage": p.get("leverage"), "margin": p.get("initialMargin"), "notional": p.get("notional")})
        return out

    def account(self):
        ex = self._x()
        b = self._call("баланс", ex.fetch_balance) or {}
        usdc = b.get(self.quote) or {}
        return {"equity": usdc.get("total"), "free": usdc.get("free"), "used": usdc.get("used")}

    def open_orders(self):
        ex = self._x()
        out = []
        for o in self._open_orders(ex) or []:
            out.append({"symbol": o.get("symbol"), "side": o.get("side"), "type": o.get("type"), "amount": o.get("amount"), "price": o.get("price"),
                        "trigger": o.get("triggerPrice"), "reduce_only": bool(o.get("reduceOnly")), "ts": o.get("timestamp")})
        return out

    def fills(self, limit=20):
        ex = self._x()
        rows = self._call("сделки", ex.fetch_my_trades, None, None, limit) or []
        return [{"symbol": t.get("symbol"), "side": t.get("side"), "amount": t.get("amount"), "price": t.get("price"), "fee": (t.get("fee") or {}).get("cost"),
                 "pnl": (t.get("info") or {}).get("closedPnl"), "ts": t.get("timestamp")} for t in rows][-limit:][::-1]


class BybitAdapter(HyperliquidAdapter):
    """Bybit — ТОЛЬКО фьючерсы: бессрочные USDT-контракты (linear) через ccxt, тот же набор действий, что у Hyperliquid. Спот, опционы, инверсные контракты не поддерживаются:
    все запросы идут с категорией linear. Символ стратегии «BTC/USDT:USDT» остаётся как есть.
    Сети: live (боевая), testnet (тестовая сеть testnet.bybit.com) и demo (демо-торговля на боевом аккаунте). Ключ API должен быть БЕЗ права вывода средств (см. verify_bybit).
    Режим позиции — односторонний (one-way); маржа — изолированная, если аккаунт позволяет (в Unified Trading Account режим маржи задаётся на уровне аккаунта).
    ВАЖНО: код проверен только на имитации биржи — на живом Bybit он не запускался, сначала прогоните его на testnet / demo."""
    name = "bybit"
    label = "Bybit"
    quote = "USDT"
    min_notional = 5.0                  # у Bybit минимум зависит от монеты (стоимость ≥ 5 USDT и минимальное количество) — дополнительно проверяется по рынку

    def __init__(self, api_key="", secret="", net="testnet", max_usd=None, exchange=None):
        super().__init__(api_key, secret, net != "live", max_usd, exchange)
        self.net = net if net in ("live", "testnet", "demo") else "testnet"

    def _log(self, event, level="info", **f):
        trade_log(event, level, user=self.user_id, exchange=self.name, testnet=self.testnet, net=self.net, **f)

    def describe(self):
        return {**super().describe(), "label": self.label, "net": self.net}

    def _not_connected(self):
        return "Счёт Bybit не подключён — отправьте боту /bybit (API-ключ и секрет без права вывода)."

    def _x(self):
        if self._ex is None:
            if not (self.wallet and self.key):
                self._log("not_configured", "warn", key=bool(self.wallet), secret=bool(self.key))
                raise NotConfigured(self._not_connected())
            import ccxt
            ex = ccxt.bybit({"apiKey": self.wallet, "secret": self.key, "enableRateLimit": True, "options": {"defaultType": "swap", "defaultSubType": "linear"}})
            if self.net == "testnet":
                ex.set_sandbox_mode(True)
            elif self.net == "demo":
                ex.enable_demo_trading(True)
            try:
                ex.load_markets()
            except Exception as e:
                trade_error("exchange_fail", e, user=self.user_id, what="load_markets", testnet=self.testnet, net=self.net)
                raise ExchangeError(f"{self.label} недоступен: {e}") from e
            self._log("exchange_connected", markets=len(getattr(ex, "markets", {}) or {}))
            self._ex = ex
        return self._ex

    @staticmethod
    def hl_symbol(symbol):
        return symbol                   # у Bybit тот же «BTC/USDT:USDT», что и в расчёте

    def _set_leverage(self, ex, sym, leverage):
        lev = int(max(1, round(leverage)))
        try:
            ex.set_margin_mode("isolated", sym, {"leverage": lev})          # в Unified-аккаунте режим маржи общий — биржа может отказать, это не мешает
        except Exception as e:
            self._log("margin_mode_skipped", "info", error=str(e)[:200])
        try:
            ex.set_leverage(lev, sym)
        except Exception as e:
            if "110043" in str(e) or "not modified" in str(e).lower():       # плечо уже такое — для Bybit это «ошибка», для нас — норма
                return
            trade_error("exchange_fail", e, user=self.user_id, what="плечо", testnet=self.testnet)
            raise ExchangeError(f"{self.label}, плечо: {e}") from e

    def _open_orders(self, ex, sym=None):
        """Обычные ордера и условные (стопы) — у Bybit это разные выборки."""
        out, seen = [], set()
        for extra in ({}, {"trigger": True}):
            params = {**extra, **({} if sym else {"settleCoin": "USDT"})}
            try:
                rows = self._call("ордера", ex.fetch_open_orders, sym, None, None, params)
            except ExchangeError:
                if not extra:
                    raise
                continue                                                      # условные ордера не отдались — не критично для обычного списка
            for o in rows or []:
                if o.get("id") not in seen:
                    seen.add(o.get("id"))
                    out.append(o)
        return out

    def _positions_raw(self, ex, symbols=None):
        return self._call("позиции", ex.fetch_positions, symbols, {"settleCoin": "USDT"}) if not symbols else self._call("позиции", ex.fetch_positions, symbols)

    def _cancel(self, ex, order, sym):
        if order.get("triggerPrice") or order.get("stopPrice"):
            ex.cancel_order(order["id"], sym, {"trigger": True})
        else:
            ex.cancel_order(order["id"], sym)

    def _min_value_ok(self, ex, sym, amount, price):
        try:
            m = ex.market(sym)
            lo = ((m.get("limits") or {}).get("amount") or {}).get("min")
        except Exception:
            return None
        if lo and amount < float(lo) - 1e-12:
            raise ExchangeError(f"Количество {amount:g} меньше минимального для {sym} на Bybit ({float(lo):g}, ≈ {float(lo) * price:.1f} $) — увеличьте объём (долю или плечо).")
        return None

    def min_order_value(self, symbol, price=None):
        """Минимальная стоимость ордера по символу: максимум из 5 USDT и минимального количества × цена."""
        try:
            ex = self._x()
            m = ex.market(self.hl_symbol(symbol))
            lo = float((((m.get("limits") or {}).get("amount") or {}).get("min")) or 0)
            px = price or self._price(ex, self.hl_symbol(symbol))
            return max(self.min_notional, lo * px)
        except Exception:
            return self.min_notional


HEX_ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")
HEX_KEY = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")


def mask_address(a):
    return f"{a[:6]}…{a[-4:]}" if a and len(a) > 12 else (a or "")


def verify_hyperliquid(wallet, key, testnet=True, exchange=None, user_id=None):
    try:
        return _verify(wallet, key, testnet, exchange, user_id)
    except ValueError as e:
        trade_log("verify_fail", "warn", user=user_id, wallet=wallet, testnet=testnet, reason=str(e))      # причина отказа — в лог (ключ туда не попадает)
        raise


def _verify(wallet, key, testnet, exchange, user_id):
    """Проверяет присланные адрес и ключ ПЕРЕД сохранением. -> {"agent": адрес ключа, "account": {...}}; ValueError с понятным текстом, если что-то не так.
    Ключ должен принадлежать API-кошельку («агенту»), подтверждённому на этот адрес: ключ основного кошелька (он умеет выводить средства) отклоняется.
    Аварийный обход проверки агента: HL_SKIP_AGENT_CHECK=true (только если биржа изменила ответ userRole)."""
    wallet, key = (wallet or "").strip(), (key or "").strip()
    if not HEX_ADDR.match(wallet):
        raise ValueError("Адрес кошелька должен выглядеть как 0x и 40 символов 0–9, a–f.")
    if not HEX_KEY.match(key):
        raise ValueError("Ключ должен быть 64 символа 0–9, a–f (можно с префиксом 0x).")
    key = key if key.startswith("0x") else "0x" + key
    trade_log("verify_start", user=user_id, wallet=wallet, testnet=testnet)
    a = HyperliquidAdapter(wallet, key, testnet, exchange=exchange)
    a.user_id = user_id
    try:
        ex = a._x()
        agent = ex.eth_get_address_from_private_key(key)
    except NotConfigured as e:
        raise ValueError(str(e))
    except ExchangeError as e:
        raise ValueError(str(e))
    except Exception as e:
        raise ValueError(f"Не удалось разобрать ключ: {e}")
    same = agent.lower() == wallet.lower()
    skip = os.getenv("HL_SKIP_AGENT_CHECK", "").strip().lower() == "true"
    role = None
    if same or not skip:
        try:
            role = ex.public_post_info({"type": "userRole", "user": agent})
        except Exception as e:
            if not same:
                raise ValueError(f"Не удалось проверить API-кошелёк на Hyperliquid: {e}")
        trade_log("verify_role", user=user_id, wallet=wallet, agent=agent, same_address=same, testnet=testnet, role=str(role)[:300])
    master = ((role or {}).get("data") or {}).get("user") if isinstance(role, dict) else None
    if same:
        if isinstance(role, dict) and role.get("role") == "agent" and master:
            raise ValueError(f"Вы прислали адрес самого API-кошелька ({mask_address(agent)}), а нужен адрес ОСНОВНОГО аккаунта ({mask_address(master)}), "
                             "на котором лежат средства. Начните заново: /hl, отправьте адрес основного аккаунта, затем ключ API-кошелька.")
        raise ValueError(f"Адрес {mask_address(wallet)} совпадает с адресом, полученным из ключа, и биржа не считает его API-кошельком "
                         f"(роль: {(role or {}).get('role') if isinstance(role, dict) else role}). Если это ключ основного кошелька — он позволяет выводить средства: "
                         "создайте API-кошелёк (Hyperliquid → More → API), подтвердите его и пришлите его ключ. "
                         "Если вы прислали ключ API-кошелька — проверьте, что в первом сообщении указан адрес основного аккаунта (а не API-кошелька), и сеть (тестовая/боевая).")
    if not skip:
        if not isinstance(role, dict) or role.get("role") != "agent" or not master or master.lower() != wallet.lower():
            raise ValueError("Этот ключ не принадлежит API-кошельку, подтверждённому для указанного адреса "
                             f"(ответ биржи: {str(role)[:120]}). Проверьте адрес основного кошелька, сеть (тестовая/боевая) и что API-кошелёк создан и не истёк.")
    try:
        acc = a.account()
    except (NotConfigured, ExchangeError) as e:
        raise ValueError(str(e))
    trade_log("verify_ok", user=user_id, wallet=wallet, agent=agent, testnet=testnet, equity=acc.get("equity"))
    return {"agent": agent, "account": acc}


BYBIT_NETS = ("live", "testnet", "demo")
BYBIT_KEY = re.compile(r"^[A-Za-z0-9]{10,40}$")
BYBIT_SECRET = re.compile(r"^[A-Za-z0-9]{16,80}$")


def verify_bybit(api_key, secret, net="testnet", exchange=None, user_id=None):
    try:
        return _verify_bybit(api_key, secret, net, exchange, user_id)
    except ValueError as e:
        trade_log("verify_fail", "warn", user=user_id, exchange="bybit", net=net, api_key=mask_address(api_key), reason=str(e))
        raise


def _walk_strings(x):
    if isinstance(x, dict):
        for k, v in x.items():
            yield str(k)
            yield from _walk_strings(v)
    elif isinstance(x, (list, tuple)):
        for v in x:
            yield from _walk_strings(v)
    elif x is not None:
        yield str(x)


def _verify_bybit(api_key, secret, net, exchange, user_id):
    """Проверяет присланные API-ключ и секрет Bybit ПЕРЕД сохранением. -> {"account": {...}, "permissions": ...}; ValueError с понятным текстом.
    Ключ должен уметь торговать (не read-only) и НЕ иметь права вывода средств (по ответу биржи /v5/user/query-api). Не удалось проверить права — ключ не принимается
    (аварийный обход: BYBIT_SKIP_PERM_CHECK=true — только если биржа изменила формат ответа; в демо-режиме средств нет, проверка прав необязательна)."""
    api_key, secret = (api_key or "").strip(), (secret or "").strip()
    if net not in BYBIT_NETS:
        raise ValueError("Сеть: live (боевая), testnet (тестовая) или demo (демо-торговля).")
    if not BYBIT_KEY.match(api_key):
        raise ValueError("API-ключ Bybit — это 10–40 латинских букв и цифр.")
    if not BYBIT_SECRET.match(secret):
        raise ValueError("Секрет Bybit — это 16–80 латинских букв и цифр.")
    trade_log("verify_start", user=user_id, exchange="bybit", net=net, api_key=mask_address(api_key))
    a = BybitAdapter(api_key, secret, net, exchange=exchange)
    a.user_id = user_id
    try:
        ex = a._x()
    except (NotConfigured, ExchangeError) as e:
        raise ValueError(str(e))
    skip = os.getenv("BYBIT_SKIP_PERM_CHECK", "").strip().lower() == "true"
    perms = None
    if not skip:
        try:
            info = ex.private_get_v5_user_query_api()
        except Exception as e:
            if net != "demo":
                raise ValueError(f"Не удалось проверить права ключа Bybit: {str(e)[:200]}. Проверьте ключ, секрет и сеть (боевая/тестовая/демо).")
            info = None
        res = (info or {}).get("result") if isinstance(info, dict) else None
        if isinstance(res, dict):
            perms = res.get("permissions")
            trade_log("verify_role", user=user_id, exchange="bybit", net=net, read_only=res.get("readOnly"), permissions=str(perms)[:300], ips=str(res.get("ips"))[:120])
            if str(res.get("readOnly")) in ("1", "True", "true"):
                raise ValueError("Это ключ только для чтения (Read-only): торговать им нельзя. Создайте ключ с правом «Контракты — ордера и позиции» (Derivatives/Contract: Orders, Positions) и БЕЗ права вывода.")
            contract = [] if not isinstance(perms, dict) else list(perms.get("ContractTrade") or []) + list(perms.get("Derivatives") or [])
            if isinstance(perms, dict) and not contract:
                raise ValueError("У ключа нет права торговли контрактами (Contract / Derivatives: Orders, Positions) — а бот торгует только USDT-фьючерсами Bybit. Создайте ключ с этим правом.")
            if any("withdraw" in s.lower() for s in _walk_strings(perms)):
                raise ValueError("У ключа есть право вывода средств — такой ключ не принимается. Создайте новый ключ API без права Withdraw (Bybit → API → Create New Key → отметьте только торговлю контрактами).")
        elif net != "demo":
            raise ValueError("Биржа не вернула права ключа — не могу убедиться, что у него нет права вывода. Если вы уверены, администратор может задать BYBIT_SKIP_PERM_CHECK=true.")
    try:
        acc = a.account()
    except (NotConfigured, ExchangeError) as e:
        raise ValueError(str(e))
    trade_log("verify_ok", user=user_id, exchange="bybit", net=net, api_key=mask_address(api_key), equity=acc.get("equity"))
    return {"account": acc, "permissions": perms}


_cache = {}
EXCHANGE_LABELS = {"hyperliquid": "Hyperliquid", "bybit": "Bybit"}


def adapter_for(db, user_id, exchange="hyperliquid"):
    """Адаптер конкретного пользователя на конкретной бирже: его подключённый счёт, а без счёта (или при TRADE_EXCHANGE=dry-run) — симуляция."""
    if os.getenv("TRADE_EXCHANGE", "").strip().lower() in ("dry-run", "off", "sim"):
        return DryRunAdapter()
    if exchange not in EXCHANGE_LABELS:
        return DryRunAdapter()
    acc = db.get_exchange_account(user_id, exchange)
    if not acc or acc["exchange"] != exchange:
        return DryRunAdapter()
    sig = (acc["wallet"], acc["key_enc"], acc["testnet"], acc.get("net"))
    hit = _cache.get((user_id, exchange))
    if hit and hit[0] == sig:
        return hit[1]
    try:
        key = decrypt(acc["key_enc"], db.path)
    except SecretError:
        key = ""                                                  # ключ не расшифровать: адаптер сообщит «подключите заново», а не упадёт
    if not key:
        trade_log("key_undecryptable", "error", user=user_id, exchange=exchange, note="сохранённый ключ не расшифровать (сменился SECRET_KEY / потерян secret.key)")
    if exchange == "bybit":
        a = BybitAdapter(acc["wallet"], key, acc.get("net") or ("testnet" if acc["testnet"] else "live"))
    else:
        a = HyperliquidAdapter(acc["wallet"], key, acc["testnet"])
    a.user_id = user_id
    _cache[(user_id, exchange)] = (sig, a)
    return a


def adapters_for(db, user_id):
    """{биржа: адаптер} по всем поддерживаемым биржам (без подключённого счёта — симуляция)."""
    return {name: adapter_for(db, user_id, name) for name in EXCHANGE_LABELS}


def forget_adapter(user_id, exchange=None):
    for k in [k for k in _cache if k[0] == user_id and (exchange is None or k[1] == exchange)]:
        _cache.pop(k, None)
