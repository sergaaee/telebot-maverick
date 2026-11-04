"""Upscale (проп-трейдинг): адаптер на имитации публичного API (fp9, ордера входа / закрытия по позициям, счета) и проверка ключа."""
import uuid

import pytest

from app import manage
from app.trading import DryRunAdapter, ExchangeError, NotConfigured, adapter_for
from app.upscale import UpscaleAdapter, as_list, from_fp9, to_fp9, verify_upscale
from test_auth import auth_env  # noqa: F401

KEY = "usk_" + "a1B2c3D4" * 4
ACC = "11111111-1111-4111-8111-111111111111"
ACC2 = "22222222-2222-4222-8222-222222222222"
BTC = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ETH = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def fp(v):
    return to_fp9(v)


class FakeUpscale:
    """Имитация api.upscale.trade: хранит позиции и ордера счёта, понимает те операции, которыми пользуется адаптер."""
    def __init__(self, price=100000.0, key=KEY):
        self.key, self.calls, self.idem = key, [], {}
        self.accounts = [self.account(ACC, "demo", equity=1000.0)]
        self.markets = [self.market(BTC, "BTC", price), self.market(ETH, "ETH", 3000.0)]
        self.positions, self.orders = [], []
        self.fail = {}                     # (метод, путь-префикс) -> (код, тело)
        self.exec_error = None             # код, с которым движок отклоняет market-ордер при исполнении
        self.close_orders_fail = False     # стоп / тейк не принимаются
        self._n = 0

    @staticmethod
    def account(aid, typ, equity=1000.0, status="active", api=True):
        return {"accountId": aid, "type": typ, "currentPhase": "active_evaluation", "status": status, "apiTrading": api, "accountBalance": fp(equity), "availableMarkets": "crypto",
                "riskStatus": {"accountId": aid, "currentBalance": fp(equity), "currentEquity": fp(equity)}}

    @staticmethod
    def market(mid, base, price, **settings):
        return {"id": mid, "config": {"baseAsset": base, "quoteAsset": "USD", "ticker": f"{base}-USD", "category": "crypto"}, "state": {"indexPrice": fp(price)},
                "settings": {"fee": "1000000", "isCloseOnly": False, "isClosed": False, "isPaused": False, **settings}, "schedule": None}

    def nid(self):
        self._n += 1
        return str(uuid.UUID(int=self._n))

    def price_of(self, mid):
        return from_fp9(next(m for m in self.markets if m["id"] == mid)["state"]["indexPrice"])

    def __call__(self, method, url, headers, params, body, timeout):
        path = url.split("api.upscale.trade", 1)[1]
        self.calls.append({"method": method, "path": path, "headers": headers, "params": params, "body": body})
        if headers.get("Authorization") != f"Bearer {self.key}":
            return 401, {"statusCode": 401, "message": "bad key", "error": "session_expired"}, {}
        for (m, prefix), (code, payload) in self.fail.items():
            if m == method and path.startswith(prefix):
                return code, payload, {}
        if method == "GET" and path == "/accounts/with-risk-status":
            return 200, self.accounts, {}
        if method == "GET" and path == "/v2/markets":
            return 200, self.markets, {}
        if method == "GET" and path.endswith("/positions/active".replace("/positions", "")) and path.startswith("/positions/"):
            return 200, [p for p in self.positions if p["status"] == "opened"], {}
        if method == "GET" and path.startswith("/orders/") and path.endswith("/active"):
            return 200, [o for o in self.orders if o["status"] == "active"], {}
        if method == "GET" and path.endswith("/portfolio/history"):
            return 200, {"items": list(self.positions), "total": len(self.positions)}, {}
        if method == "POST" and path == "/orders":
            return self.create(body, headers.get("x-idempotency-key"))
        if method == "DELETE" and path.startswith("/orders/"):
            oid = path.rsplit("/", 1)[1]
            o = next((x for x in self.orders if x["id"] == oid), None)
            if o is None:
                return 404, {"statusCode": 404, "message": "x", "error": "order_not_found"}, {}
            if o["status"] != "active":
                return 403, {"statusCode": 403, "message": "x", "error": "order_not_active"}, {}
            o["status"] = "canceled"
            return 200, o, {}
        if method == "POST" and path.endswith("/close-all"):
            for p in self.positions:
                p["status"] = "closed"
            for o in self.orders:
                if o["status"] == "active":
                    o["status"] = "canceled"
            return 204, None, {}
        return 404, {"statusCode": 404, "message": path, "error": "not_found"}, {}

    def order(self, body, status, **extra):
        return {"id": self.nid(), "trader": body["accountId"], "market": body["marketId"], "status": status, "type": body["type"], "direction": body["direction"],
                "triggerPrice": body.get("triggerPrice") or "0", "amount": body["amount"], "positionId": body.get("positionId"), "leverage": body.get("leverage"), "errorCode": None, "createdAt": "2026-11-04T10:00:00.000Z", **extra}

    def create(self, body, idem):
        if idem and idem in self.idem:
            return 200, self.idem[idem], {"X-Idempotency-Cached": "true"}
        t = body["type"]
        if t in ("market", "limit"):
            mx = next((m for m in self.markets if m["id"] == body["marketId"]), None)
            lev = from_fp9(body["leverage"])
            if lev > 50:
                return 400, {"statusCode": 400, "message": "lev", "error": "invalid_leverage", "minLeverage": fp(1), "maxLeverage": fp(50)}, {}
            if mx["settings"]["isPaused"]:
                return 403, {"statusCode": 403, "message": "paused", "error": "market_paused"}, {}
            if t == "limit":
                o = self.order(body, "active")
                self.orders.append(o)
                self.idem[idem] = o
                return 200, o, {}
            price = self.price_of(body["marketId"])
            margin = from_fp9(body["amount"])
            if self.exec_error:
                o = self.order(body, "canceled_by_error", errorCode=self.exec_error)
                return 200, o, {}
            size = (margin * 0.999) * lev / price                       # комиссия и спред съедают часть суммы
            pos = next((p for p in self.positions if p["status"] == "opened" and p["market"] == body["marketId"] and p["direction"] == body["direction"]), None)
            if pos:
                pos["size"] = fp(from_fp9(pos["size"]) + size)
                pos["notional"] = fp(from_fp9(pos["notional"]) + size * price)
                pos["margin"] = fp(from_fp9(pos["margin"]) + margin)
            else:
                self.positions.append({"idx": self.nid(), "status": "opened", "market": body["marketId"], "trader": body["accountId"], "direction": body["direction"], "size": fp(size),
                                       "notional": fp(size * price), "margin": fp(margin), "lastUpdatedAt": "2026-11-04T10:00:00.000Z", "exchangedBase": fp(size), "exchangedQuote": fp(size * price),
                                       "feeInEvent": fp(1), "pnlInEvent": "0"})
            o = self.order(body, "executed", indexPrice=fp(price))
            self.idem[idem] = o
            return 200, o, {}
        pos = next((p for p in self.positions if p["idx"] == body.get("positionId") and p["status"] == "opened"), None)
        if pos is None:
            return 404, {"statusCode": 404, "message": "x", "error": "position_not_found"}, {}
        if t == "take" and body["triggerPrice"] == "0":                        # закрытие по рынку
            left = from_fp9(pos["size"]) - from_fp9(body["amount"])
            if left <= 1e-9:
                pos["status"] = "closed"
                for o in self.orders:
                    if o["positionId"] == pos["idx"] and o["status"] == "active":
                        o["status"] = "canceled_by_position"
            else:
                pos["size"] = fp(left)
            o = self.order(body, "executed")
            self.idem[idem] = o
            return 200, o, {}
        if self.close_orders_fail:
            return 400, {"statusCode": 400, "message": "x", "error": "trigger_price_gt_current"}, {}
        o = self.order(body, "active")
        self.orders.append(o)
        self.idem[idem] = o
        return 200, o, {}


