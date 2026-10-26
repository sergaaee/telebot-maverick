"""API: портфель, выгрузки, сводка, аварийная кнопка, walk-forward."""
from app.config import load_settings
from app.main import create_app
from test_auth import H_, _open_cfg, auth_env, client, login  # noqa: F401


def two_users(db):
    alice = db.check_login("alice", "alice-password")
    db.add_config(alice["id"], "Открытая", _open_cfg())
    db.add_config(alice["id"], "Сломанная", {"symbol": "BTC/USDT:USDT", "source": "нет-такого"})
    app = create_app(load_settings())
    a, b = client(app), client(app)
    assert login(a, "alice", "alice-password").status_code == 200 and login(b, "bob", "bob-password-1").status_code == 200
    return alice, a, b


def test_portfolio_exports_digest_panic(auth_env):
    db = auth_env
    alice, a, b = two_users(db)
    r = a.post("/api/portfolio", headers=H_).json()
    assert [i["name"] for i in r["items"]] == ["Открытая", "Сломанная"] and r["items"][1]["error"] and r["portfolio"]["strategies"] == 1
    assert r["exposure"]["symbols"][0]["asset"] == "BTC" and r["exposure"]["gross"] > 0
    assert b.post("/api/portfolio", headers=H_).json()["items"] == []                       # чужих конфигов нет
    # CSV
    t = a.get("/api/export/trades.csv")
    assert t.status_code == 200 and t.headers["content-type"].startswith("text/csv") and "attachment" in t.headers["content-disposition"] and t.text.lstrip("﻿").startswith("strategy,symbol")
    assert a.get("/api/export/trades.csv?config_id=1").status_code == 200 and a.get("/api/export/trades.csv?config_id=1").text.count("\n") >= 2
    assert b.get("/api/export/trades.csv?config_id=1").status_code == 404                  # чужой конфиг
    assert a.get("/api/export/autolog.csv").status_code == 200
    assert a.get("/api/export/fills.csv").status_code == 409                                # счёт не подключён
    assert a.get("/api/export/nothing.csv").status_code == 404
    # сводка
    assert a.get("/api/digest").json() == {"mode": "off", "telegram": False, "hour_utc": 8}
    assert a.put("/api/digest", json={"mode": "daily"}, headers=H_).status_code == 409     # без Telegram слать некуда
    db.link_telegram("alice", 4242)
    assert a.put("/api/digest", json={"mode": "daily"}, headers=H_).json() == {"mode": "daily"} and a.get("/api/digest").json()["mode"] == "daily"
    assert a.put("/api/digest", json={"mode": "hourly"}, headers=H_).status_code == 422
    # аварийная кнопка
    a.put("/api/monitor/configs/1/auto", json={"open": True}, headers=H_)
    assert a.post("/api/panic", json={}, headers=H_).status_code == 422                    # нужно подтверждение
    p = a.post("/api/panic", json={"confirm": True}, headers=H_).json()
    assert p["auto_off"] == 1 and p["closed"] == [] and p["errors"] == []
    assert db.get_config(1)["auto"] == {"open": False, "manage": False, "base": "config"}
    m = a.post("/api/monitor", json={}, headers=H_).json()
    assert m["bot"]["state"] == "unknown"                                                  # бот ещё не запускался


def test_portfolio_prefs_exclude_and_common_deposit(auth_env):
    db = auth_env
    alice, a, b = two_users(db)
    assert a.get("/api/portfolio/prefs").json() == {"exclude": [], "total_deposit": None, "sizing": {}, "compound": None}
    assert a.put("/api/portfolio/prefs", json={"total_deposit": -5}, headers=H_).status_code == 422
    assert a.put("/api/portfolio/prefs", json={"total_deposit": "много"}, headers=H_).status_code == 422
    assert a.put("/api/portfolio/prefs", json={"exclude": "1"}, headers=H_).status_code == 422
    assert a.put("/api/portfolio/prefs", json={"total_deposit": 1000, "exclude": [2]}, headers=H_).json() == {"exclude": [2], "total_deposit": 1000.0, "sizing": {}, "compound": None}
    assert b.get("/api/portfolio/prefs").json() == {"exclude": [], "total_deposit": None, "sizing": {}, "compound": None}                  # настройки у каждого пользователя свои
    r = a.post("/api/portfolio", headers=H_).json()
    assert r["settings"] == {"exclude": [2], "total_deposit": 1000.0, "sizing": {}, "compound": None} and r["portfolio"]["joint"] and r["portfolio"]["deposit"] == 1000.0
    assert [i["included"] for i in r["items"]] == [True, False]
    assert a.put("/api/portfolio/prefs", json={"total_deposit": None}, headers=H_).json()["total_deposit"] is None
    r2 = a.post("/api/portfolio", headers=H_).json()
    assert not r2["portfolio"]["joint"] and r2["portfolio"]["deposit"] == 10000.0 and r2["settings"]["exclude"] == [2]
    # размер входа автомода: база счёта сохраняется отдельно от включения
    assert a.put("/api/monitor/configs/1/auto", json={"base": "account"}, headers=H_).json()["auto"] == {"open": False, "manage": False, "base": "account"}
    assert a.put("/api/monitor/configs/1/auto", json={"base": "bogus"}, headers=H_).status_code == 422
    assert a.post("/api/panic", json={"confirm": True}, headers=H_).status_code == 200
    assert db.get_config(1)["auto"]["base"] == "account"                                                    # СТОП ВСЁ выключает автомод, но не сбрасывает выбор базы


