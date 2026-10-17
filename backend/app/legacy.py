"""Перенос данных одиночной версии (один пользователь): сохранённое состояние интерфейса и конфиги бота -> в базу первого пользователя."""
import json
import os

from .state import read_state


def legacy_bot_file():
    from .config import ROOT
    p = os.getenv("SIGNALBOT_STATE_FILE", "").strip() or "data/signalbot.json"
    return p if os.path.isabs(p) else os.path.join(ROOT, p)


def import_legacy(db, settings, user):
    """Копирует в базу данные из data/ui_state.json и data/signalbot.json (сами файлы не трогает).
    Повторный вызов безопасен: уже существующее состояние и конфиги с теми же именами не затираются. -> список строк-итогов."""
    out = []
    state = read_state(settings.state_file)
    if state and not db.get_ui_state(user["id"]):
        db.put_ui_state(user["id"], state)
        out.append(f"состояние интерфейса из {settings.state_file}")
    path = legacy_bot_file()
    n = 0
    try:
        with open(path, encoding="utf-8") as f:
            legacy = json.load(f)
    except (OSError, ValueError):
        legacy = {}
    for c in legacy.get("configs", []):
        if db.find_config(user["id"], c["name"]):
            continue
        cfg, _ = db.add_config(user["id"], c["name"], c["params"], c.get("tf", "auto"))
        db.update_config(cfg["id"], paused=bool(c.get("paused")), baseline=bool(c.get("baseline")), last_ts=int(c.get("last_ts") or 0), alerts=int(c.get("alerts") or 0))
        n += 1
    if n:
        out.append(f"конфигов бота: {n} (из {path})")
    chats = [x.strip() for x in os.getenv("SIGNALBOT_CHATS", "").replace(";", ",").split(",") if x.strip().lstrip("-").isdigit()]
    if chats and not user.get("telegram_id") and db.user_by_telegram(int(chats[0])) is None:
        db.link_telegram(user["login"], int(chats[0]))
        out.append(f"Telegram id {chats[0]} (из SIGNALBOT_CHATS)")
    return out
