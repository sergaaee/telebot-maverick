"""Единая точка входа для консольных инструментов (запускать из папки backend/):

    python cli.py serve                       # API для интерфейса (host/port из .env)
    python cli.py backtest history.csv ...    # бэктест и свипы (флаги как раньше, см. --help)
    python cli.py export history.csv --chat 2119980662
    python cli.py user add LOGIN              # пользователи сайта и бота сигналов (регистрация только здесь), см. python cli.py user -h
    python cli.py bot                         # Telegram-бот (DRY_RUN=true по умолчанию)
    python cli.py watchdog                    # сторож: пишет в Telegram, если бот сигналов не отвечает (токен — SIGNALBOT_TOKEN из signalbot/.env)

Рабочая папка переключается в корень репозитория: history*.csv, trades.csv, data/ и файлы сессий Telethon лежат там.
"""
import os
import sys

from app.config import ROOT, load_settings


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("serve", "backtest", "export", "bot", "user", "watchdog"):
        raise SystemExit(__doc__)
    cmd = sys.argv.pop(1)
    os.chdir(ROOT)
    sys.argv[0] = cmd
    if cmd == "serve":
        import uvicorn
        s = load_settings()
        uvicorn.run("app.main:app", host=s.host, port=s.port, reload="--reload" in sys.argv)
    elif cmd == "backtest":
        from app.core.backtest import main as run
        run()
    elif cmd == "watchdog":
        import logging
        import time
        from dotenv import load_dotenv
        from app import watchdog as wd
        from app.db import Database
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        load_dotenv(ROOT / "signalbot" / ".env")
        token = os.getenv("SIGNALBOT_TOKEN", "").strip()
        if not token:
            raise SystemExit("Не задан SIGNALBOT_TOKEN (signalbot/.env)")
        db, state, notify = Database(load_settings().db_file), {}, wd.telegram_notify(token)
        while True:
            wd.check(db, state, notify)
            time.sleep(60)
    elif cmd == "user":
        from tools import users
        users.main(sys.argv[1:])
    elif cmd == "export":
        import argparse
        import asyncio
        from tools import export_history as e
        ap = argparse.ArgumentParser()
        ap.add_argument("out", nargs="?", default="history.csv")
        ap.add_argument("--chat", default=os.getenv("TG_CHANNEL_ID", "3217240841"))
        ap.add_argument("--full", action="store_true")
        a = ap.parse_args()
        asyncio.run(e.main(a.out, a.chat, a.full))
    else:
        import asyncio
        from tools.bot import main as run
        asyncio.run(run())


if __name__ == "__main__":
    main()
