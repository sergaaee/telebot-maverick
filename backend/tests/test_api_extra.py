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
    assert db.get_config(1)["auto"] == {"open": False, "manage": False}
    m = a.post("/api/monitor", json={}, headers=H_).json()
    assert m["bot"]["state"] == "unknown"                                                  # бот ещё не запускался