def adapter(fake=None, **kw):
    fake = fake or FakeUpscale()
    a = UpscaleAdapter(KEY, ACC, "demo", transport=fake, max_usd=kw.pop("max_usd", 100000), **kw)
    a.fake = fake
    return a


def writes(fake):
    return [c for c in fake.calls if c["method"] in ("POST", "DELETE")]


def test_fp9_roundtrip_and_page_shapes():
    assert to_fp9(100) == "100000000000" and to_fp9("0.5") == "500000000" and to_fp9(5.123456789123) == "5123456789" and from_fp9("1500000000") == 1.5 and from_fp9(None) == 0.0
    assert to_fp9(1e-05) == "10000"
    assert as_list([1]) == [1] and as_list({"items": [2], "total": 1}) == [2] and as_list({"total": 0, "rows": [3]}) == [3] and as_list(None) == []


def test_verify_checks_key_format_accounts_and_picks_the_only_tradable_one():
    with pytest.raises(ValueError, match="usk_"):
        verify_upscale("sk-123")
    fake = FakeUpscale()
    r = verify_upscale(KEY, transport=fake)
    assert r["account"]["accountId"] == ACC and r["account"]["type"] == "demo" and r["account"]["equity"] == 1000.0 and len(r["accounts"]) == 1
    assert fake.calls[0]["headers"]["Authorization"] == f"Bearer {KEY}" and "x-idempotency-key" not in fake.calls[0]["headers"]          # чтение без ключа идемпотентности
    with pytest.raises(ValueError, match="недействителен"):
        verify_upscale(KEY, transport=FakeUpscale(key="usk_другой_ключ_0000"))                                   # 401
    fake2 = FakeUpscale()
    fake2.accounts = [FakeUpscale.account(ACC, "demo"), FakeUpscale.account(ACC2, "real", equity=50000.0), FakeUpscale.account("33333333-3333-4333-8333-333333333333", "real", api=False),
                      FakeUpscale.account("44444444-4444-4444-8444-444444444444", "real", status="failed")]
    r2 = verify_upscale(KEY, transport=fake2)
    assert r2["account"] is None and [x["accountId"] for x in r2["accounts"]] == [ACC, ACC2]                      # несколько подходящих — выбирает пользователь; закрытые и без API не предлагаются
    assert verify_upscale(KEY, account_id=ACC2, transport=fake2)["account"]["type"] == "real"
    with pytest.raises(ValueError, match="недоступен"):
        verify_upscale(KEY, account_id="33333333-3333-4333-8333-333333333333", transport=fake2)
    fake3 = FakeUpscale()
    fake3.accounts = [FakeUpscale.account(ACC, "real", api=False)]
    with pytest.raises(ValueError, match="торговать по API") as e:
        verify_upscale(KEY, transport=fake3)
    assert "API выключен" in str(e.value)
    fake3.accounts = []
    with pytest.raises(ValueError, match="нет доступных счетов"):
        verify_upscale(KEY, transport=fake3)


