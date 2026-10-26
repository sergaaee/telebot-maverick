"""Сторож бота: следит за пульсом, который бот пишет в базу (таблица meta), и пишет владельцам автоторговли в Telegram, если бот молчит.

Бот пишет два значения:
  bot_alive — раз в ~15 с, пока жив процесс и его цикл событий;
  bot_cycle — время конца последнего цикла проверки конфигов (если цикл завис, а процесс жив, это значение перестаёт обновляться).
Состояния: ok / dead (процесс не отвечает) / stalled (процесс жив, но циклы не идут) / unknown (бот ещё ни разу не запускался).
Сообщение уходит один раз за сбой, потом — «снова работает». Получатели — пользователи с привязанным Telegram и включённым автомодом хотя бы у одного конфига.
Сторож работает отдельно от бота: в процессе backend (если в его окружении есть SIGNALBOT_TOKEN) или командой `python cli.py watchdog`."""
import json
import logging
import os
import threading
import time
import urllib.parse
import urllib.request

log = logging.getLogger("watchdog")


def _num(name, default):
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


DEAD_MIN = _num("WATCHDOG_DEAD_MIN", 3.0)        # нет пульса процесса дольше стольких минут — бот не работает
STALL_MIN = _num("WATCHDOG_STALL_MIN", 20.0)     # циклы не заканчиваются дольше (первая загрузка свечей может идти долго — поэтому с запасом)


def bot_status(db, now=None):
    now = time.time() if now is None else now
    alive, cycle = db.get_meta("bot_alive"), db.get_meta("bot_cycle")
    try:
        a = now - float(alive) if alive else None
        c = now - float(cycle) if cycle else None
    except ValueError:
        a = c = None
    if a is None:
        state = "unknown"
    elif a > DEAD_MIN * 60:
        state = "dead"
    elif c is None or c > STALL_MIN * 60:
        state = "stalled"
    else:
        state = "ok"
    return {"state": state, "alive_age_s": a, "cycle_age_s": c, "dead_min": DEAD_MIN, "stall_min": STALL_MIN}


def recipients(db):
    """Пользователи, которым бот нужен прямо сейчас: Telegram привязан и автомод включён у неактивного на паузе конфига."""
    uids = {c["user_id"] for c in db.list_configs() if not c["paused"] and (c["auto"]["open"] or c["auto"]["manage"])}
    return [u for u in db.list_users() if u["id"] in uids and u["telegram_id"] and not u["disabled"]]


TEXT = {
    "dead": "🚨 <b>Бот не отвечает</b> уже {age} мин. Автоторговля не работает: новые сигналы не открываются, стопы в безубыток не переставляются.\n"
            "Стопы и тейки, уже стоящие на бирже, продолжают работать. Проверьте процесс бота.",
    "stalled": "⚠️ <b>Бот завис</b>: процесс жив, но цикл проверки не заканчивается {age} мин. Автоторговля фактически стоит. Проверьте логи бота.",
    "ok": "✅ Бот снова работает.",
}


def check(db, state, notify, now=None):
    """Один проход. state — словарь между проходами {'last': состояние, 'sent': что уже отправляли}. notify(telegram_id, text)."""
    st = bot_status(db, now)
    cur = st["state"]
    state["last"] = cur
    if cur == "unknown":
        return st
    told = state.setdefault("told", {})              # кому уже сообщили о текущем сбое: chat -> вид сбоя
    if cur in ("dead", "stalled"):
        age = int((st["alive_age_s"] if cur == "dead" else st["cycle_age_s"] or 0) // 60)
        for u in recipients(db):                    # включил автомод уже во время сбоя — тоже узнает, но один раз
            if told.get(u["telegram_id"]) != cur:
                notify(u["telegram_id"], TEXT[cur].format(age=age))
                told[u["telegram_id"]] = cur
    elif cur == "ok" and told:
        for chat in list(told):
            notify(chat, TEXT["ok"])
        told.clear()
    return st


def telegram_notify(token):
    def send(chat, text):
        data = urllib.parse.urlencode({"chat_id": chat, "text": text, "parse_mode": "HTML"}).encode()
        try:
            urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data), timeout=15).read()
        except Exception as e:
            log.warning("сторож: не отправить в %s: %s", chat, e)
    return send


class Watchdog:
    def __init__(self, db, notify, interval=60.0):
        self.db, self.notify, self.interval = db, notify, interval
        self.state = {}
        self._stop = threading.Event()
        self._t = None

    def start(self):
        self._t = threading.Thread(target=self._run, daemon=True, name="watchdog")
        self._t.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.wait(self.interval):
            try:
                check(self.db, self.state, self.notify)
            except Exception:
                log.exception("сторож")
