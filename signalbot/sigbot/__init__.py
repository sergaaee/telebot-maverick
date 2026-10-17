"""Telegram-бот сигналов: присылает новые сигналы по сохранённым конфигам вместе со статистикой и принимает конфиги файлом или текстом."""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]           # signalbot/
ROOT = HERE.parent                                   # корень репозитория
BACKEND = ROOT / "backend"

# движок бэктеста и данные берём из backend (пакет `app`), поэтому backend должен быть в пути импорта
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(HERE / ".env")                           # раньше backend/.env: значения бота имеют приоритет
os.environ.setdefault("SIGBOT_LOADED", "1")