def test_open_market_sends_quote_amount_and_leverage_then_places_stop_and_takes_on_the_position():
    a = adapter()
    r = a.open_market("BTC/USDT:USDT", "long", 1000.0, 5.0, stop=95000.0, takes=[(103000.0, 50), (106000.0, 50)])
    fake = a.fake
    entry = writes(fake)[0]["body"]
    assert entry == {"accountId": ACC, "marketId": BTC, "type": "market", "direction": "long", "amount": fp(200.0), "leverage": fp(5.0)}              # amount — маржа (1000 / 5), не размер
    assert writes(fake)[0]["headers"]["x-idempotency-key"] and writes(fake)[0]["headers"]["Content-Type"] == "application/json"
    pos = fake.positions[0]
    assert r["status"] == "sent" and r["symbol"] == "BTC/USDT:USDT" and abs(r["amount"] - from_fp9(pos["size"])) < 1e-9 and r["position"] == pos["idx"] and 0.0099 < r["amount"] < 0.01
    stop, tk1, tk2 = [w["body"] for w in writes(fake)[1:]]
    assert stop["type"] == "stop" and stop["positionId"] == pos["idx"] and stop["direction"] == "long" and stop["triggerPrice"] == fp(95000.0) and stop["amount"] == fp(r["amount"])
    assert tk1["type"] == "take" and tk1["triggerPrice"] == fp(103000.0) and from_fp9(tk1["amount"]) == pytest.approx(r["amount"] / 2, abs=1e-8)
    assert tk2["type"] == "take" and tk2["triggerPrice"] == fp(106000.0) and [o["kind"] for o in r["orders"]] == ["stop", "take", "take"]
    keys = [w["headers"]["x-idempotency-key"] for w in writes(fake)]
    assert len(set(keys)) == len(keys)                                                                                                          # у каждого запроса записи свой ключ


