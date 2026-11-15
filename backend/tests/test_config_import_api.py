"""Добавление конфигов бота через сайт: POST /api/monitor/configs/import (то же, что отправить конфиг боту в Telegram)."""
import json

from app.config import load_settings
from app.main import create_app
from test_auth import H_, _open_cfg, auth_env, client, login  # noqa: F401

URL = "/api/monitor/configs/import"


def setup(db):
    app = create_app(load_settings())
    a, b = client(app), client(app)
    assert login(a, "alice", "alice-password").status_code == 200 and login(b, "bob", "bob-password-1").status_code == 200
    return a, b, db.check_login("alice", "alice-password")


def wrap(**extra):
    return json.dumps({"format": "maverick-config", "version": 1, "tf": "auto", "params": {**_open_cfg(), **extra}})


def test_import_adds_a_config_for_the_user_only_and_it_shows_up_in_the_monitor(auth_env):
    db = auth_env
    a, b, alice = setup(db)
    assert client(create_app(load_settings())).post(URL, json={"text": wrap()}, headers=H_).status_code == 401
    r = a.post(URL, json={"text": wrap(), "name": "Моя стратегия"}, headers=H_)
    assert r.status_code == 200 and r.json()["errors"] == []
    (it,) = r.json()["added"]
    assert it["name"] == "Моя стратегия" and it["created"] is True and it["ignored"] == []
    cfg = db.get_config(it["id"])
    assert cfg["user_id"] == alice["id"] and cfg["params"]["sl_pct"] == 50 and cfg["params"]["source"] and "tps" in cfg["params"]      # недостающие поля дополнены умолчаниями .env
    assert not cfg["paused"] and not cfg["baseline"] and not cfg["any_open"]                                                         # автомод по умолчанию выключен
    assert [e["title"] for e in a.post("/api/monitor", json={}, headers=H_).json()["entries"]] == ["Моя стратегия"]
    assert b.post("/api/monitor", json={}, headers=H_).json()["entries"] == []                                                       # у другого пользователя его нет
    assert any(x["kind"] == "config_import" for x in db.list_auto_log(alice["id"], 5))


def test_name_is_taken_from_the_json_then_from_source_and_asset_and_conflicts_get_a_number(auth_env):
    db = auth_env
    a, _b, alice = setup(db)
    named = json.dumps({"name": "Из JSON", "params": _open_cfg()})
    assert a.post(URL, json={"text": named}, headers=H_).json()["added"][0]["name"] == "Из JSON"
    bare = a.post(URL, json={"text": json.dumps(_open_cfg())}, headers=H_).json()["added"][0]                                         # «голый» объект параметров
    assert bare["name"].endswith("-BTC")
    again = a.post(URL, json={"text": named}, headers=H_).json()["added"][0]                                                        # то же имя без replace: старый не трогаем
    assert again["name"] == "Из JSON 2" and again["created"] is True and len(db.list_configs(alice["id"])) == 3


def test_replace_overwrites_the_same_name_and_resets_its_state_but_not_other_users(auth_env):
    db = auth_env
    a, b, alice = setup(db)
    first = a.post(URL, json={"text": wrap(sl_pct=50), "name": "Одна"}, headers=H_).json()["added"][0]
    db.update_config(first["id"], baseline=True, last_ts=123)
    other = b.post(URL, json={"text": wrap(), "name": "Одна"}, headers=H_).json()["added"][0]                                         # у другого пользователя имя независимо
    assert other["created"] is True and other["id"] != first["id"]
    r = a.post(URL, json={"text": wrap(sl_pct=30), "name": "Одна", "replace": True}, headers=H_).json()["added"][0]
    assert r["id"] == first["id"] and r["created"] is False
    c = db.get_config(first["id"])
    assert c["params"]["sl_pct"] == 30 and not c["baseline"] and db.get_config(other["id"])["params"]["sl_pct"] == 50


def test_batches_report_each_config_and_bad_ones_do_not_block_good_ones(auth_env):
    db = auth_env
    a, _b, alice = setup(db)
    pack = json.dumps({"configs": [{"name": "Хороший", "params": _open_cfg()}, {"name": "Плохой источник", "params": {**_open_cfg(), "source": "нет-такого"}},
                                   {"name": "Плохое значение", "params": {**_open_cfg(), "sl_pct": -5}}, {"name": "Лишнее поле", "params": {**_open_cfg(), "bogus": 1}}]})
    r = a.post(URL, json={"text": pack}, headers=H_)
    assert r.status_code == 200
    body = r.json()
    assert [x["name"] for x in body["added"]] == ["Хороший", "Лишнее поле"] and body["added"][1]["ignored"] == ["bogus"]
    assert {x["name"] for x in body["errors"]} == {"Плохой источник", "Плохое значение"} and any("нет-такого" in x["error"] for x in body["errors"])
    assert len(db.list_configs(alice["id"])) == 2


def test_nothing_accepted_is_a_clear_422_and_bad_input_is_rejected(auth_env):
    db = auth_env
    a, _b, alice = setup(db)
    bad = a.post(URL, json={"text": wrap(source="нет-такого")}, headers=H_)
    assert bad.status_code == 422 and "нет-такого" in bad.json()["detail"] and db.list_configs(alice["id"]) == []
    for body, word in (({"text": "это не json"}, "JSON"), ({"text": ""}, "JSON"), ({}, "JSON"), ({"text": json.dumps({"format": "чужой", "params": {}})}, "не конфиг Maverick"),
                       ({"text": json.dumps({"configs": []})}, "Пустой"), ({"text": wrap(), "replace": "да"}, "replace"), ({"text": wrap(), "name": 5}, "name")):
        r = a.post(URL, json=body, headers=H_)
        assert r.status_code == 422 and word in r.json()["detail"], (body, r.json())
    assert a.post(URL, json={"text": "{" + " " * 2_100_000 + "}"}, headers=H_).status_code == 413
    assert a.post(URL, json={"text": json.dumps({"configs": [{"params": _open_cfg()}] * 31})}, headers=H_).status_code == 422          # больше 30 за раз
    assert db.list_configs(alice["id"]) == []


def test_text_can_be_wrapped_in_a_code_block_like_in_telegram(auth_env):
    db = auth_env
    a, _b, _alice = setup(db)
    r = a.post(URL, json={"text": "Вот конфиг:\n```json\n" + wrap() + "\n```", "name": "Из чата"}, headers=H_)
    assert r.status_code == 200 and r.json()["added"][0]["name"] == "Из чата"
