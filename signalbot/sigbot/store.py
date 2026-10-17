"""Хранилище конфигов и служебного состояния бота (один JSON-файл, запись атомарная)."""
import json
import os
import threading
import time


class ConfigStore:
    def __init__(self, path):
        self.path = str(path)
        self._lock = threading.RLock()
        self.data = {"version": 1, "next_id": 1, "update_offset": None, "configs": []}
        try:
            with open(self.path, encoding="utf-8") as f:
                self.data.update(json.load(f))
        except (OSError, ValueError):
            pass

    def save(self):
        with self._lock:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)

    # ---------- конфиги ----------
    @property
    def configs(self):
        return self.data["configs"]

    def find(self, ref):
        """Конфиг по номеру (#3, 3) или имени (без учёта регистра)."""
        ref = (ref or "").strip().lstrip("#")
        with self._lock:
            if ref.isdigit():
                for c in self.configs:
                    if c["id"] == int(ref):
                        return c
            for c in self.configs:
                if c["name"].lower() == ref.lower():
                    return c
        return None

    def add(self, name, params, tf="auto"):
        """Новый конфиг (или обновление существующего с тем же именем: расчёт и база сигналов начинаются заново)."""
        with self._lock:
            old = next((c for c in self.configs if c["name"].lower() == name.lower()), None)
            fields = dict(params=params, tf=tf, baseline=False, last_ts=0, error=None, updated=time.time())
            if old:
                old.update(fields)
                self.save()
                return old, False
            cfg = {"id": self.data["next_id"], "name": name, "paused": False, "created": time.time(), "alerts": 0, **fields}
            self.data["next_id"] += 1
            self.configs.append(cfg)
            self.save()
            return cfg, True

    def remove(self, cfg):
        with self._lock:
            self.data["configs"] = [c for c in self.configs if c["id"] != cfg["id"]]
            self.save()

    def unique_name(self, base):
        names = {c["name"].lower() for c in self.configs}
        if base.lower() not in names:
            return base
        i = 2
        while f"{base} {i}".lower() in names:
            i += 1
        return f"{base} {i}"