def test_add_to_existing_position_protects_only_the_added_size_and_short_is_a_separate_position():
    a = adapter()
    a.open_market("BTC/USDT:USDT", "long", 1000.0, 5.0, stop=95000.0, takes=[(103000.0, 100)])
    first = from_fp9(a.fake.positions[0]["size"])
    n = len(writes(a.fake))
    r = a.open_market("BTC/USDT:USDT", "long", 500.0, 5.0, stop=95000.0, takes=[(103000.0, 100)])
    assert len(a.fake.positions) == 1 and abs(r["amount"] - (from_fp9(a.fake.positions[0]["size"]) - first)) < 1e-9 and r["amount"] < first                  # добор увеличил ту же позицию
    stops = [w["body"] for w in writes(a.fake)[n + 1:] if w["body"]["type"] == "stop"]
    assert stops and from_fp9(stops[0]["amount"]) == pytest.approx(r["amount"], abs=1e-8)
    b = a.open_market("BTC/USDT:USDT", "short", 400.0, 4.0)
    assert len(a.fake.positions) == 2 and {p["direction"] for p in a.fake.positions} == {"long", "short"} and b["position"] != r["position"]


def test_open_market_errors_are_clear_and_nothing_stays_unprotected():
    a = adapter(max_usd=500)
    with pytest.raises(ExchangeError, match="TRADE_MAX_USD"):
        a.open_market("BTC/USDT:USDT", "long", 1000.0, 5.0)
    assert not writes(a.fake)
    a = adapter()
    with pytest.raises(ExchangeError, match="плечо вне допустимых границ.*до 50"):
        a.open_market("BTC/USDT:USDT", "long", 1000.0, 100.0)
    with pytest.raises(ExchangeError, match="нет рынка DOGE"):
        a.open_market("DOGE/USDT:USDT", "long", 100.0, 2.0)
    a.fake.markets[0]["settings"]["isPaused"] = True
    with pytest.raises(ExchangeError, match="на паузе"):
        a.open_market("BTC/USDT:USDT", "long", 100.0, 2.0)
    a.fake.markets[0]["settings"]["isPaused"] = False
    a.fake.exec_error = "order_below_min_notional"                                                                                          # движок отклонил ордер при исполнении
    with pytest.raises(ExchangeError, match="объём меньше минимального ордера"):
        a.open_market("BTC/USDT:USDT", "long", 5.0, 5.0)
    assert not a.fake.positions
    # стоп не встал после входа -> позиция закрывается по рынку, ошибка говорит об этом
    a = adapter()
    a.fake.close_orders_fail = True
    with pytest.raises(ExchangeError, match="стоп/тейки не поставлены.*закрыта по рынку"):
        a.open_market("BTC/USDT:USDT", "long", 1000.0, 5.0, stop=95000.0)
    assert a.fake.positions[0]["status"] == "closed"


def test_stop_prices_set_stop_clear_stops_and_protect():
    a = adapter()
    a.open_market("BTC/USDT:USDT", "long", 1000.0, 5.0, stop=95000.0, takes=[(103000.0, 100)])
    assert a.stop_prices("BTC/USDT:USDT", "long") == [95000.0] and a.stop_prices("BTC/USDT:USDT", "short") == []
    r = a.set_stop("BTC/USDT:USDT", "long", 100000.0)
    assert r["status"] == "sent" and r["removed_old"] == 1 and a.stop_prices("BTC/USDT:USDT", "long") == [100000.0]
    new = [o for o in a.fake.orders if o["type"] == "stop" and o["status"] == "active"][0]
    assert new["amount"] == a.fake.positions[0]["size"]                                                                                       # стоп на весь размер позиции
    assert any(o["type"] == "take" and o["status"] == "active" for o in a.fake.orders)                                                       # тейк не тронут
    assert a.clear_stops("BTC/USDT:USDT", "long") == 1 and a.stop_prices("BTC/USDT:USDT", "long") == []
    assert a.set_stop("ETH/USDT:USDT", "long", 1.0)["status"] == "skipped"
    p = a.protect("BTC/USDT:USDT", "long", from_fp9(a.fake.positions[0]["size"]), stop=96000.0, takes=[(104000.0, 100)])
    assert p["status"] == "sent" and [o["kind"] for o in p["orders"]] == ["stop", "take"] and a.stop_prices("BTC/USDT:USDT", "long") == [96000.0]
    assert a.protect("ETH/USDT:USDT", "long", 1.0, stop=1.0)["status"] == "skipped"


