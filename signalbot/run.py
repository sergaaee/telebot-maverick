"""Запуск бота сигналов:  python signalbot/run.py  (или из папки signalbot:  python run.py)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sigbot.main import main  # noqa: E402

if __name__ == "__main__":
    main()
