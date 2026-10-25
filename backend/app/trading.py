"""Торговля: слой исполнения действий (открыть позицию, переставить стоп, закрыть) за единым интерфейсом.

Данные (свечи, отчёты) идут с Binance как раньше, а торговать можно на Hyperliquid (адаптер на ccxt). Счёт у каждого пользователя свой: адрес кошелька и ключ API-кошелька он
присылает боту (/hl), они проверяются и хранятся в базе ЗАШИФРОВАННЫМИ (secrets_store.py); пока счёт не подключён — режим симуляции: действие записывается в журнал, ордера не отправляются.
TRADE_EXCHANGE=dry-run принудительно включает симуляцию для всех.

ВАЖНО про Hyperliquid:
 * сеть (тестовая / боевая) пользователь выбирает при подключении, тестовая — по умолчанию в боте;
 * принимается только ключ API-кошелька («агента», он умеет торговать, но не выводить средства), ключ основного кошелька отклоняется;
 * каждый ордер ограничен TRADE_MAX_USD (по умолчанию 100 $ объёма) — предохранитель от слишком большого размера по конфигу;
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

    def __init__(self, wallet="", key="", testnet=True, max_usd=None, exchange=None):
        self.wallet, self.key, self.testnet = wallet or "", key or "", bool(testnet)
        self.max_usd = float(max_usd if max_usd is not None else (os.getenv("TRADE_MAX_USD") or 100))
        self._ex = exchange
        self.user_id = None                 # для лога: чей счёт

    def _log(self, event, level="info", **f):
        trade_log(event, level, user=self.user_id, exchange=self.name, testnet=self.testnet, **f)

    # ---------- соединение ----------
    def _x(self):
        if self._ex is None:
            if not (self.wallet and self.key):
                self._log("not_configured", "warn", wallet=bool(self.wallet), key=bool(self.key))
                raise NotConfigured("Счёт Hyperliquid не подключён — отправьте боту /hl (адрес кошелька и ключ API-кошелька).")
            import ccxt
            ex = ccxt.hyperliquid({"walletAddress": self.wallet, "privateKey": self.key, "enableRateLimit": True})
            if self.testnet:
                ex.set_sandbox_mode(True)
            try:
                ex.load_markets()
            except Exception as e:
                trade_error("exchange_fail", e, user=self.user_id, what="load_markets", testnet=self.testnet)
                raise ExchangeError(f"Hyperliquid недоступен: {e}") from e
            self._log("exchange_connected", markets=len(getattr(ex, "markets", {}) or {}))
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
            trade_error("exchange_fail", e, user=self.user_id, what=what, testnet=self.testnet)
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
        if amount * price < self.min_notional * 1.01:         # округление вниз (и проскальзывание рыночной цены) увело бы ордер под минимум биржи — берём с небольшим запасом
            amount = float(ex.amount_to_precision(sym, self.min_notional * 1.03 / price))
            notional = max(notional, amount * price)
            if notional > self.max_usd + 1e-9:
                raise ExchangeError(f"Объём {notional:.2f} $ с учётом минимума биржи больше лимита TRADE_MAX_USD={self.max_usd:g} $ — ордер не отправлен.")
        if amount <= 0:
            raise ExchangeError("Размер позиции после округления равен нулю.")
        buy = side == "long"
        entry_side, exit_side = ("buy", "sell") if buy else ("sell", "buy")
        self._log("order_request", kind="open", symbol=sym, side=side, notional=notional, amount=amount, price=price, leverage=leverage, stop=stop, takes=takes)
        self._call("плечо", ex.set_leverage, int(max(1, round(leverage))), sym, {"marginMode": "isolated"})
        order = self._call("вход", ex.create_order, sym, "market", entry_side, amount, price)
        self._log("order_ok", kind="entry", symbol=sym, order=order if isinstance(order, dict) and len(str(order)) < 1500 else (order.get("id") if isinstance(order, dict) else str(order)[:300]))
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
            trade_error("order_fail", e, user=self.user_id, kind="stop_or_takes", symbol=sym, side=side, note="после входа защита не встала — закрываем позицию")
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
        self._log("order_request", kind="move_stop", symbol=sym, side=pos_side, size=size, stop=price)
        for o in self._call("ордера", ex.fetch_open_orders, sym) or []:
            if o.get("triggerPrice") and o.get("reduceOnly") and (o.get("side") == exit_side):
                self._call("снятие стопа", ex.cancel_order, o["id"], sym)
                removed += 1
        o = self._call("новый стоп", ex.create_order, sym, "market", exit_side, size, float(price), {"stopLossPrice": float(price), "reduceOnly": True})
        self._log("order_ok", kind="move_stop", symbol=sym, stop=price, removed_old=removed, order=o.get("id") if isinstance(o, dict) else None)
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
        self._log("order_request", kind="close", symbol=sym, side=pos_side, size=size, share=part, amount=amount, price=price)
        o = self._call("закрытие", ex.create_order, sym, "market", exit_side, amount, price, {"reduceOnly": True})
        self._log("order_ok", kind="close", symbol=sym, amount=amount, order=o.get("id") if isinstance(o, dict) else None)
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

    def open_orders(self):
        ex = self._x()
        out = []
        for o in self._call("ордера", ex.fetch_open_orders) or []:
            out.append({"symbol": o.get("symbol"), "side": o.get("side"), "type": o.get("type"), "amount": o.get("amount"), "price": o.get("price"),
                        "trigger": o.get("triggerPrice"), "reduce_only": bool(o.get("reduceOnly")), "ts": o.get("timestamp")})
        return out

    def fills(self, limit=20):
        ex = self._x()
        rows = self._call("сделки", ex.fetch_my_trades, None, None, limit) or []
        return [{"symbol": t.get("symbol"), "side": t.get("side"), "amount": t.get("amount"), "price": t.get("price"), "fee": (t.get("fee") or {}).get("cost"),
                 "pnl": (t.get("info") or {}).get("closedPnl"), "ts": t.get("timestamp")} for t in rows][-limit:][::-1]


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


_cache = {}


def adapter_for(db, user_id):
    """Адаптер конкретного пользователя: его подключённый счёт Hyperliquid, а без счёта (или при TRADE_EXCHANGE=dry-run) — симуляция."""
    if os.getenv("TRADE_EXCHANGE", "").strip().lower() in ("dry-run", "off", "sim"):
        return DryRunAdapter()
    acc = db.get_exchange_account(user_id)
    if not acc or acc["exchange"] != "hyperliquid":
        return DryRunAdapter()
    sig = (acc["wallet"], acc["key_enc"], acc["testnet"])
    hit = _cache.get(user_id)
    if hit and hit[0] == sig:
        return hit[1]
    try:
        key = decrypt(acc["key_enc"], db.path)
    except SecretError:
        key = ""                                                  # ключ не расшифровать: адаптер сообщит «подключите заново», а не упадёт
    if not key:
        trade_log("key_undecryptable", "error", user=user_id, note="сохранённый ключ не расшифровать (сменился SECRET_KEY / потерян secret.key)")
    a = HyperliquidAdapter(acc["wallet"], key, acc["testnet"])
    a.user_id = user_id
    _cache[user_id] = (sig, a)
    return a


def forget_adapter(user_id):
    _cache.pop(user_id, None)