def test_close_market_partial_full_and_missing_position():
    a = adapter()
    a.open_market("BTC/USDT:USDT", "long", 1000.0, 5.0, stop=95000.0, takes=[(103000.0, 100)])
    size = from_fp9(a.fake.positions[0]["size"])
    r = a.close_market("BTC/USDT:USDT", "long", 0.5)
    close = writes(a.fake)[-1]["body"]
    assert r["status"] == "sent" and close["type"] == "take" and close["triggerPrice"] == "0" and close["positionId"] == a.fake.positions[0]["idx"] and from_fp9(close["amount"]) == pytest.approx(size / 2, abs=1e-8)
    assert a.fake.positions[0]["status"] == "opened" and from_fp9(a.fake.positions[0]["size"]) == pytest.approx(size / 2, abs=1e-8)
    assert a.close_market("BTC/USDT:USDT", "short", 1.0)["status"] == "skipped" and a.close_market("ETH/USDT:USDT", "long")["status"] == "skipped"
    r = a.close_market("BTC/USDT:USDT", "long", 1.0)
    assert r["status"] == "sent" and a.fake.positions[0]["status"] == "closed" and all(o["status"] != "active" for o in a.fake.orders)                # висящие стоп и тейк закрылись вместе с позицией


def test_limit_entry_state_and_cancel():
    a = adapter()
    r = a.place_limit_entry("BTC/USDT:USDT", "long", 1000.0, 99000.0, 5.0)
    body = writes(a.fake)[0]["body"]
    assert r["status"] == "sent" and body["type"] == "limit" and body["triggerPrice"] == fp(99000.0) and body["amount"] == fp(200.0) and body["leverage"] == fp(5.0)
    assert a.order_state(r["order_id"], "BTC/USDT:USDT")["status"] == "open"
    assert a.cancel_order_id(r["order_id"], "BTC/USDT:USDT")["status"] == "sent"
    assert a.order_state(r["order_id"], "BTC/USDT:USDT")["status"] == "unknown"                                                              # снят или исполнен — вызывающий смотрит позицию
    with pytest.raises(ExchangeError, match="уже не активен"):
        a.cancel_order_id(r["order_id"], "BTC/USDT:USDT")


def test_positions_account_orders_fills_and_close_all():
    a = adapter()
    a.open_market("BTC/USDT:USDT", "long", 1000.0, 5.0, stop=95000.0, takes=[(103000.0, 100)])
    a.place_limit_entry("ETH/USDT:USDT", "short", 300.0, 3100.0, 3.0)
    a.fake.markets[0]["state"]["indexPrice"] = fp(101000.0)
    [p] = a.positions()
    assert p["symbol"] == "BTC/USD" and p["side"] == "long" and p["leverage"] == pytest.approx(5.0, abs=0.05) and p["mark"] == 101000.0 and p["liq"] is None and p["margin"] == 200.0
    assert p["pnl_usd"] == pytest.approx((101000.0 - p["entry"]) * p["size"]) and p["pnl_usd"] > 0
    acc = a.account()
    assert acc["equity"] == 1000.0 and acc["free"] is None and acc["used"] == 200.0 and acc["type"] == "demo"                              # свободный баланс не выдумываем
    kinds = {(o["type"], o["side"]) for o in a.open_orders()}
    assert kinds == {("stop", "sell"), ("take", "sell"), ("limit", "sell")} and next(o for o in a.open_orders() if o["type"] == "stop")["trigger"] == 95000.0
    f = a.fills(5)
    assert f and f[0]["symbol"] == "BTC/USD" and f[0]["amount"] > 0 and f[0]["price"] == pytest.approx(100000.0)
    r = a.close_all()
    assert r["closed"] == [{"symbol": "BTC/USD", "side": "long", "size": p["size"], "status": "sent"}] and r["cancelled"] == 3 and r["errors"] == []
    assert a.positions() == [] and writes(a.fake)[-1]["path"] == f"/accounts/{ACC}/close-all"


