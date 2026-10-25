import json
import os

import pytest
from fastapi.testclient import TestClient

from app.config import load_settings
from app.db import Database, DbError, hash_password, verify_password
from app.main import create_app
from test_api import H, M, T0, _write_history

H_ = {"X-Maverick": "1"}


@pytest.fixture()
def auth_env(tmp_path, monkeypatch):
    rows = [(T0 + i * H // 2, 2.0, 2.5, 1.0, 1.0) for i in range(8)]
    _write_history(tmp_path / "history.csv", rows)
    cache = tmp_path / "cache"
    cache.mkdir()
    import csv
    price, candles = 100.0, []
    for i in range(600):
        price *= 1.0004
        candles.append((T0 + i * M, price, price * 1.0002, price * 0.9998, price))
    with open(cache / "binanceusdm_BTCUSDT_USDT_1m.csv", "w", newline="") as f:
        csv.writer(f).writerows(candles)
    for k, v in dict(HISTORY_FILE=str(tmp_path / "history.csv"), CACHE_DIR=str(cache), SOURCES="", LIQ_FILE="", LIQ_SOURCE="", SYMBOLS="BTC/USDT:USDT",
                     SYMBOL_DEFAULT="BTC/USDT:USDT", SYMBOL="", CONFIRM="2", TP_PCT="3", SL_PCT="1", STATE_FILE=str(tmp_path / "s.json"),
                     ASSETS_FILE=str(tmp_path / "a.json"), AUTH_ENABLED="true", DB_FILE=str(tmp_path / "users.db")).items():
        monkeypatch.setenv(k, v)
    db = Database(tmp_path / "users.db")
    db.add_user("alice", "alice-password")
    db.add_user("bob", "bob-password-1")
    return db


def client(app):
    return TestClient(app, base_url="http://localhost")


def login(c, user, pw):
    return c.post("/api/auth/login", json={"login": user, "password": pw}, headers=H_)


def test_password_hash():
    h = hash_password("секрет-пароль")
    assert h.startswith("scrypt$") and "секрет" not in h and verify_password("секрет-пароль", h)
    assert not verify_password("другой", h) and not verify_password("x", "мусор") and hash_password("a1234567") != hash_password("a1234567")


def test_db_users_rules(tmp_path):
    db = Database(tmp_path / "x.db")
    a = db.add_user("Admin", "12345678")
    assert a["is_admin"] and not db.add_user("user2", "12345678")["is_admin"]                       # первый — админ
    for login_, pw, msg in (("ab", "12345678", "Логин"), ("bad login", "12345678", "Логин"), ("good", "short", "Пароль"), ("ADMIN", "12345678", "уже существует")):
        with pytest.raises(DbError, match=msg):
            db.add_user(login_, pw)
    assert db.check_login("admin", "12345678")["login"] == "Admin"                                  # логин без учёта регистра
    assert db.check_login("admin", "bad") is None and db.check_login("ghost", "12345678") is None
    db.set_disabled("user2", True)
    assert db.check_login("user2", "12345678") is None


def test_login_required_and_public_routes(auth_env):
    c = client(create_app(load_settings()))
    assert c.get("/api/health").status_code == 200
    assert c.get("/api/auth/status").json() == {"auth_enabled": True, "users_exist": True}
    for path in ("/api/config", "/api/candles", "/api/ui-state", "/api/jobs", "/api/latest", "/api/sync"):
        assert c.get(path).status_code == 401, path
    assert c.post("/api/backtest", json={}, headers=H_).status_code == 401
    assert c.get("/api/auth/me").status_code == 401


def test_login_logout_cookie_flags(auth_env):
    c = client(create_app(load_settings()))
    assert login(c, "alice", "wrong").status_code == 401
    r = login(c, "alice", "alice-password")
    assert r.status_code == 200 and r.json()["user"]["login"] == "alice"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "mav_session" in cookie
    assert c.get("/api/config").status_code == 200 and c.get("/api/auth/me").json()["user"]["login"] == "alice"
    assert c.post("/api/backtest", json={}, headers=H_).status_code == 200
    assert c.post("/api/auth/logout", headers=H_).status_code == 200
    assert c.get("/api/config").status_code == 401                                                  # сессия удалена на сервере


def test_csrf_header_required_for_changes(auth_env):
    c = client(create_app(load_settings()))
    assert c.post("/api/auth/login", json={"login": "alice", "password": "alice-password"}).status_code == 403     # без заголовка
    login(c, "alice", "alice-password")
    assert c.post("/api/backtest", json={}).status_code == 403
    assert c.put("/api/ui-state", json={"a": 1}).status_code == 403
    assert c.get("/api/ui-state").status_code == 200


def test_ui_state_is_per_user_but_data_is_shared(auth_env):
    app = create_app(load_settings())
    a, b = client(app), client(app)
    login(a, "alice", "alice-password")
    login(b, "bob", "bob-password-1")
    assert a.put("/api/ui-state", json={"last": {"symbol": "A"}}, headers=H_).status_code == 200
    assert b.put("/api/ui-state", json={"last": {"symbol": "B"}}, headers=H_).status_code == 200
    assert a.get("/api/ui-state").json() == {"last": {"symbol": "A"}} and b.get("/api/ui-state").json() == {"last": {"symbol": "B"}}
    # общие данные: свечи и история у всех одни и те же, второй пользователь ничего не скачивает заново
    assert a.get("/api/candles").json()["bars"] == b.get("/api/candles").json()["bars"] != []
    assert a.post("/api/backtest", json={}, headers=H_).json()["stats"] == b.post("/api/backtest", json={}, headers=H_).json()["stats"]
    # после выхода и повторного входа подгружается последнее сохранённое
    a.post("/api/auth/logout", headers=H_)
    login(a, "alice", "alice-password")
    assert a.get("/api/ui-state").json() == {"last": {"symbol": "A"}}
    # и переживает перезапуск сервера
    again = client(create_app(load_settings()))
    login(again, "alice", "alice-password")
    assert again.get("/api/ui-state").json() == {"last": {"symbol": "A"}}


def test_brute_force_lock(auth_env):
    c = client(create_app(load_settings()))
    for _ in range(5):
        assert login(c, "alice", "bad").status_code == 401
    r = login(c, "alice", "alice-password")
    assert r.status_code == 429 and "Подождите" in r.json()["detail"]                              # даже верный пароль в блокировке
    assert login(c, "bob", "bob-password-1").status_code == 200                                    # другой логин не затронут


def test_disabled_user_and_password_change_close_sessions(auth_env):
    app = create_app(load_settings())
    c = client(app)
    login(c, "bob", "bob-password-1")
    assert c.get("/api/config").status_code == 200
    auth_env.set_password("bob", "new-password-9")
    assert c.get("/api/config").status_code == 401
    assert login(c, "bob", "bob-password-1").status_code == 401 and login(c, "bob", "new-password-9").status_code == 200
    auth_env.set_disabled("bob", True)
    assert c.get("/api/config").status_code == 401 and login(c, "bob", "new-password-9").status_code == 401


def test_telegram_link_flow(auth_env):
    app = create_app(load_settings())
    c = client(app)
    login(c, "alice", "alice-password")
    code = c.post("/api/me/telegram/link-code", headers=H_).json()["code"]
    assert len(code) == 8
    u = auth_env.consume_link_code(code.lower(), 777, "alice_tg")                                    # как команда /link в боте
    assert u["login"] == "alice" and auth_env.user_by_telegram(777)["login"] == "alice"
    assert c.get("/api/auth/me").json()["user"]["telegram"] is True
    with pytest.raises(DbError, match="не подошёл"):
        auth_env.consume_link_code(code, 888)                                                         # код одноразовый
    b = client(app)
    login(b, "bob", "bob-password-1")
    with pytest.raises(DbError, match="уже привязан"):
        auth_env.consume_link_code(b.post("/api/me/telegram/link-code", headers=H_).json()["code"], 777)
    assert c.delete("/api/me/telegram", headers=H_).status_code == 200
    assert auth_env.user_by_telegram(777) is None and c.get("/api/auth/me").json()["user"]["telegram"] is False


def test_configs_belong_to_users_and_cascade(auth_env):
    a, b = auth_env.get_user("alice"), auth_env.get_user("bob")
    ca, _ = auth_env.add_config(a["id"], "Мой", {"x": 1})
    auth_env.add_config(b["id"], "Мой", {"x": 2})                                                    # то же имя у другого пользователя допустимо
    assert auth_env.find_config(b["id"], str(ca["id"])) is None                                      # чужой номер недоступен
    assert auth_env.find_config(a["id"], "мой")["params"] == {"x": 1}
    auth_env.remove_user("alice")
    assert auth_env.list_configs(a["id"]) == [] and len(auth_env.list_configs(b["id"])) == 1


def test_auth_disabled_mode_uses_local_user(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setenv("DB_FILE", str(tmp_path / "l.db"))
    monkeypatch.setenv("HISTORY_FILE", str(tmp_path / "h.csv"))
    c = TestClient(create_app(load_settings()))
    assert c.put("/api/ui-state", json={"a": 1}).status_code == 200 and c.get("/api/ui-state").json() == {"a": 1}
    assert c.get("/api/auth/me").json()["auth_enabled"] is False


def test_legacy_import_and_cli(tmp_path, monkeypatch, capsys):
    from tools import users
    legacy_state = tmp_path / "ui_state.json"
    legacy_state.write_text(json.dumps({"last": {"symbol": "ETH"}}))
    bot_file = tmp_path / "signalbot.json"
    bot_file.write_text(json.dumps({"configs": [{"id": 3, "name": "Старый", "params": {"symbol": "BTC/USDT:USDT"}, "tf": "auto", "paused": False,
                                                 "baseline": True, "last_ts": 12345, "alerts": 4}]}))
    for k, v in dict(STATE_FILE=str(legacy_state), SIGNALBOT_STATE_FILE=str(bot_file), SIGNALBOT_CHATS="555", DB_FILE=str(tmp_path / "m.db"),
                     HISTORY_FILE=str(tmp_path / "h.csv")).items():
        monkeypatch.setenv(k, v)
    users.main(["add", "owner", "--password", "owner-pass-1"])
    out = capsys.readouterr().out
    assert "администратор" in out and "конфигов бота: 1" in out and "555" in out
    db = Database(tmp_path / "m.db")
    u = db.get_user("owner")
    assert u["telegram_id"] == 555 and db.get_ui_state(u["id"]) == {"last": {"symbol": "ETH"}}
    cfg = db.find_config(u["id"], "Старый")
    assert cfg["baseline"] and cfg["last_ts"] == 12345 and cfg["alerts"] == 4                          # старые сигналы заново не пришлют
    users.main(["add", "second", "--password", "second-pass-1"])                                       # второй пользователь ничего не наследует
    assert db.list_configs(db.get_user("second")["id"]) == []
    users.main(["import-legacy", "owner"])
    assert len(db.list_configs(u["id"])) == 1                                                          # идемпотентно
    users.main(["link-code", "second"])
    assert "/link " in capsys.readouterr().out
    with pytest.raises(SystemExit, match="уже существует"):
        users.main(["add", "owner", "--password", "owner-pass-1"])
    users.main(["remove", "second", "--yes"])
    assert db.get_user("second") is None


def test_old_database_gets_fill_ts_column_with_null(tmp_path):
    import sqlite3
    from app.db import Database
    p = str(tmp_path / "old.db")
    c = sqlite3.connect(p)
    c.executescript("""CREATE TABLE configs(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, name TEXT, params TEXT, tf TEXT, paused INTEGER DEFAULT 0,
        baseline INTEGER DEFAULT 0, last_ts INTEGER DEFAULT 0, alerts INTEGER DEFAULT 0, error TEXT, created REAL, updated REAL);
        INSERT INTO configs(user_id, name, params, tf, created, updated) VALUES (1, 'a', '{}', 'auto', 0, 0);""")
    c.commit()
    c.close()
    db = Database(p)                                       # миграция при открытии
    assert db.list_configs()[0]["fill_ts"] is None         # NULL = «исполнения ещё не фиксировали» — прошлое не присылается
    Database(p)                                            # повторное открытие не падает


def test_saved_runs_are_per_user_with_unique_case_insensitive_names(auth_env):
    app = create_app(load_settings())
    a, b = client(app), client(app)
    assert login(a, "alice", "alice-password").status_code == 200 and login(b, "bob", "bob-password-1").status_code == 200
    assert client(app).get("/api/runs").status_code == 401
    body = {"name": "База v1", "params": {"side": "long", "sl_pct": 2}, "tf": 60, "summary": {"trades": 10, "total_usd": 123.4}}
    r = a.post("/api/runs", json=body, headers=H_)
    assert r.status_code == 200 and r.json()["name"] == "База v1" and r.json()["summary"]["trades"] == 10 and r.json()["params"]["sl_pct"] == 2
    again = a.post("/api/runs", json={**body, "name": "база V1", "summary": {"trades": 11}}, headers=H_).json()      # то же имя без учёта регистра (в т.ч. кириллица) — перезапись
    runs = a.get("/api/runs").json()["runs"]
    assert len(runs) == 1 and runs[0]["id"] == again["id"] and runs[0]["summary"]["trades"] == 11 and runs[0]["name"] == "база V1"
    assert b.get("/api/runs").json()["runs"] == []                                                                  # у другого пользователя своих нет
    assert b.delete(f"/api/runs/{runs[0]['id']}", headers=H_).status_code == 404                                    # чужой прогон не удалить
    assert a.post("/api/runs", json={**body, "name": "  "}, headers=H_).status_code == 422
    assert a.post("/api/runs", json={"name": "x", "params": [], "summary": {}}, headers=H_).status_code == 422
    assert a.delete(f"/api/runs/{runs[0]['id']}", headers=H_).status_code == 200 and a.get("/api/runs").json()["runs"] == []


def test_saved_runs_limit(tmp_path):
    db = Database(tmp_path / "x.db")
    u = db.add_user("alice", "12345678")
    db.MAX_RUNS = 3
    for i in range(3):
        db.save_run(u["id"], f"r{i}", {}, "auto", {})
    with pytest.raises(DbError, match="Не больше"):
        db.save_run(u["id"], "r3", {}, "auto", {})
    db.save_run(u["id"], "R0", {}, "auto", {"x": 1})                                                                 # перезапись существующего лимит не затрагивает


def _open_cfg():
    return {"symbol": "BTC/USDT:USDT", "side": "long", "confirm": 2, "tps": [{"pct": 50, "share": 50}, {"pct": 80, "share": 50}], "sl_pct": 50, "deposit": 10000, "trade_pct": 10, "leverage": 5}


def test_monitor_shows_open_position_with_levels_and_per_user_auto(auth_env):
    db = auth_env
    alice = db.check_login("alice", "alice-password")
    db.add_config(alice["id"], "Открытая", _open_cfg())
    db.add_config(alice["id"], "Сломанная", {"symbol": "BTC/USDT:USDT", "source": "нет-такого"})
    app = create_app(load_settings())
    a, b = client(app), client(app)
    assert login(a, "alice", "alice-password").status_code == 200 and login(b, "bob", "bob-password-1").status_code == 200
    assert client(app).post("/api/monitor", json={}, headers=H_).status_code == 401
    r = a.post("/api/monitor", json={}, headers=H_).json()
    assert [e["name"] for e in r["entries"]] == ["#1 Открытая", "#2 Сломанная"]
    e = r["entries"][0]
    pos = e["position"]
    assert pos and pos["side"] == "long" and e["price"] > 0 and e["auto"] == {"open": False, "manage": False}                 # автомод выключен, пока явно не включён
    assert pos["legs_n"] == 1 and "legs" not in pos and pos["notional"] == pos["margin"] * 5 and pos["danger"] is False                     # в списке — краткая сводка, ноги в подробностях
    cid0 = e["config_id"]
    det = a.get(f"/api/monitor/configs/{cid0}").json()
    leg = det["position"]["legs"][0]
    assert leg["stop_kind"] == "SL" and leg["to_stop_pct"] > 0 and leg["to_next_tp_pct"] > 0 and leg["liq_price"] < leg["entry"] and leg["leverage"] == 5
    assert [t["n"] for t in leg["tps"]] == [1, 2] and not any(t["hit"] for t in leg["tps"])
    assert abs(det["position"]["pnl_usd"] - sum(x["pnl_usd"] for x in det["position"]["legs"])) < 1e-9 and leg["metrics_at_entry"]["dl"] is not None
    assert det["stats"]["trades"] >= 1 and det["params"]["leverage"] == 5 and isinstance(det["history"], list) and det["title"] == "Открытая" and det["log"] == []
    assert b.get(f"/api/monitor/configs/{cid0}").status_code == 404                                                                        # чужую стратегию не открыть
    assert r["entries"][1]["error"] and r["entries"][1]["position"] is None                                                   # сломанный конфиг не роняет страницу
    assert r["exchange"] == {"name": "dry-run", "real": False, "testnet": False, "min_notional": 0.0}
    assert b.post("/api/monitor", json={}, headers=H_).json()["entries"] == []                                              # у другого пользователя своих конфигов нет
    db.save_run(alice["id"], "Мой прогон", _open_cfg(), "auto", {"trades": 1})
    runs_r = a.post("/api/monitor", json={"runs": True}, headers=H_).json()["entries"]
    assert [x["name"] for x in runs_r][-1] == "Прогон: Мой прогон" and runs_r[-1]["config_id"] is None and runs_r[-1]["auto"] is None and runs_r[-1]["position"]      # прогон — без автомода
    assert all(not x["name"].startswith("Прогон") for x in a.post("/api/monitor", json={}, headers=H_).json()["entries"])                                          # по умолчанию только конфиги бота
    # автомод
    cid = e["config_id"]
    s = a.put(f"/api/monitor/configs/{cid}/auto", json={"open": True}, headers=H_).json()
    assert s["auto"] == {"open": True, "manage": False}
    assert a.put(f"/api/monitor/configs/{cid}/auto", json={"manage": True}, headers=H_).json()["auto"] == {"open": True, "manage": True}
    assert b.put(f"/api/monitor/configs/{cid}/auto", json={"open": True}, headers=H_).status_code == 404                     # чужой конфиг
    r2 = a.post("/api/monitor", json={}, headers=H_).json()
    assert r2["entries"][0]["auto"] == {"open": True, "manage": True} and any(x["kind"] == "auto_toggle" for x in r2["log"])
    assert a.post("/api/monitor/auto/off", headers=H_).json()["disabled"] == 1
    assert a.post("/api/monitor", json={}, headers=H_).json()["entries"][0]["auto"] == {"open": False, "manage": False}


def test_test_position_requires_confirmation_limits_amount_and_is_dry_run(auth_env, monkeypatch):
    app = create_app(load_settings())
    a = client(app)
    login(a, "alice", "alice-password")
    assert a.post("/api/trading/test-position", json={}, headers=H_).status_code == 422                                      # без подтверждения — нельзя
    assert a.post("/api/trading/test-position", json={"confirm": True, "usd": 1000}, headers=H_).status_code == 422          # потолок суммы
    assert a.post("/api/trading/test-position", json={"confirm": True, "side": "x"}, headers=H_).status_code == 422
    r = a.post("/api/trading/test-position", json={"confirm": True}, headers=H_).json()
    assert r["status"] == "dry_run" and r["exchange"]["real"] is False and r["result"]["notional"] == 1.0 and r["leverage"] == 1 and r["margin"] == 1.0 and r["result"]["symbol"] == "BTC/USDT:USDT"
    log = a.post("/api/monitor", json={}, headers=H_).json()["log"]
    assert log[0]["kind"] == "test_position" and log[0]["status"] == "dry_run"
    # Hyperliquid без ключей: честная ошибка, а не «успех»
    monkeypatch.setenv("TRADE_EXCHANGE", "hyperliquid")
    monkeypatch.setenv("HL_WALLET_ADDRESS", "")
    monkeypatch.setenv("HL_PRIVATE_KEY", "")
    app2 = create_app(load_settings())
    c2 = client(app2)
    login(c2, "alice", "alice-password")
    r2 = c2.post("/api/trading/test-position", json={"confirm": True}, headers=H_)
    assert r2.status_code == 503 and "HL_WALLET_ADDRESS" in r2.json()["detail"]
    assert c2.get("/api/exchange/positions").status_code == 503
    assert a.get("/api/exchange/positions").json() == {"exchange": {"name": "dry-run", "real": False, "testnet": False, "min_notional": 0.0}, "positions": [], "account": {}}


def test_monitor_config_management_is_per_user(auth_env):
    db = auth_env
    alice = db.check_login("alice", "alice-password")
    c1, _ = db.add_config(alice["id"], "Первая", _open_cfg())
    c2, _ = db.add_config(alice["id"], "Вторая", _open_cfg())
    app = create_app(load_settings())
    a, b = client(app), client(app)
    login(a, "alice", "alice-password"); login(b, "bob", "bob-password-1")
    db.set_auto(alice["id"], c1["id"], True, True)
    # пауза: автомод выключается, возобновление — пересчёт с нуля
    assert a.post(f"/api/monitor/configs/{c1['id']}/pause", json={"paused": True}, headers=H_).json() == {"paused": True}
    got = db.get_config(c1["id"])
    assert got["paused"] and got["auto"] == {"open": False, "manage": False}
    db.update_config(c1["id"], baseline=True, last_ts=123)
    assert a.post(f"/api/monitor/configs/{c1['id']}/pause", json={"paused": False}, headers=H_).json() == {"paused": False}
    got = db.get_config(c1["id"])
    assert not got["paused"] and not got["baseline"] and got["last_ts"] == 0
    # переименование: пустое и занятое имя — ошибки
    assert a.put(f"/api/monitor/configs/{c1['id']}/name", json={"name": "Новое имя"}, headers=H_).json() == {"name": "Новое имя"}
    assert a.put(f"/api/monitor/configs/{c1['id']}/name", json={"name": "  "}, headers=H_).status_code == 422
    assert a.put(f"/api/monitor/configs/{c1['id']}/name", json={"name": "вторая"}, headers=H_).status_code == 409
    # экспорт
    ex = a.get(f"/api/monitor/configs/{c1['id']}/export").json()
    assert ex["format"] == "maverick-config" and ex["name"] == "Новое имя" and ex["params"]["leverage"] == 5
    # чужой пользователь ничего из этого не может
    for method, path, body in (("post", "pause", {"paused": True}), ("put", "name", {"name": "x"}), ("delete", "", None), ("get", "export", None)):
        url = f"/api/monitor/configs/{c1['id']}" + (f"/{path}" if path else "")
        r = getattr(b, method)(url, headers=H_, **({"json": body} if body is not None else {}))
        assert r.status_code == 404, (method, path)
    # удаление
    assert a.delete(f"/api/monitor/configs/{c2['id']}", headers=H_).json() == {"ok": True}
    assert [c["name"] for c in db.list_configs(alice["id"])] == ["Новое имя"]
    assert a.delete(f"/api/monitor/configs/{c2['id']}", headers=H_).status_code == 404
