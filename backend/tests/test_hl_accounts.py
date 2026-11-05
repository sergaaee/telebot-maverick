"""Несколько аккаунтов Hyperliquid у одного пользователя: «слоты» hyperliquid (первый, прежний), hyperliquid2, hyperliquid3 … Для системы каждый слот — отдельная биржа:
свой счёт, свои «открывать / управлять / база / размер», свои ожидающие входы. Bybit и Upscale по-прежнему по одному счёту."""
import pytest

from app import db as db_mod, manage
from app.config import load_settings
from app.db import EXCHANGES, HL_SLOTS, hl_name, hl_slot, is_hl
from app.main import create_app
from app.trading import EXCHANGE_LABELS, DryRunAdapter, HyperliquidAdapter, adapter_for, adapters_for, forget_adapter
from test_auth import H_, auth_env, client, login  # noqa: F401
from test_bybit import Spy, _cfg

W1, W2 = "0x" + "ab" * 20, "0x" + "cd" * 20


def _connect(db, uid, slot, wallet, key="0x" + "11" * 32, testnet=True):
    from app.secrets_store import encrypt
    db.set_exchange_account(uid, slot, wallet, encrypt(key, db.path), testnet, "0x" + "ee" * 20)


def test_slots_are_exchanges_with_stable_names_and_labels():
    assert HL_SLOTS[0] == "hyperliquid" and HL_SLOTS == tuple(hl_name(i) for i in range(1, len(HL_SLOTS) + 1)) and len(HL_SLOTS) == 3
    assert EXCHANGES == HL_SLOTS + ("bybit", "upscale") and all(is_hl(n) for n in HL_SLOTS) and not is_hl("bybit")
    assert [hl_slot(n) for n in HL_SLOTS] == [1, 2, 3] and hl_name(1) == "hyperliquid" and hl_name("3") == "hyperliquid3"
    assert EXCHANGE_LABELS["hyperliquid"] == "Hyperliquid" and EXCHANGE_LABELS["hyperliquid2"] == "Hyperliquid 2" and set(EXCHANGE_LABELS) == set(EXCHANGES)


def test_number_of_slots_is_an_env_setting_clamped_to_1_10(monkeypatch):
    for raw, want in (("1", 1), ("5", 5), ("0", 1), ("99", 10), ("много", 3), ("", 3)):
        monkeypatch.setenv("HL_MAX_ACCOUNTS", raw)
        assert db_mod._hl_slots_n() == want, raw
    monkeypatch.delenv("HL_MAX_ACCOUNTS")
    assert db_mod._hl_slots_n() == 3


def test_adapter_per_slot_has_its_own_name_label_and_cache(auth_env):
    db = auth_env
    uid = db.check_login("alice", "alice-password")["id"]
    _connect(db, uid, "hyperliquid", W1)
    _connect(db, uid, "hyperliquid2", W2, key="0x" + "22" * 32)
    a1, a2 = adapter_for(db, uid), adapter_for(db, uid, "hyperliquid2")
    assert type(a1) is HyperliquidAdapter and type(a2) is HyperliquidAdapter and a1 is not a2
    assert (a1.name, a1.label, a1.wallet, a1.key) == ("hyperliquid", "Hyperliquid", W1, "0x" + "11" * 32)
    assert (a2.name, a2.label, a2.wallet, a2.key) == ("hyperliquid2", "Hyperliquid 2", W2, "0x" + "22" * 32) and a2.user_id == uid
    assert HyperliquidAdapter.name == "hyperliquid" and a2.describe()["name"] == "hyperliquid2"                    # имя слота — у экземпляра, класс не тронут
    assert adapter_for(db, uid, "hyperliquid2") is a2 and adapter_for(db, uid) is a1
    forget_adapter(uid, "hyperliquid2")
    assert adapter_for(db, uid) is a1 and adapter_for(db, uid, "hyperliquid2") is not a2                            # забыли только второй
    ads = adapters_for(db, uid)
    assert set(ads) == set(EXCHANGES) and ads["hyperliquid"].real and ads["hyperliquid2"].real and not ads["hyperliquid3"].real and isinstance(ads["hyperliquid3"], DryRunAdapter)
    bob = db.check_login("bob", "bob-password-1")["id"]
    assert isinstance(adapter_for(db, bob, "hyperliquid2"), DryRunAdapter)                                          # у другого пользователя счёта нет


def test_not_connected_hint_names_the_slot_command():
    assert "/hl (" in HyperliquidAdapter("", "")._not_connected() and "Hyperliquid не подключён" in HyperliquidAdapter("", "")._not_connected()
    msg = HyperliquidAdapter("", "", slot="hyperliquid3")._not_connected()
    assert "Hyperliquid 3 не подключён" in msg and "/hl 3" in msg


