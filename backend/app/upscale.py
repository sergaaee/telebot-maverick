"""Upscale (проп-трейдинг, upscale.trade): адаптер торговли по публичному API с личным ключом (https://docs.upscale.trade/developers).

Как это устроено на стороне Upscale (по документации):
 * ключ — простой bearer-токен `usk_…` без подписи запросов; ключом принимаются только перечисленные в документации операции (вывода средств среди них нет);
 * у пользователя несколько счетов (`demo`, платный челлендж `real`, `tournament`); торговля по ключу включается у каждого счёта отдельно (`apiTrading`), счёт выбирается при подключении;
 * все числа — строки fp9 (значение × 10⁹); рынки адресуются по uuid, а в расчёте символ «BTC/USDT:USDT» превращается в базовый актив «BTC»;
 * позиции хранятся по направлениям (лонг и шорт одного рынка — две позиции); вход — ордер `market`/`limit`, где `amount` — КОТИРУЕМАЯ сумма (маржа с комиссией), плечо — `leverage`;
   закрытие — ордер `take` с `triggerPrice: 0` (по рынку) и `amount` в базовом активе; стоп — ордер `stop` с `positionId`;
 * лимиты ключа: 10 запросов в секунду и 60 в минуту отдельно на чтение и на запись; повтор записи с тем же `x-idempotency-key` безопасен.

ВАЖНО: код проверен только на имитации API (тесты) и по документации — на живом Upscale он не запускался. Сначала прогоните его на ДЕМО-счёте. Платный счёт с правилами просадки бот может «провалить»
(правила челленджа действуют и при торговле через API) — подключайте его осознанно. Размеры считаются как у остальных бирж, но фактический объём на Upscale чуть меньше расчётного: из суммы вычитаются комиссия и спред.
"""
import os
import re
import time
import uuid
from decimal import ROUND_DOWN, Decimal

from .tradelog import trade_error, trade_log
from .trading import ExchangeAdapter, ExchangeError, NotConfigured

BASE_URL = "https://api.upscale.trade"
FP9 = Decimal(10) ** 9
KEY_RE = re.compile(r"^usk_[A-Za-z0-9_\-]{8,200}$")
ACCOUNT_TYPES = {"demo": "демо-счёт", "real": "платный счёт (челлендж / фондированный)", "tournament": "турнирный счёт"}
INCREASE = ("market", "limit", "stop_market", "stop_limit")
CLOSE_ORDERS = ("stop", "take", "trailing_stop")

# понятные тексты для стабильных кодов ошибок Upscale (по документации); незнакомый код показывается как есть
ERRORS = {
    "insufficient_balance": "не хватает свободного баланса счёта",
    "insufficient_reserve_at_execution": "не хватило резерва на исполнение ордера",
    "order_below_min_notional": "объём меньше минимального ордера на этом рынке",
    "order_exceeds_max_open_notional": "объём превышает лимит открытой позиции на рынке",
    "order_exceeds_market_depth": "объём слишком велик для глубины рынка",
    "order_exceeds_max_price_impact": "слишком большое влияние ордера на цену",
    "order_zero_size_at_execution": "размер ордера при исполнении получился нулевым",
    "slippage_tolerance": "цена исполнения вышла за допустимое проскальзывание",
    "invalid_leverage": "плечо вне допустимых границ рынка / фазы счёта",
    "api_trading_not_enabled": "у счёта выключена торговля по API — включите её в приложении Upscale",
    "api_key_not_allowed": "эта операция не принимает API-ключ",
    "account_access_denied": "счёт принадлежит другому пользователю",
    "account_not_found": "счёт не найден",
    "account_phase_not_allowed": "счёт ещё не в торгуемой фазе",
    "challenge_closed": "торговля на счёте завершена (челлендж закрыт или провален)",
    "funded_limit_trading_locked": "счёт заблокирован лимитом управляемого капитала — разрешено только закрытие",
    "market_paused": "рынок на паузе",
    "market_close_only": "рынок принимает только закрытие позиций",
    "market_category_not_allowed": "рынок не из категории, доступной этому счёту",
    "market_not_found": "рынок не найден",
    "market_price_stale": "цена рынка давно не обновлялась — торговать нельзя",
    "market_price_unavailable": "нет текущей цены рынка",
    "account_not_loaded": "счёт не загружен торговым движком — повторите позже",
    "position_not_found": "позиция не найдена",
    "position_not_available": "позиция недоступна для этого ордера (другой счёт, рынок или направление)",
    "order_not_found": "ордер не найден",
    "order_not_active": "ордер уже не активен",
    "idempotency_key_in_flight": "такой же запрос ещё выполняется",
    "api_key_rate_limit_exceeded": "лимит запросов ключа (60 в минуту на чтение и на запись) — повторю позже",
    "session_expired": "ключ недействителен, отозван или истёк",
    "validation_failed": "запрос не прошёл проверку формата",
}


