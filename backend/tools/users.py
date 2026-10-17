"""Управление пользователями (регистрация только здесь, на сайте её нет):

    python cli.py user add LOGIN [--password P] [--no-import]   # создать (пароль спросит, если не задан); первый пользователь — админ
    python cli.py user list
    python cli.py user passwd LOGIN [--password P]              # сменить пароль (все сессии сайта этого пользователя закрываются)
    python cli.py user disable LOGIN | enable LOGIN
    python cli.py user remove LOGIN [--yes]                     # удаляет пользователя вместе с его состоянием и конфигами бота
    python cli.py user link LOGIN TELEGRAM_ID | unlink LOGIN    # привязка Telegram вручную (обычно: код на сайте -> /link в боте)
    python cli.py user link-code LOGIN                          # одноразовый код для привязки (10 минут): отправьте боту /link КОД
    python cli.py user import-legacy LOGIN                      # перенести данные версии с одним пользователем (ui_state.json, signalbot.json)
"""
import argparse
import getpass
import time

from app.config import load_settings
from app.db import Database, DbError
from app.legacy import import_legacy


def _password(a, confirm=True):
    if a.password:
        return a.password
    p = getpass.getpass("Пароль: ")
    if confirm and getpass.getpass("Повторите пароль: ") != p:
        raise DbError("Пароли не совпали")
    return p


def main(argv):
    ap = argparse.ArgumentParser(prog="cli.py user", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("add")
    p.add_argument("login")
    p.add_argument("--password")
    p.add_argument("--no-import", action="store_true", help="не переносить данные версии с одним пользователем")
    sub.add_parser("list")
    p = sub.add_parser("passwd")
    p.add_argument("login")
    p.add_argument("--password")
    for name in ("disable", "enable", "unlink", "link-code", "import-legacy"):
        sub.add_parser(name).add_argument("login")
    p = sub.add_parser("remove")
    p.add_argument("login")
    p.add_argument("--yes", action="store_true")
    p = sub.add_parser("link")
    p.add_argument("login")
    p.add_argument("telegram_id", type=int)
    a = ap.parse_args(argv)
    s = load_settings()
    db = Database(s.db_file)
    try:
        run(a, s, db)
    except DbError as e:
        raise SystemExit(f"Ошибка: {e}")


def run(a, s, db):
    if a.cmd == "add":
        first = db.user_count() == 0
        u = db.add_user(a.login, _password(a))
        print(f"Создан пользователь «{u['login']}»" + (" (администратор)" if u["is_admin"] else ""))
        if first and not a.no_import:
            done = import_legacy(db, s, u)
            print("Перенесено: " + "; ".join(done) if done else "Данных прежней версии для переноса нет.")
    elif a.cmd == "list":
        for u in db.list_users():
            n = len(db.list_configs(u["id"]))
            print(f"{u['id']:>3}  {u['login']:<20} {'admin' if u['is_admin'] else '     '}  {'выключен' if u['disabled'] else 'активен '}  "
                  f"telegram: {u['telegram_id'] or '—':<12} конфигов бота: {n}  создан {time.strftime('%Y-%m-%d', time.localtime(u['created']))}")
        if not db.user_count():
            print("Пользователей нет. Создайте: python cli.py user add LOGIN")
    elif a.cmd == "passwd":
        db.set_password(a.login, _password(a))
        print("Пароль изменён, прежние входы на сайте закрыты.")
    elif a.cmd in ("disable", "enable"):
        db.set_disabled(a.login, a.cmd == "disable")
        print("Готово.")
    elif a.cmd == "remove":
        if not a.yes and input(f"Удалить «{a.login}» вместе с настройками и конфигами бота? [y/N] ").strip().lower() != "y":
            raise SystemExit("Отменено")
        db.remove_user(a.login)
        print("Удалён.")
    elif a.cmd == "link":
        db.link_telegram(a.login, a.telegram_id)
        print("Telegram привязан.")
    elif a.cmd == "unlink":
        u = db.get_user(a.login)
        if not u:
            raise DbError(f"Нет пользователя «{a.login}»")
        db.unlink_telegram(u["id"])
        print("Telegram отвязан.")
    elif a.cmd == "link-code":
        u = db.get_user(a.login)
        if not u:
            raise DbError(f"Нет пользователя «{a.login}»")
        print(f"Отправьте боту сообщение:  /link {db.create_link_code(u['id'])}   (код действует 10 минут)")
    elif a.cmd == "import-legacy":
        u = db.get_user(a.login)
        if not u:
            raise DbError(f"Нет пользователя «{a.login}»")
        done = import_legacy(db, s, u)
        print("Перенесено: " + "; ".join(done) if done else "Переносить нечего (или всё уже перенесено).")