def test_automode_settings_are_independent_per_slot(auth_env):
    db = auth_env
    uid, cid = _cfg(db)
    ad = DryRunAdapter()
    manage.set_auto(db, ad, uid, cid, open_=True, manage=True, exchange="hyperliquid2")
    manage.set_auto(db, ad, uid, cid, base="account", exchange="hyperliquid2")
    c = db.get_config(cid)
    assert c["ex"]["hyperliquid2"] == {"open": True, "manage": True, "base": "account", "over": {}} and c["ex"]["hyperliquid"]["open"] is False and c["auto"]["open"] is False      # колонка auto — только у первого
    manage.set_auto(db, ad, uid, cid, open_=True, exchange="hyperliquid")
    assert db.get_config(cid)["auto"]["open"] is True and db.get_config(cid)["ex"]["hyperliquid3"]["open"] is False
    assert manage.set_ex_over(db, ad, uid, cid, "hyperliquid2", trade_pct=5, leverage=2) == {"trade_pct": 5, "leverage": 2}
    c = db.get_config(cid)
    assert c["ex"]["hyperliquid2"]["over"] == {"trade_pct": 5, "leverage": 2} and c["ex"]["hyperliquid"]["over"] == {} and c["params"]["trade_pct"] == 20
    assert manage.exchange_name("HyperLiquid2") == "hyperliquid2"
    with pytest.raises(manage.ManageError):
        manage.exchange_name("hyperliquid9")


def test_disable_all_auto_and_pause_touch_only_the_asked_slot(auth_env):
    db = auth_env
    uid, cid = _cfg(db)
    ad = DryRunAdapter()
    for n in ("hyperliquid", "hyperliquid2", "bybit"):
        manage.set_auto(db, ad, uid, cid, open_=True, manage=True, exchange=n)
    assert db.disable_all_auto(uid, "hyperliquid2") == 1
    c = db.get_config(cid)
    assert not c["ex"]["hyperliquid2"]["open"] and not c["ex"]["hyperliquid2"]["manage"] and c["ex"]["hyperliquid"]["open"] and c["ex"]["bybit"]["manage"]
    assert db.disable_all_auto(uid, "hyperliquid") == 1 and db.get_config(cid)["ex"]["bybit"]["open"] and not db.get_config(cid)["auto"]["open"]
    manage.set_auto(db, ad, uid, cid, open_=True, exchange="hyperliquid2")
    manage.set_paused(db, uid, cid, True)                                                      # пауза выключает автомод на всех слотах
    assert not db.get_config(cid)["any_open"]
    manage.set_auto(db, ad, uid, cid, open_=True, exchange="hyperliquid3")
    assert manage.auto_off_all(db, uid) == 1 and not db.get_config(cid)["any_open"]


def test_pending_and_retry_are_per_slot(auth_env):
    db = auth_env
    uid, cid = _cfg(db)
    for ex in ("hyperliquid", "hyperliquid2"):
        db.set_pending(cid, uid, 1, "BTC/USDT:USDT", "long", "pullback", 99.0, 100.0, 400.0, 200.0, 2.0, f"o-{ex}", 9e9, exchange=ex)
        db.set_retry(cid, 1, "long", reason=ex, exchange=ex)
    assert db.get_pending(cid)["order_id"] == "o-hyperliquid" and db.get_pending(cid, "hyperliquid2")["order_id"] == "o-hyperliquid2" and len(db.pending_of(cid)) == 2
    db.set_exchange_account(uid, "hyperliquid2", W2, "e", True, None)
    db.set_exchange_account(uid, "hyperliquid", W1, "e", True, None)
    manage.set_auto(db, DryRunAdapter(), uid, cid, open_=True, exchange="hyperliquid2")
    manage.set_auto(db, DryRunAdapter(), uid, cid, open_=True, exchange="hyperliquid")
    assert manage.disconnect_account(db, uid, "hyperliquid2") is True                          # второй аккаунт отключён: его автомод, лимитка и повтор сняты, первый как был
    c = db.get_config(cid)
    assert db.get_exchange_account(uid, "hyperliquid2") is None and db.get_exchange_account(uid)["wallet"] == W1
    assert not c["ex"]["hyperliquid2"]["open"] and c["ex"]["hyperliquid"]["open"]
    assert [r["exchange"] for r in db.pending_of(cid)] == ["hyperliquid"] and [r["exchange"] for r in db.list_retries()] == ["hyperliquid"]


