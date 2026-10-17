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
