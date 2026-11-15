"""Разбор и добавление конфигов бота. Сама логика — в backend/app/botconfigs.py (её же использует сайт); здесь — прежние имена для бота и тестов."""
from app.botconfigs import FORMAT, KNOWN, ConfigError, default_name, extract_json, import_configs, parse_configs, validate  # noqa: F401
