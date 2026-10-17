"""Разбор конфигов, присланных файлом или обычным сообщением.

Принимается всё, что выгружает интерфейс («Скачать конфиг»/«Копировать»): {"format": "maverick-config", "params": {...}, "tf": ...},
а также «голый» объект параметров, список конфигов и пакет {"configs": [...]}. JSON можно прислать в блоке кода (```json …```)
и с командой: «/add Имя {…}». Имя берётся из команды, подписи к файлу, поля "name", иначе строится из источника и актива."""
import json
import re

from pydantic import ValidationError

from app.schemas import BacktestParams
from app.service import effective_params

FORMAT = "maverick-config"
KNOWN = set(BacktestParams.model_fields)


class ConfigError(ValueError):
    pass


def extract_json(text):
    """Достаёт JSON из сообщения: снимает ``` ограждения, команду /add и имя перед первой фигурной/квадратной скобкой."""
    t = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", t, re.S | re.I)
    if fence:
        t = fence.group(1).strip()
    start = min([i for i in (t.find("{"), t.find("[")) if i >= 0], default=-1)
    if start < 0:
        raise ConfigError("В сообщении нет JSON. Пришлите конфиг файлом или текстом (кнопка «Скачать конфиг»/«Копировать» в интерфейсе).")
    try:
        return json.loads(t[start:])
    except ValueError as e:
        raise ConfigError(f"Не удалось разобрать JSON: {e}") from None


def _one(obj, name_hint):
    if not isinstance(obj, dict):
        raise ConfigError("Конфиг должен быть JSON-объектом")
    if obj.get("format") not in (None, FORMAT):
        raise ConfigError(f"Это не конфиг Maverick (format = «{obj.get('format')}»)")
    params = obj["params"] if isinstance(obj.get("params"), dict) else obj
    name = (name_hint or obj.get("name") or "").strip() or None
    tf = obj.get("tf", "auto") if (obj.get("tf") == "auto" or isinstance(obj.get("tf"), int)) else "auto"
    return {"name": name, "params": params, "tf": tf}


def parse_configs(text, name_hint=None):
    """-> [{name|None, params(dict, как прислали), tf}]. Бросает ConfigError с понятным текстом."""
    obj = extract_json(text)
    if isinstance(obj, list):
        items = obj
    elif isinstance(obj, dict) and isinstance(obj.get("configs"), list):
        items = obj["configs"]
    else:
        items = [obj]
    if not items:
        raise ConfigError("Пустой список конфигов")
    if len(items) > 30:
        raise ConfigError("Слишком много конфигов в одном сообщении (максимум 30)")
    return [_one(x, name_hint if len(items) == 1 else None) for x in items]


def validate(params, defaults):
    """Проверяет параметры теми же правилами, что API бэктеста, и возвращает (полные эффективные параметры, пропущенные поля).
    Недостающие поля берутся из текущих умолчаний backend/.env и фиксируются в конфиге, чтобы позже смена .env его не меняла."""
    ignored = sorted(k for k in params if k not in KNOWN)
    try:
        p = BacktestParams(**{k: v for k, v in params.items() if k in KNOWN})
    except ValidationError as e:
        msgs = [f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()[:6]]
        raise ConfigError("Неверные параметры — " + "; ".join(msgs)) from None
    eff = effective_params(defaults, p)
    # tps в effective_params уже списки словарей; null допустим только у необязательных параметров
    return {k: v for k, v in eff.items()}, ignored


def default_name(params):
    sym = str(params.get("symbol") or "").split("/")[0] or "config"
    return f"{params.get('source') or 'src'}-{sym}"