def test_panic_and_remove_config_cover_every_hl_account(auth_env):
    db = auth_env
    uid, cid = _cfg(db)
    for n in ("hyperliquid", "hyperliquid2"):
        manage.set_auto(db, DryRunAdapter(), uid, cid, open_=True, exchange=n)
    h1, h2 = Spy("hyperliquid"), Spy("hyperliquid2")
    out = manage.panic(db, {"hyperliquid": h1, "hyperliquid2": h2}, uid)
    assert h1.closed_all == 1 and h2.closed_all == 1 and {c["exchange"] for c in out["closed"]} == {"hyperliquid", "hyperliquid2"} and not db.get_config(cid)["any_open"]
    out = manage.panic(db, {"hyperliquid": h1, "hyperliquid2": Spy("hyperliquid2", fail=True)}, uid)                       # у второго сбой — первый всё равно закрыт, ошибка подписана вторым аккаунтом
    assert h1.closed_all == 2 and len(out["closed"]) == 1 and any(e.startswith("Hyperliquid 2:") for e in out["errors"])
    db.set_pending(cid, uid, 1, "BTC/USDT:USDT", "long", "pullback", 99.0, 100.0, 400.0, 200.0, 2.0, "A1", 9e9)
    db.set_pending(cid, uid, 1, "BTC/USDT:USDT", "long", "pullback", 99.0, 100.0, 400.0, 200.0, 2.0, "A2", 9e9, exchange="hyperliquid2")
    with pytest.raises(manage.ManageError, match="Hyperliquid 2"):
        manage.remove_config(db, uid, cid, {"hyperliquid": h1, "hyperliquid2": Spy("hyperliquid2", fail=True)})
    assert db.get_config(cid)
    assert manage.remove_config(db, uid, cid, {"hyperliquid": h1, "hyperliquid2": h2}) == "Конфиг" and "A1" in h1.cancelled and "A2" in h2.cancelled


def test_api_second_account_is_a_separate_exchange(auth_env, monkeypatch):
    manage._acc.clear()
    db = auth_env
    alice = db.check_login("alice", "alice-password")
    cid = db.add_config(alice["id"], "Для API", {"symbol": "BTC/USDT:USDT", "side": "long", "confirm": 2, "tps": [{"pct": 50, "share": 100}], "sl_pct": 50, "deposit": 10000, "trade_pct": 10, "leverage": 5})[0]["id"]
    _connect(db, alice["id"], "hyperliquid", W1)
    _connect(db, alice["id"], "hyperliquid2", W2)
    monkeypatch.setattr(HyperliquidAdapter, "account", lambda self: {"equity": 100.0 if self.name == "hyperliquid" else 250.0, "free": 50.0, "used": 0.0})
    monkeypatch.setattr(HyperliquidAdapter, "positions", lambda self: [])
    monkeypatch.setattr(HyperliquidAdapter, "open_orders", lambda self: [])
    monkeypatch.setattr(HyperliquidAdapter, "fills", lambda self, limit=20: [])
    closed = []
    monkeypatch.setattr(HyperliquidAdapter, "close_all", lambda self: closed.append(self.name) or {"closed": [], "cancelled": 0, "errors": []})
    app = create_app(load_settings())
    a = client(app)
    login(a, "alice", "alice-password")
    m = a.post("/api/monitor", json={}, headers=H_).json()
    assert m["exchanges"]["hyperliquid2"]["real"] and m["exchanges"]["hyperliquid2"]["name"] == "hyperliquid2" and not m["exchanges"]["hyperliquid3"]["real"]
    assert m["accounts"]["hyperliquid"]["equity"] == 100.0 and m["accounts"]["hyperliquid2"]["equity"] == 250.0 and set(m["entries"][0]["ex"]) == set(EXCHANGES)
    one, two = a.get("/api/exchange/account").json(), a.get("/api/exchange/account?exchange=hyperliquid2").json()
    assert one["wallet"] != two["wallet"] and two["connected"] and two["exchange"]["name"] == "hyperliquid2" and two["account"]["equity"] == 250.0 and one["account"]["equity"] == 100.0
    assert a.get("/api/exchange/account?exchange=hyperliquid9").status_code == 422 and a.get("/api/exchange/account?exchange=hyperliquid3").json()["connected"] is False
    allacc = a.get("/api/exchange/accounts").json()["accounts"]
    assert allacc["hyperliquid2"]["connected"] and set(allacc) == set(EXCHANGES)
    r = a.put(f"/api/monitor/configs/{cid}/auto", json={"open": True, "manage": True, "exchange": "hyperliquid2"}, headers=H_)
    assert r.status_code == 200 and r.json()["target"] == "hyperliquid2" and db.get_config(cid)["ex"]["hyperliquid2"]["open"] and not db.get_config(cid)["auto"]["open"]
    r = a.put(f"/api/monitor/configs/{cid}/exchange/hyperliquid2", json={"trade_pct": 4, "leverage": 2}, headers=H_)
    assert r.status_code == 200 and r.json()["over"] == {"trade_pct": 4, "leverage": 2} and db.get_config(cid)["ex"]["hyperliquid"]["over"] == {}
    pf = a.post("/api/portfolio", headers=H_).json()
    assert pf["exposure"]["real"]["equity"] == 350.0                                                   # реальные счета обоих аккаунтов складываются
    assert a.post("/api/panic", json={"confirm": True}, headers=H_).status_code == 200 and sorted(closed) == ["hyperliquid", "hyperliquid2"]
    assert a.delete("/api/exchange/account?exchange=hyperliquid2", headers=H_).json() == {"removed": True}
    assert db.get_exchange_account(alice["id"], "hyperliquid2") is None and db.get_exchange_account(alice["id"])
    assert a.get("/api/exchange/account?exchange=hyperliquid2").json()["connected"] is False and a.get("/api/exchange/account").json()["connected"] is True