def test_transport_errors_are_retried_with_the_same_idempotency_key_and_401_is_explained(monkeypatch):
    monkeypatch.setattr("app.upscale.time.sleep", lambda s: None)
    fake = FakeUpscale()
    state = {"n": 0}

    def flaky(method, url, headers, params, body, timeout):
        if method == "POST" and state["n"] < 1:
            state["n"] += 1
            raise TimeoutError("таймаут")
        return fake(method, url, headers, params, body, timeout)
    a = UpscaleAdapter(KEY, ACC, "demo", transport=flaky, max_usd=100000)
    a.place_limit_entry("BTC/USDT:USDT", "long", 1000.0, 99000.0, 5.0)
    posts = [c for c in fake.calls if c["method"] == "POST"]
    assert len(posts) == 1 and len(fake.orders) == 1                                                                                           # повтор дошёл, дубля ордера нет
    seen = []

    def rate_limited(method, url, headers, params, body, timeout):
        if method == "POST":
            seen.append(headers.get("x-idempotency-key"))
            if len(seen) == 1:
                return 429, {"statusCode": 429, "message": "x", "error": "api_key_rate_limit_exceeded"}, {"Retry-After": "1"}
        return fake(method, url, headers, params, body, timeout)
    b = UpscaleAdapter(KEY, ACC, "demo", transport=rate_limited, max_usd=100000)
    b.place_limit_entry("BTC/USDT:USDT", "long", 1000.0, 98000.0, 5.0)
    assert seen[0] and seen[0] == seen[1]
    c = UpscaleAdapter("usk_неверный_ключ_00000", ACC, "demo", transport=fake)
    with pytest.raises(ExchangeError, match="недействителен.*/upscale"):
        c.positions()
    always500 = UpscaleAdapter(KEY, ACC, "demo", transport=lambda *a: (503, {"error": "x"}, {}))
    with pytest.raises(ExchangeError, match="HTTP 503"):
        always500.positions()
    dead = UpscaleAdapter(KEY, ACC, "demo", transport=lambda *a: (_ for _ in ()).throw(ConnectionError("сеть")))
    with pytest.raises(ExchangeError, match="нет ответа от биржи"):
        dead.positions()
    with pytest.raises(NotConfigured, match="/upscale"):
        UpscaleAdapter("", "").positions()


def test_trading_log_hides_the_upscale_key_even_inside_error_text(tmp_path, monkeypatch):
    from app import tradelog
    monkeypatch.setenv("TRADE_LOG_FILE", str(tmp_path / "t.log"))
    tradelog._logger = None
    tradelog.trade_log("auto_fail", user=1, error=f"Upscale: запрос с ключом {KEY} отклонён", api_key=KEY, exchange="upscale")
    text = (tmp_path / "t.log").read_text(encoding="utf-8")
    assert KEY not in text and "a1B2c3D4" not in text and "usk_***" in text and '"api_key": "***"' in text
    tradelog._logger = None


def test_adapter_for_builds_upscale_from_the_saved_account_and_test_size_uses_its_minimum(auth_env, monkeypatch):
    from app.secrets_store import encrypt
    db = auth_env
    uid = db.check_login("alice", "alice-password")["id"]
    assert isinstance(adapter_for(db, uid, "upscale"), DryRunAdapter)
    db.set_exchange_account(uid, "upscale", ACC, encrypt(KEY, db.path), False, None, net="real")
    a = adapter_for(db, uid, "upscale")
    assert isinstance(a, UpscaleAdapter) and a.api_key == KEY and a.account_id == ACC and a.acct_type == "real" and a.testnet is False and a.user_id == uid
    assert a.describe()["name"] == "upscale" and a.describe()["label"] == "Upscale" and a.describe()["net"] == "real" and adapter_for(db, uid, "upscale") is a
    db.set_exchange_account(uid, "upscale", ACC, encrypt(KEY, db.path), True, None, net="demo")
    assert adapter_for(db, uid, "upscale").testnet is True and adapter_for(db, uid, "upscale") is not a
    monkeypatch.delenv("UPSCALE_MIN_NOTIONAL", raising=False)
    margin, lev = manage.test_size(adapter(), 1.0)
    assert (margin, lev) == (1.0, 11.0)                                                                                                        # минимум 10 $ с запасом 10% -> плечо 11×
    monkeypatch.setenv("UPSCALE_MIN_NOTIONAL", "100")
    assert manage.test_size(adapter(), 1.0) == (4.4, 25.0)
    r = manage.test_open(db, adapter(), uid, "long", 1.0)
    assert r["status"] == "sent" and r["exchange"]["name"] == "upscale" and r["leverage"] == 25 and r["notional"] == pytest.approx(110.0)
