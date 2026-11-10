"""Ручное закрытие любой позиции на бирже по рынку (целиком или долей): через manage.close_position и API /api/trading/close-position."""
import pytest

from app import manage
from app.config import load_settings
from app.main import create_app
from app.trading import DryRunAdapter, ExchangeError, HyperliquidAdapter
from test_auth import H_, auth_env, client, login  # noqa: F401
from test_hl_accounts import W1, _connect


class Real(DryRunAdapter):
    """Имитация реального счёта: список позиций и запись закрытий."""
    real = True
    name = "hyperliquid"

    def __init__(self, positions, fail=None, result=None):
        self._pos, self.fail, self.result, self.calls = positions, fail, result, []

    def positions(self):
        return list(self._pos)

    def close_market(self, symbol, side, share=1.0):
        self.calls.append((symbol, side, share))
        if self.fail:
            raise self.fail
        return self.result or {"status": "sent", "symbol": symbol, "closed": 1.5 * share}


POS = [{"symbol": "BTC/USDC:USDC", "side": "long", "size": 0.5, "entry": 100.0, "pnl_usd": 12.0},
       {"symbol": "kPEPE/USDC:USDC", "side": "short", "size": 1000.0, "entry": 0.01, "pnl_usd": -3.0}]


def uid_of(db):
    return db.check_login("alice", "alice-password")["id"]


def test_closes_the_whole_position_by_coin_ignoring_case_and_logs_it(auth_env):
    db = auth_env
    ad = Real(POS)
    r = manage.close_position(db, ad, uid_of(db), "btc", "long")
    assert ad.calls == [("BTC/USDC:USDC", "long", 1.0)] and r["status"] == "sent" and r["symbol"] == "BTC/USDC:USDC" and r["share"] == 1.0
    log = [x for x in db.list_auto_log(uid_of(db), 10) if x["kind"] == "manual_close"]
    assert len(log) == 1 and log[0]["side"] == "long" and log[0]["status"] == "sent" and log[0]["details"]["share"] == 1.0 and log[0]["details"]["size"] == 0.5
    manage.close_position(db, ad, uid_of(db), "KPEPE/USDT:USDT", "short", 0.25)                           # любой контракт; биржевой символ берётся из списка позиций, а не из запроса
    assert ad.calls[-1] == ("kPEPE/USDC:USDC", "short", 0.25)


def test_validation_and_missing_positions(auth_env):
    db, uid = auth_env, uid_of(auth_env)
    ad = Real(POS)
    for bad, code in (({"side": "up"}, 422), ({"share": 0}, 422), ({"share": 1.5}, 422), ({"share": "много"}, 422), ({"symbol": ""}, 422), ({"symbol": "ETH"}, 404), ({"side": "short"}, 404)):
        kw = {"symbol": "BTC", "side": "long", "share": 1.0} | bad
        with pytest.raises(manage.ManageError) as e:
            manage.close_position(db, ad, uid, kw["symbol"], kw["side"], kw["share"])
        assert e.value.code == code, bad
    assert ad.calls == []                                                                                # ни одного ордера по неверным запросам
    with pytest.raises(manage.ManageError) as e:
        manage.close_position(db, DryRunAdapter(), uid, "BTC", "long")
    assert e.value.code == 409 and "не подключён" in str(e.value)
    with pytest.raises(manage.ManageError):
        manage.close_position(db, ad, uid, "BTC", "long", exchange="kraken")


def test_exchange_errors_are_reported_and_logged_and_skipped_results_pass_through(auth_env):
    db, uid = auth_env, uid_of(auth_env)
    with pytest.raises(manage.ManageError) as e:
        manage.close_position(db, Real(POS, fail=ExchangeError("биржа не отвечает")), uid, "BTC", "long")
    assert e.value.code == 502 and "биржа не отвечает" in str(e.value)
    assert [(x["kind"], x["status"]) for x in db.list_auto_log(uid, 5)][0] == ("manual_close", "error")
    r = manage.close_position(db, Real(POS, result={"status": "skipped", "note": "закрываемая часть меньше минимального количества"}), uid, "BTC", "long", 0.01)
    assert r["status"] == "skipped" and "минимального" in r["result"]["note"]


def test_api_closes_positions_on_any_account_and_needs_confirmation(auth_env, monkeypatch):
    manage._acc.clear()
    db = auth_env
    alice = db.check_login("alice", "alice-password")
    _connect(db, alice["id"], "hyperliquid", W1)
    _connect(db, alice["id"], "hyperliquid2", "0x" + "cd" * 20)
    closed = []
    monkeypatch.setattr(HyperliquidAdapter, "positions", lambda self: [dict(p) for p in POS] if self.name == "hyperliquid2" else [])
    monkeypatch.setattr(HyperliquidAdapter, "close_market", lambda self, symbol, side, share=1.0: closed.append((self.name, symbol, side, share)) or {"status": "sent", "symbol": symbol, "closed": 0.5})
    app = create_app(load_settings())
    a = client(app)
    login(a, "alice", "alice-password")
    body = {"exchange": "hyperliquid2", "symbol": "BTC/USDC:USDC", "side": "long", "share": 0.5}
    assert a.post("/api/trading/close-position", json=body, headers=H_).status_code == 422             # без подтверждения
    r = a.post("/api/trading/close-position", json=body | {"confirm": True}, headers=H_)
    assert r.status_code == 200 and r.json()["status"] == "sent" and r.json()["exchange"]["name"] == "hyperliquid2" and closed == [("hyperliquid2", "BTC/USDC:USDC", "long", 0.5)]
    assert a.post("/api/trading/close-position", json=body | {"confirm": True, "exchange": "hyperliquid"}, headers=H_).status_code == 404       # на первом аккаунте позиции нет
    assert a.post("/api/trading/close-position", json=body | {"confirm": True, "exchange": "bybit"}, headers=H_).status_code == 409             # Bybit не подключён
    assert a.post("/api/trading/close-position", json=body | {"confirm": True, "exchange": "kraken"}, headers=H_).status_code == 422
    assert a.post("/api/trading/close-position", json=body | {"confirm": True, "share": 2}, headers=H_).status_code == 422
    assert len(closed) == 1
    bob = client(app)
    login(bob, "bob", "bob-password-1")
    assert bob.post("/api/trading/close-position", json=body | {"confirm": True}, headers=H_).status_code == 409                                  # чужой счёт закрыть нельзя: у Боба своего нет
    assert len(closed) == 1