def test_config_sizing_can_be_changed_per_config(auth_env):
    db = auth_env
    alice, a, b = two_users(db)
    r = a.put("/api/monitor/configs/1/sizing", json={"trade_pct": 25, "leverage": 3}, headers=H_)
    assert r.status_code == 200 and r.json()["sizing"]["trade_pct"] == 25 and r.json()["sizing"]["leverage"] == 3
    assert db.get_config(1)["params"]["trade_pct"] == 25 and db.get_config(1)["params"]["leverage"] == 3 and db.get_config(1)["params"]["sl_pct"] == 50        # остальные параметры целы
    e = a.post("/api/monitor", json={}, headers=H_).json()["entries"][0]
    assert e["position"]["notional"] == 10000 * 0.25 * 3 and e["settings"]["trade_pct"] == 25 and e["settings"]["leverage"] == 3                              # расчёты сразу идут с новым размером
    assert any(x["kind"] == "sizing" for x in db.list_auto_log(alice["id"], 5))
    for bad in ({"trade_pct": 0}, {"trade_pct": 101}, {"leverage": 0.5}, {"leverage": 200}, {"leverage": "много"}, {}):
        assert a.put("/api/monitor/configs/1/sizing", json=bad, headers=H_).status_code == 422, bad
    assert b.put("/api/monitor/configs/1/sizing", json={"leverage": 2}, headers=H_).status_code == 404                                                       # чужой конфиг
    db.update_params(1, {**db.get_config(1)["params"], "pyramid_enabled": True, "pyramid_pct": 40, "pyramid_max": 2})
    assert "места не остаётся" in a.put("/api/monitor/configs/1/sizing", json={"trade_pct": 70}, headers=H_).json()["detail"]


def test_portfolio_what_if_sizing_does_not_touch_the_config(auth_env):
    db = auth_env
    alice, a, b = two_users(db)
    base = a.post("/api/portfolio", headers=H_).json()
    it0 = base["items"][0]
    assert it0["trade_pct"] == 10 and it0["leverage"] == 5 and not it0["overridden"] and it0["cfg_trade_pct"] == 10
    for bad in ({"1": {"trade_pct": 0}}, {"1": {"trade_pct": 150}}, {"1": {"leverage": 0.5}}, {"1": {"leverage": 500}}, {"x": {"leverage": 2}}, {"1": "много"}, [1]):
        assert a.put("/api/portfolio/prefs", json={"sizing": bad}, headers=H_).status_code == 422, bad
    assert a.put("/api/portfolio/prefs", json={"sizing": {"1": {"trade_pct": 20, "leverage": 5}}}, headers=H_).json()["sizing"] == {"1": {"trade_pct": 20.0, "leverage": 5.0}}
    r = a.post("/api/portfolio", headers=H_).json()
    it = r["items"][0]
    assert it["overridden"] and it["trade_pct"] == 20 and it["cfg_trade_pct"] == 10 and r["settings"]["sizing"] == {"1": {"trade_pct": 20.0, "leverage": 5.0}}
    assert abs(r["exposure"]["gross"] - 2 * base["exposure"]["gross"]) < 1e-6                         # доля вдвое больше -> позиция вдвое больше
    assert db.get_config(1)["params"]["trade_pct"] == 10                                              # конфиг (и автоторговля) не тронуты
    assert b.get("/api/portfolio/prefs").json()["sizing"] == {}
    assert a.put("/api/portfolio/prefs", json={"sizing": {}, "compound": None}, headers=H_).json()["sizing"] == {}


def test_portfolio_compounding_switch_for_all(auth_env):
    db = auth_env
    alice, a, b = two_users(db)
    assert a.put("/api/portfolio/prefs", json={"compound": "да"}, headers=H_).status_code == 422
    assert a.put("/api/portfolio/prefs", json={"compound": False}, headers=H_).json()["compound"] is False
    r = a.post("/api/portfolio", headers=H_).json()
    assert r["settings"]["compound"] is False and r["items"][0]["compound"] is False and r["items"][0]["cfg_compound"] is True
    assert db.get_config(1)["params"].get("compound", True) is True                                       # конфиги не тронуты
    assert a.put("/api/portfolio/prefs", json={"compound": None}, headers=H_).json()["compound"] is None
    assert a.post("/api/portfolio", headers=H_).json()["items"][0]["compound"] is True
