"""Хранилище состояния интерфейса: один JSON-файл (путь — STATE_FILE), запись атомарная."""
import json
import os
import threading
from pathlib import Path

_lock = threading.Lock()


def read_state(path: Path) -> dict:
    try:
        with _lock, open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(path: Path, state: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with _lock:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, path)