def to_fp9(value):
    """Десятичное число -> строка fp9 (значение × 10⁹, округление вниз)."""
    return str(int((Decimal(str(value)) * FP9).to_integral_value(rounding=ROUND_DOWN)))


def from_fp9(raw):
    if raw in (None, ""):
        return 0.0
    return float(Decimal(str(raw)) / FP9)


def as_list(payload, *keys):
    """Ответ-список бывает массивом или объектом со списком внутри (страница с общим числом): достаём список."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for k in keys + ("items", "data", "results", "positions", "orders"):
            if isinstance(payload.get(k), list):
                return payload[k]
        for v in payload.values():
            if isinstance(v, list):
                return v
    return []


class UpscaleError(ExchangeError):
    """Ошибка Upscale с кодом и телом ответа (нужны, например, границы плеча из invalid_leverage)."""
    def __init__(self, msg, code=None, payload=None, status=None):
        super().__init__(msg)
        self.code, self.payload, self.status = code, payload if isinstance(payload, dict) else {}, status


def _requests_transport(method, url, headers, params, body, timeout):
    import requests
    r = requests.request(method, url, headers=headers, params=params, json=body, timeout=timeout)
    try:
        payload = r.json() if (r.status_code != 204 and r.content) else None
    except ValueError:
        payload = None
    return r.status_code, payload, dict(r.headers)


def _ms(iso):
    """Время из ответа Upscale (ISO 8601) -> миллисекунды, как у остальных бирж; не разобралось — None."""
    if isinstance(iso, (int, float)):
        return int(iso)
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def _mask(key):
    return f"{key[:8]}…{key[-4:]}" if key and len(key) > 14 else ""


class UpscaleAdapter(ExchangeAdapter):
    """Upscale: торговля на выбранном счёте по личному API-ключу. Символ стратегии «BTC/USDT:USDT» -> рынок с базовым активом BTC."""
    name = "upscale"
    label = "Upscale"
    quote = "USD"
    real = True
    min_notional = 0.0               # минимум объёма на Upscale в документации не указан: локально не проверяем, а биржа ответит order_below_min_notional

    def __init__(self, api_key="", account_id="", acct_type="demo", max_usd=None, base_url=None, transport=None, markets_ttl=30.0):
        self.api_key, self.account_id = api_key or "", account_id or ""
        self.acct_type = acct_type if acct_type in ACCOUNT_TYPES else "real"        # неизвестный тип считаем боевым (осторожнее)
        self.testnet = self.acct_type == "demo"
        self.net = self.acct_type
        self.max_usd = float(max_usd if max_usd is not None else (os.getenv("TRADE_MAX_USD") or 5000))
        self.base_url = (base_url or os.getenv("UPSCALE_API_URL") or BASE_URL).rstrip("/")
        self._transport = transport or _requests_transport
        self._markets = None
        self._markets_ts = 0.0
        self._markets_ttl = markets_ttl
        self._lev_bounds = {}            # uuid рынка -> (время, минимум, максимум) плеча, которые биржа назвала в ответе invalid_leverage (границы зависят от фазы счёта — запоминаем ненадолго)
        self.user_id = None

    def describe(self):
        return {**super().describe(), "label": self.label, "net": self.net}

    def _log(self, event, level="info", **f):
        trade_log(event, level, user=self.user_id, exchange=self.name, testnet=self.testnet, net=self.net, **f)

    # ---------- HTTP ----------
    def _call(self, method, path, body=None, params=None, what="запрос", idem=None):
        if not (self.api_key and self.account_id):
            self._log("not_configured", "warn", key=bool(self.api_key), account=bool(self.account_id))
            raise NotConfigured("Счёт Upscale не подключён — отправьте боту /upscale (API-ключ и выбор счёта).")
        headers = {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
            headers["x-idempotency-key"] = idem or str(uuid.uuid4())        # повтор того же запроса с тем же ключом не создаст второй ордер
        url = self.base_url + path
        last = None
        for attempt in range(3):
            try:
                status, payload, rh = self._transport(method, url, headers, params, body, 20)
            except Exception as e:                                           # сеть / таймаут: повторяем (для записи — с тем же ключом идемпотентности)
                last = e
                time.sleep(min(1.0 * (attempt + 1), 2.0))
                continue
            if status == 429 and attempt < 2:
                time.sleep(min(float((rh or {}).get("Retry-After") or (rh or {}).get("retry-after") or 1), 3.0))
                continue
            if status >= 500 and attempt < 2:
                last = ExchangeError(f"HTTP {status}")
                time.sleep(1.0)
                continue
            if status >= 400:
                raise self._error(status, payload, what)
            return payload
        trade_error("exchange_fail", last or RuntimeError("нет ответа"), user=self.user_id, what=what, exchange=self.name)
        raise ExchangeError(f"{self.label}, {what}: нет ответа от биржи ({type(last).__name__ if last else 'ошибка'}: {str(last)[:150]})") from last

    def _error(self, status, payload, what):
        code = (payload or {}).get("error") if isinstance(payload, dict) else None
        msg = (payload or {}).get("message") if isinstance(payload, dict) else None
        if status == 401:
            text = "ключ недействителен, отозван или истёк — подключите счёт заново: /upscale"
        else:
            text = ERRORS.get(code) or (str(msg)[:200] if msg else f"HTTP {status}")
            if code == "invalid_leverage" and isinstance(payload, dict):
                lo, hi = payload.get("minLeverage"), payload.get("maxLeverage")
                if lo is not None and hi is not None:
                    text += f" (от {from_fp9(lo):g}× до {from_fp9(hi):g}×)"
                elif hi is not None:
                    text += f" (максимум {from_fp9(hi):g}×)"
                elif lo is not None:
                    text += f" (минимум {from_fp9(lo):g}×)"
        trade_log("exchange_fail", "error", user=self.user_id, exchange=self.name, what=what, http=status, code=code, message=str(msg)[:300] if msg else None)
        return UpscaleError(f"{self.label}, {what}: {text}" + (f" [{code}]" if code and code not in text else ""), code, payload, status)

    # ---------- рынки ----------
    def markets(self, refresh=False):
        now = time.monotonic()
        if self._markets is None or refresh or now - self._markets_ts > self._markets_ttl:
            self._markets = as_list(self._call("GET", "/v2/markets", params={"accountId": self.account_id}, what="рынки"))
            self._markets_ts = now
        return self._markets

    @staticmethod
    def base_of(symbol):
        return symbol.split("/")[0].upper()

    def _market(self, symbol, closing=False):
        base = self.base_of(symbol)
        found = [m for m in self.markets() if ((m.get("config") or {}).get("baseAsset") or "").upper() == base]
        if not found:
            raise ExchangeError(f"На Upscale нет рынка {base} (или он недоступен этому счёту) — вход по {symbol} невозможен.")
        m = found[0]
        st = m.get("settings") or {}
        if not closing:
            if st.get("isPaused") or (m.get("schedule") or {}).get("inPause"):
                raise ExchangeError(f"Рынок {base} на Upscale сейчас на паузе.")
            if st.get("isClosed"):
                raise ExchangeError(f"Рынок {base} на Upscale закрыт.")
            if st.get("isCloseOnly"):
                raise ExchangeError(f"Рынок {base} на Upscale принимает только закрытие позиций.")
        return m

    def _market_by_id(self, market_id):
        return next((m for m in self.markets() if m.get("id") == market_id), None)

    @staticmethod
    def _price(m):
        p = from_fp9(((m.get("state") or {}).get("indexPrice")))
        if p <= 0:
            raise ExchangeError("Upscale: нет текущей цены рынка")
        return p

    def min_order_value(self, symbol=None, price=None):
        """Минимальный объём ордера в документации не указан; для тестовой позиции берём UPSCALE_MIN_NOTIONAL (10 $ по умолчанию) — меньше биржа отклонит с понятным кодом."""
        try:
            return float(os.getenv("UPSCALE_MIN_NOTIONAL") or 10.0)
        except ValueError:
            return 10.0

    # ---------- позиции и ордера ----------
    def _raw_positions(self):
        return [p for p in as_list(self._call("GET", f"/positions/{self.account_id}/active", what="позиции")) if (p.get("status") or "opened") == "opened"]

    def _raw_orders(self):
        return as_list(self._call("GET", f"/orders/{self.account_id}/active", what="ордера"))

    def _find_position(self, symbol, side, raw=None):
        m = self._market(symbol, closing=True)
        for p in (raw if raw is not None else self._raw_positions()):
            if p.get("market") == m.get("id") and p.get("direction") == side and from_fp9(p.get("size")) > 0:
                return p
        return None

    def positions(self):
        out = []
        for p in self._raw_positions():
            size, notional, margin = from_fp9(p.get("size")), from_fp9(p.get("notional")), from_fp9(p.get("margin"))
            if size <= 0:
                continue
            m = self._market_by_id(p.get("market")) or {}
            cfg = m.get("config") or {}
            entry = notional / size if size else None
            mark = from_fp9((m.get("state") or {}).get("indexPrice")) or None
            d = 1 if p.get("direction") == "long" else -1
            out.append({"symbol": f"{cfg.get('baseAsset', '?')}/{cfg.get('quoteAsset', 'USD')}", "side": p.get("direction"), "size": size, "entry": entry, "mark": mark, "liq": None,
                        "pnl_usd": d * (mark - entry) * size if (mark and entry) else None, "leverage": round(notional / margin, 2) if margin else None, "margin": margin, "notional": notional, "id": p.get("idx")})
        return out

    def account(self):
        """{"equity", "free", "used", ...}. Свободный баланс Upscale не отдаёт одним числом (баланс без нереализованного pnl и резервы ордеров считаются по-разному), поэтому free = None:
        проверку маржи делает сама биржа и отвечает insufficient_balance."""
        accs = as_list(self._call("GET", "/accounts/with-risk-status", what="счёт"))
        a = next((x for x in accs if x.get("accountId") == self.account_id), None)
        if a is None:
            raise ExchangeError("Счёт Upscale не найден среди счетов ключа (отключена торговля по API или счёт закрыт) — переподключите: /upscale")
        rs = a.get("riskStatus") or {}
        used = sum(from_fp9(p.get("margin")) for p in self._raw_positions())
        return {"equity": from_fp9(rs.get("currentEquity")), "free": None, "used": used, "balance": from_fp9(rs.get("currentBalance") or a.get("accountBalance")),
                "status": a.get("status"), "phase": a.get("currentPhase"), "type": a.get("type")}

    def open_orders(self):
        out = []
        for o in self._raw_orders():
            m = self._market_by_id(o.get("market")) or {}
            base = (m.get("config") or {}).get("baseAsset", "?")
            closing = o.get("type") in CLOSE_ORDERS
            buy = (o.get("direction") == "long") != closing
            trig = from_fp9(o.get("triggerPrice")) or None
            out.append({"symbol": f"{base}/{(m.get('config') or {}).get('quoteAsset', 'USD')}", "side": "buy" if buy else "sell", "type": o.get("type"), "amount": from_fp9(o.get("amount")),
                        "price": from_fp9(o.get("limitPrice")) or trig, "trigger": trig if o.get("type") in ("stop", "stop_market", "stop_limit", "trailing_stop") else None, "reduce_only": closing,
                        "ts": _ms(o.get("createdAt"))})
        return out

    def fills(self, limit=20):
        """Последние события по позициям (история позиций счёта): у Upscale нет ленты исполнений, поэтому одна строка — последнее событие позиции."""
        rows = as_list(self._call("GET", f"/positions/{self.account_id}/portfolio/history", params={"limit": limit}, what="история"))
        out = []
        for p in rows[:limit]:
            base_amt, quote_amt = from_fp9(p.get("exchangedBase")), from_fp9(p.get("exchangedQuote"))
            m = self._market_by_id(p.get("market")) or {}
            out.append({"symbol": f"{(m.get('config') or {}).get('baseAsset', '?')}/{(m.get('config') or {}).get('quoteAsset', 'USD')}", "side": p.get("direction"), "amount": base_amt,
                        "price": quote_amt / base_amt if base_amt else None, "fee": from_fp9(p.get("feeInEvent")), "pnl": str(from_fp9(p.get("pnlInEvent"))) if p.get("pnlInEvent") not in (None, "0") else None,
                        "ts": _ms(p.get("lastUpdatedAt"))})
        return out

    # ---------- действия ----------
    LEV_CACHE_SEC = 600.0

    def _clamp_leverage(self, market_id, lev):
        """Плечо в границах, которые биржа ранее назвала для этого рынка (запоминаются на LEV_CACHE_SEC секунд: после повышения фазы счёта максимум вырастает)."""
        hit = self._lev_bounds.get(market_id)
        if not hit or time.monotonic() - hit[0] > self.LEV_CACHE_SEC:
            return lev
        _, lo, hi = hit
        return hi if (hi and lev > hi) else lo if (lo and lev < lo) else lev

    def _increase_order(self, m, side, kind, notional, lev, what, trigger=None, deferred=False):
        """Ордер, открывающий или увеличивающий позицию (market / limit): amount — котируемая сумма (маржа), leverage — плечо. Если плечо вне границ рынка / фазы счёта (invalid_leverage),
        ордер повторяется один раз с ближайшим допустимым плечом: объём сделки (notional) сохраняется, поэтому маржа растёт (при плече ниже запрошенного). -> (ордер, плечо, маржа)."""
        lev_used = self._clamp_leverage(m["id"], lev)
        order = None
        for attempt in (0, 1):
            margin = notional / lev_used
            body = {"accountId": self.account_id, "marketId": m["id"], "type": kind, "direction": side, "amount": to_fp9(margin), "leverage": to_fp9(lev_used)}
            if trigger is not None:
                body["triggerPrice"] = to_fp9(trigger)
            try:
                order = self._call("POST", "/orders", body=body, what=what)
                break
            except UpscaleError as e:
                lo = from_fp9(e.payload.get("minLeverage")) if e.payload.get("minLeverage") else None
                hi = from_fp9(e.payload.get("maxLeverage")) if e.payload.get("maxLeverage") else None
                new = (hi if (hi and lev_used > hi) else lo if (lo and lev_used < lo) else None) if e.code == "invalid_leverage" else None
                if attempt or new is None:
                    raise
                self._lev_bounds[m["id"]] = (time.monotonic(), lo, hi)
                self._log("leverage_adjusted", "warn", requested=lev_used, used=new, min=lo, max=hi, what=what)
                lev_used = new
        return self._order_ok(order, what, deferred), lev_used, margin

    def _check_size(self, notional):
        if notional > self.max_usd + 1e-9:
            raise ExchangeError(f"Объём {notional:.2f} $ больше лимита TRADE_MAX_USD={self.max_usd:g} $ — ордер не отправлен.")
        if notional <= 0:
            raise ExchangeError("Объём ордера должен быть больше нуля.")

    def _order_ok(self, order, what, deferred=False):
        status = (order or {}).get("status")
        if status in ("canceled_by_error", "canceled", "canceled_by_position"):
            code = (order or {}).get("errorCode") or (order or {}).get("reason")
            raise ExchangeError(f"{self.label}, {what}: ордер не исполнен — {ERRORS.get(code, code or status)}" + (f" [{code}]" if code else ""))
        if not deferred and status != "executed":
            self._log("order_status_unexpected", "warn", what=what, status=status)
        return order

    def open_market(self, symbol, side, notional, leverage=1.0, stop=None, takes=None):
        self._check_size(notional)
        m = self._market(symbol)
        price = self._price(m)
        before = self._find_position(symbol, side)
        size_before = from_fp9(before.get("size")) if before else 0.0
        lev = max(1.0, float(leverage))                          # amount — котируемая сумма (маржа вместе с комиссией и спредом): фактический объём чуть меньше расчётного
        self._log("order_request", kind="open", symbol=symbol, side=side, notional=notional, margin=notional / lev, price=price, leverage=lev, stop=stop, takes=takes)
        order, lev_used, margin = self._increase_order(m, side, "market", notional, lev, "вход")
        self._log("order_ok", kind="entry", symbol=symbol, order=order.get("id"), status=order.get("status"), leverage=lev_used)
        pos = self._find_position(symbol, side)
        if pos is None:
            raise ExchangeError("Ордер на вход принят, но позиции на Upscale нет (ордер мог быть отклонён при исполнении) — проверьте счёт и журнал ордеров.")
        filled = from_fp9(pos.get("size")) - size_before
        if filled <= 0:
            raise ExchangeError("Ордер на вход принят, но размер позиции не вырос — проверьте счёт.")
        res = {"status": "sent", "entry_order": order.get("id"), "symbol": symbol, "side": side, "amount": filled, "price": price, "notional": filled * price, "leverage": lev_used, "margin": margin,
               "position": pos.get("idx"), "orders": []}
        if abs(lev_used - lev) > 1e-9:
            res.update(leverage_requested=lev, leverage_clamped=True)
        res["orders"] = self._protect(symbol, side, pos.get("idx"), filled, stop, takes)
        return res

    def _protect(self, symbol, side, position_id, size, stop, takes):
        """Стоп и тейки на позицию (размер size — доля позиции, которую закрывает этот набор). Не встали — позицию без защиты не оставляем: закрываем по рынку."""
        m = self._market(symbol, closing=True)
        placed = []
        try:
            if stop:
                o = self._call("POST", "/orders", body=self._close_body(m, side, position_id, "stop", size, stop), what="стоп")
                placed.append({"kind": "stop", "id": o.get("id"), "price": stop})
            for tp_price, share in (takes or []):
                part = size * float(share) / 100
                if part > 0:
                    o = self._call("POST", "/orders", body=self._close_body(m, side, position_id, "take", part, tp_price), what="тейк")
                    placed.append({"kind": "take", "id": o.get("id"), "price": tp_price, "share": share})
        except Exception as e:
            trade_error("order_fail", e, user=self.user_id, kind="stop_or_takes", symbol=symbol, side=side, note="после входа защита не встала — закрываем позицию")
            try:
                self.close_market(symbol, side, 1.0)
                closed = "позиция закрыта по рынку"
            except Exception as e2:
                closed = f"закрыть не удалось: {e2}"
            raise ExchangeError(f"Вход выполнен, но стоп/тейки не поставлены ({e}); {closed}.") from e
        return placed

    def _close_body(self, m, side, position_id, kind, size, price):
        return {"accountId": self.account_id, "marketId": m["id"], "type": kind, "direction": side, "positionId": position_id, "amount": to_fp9(size), "triggerPrice": to_fp9(price)}

    def protect(self, symbol, side, amount, stop=None, takes=None):
        pos = self._find_position(symbol, side)
        if pos is None:
            return {"status": "skipped", "note": "позиции на Upscale нет — стоп и тейки не ставились", "symbol": symbol}
        self._log("order_request", kind="protect", symbol=symbol, side=side, amount=amount, stop=stop, takes=takes)
        placed = self._protect(symbol, side, pos.get("idx"), min(float(amount), from_fp9(pos.get("size"))) if amount else from_fp9(pos.get("size")), stop, takes)
        self._log("order_ok", kind="protect", symbol=symbol, orders=placed)
        return {"status": "sent", "symbol": symbol, "orders": placed}

    def place_limit_entry(self, symbol, side, notional, price, leverage=1.0):
        self._check_size(notional)
        m = self._market(symbol)
        lev = max(1.0, float(leverage))
        self._log("order_request", kind="limit_entry", symbol=symbol, side=side, notional=notional, margin=notional / lev, price=price, leverage=lev)
        order, lev_used, margin = self._increase_order(m, side, "limit", notional, lev, "лимитный вход", trigger=price, deferred=True)
        self._log("order_ok", kind="limit_entry", symbol=symbol, order=order.get("id"), leverage=lev_used)
        res = {"status": "sent", "order_id": order.get("id"), "symbol": symbol, "side": side, "price": price, "notional": notional, "leverage": lev_used, "margin": margin}
        if abs(lev_used - lev) > 1e-9:
            res.update(leverage_requested=lev, leverage_clamped=True)
        return res

    def order_state(self, order_id, symbol):
        """Нет запроса «ордер по id»: стоит среди активных — open, иначе unknown (исполнился или снят — вызывающий смотрит позицию, как и у остальных бирж)."""
        if any(str(o.get("id")) == str(order_id) and o.get("status") == "active" for o in self._raw_orders()):
            return {"status": "open", "filled": 0.0, "amount": 0.0, "avg": None}
        return {"status": "unknown", "filled": 0.0, "amount": 0.0, "avg": None}

    def cancel_order_id(self, order_id, symbol):
        self._call("DELETE", f"/orders/{order_id}", what="отмена ордера")
        self._log("order_ok", kind="cancel_entry", order=order_id)
        return {"status": "sent", "order_id": order_id}

    def _stops_of(self, position_id):
        return [o for o in self._raw_orders() if o.get("positionId") == position_id and o.get("type") in ("stop", "trailing_stop") and o.get("status") == "active"]

    def stop_prices(self, symbol, side):
        pos = self._find_position(symbol, side)
        if pos is None:
            return []
        return sorted(from_fp9(o.get("triggerPrice")) for o in self._stops_of(pos.get("idx")) if from_fp9(o.get("triggerPrice")) > 0)

    def clear_stops(self, symbol, side):
        pos = self._find_position(symbol, side)
        if pos is None:
            return 0
        n = 0
        for o in self._stops_of(pos.get("idx")):
            self._call("DELETE", f"/orders/{o['id']}", what="снятие стопа")
            n += 1
        self._log("order_ok", kind="clear_stops", symbol=symbol, removed=n)
        return n

    def set_stop(self, symbol, side, price):
        """Переставляет стоп на остаток позиции: старые стопы позиции снимаются, ставится новый на весь размер. Тейки не трогаются."""
        pos = self._find_position(symbol, side)
        if pos is None:
            return {"status": "skipped", "note": "позиции на Upscale нет — стоп не переставлялся", "symbol": symbol}
        self._log("order_request", kind="move_stop", symbol=symbol, side=side, size=pos.get("size"), stop=price)
        removed = self.clear_stops(symbol, side)
        m = self._market(symbol, closing=True)
        o = self._call("POST", "/orders", body={**self._close_body(m, side, pos.get("idx"), "stop", from_fp9(pos.get("size")), price), "amount": pos.get("size")}, what="новый стоп")
        self._log("order_ok", kind="move_stop", symbol=symbol, stop=price, removed_old=removed, order=o.get("id"))
        return {"status": "sent", "symbol": symbol, "stop": price, "removed_old": removed, "order": o.get("id")}

    def close_market(self, symbol, side, share=1.0):
        pos = self._find_position(symbol, side)
        if pos is None:
            return {"status": "skipped", "note": "позиции на Upscale нет — закрывать нечего", "symbol": symbol}
        m = self._market(symbol, closing=True)
        part = min(1.0, max(0.0, float(share)))
        raw = pos.get("size") if part >= 0.999 else to_fp9(from_fp9(pos.get("size")) * part)
        if int(raw) <= 0:
            return {"status": "skipped", "note": "закрываемая часть слишком мала", "symbol": symbol}
        body = {"accountId": self.account_id, "marketId": m["id"], "type": "take", "direction": side, "positionId": pos.get("idx"), "amount": raw, "triggerPrice": "0"}
        self._log("order_request", kind="close", symbol=symbol, side=side, size=pos.get("size"), share=part)
        o = self._order_ok(self._call("POST", "/orders", body=body, what="закрытие"), "закрытие")
        self._log("order_ok", kind="close", symbol=symbol, order=o.get("id"))
        return {"status": "sent", "symbol": symbol, "closed": from_fp9(raw), "order": o.get("id")}

    def close_all(self):
        """Аварийно: все ордера счёта снимаются, все позиции закрываются по рынку одним запросом."""
        closed, errors = [], []
        self._log("close_all_start", "warn")
        try:
            pos, orders = self.positions(), self._raw_orders()
        except (NotConfigured, ExchangeError) as e:
            pos, orders = [], []
            errors.append(f"состояние счёта: {e}")
        try:
            self._call("POST", f"/accounts/{self.account_id}/close-all", body={}, what="закрыть всё")
            closed = [{"symbol": p["symbol"], "side": p["side"], "size": p["size"], "status": "sent"} for p in pos]
        except (NotConfigured, ExchangeError) as e:
            errors.append(str(e))
        self._log("close_all_done", "warn", closed=len(closed), cancelled=len(orders), errors=errors)
        return {"closed": closed, "cancelled": len(orders) if not errors else 0, "errors": errors}


def verify_upscale(api_key, account_id=None, base_url=None, transport=None, user_id=None):
    """Проверяет присланный API-ключ Upscale ПЕРЕД сохранением. -> {"accounts": [счета, доступные для торговли по API], "account": выбранный | None}; ValueError с понятным текстом.
    Торговать можно на счетах со статусом active и включённым apiTrading. Если счёт не указан и подходящий один — он и выбирается."""
    api_key = (api_key or "").strip()
    if not KEY_RE.match(api_key):
        raise ValueError("API-ключ Upscale начинается с «usk_» (создаётся в приложении Upscale; показывается один раз) — проверьте, что скопировали его целиком.")
    trade_log("verify_start", user=user_id, exchange="upscale", api_key=_mask(api_key))
    a = UpscaleAdapter(api_key, account_id="probe", base_url=base_url, transport=transport)
    a.user_id = user_id
    try:
        accounts = as_list(a._call("GET", "/accounts/with-risk-status", what="счета"))
    except NotConfigured as e:
        raise ValueError(str(e))
    except ExchangeError as e:
        raise ValueError(str(e))
    eligible = []
    for x in accounts:
        rs = x.get("riskStatus") or {}
        eligible.append({"accountId": x.get("accountId"), "type": x.get("type"), "phase": x.get("currentPhase"), "status": x.get("status"), "apiTrading": bool(x.get("apiTrading")),
                         "equity": from_fp9(rs.get("currentEquity")), "balance": from_fp9(rs.get("currentBalance") or x.get("accountBalance")), "markets": x.get("availableMarkets")})
    ok = [x for x in eligible if x["status"] == "active" and x["apiTrading"]]
    if not accounts:
        raise ValueError("У ключа нет доступных счетов: на Upscale торговля по API включается у каждого счёта отдельно — включите её у нужного счёта в приложении и пришлите ключ снова.")
    if not ok:
        raise ValueError("Нет счёта, на котором можно торговать по API: нужен счёт со статусом active и включённой торговлей по API. "
                         + "; ".join(f"{ACCOUNT_TYPES.get(x['type'], x['type'])}: статус {x['status']}, API {'включён' if x['apiTrading'] else 'выключен'}" for x in eligible[:5]))
    chosen = None
    if account_id:
        chosen = next((x for x in ok if x["accountId"] == account_id), None)
        if chosen is None:
            raise ValueError("Выбранный счёт недоступен для торговли по API (закрыт, заблокирован или API выключен).")
    elif len(ok) == 1:
        chosen = ok[0]
    trade_log("verify_ok", user=user_id, exchange="upscale", api_key=_mask(api_key), accounts=len(ok), chosen=chosen["accountId"] if chosen else None)
    return {"accounts": ok, "account": chosen}
