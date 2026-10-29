"""Ядро бота (без привязки к aiogram): пользователи, их конфиги, команды, цикл проверки новых сигналов и рассылка.

Многопользовательская модель: Telegram-аккаунт привязывается к пользователю сайта по одноразовому коду (/link КОД). У каждого
пользователя свои конфиги (таблица configs в SQLite), сигналы по ним приходят только владельцу. Данные рынка (история отчётов,
свечи) общие: одинаковые расчёты в одном цикле выполняются один раз."""
import json
import logging
import os
import threading
import time
from html import escape as esc

from app.db import EXCHANGES, DbError

from . import formatting as fm
from .configs import ConfigError, default_name, parse_configs, validate
from app.core import backtest
from app import digest as digest_mod, exporter, manage
from app.monitor import cached_result
from app.service import ParamError
from app.secrets_store import SecretError, encrypt
from app.tradelog import read_tail, trade_error, trade_log
from app.trading import EXCHANGE_LABELS, adapter_for, adapters_for, forget_adapter, mask_address, verify_bybit, verify_hyperliquid

from app.trading import BYBIT_KEY

from .engine import Cancelled, EvalError, fingerprint
from .ui import MonitorUI
from .transport import TelegramError

log = logging.getLogger("signalbot")
FILL_CHECK_SEC = 300

LINK_TRIES, LINK_WINDOW = 5, 600.0


class SignalService:
    def __init__(self, settings, tg, db, engine, bsettings, sync=None, clock=time.time):
        self.s, self.tg, self.db, self.engine, self.b, self.sync, self.clock = settings, tg, db, engine, bsettings, sync, clock
        self._fp = {}                       # id конфига -> отпечаток файлов истории на момент последнего расчёта
        self._cycle_lock = threading.Lock()
        self._link_tries = {}
        self._cancel = set()                # пользователи, нажавшие /cancel: их текущий подсчёт прерывается
        self._force = set()                 # пользователи, запросившие /check
        self._wake = threading.Event()
        self._stop = threading.Event()
        self.last_cycle = None
        self.ui = MonitorUI(self)
        self._retry_ids = {(r["config_id"], r["exchange"]) for r in db.list_retries()}      # входы, которые не удались до перезапуска: повторяются при первой проверке конфига
        self._hl = {}                       # user_id -> шаг подключения Hyperliquid (ждём адрес кошелька / ключ API-кошелька)
        self._by = {}                       # user_id -> шаг подключения Bybit (ждём API-ключ / секрет)

    # ---------------- отправка ----------------
    def send(self, chat, text, keyboard=None):
        try:
            self.tg.send_message(chat, text, keyboard=keyboard) if keyboard else self.tg.send_message(chat, text)
        except TelegramError as e:
            log.warning("не удалось отправить в %s: %s", chat, e)

    reply = send

    # ---------------- входящие ----------------
    def adapter_for(self, user_id, exchange="hyperliquid"):
        """Адаптер биржи пользователя: его подключённый счёт (Hyperliquid по умолчанию, Bybit) или симуляция (ордера не отправляются)."""
        return adapter_for(self.db, user_id, exchange)

    def adapters_for(self, user_id):
        return adapters_for(self.db, user_id)

    def forget(self, cfg_id):
        self._fp.pop(cfg_id, None)                                  # следующий цикл пересчитает конфиг

    def refresh_live(self):
        self.ui.refresh_live()

    def handle_update(self, upd):
        cb = upd.get("callback_query")
        if cb:
            try:
                self.ui.handle_callback(cb)
            except Exception:
                log.exception("ошибка обработки кнопки")
            return
        msg = upd.get("message")
        if not msg:
            return
        chat = msg["chat"]["id"]
        from_id = (msg.get("from") or {}).get("id", chat)
        try:
            self._handle_message(chat, from_id, (msg.get("from") or {}).get("username"), msg)
        except Exception as e:                               # одна плохая команда не должна ронять бота
            log.exception("ошибка обработки сообщения")
            self.send(chat, f"⚠️ Ошибка: {esc(str(e) or type(e).__name__)}")

    def _handle_message(self, chat, from_id, username, msg):
        text = (msg.get("text") or msg.get("caption") or "").strip()
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower() if text.startswith("/") else ""
        arg = arg.strip()
        if cmd in ("/start", "/help") or cmd == "/link":
            if cmd == "/link":
                self.cmd_link(chat, from_id, username, arg)
            else:
                user = self.db.user_by_telegram(from_id)
                self.send(chat, fm.HELP if user else fm.WELCOME)
            return
        user = self.db.user_by_telegram(from_id)
        if not user:
            self.send(chat, fm.NOT_LINKED)
            return
        if self._hl_pending(chat, user, msg, text, cmd) or self._by_pending(chat, user, msg, text, cmd):         # идёт подключение счёта: сообщение — адрес кошелька / ключ
            return
        doc = msg.get("document")
        if doc:
            name = doc.get("file_name") or ""
            if not name.lower().endswith((".json", ".txt")) and doc.get("mime_type") != "application/json":
                self.send(chat, "Пришлите конфиг файлом .json (кнопка «Скачать конфиг» в интерфейсе).")
                return
            try:
                body = self.tg.download(doc["file_id"]).decode("utf-8-sig")
            except (TelegramError, UnicodeDecodeError) as e:
                self.send(chat, f"Не удалось прочитать файл: {esc(str(e))}")
                return
            hint = text.split("\n")[0].strip() if text and not text.startswith("/") else (arg if cmd == "/add" else "")
            if not hint and not name.lower().startswith(("maverick-config", "config")):
                hint = name.rsplit(".", 1)[0]
            self.import_text(chat, user, body, hint or None)
            return
        if cmd:
            if cmd == "/add":
                name, _, rest = arg.partition("{")
                self.import_text(chat, user, ("{" + rest) if rest else arg, name.strip() or None)
                return
            handler = getattr(self, "cmd_" + cmd[1:].replace("-", "_"), None)
            if handler and cmd[1:] not in ("link",):
                handler(chat, user, arg)
            else:
                self.send(chat, "Не знаю такой команды. /help — список команд.")
            return
        if "{" in text or "[" in text:
            self.import_text(chat, user, text, None)
        elif text:
            self.send(chat, "Не понял. Пришлите конфиг (файл .json или текст JSON) или /help.")

    # ---------------- привязка аккаунта ----------------
    def cmd_link(self, chat, from_id, username, arg):
        now = self.clock()
        tries = [t for t in self._link_tries.get(from_id, []) if now - t < LINK_WINDOW]
        if len(tries) >= LINK_TRIES:
            self.send(chat, "Слишком много попыток. Подождите несколько минут.")
            return
        self._link_tries[from_id] = tries + [now]
        if not arg:
            self.send(chat, "Формат: <code>/link КОД</code>. Код — на сайте: меню пользователя → «Привязать Telegram».")
            return
        try:
            user = self.db.consume_link_code(arg, from_id, username)
        except DbError as e:
            self.send(chat, f"❌ {esc(str(e))}")
            return
        self.send(chat, f"✅ Telegram привязан к аккаунту <b>{esc(user['login'])}</b>.\nТеперь можно присылать конфиги и получать сигналы. /help")

    def cmd_unlink(self, chat, user, arg):
        self.db.unlink_telegram(user["id"])
        self.send(chat, "Telegram отвязан от аккаунта. Конфиги сохранены на сайте/в базе и вернутся после новой привязки, но сигналы приходить перестанут.")

    # ---------------- импорт конфигов ----------------
    def import_text(self, chat, user, text, name_hint):
        try:
            items = parse_configs(text, name_hint)
        except ConfigError as e:
            self.send(chat, f"❌ {esc(str(e))}")
            return
        added, errors = [], []
        for it in items:
            try:
                params, ignored = validate(it["params"], self.s.defaults)
                if params["source"] not in self.s.sources:
                    raise ConfigError(f"источник «{params['source']}» не настроен у бота (доступны: {', '.join(self.s.sources)})")
            except ConfigError as e:
                errors.append(f"{esc(it['name'] or default_name(it['params']))}: {esc(str(e))}")
                continue
            name = (it["name"] or default_name(params))[:60]
            existing = self.db.find_config(user["id"], name) if it["name"] else None
            if not existing:
                name = self.db.unique_config_name(user["id"], name)
            cfg, created = self.db.add_config(user["id"], name, params, it["tf"])
            self._fp.pop(cfg["id"], None)
            added.append(f"{'➕' if created else '♻️ обновлён'} <b>#{cfg['id']} {esc(cfg['name'])}</b>"
                         + (f" <i>(пропущены поля: {esc(', '.join(ignored[:6]))})</i>" if ignored else ""))
        out = []
        if added:
            out.append("Принято:\n" + "\n".join(added) + "\n\nСчитаю статистику и пришлю карточку конфига…")
        if errors:
            out.append("❌ Не принято:\n" + "\n".join(errors))
        self.send(chat, "\n\n".join(out))
        if added:
            self._wake.set()

    # ---------------- команды (все — только по конфигам самого пользователя) ----------------
    def _cfg(self, chat, user, ref):
        c = self.db.find_config(user["id"], ref)
        if not c:
            self.send(chat, "Конфиг не найден. /list — список." if ref else "Укажите номер или имя конфига: /list")
        return c

    def cmd_monitor(self, chat, user, arg):
        """Мониторинг в чате: список стратегий с позициями, карточки, автомод, пауза, live — то же, что страница мониторинга на сайте."""
        self.ui.open(chat, user, "m")

    cmd_m = cmd_monitor

    def cmd_panic(self, chat, user, arg):
        """Аварийная кнопка: подтверждение → автомод выкл. везде, все позиции закрыть, все ордера снять."""
        self.ui.open(chat, user, "cpn")

    def cmd_tradelog(self, chat, user, arg):
        """Последние записи лога автоторговли по вашему аккаунту (ошибки биржи, решения автомода, запросы/ответы). Полный файл — trading.log рядом с базой."""
        n = int(arg) if arg.strip().isdigit() else 40
        rows = read_tail(user["id"], min(max(n, 1), 200), self.db.path)
        if not rows:
            self.send(chat, "Лог автоторговли по вашему аккаунту пока пуст.")
            return
        self.tg.send_document(chat, "trading-log.jsonl", ("\n".join(rows) + "\n").encode("utf-8"), caption=f"Лог автоторговли: последние {len(rows)} записей (JSON по строке)")

    def cmd_size(self, chat, user, arg):
        """Размер входа конфига: /size 3 — экран с кнопками; /size 3 10 5 — 10% депозита с плечом 5×; /size 3 10 — только доля."""
        parts = arg.replace(",", ".").replace("%", " ").replace("x", " ").replace("×", " ").split()
        if not parts or not parts[0].lstrip("#").isdigit():
            self.send(chat, "Укажите конфиг: /size № [доля % [плечо]]\nПример: /size 3 10 5 — 10% депозита с плечом 5×. Экран с кнопками: /size 3")
            return
        c = self._cfg(chat, user, parts[0].lstrip("#"))
        if not c:
            return
        if len(parts) == 1:
            self.ui.open(chat, user, f"sz:{c['id']}")
            return
        try:
            vals = [float(x) for x in parts[1:3]]
        except ValueError:
            self.send(chat, "Доля и плечо — числа: /size 3 10 5")
            return
        new = {"trade_pct": vals[0]} | ({"leverage": vals[1]} if len(vals) > 1 else {})
        try:
            r = manage.set_sizing(self.db, self.adapter_for(user["id"]), user["id"], c["id"], **new)
        except manage.ManageError as e:
            self.send(chat, f"⚠️ {esc(str(e))}")
            return
        self.forget(c["id"])
        self.send(chat, f"✅ <b>#{c['id']} {esc(c['name'])}</b>: теперь {r['trade_pct']:g}% депозита × {r['leverage']:g}× (объём {r['trade_pct'] * r['leverage']:g}% депозита). Новые входы пойдут с этим размером; открытые позиции не меняются.")

    def cmd_catchup(self, chat, user, arg):
        """/catchup № on|off [минут [% движения]] — при включении автомода бот входит по свежему сигналу: не старше N минут, цена не ушла в прибыль больше чем на X %."""
        parts = arg.replace(",", ".").replace("%", " ").split()
        if len(parts) < 2 or parts[1].lower() not in ("on", "off", "вкл", "выкл"):
            self.send(chat, "Использование: /catchup № on|off [минут [%]]\nПример: /catchup 3 on 15 0.1 — догонять сигнал не старше 15 мин, если цена не ушла в прибыль больше чем на 0.1%.")
            return
        c = self._cfg(chat, user, parts[0].lstrip("#"))
        if not c:
            return
        on = parts[1].lower() in ("on", "вкл")
        try:
            age = float(parts[2]) if len(parts) > 2 else None
            mv = float(parts[3]) if len(parts) > 3 else None
            cu = manage.set_catchup(self.db, self.adapter_for(user["id"]), user["id"], c["id"], on, age, mv)
        except ValueError:
            self.send(chat, "Минуты и процент — числа: /catchup 3 on 15 0.1")
            return
        except manage.ManageError as e:
            self.send(chat, f"⚠️ {esc(str(e))}")
            return
        self.send(chat, f"⏩ <b>#{c['id']} {esc(c['name'])}</b>: догнать свежий сигнал — {'ВКЛ' if cu['on'] else 'выкл'} (не старше {cu['age_min']:g} мин, цена не дальше {cu['move_pct']:g}% в прибыль)."
                  + (" Автомод «открывать» включён — проверяю сигнал при ближайшей проверке." if cu["armed"] else (" Сработает при включении автомода «открывать»." if cu["on"] else "")))

    def cmd_digest(self, chat, user, arg):
        """Регулярная сводка: /digest daily — каждый день, /digest weekly — по понедельникам, /digest off — выключить, /digest now — прислать сейчас."""
        a = arg.strip().lower()
        if a == "now":
            mode = user.get("digest") if user.get("digest") in digest_mod.PERIOD_H else "daily"
            self.send(chat, digest_mod.build_digest(self.db, self.engine.store, self.s.defaults, user, self.adapters_for(user["id"]), self.engine.available, mode))
            return
        if a in ("off", "daily", "weekly"):
            self.db.set_digest(user["id"], a)
            hour = int(os.getenv("DIGEST_HOUR_UTC", "8") or 8)
            self.send(chat, {"off": "Сводка выключена.", "daily": f"Сводка включена: каждый день в {hour:02d}:00 UTC.", "weekly": f"Сводка включена: по понедельникам в {hour:02d}:00 UTC."}[a])
            return
        cur = user.get("digest") or "off"
        self.send(chat, f"Сводка сейчас: <b>{cur}</b>.\n/digest daily — каждый день\n/digest weekly — по понедельникам\n/digest off — выключить\n/digest now — прислать сейчас")

    def cmd_export(self, chat, user, arg):
        """Выгрузка в CSV: /export trades (сделки стратегий, расчёт), /export log (журнал автоторговли), /export fills (реальные исполнения на бирже)."""
        kind, _, which = arg.strip().lower().partition(" ")
        if kind == "trades":
            items = []
            for c in self.db.list_configs(user["id"]):
                try:
                    items.append((c["name"], cached_result(self.engine.store, self.s.defaults, c["params"], self.engine.available)))
                except (ParamError, FileNotFoundError, KeyError, ValueError):
                    continue
            body, name = exporter.trades_csv(items), "maverick-trades.csv"
        elif kind in ("log", "autolog"):
            body, name = exporter.autolog_csv(self.db.list_auto_log(user["id"], 500)), "maverick-autolog.csv"
        elif kind == "fills":
            exname = self.EX_ALIASES.get(which.strip() or "hl")
            if not exname:
                self.send(chat, "Биржа: hl (Hyperliquid) или bybit — например, /export fills bybit.")
                return
            ad = self.adapter_for(user["id"], exname)
            if not ad.real:
                self.send(chat, f"Счёт {EXCHANGE_LABELS[exname]} не подключён ({'/hl' if exname == 'hyperliquid' else '/bybit'}) — реальных исполнений нет.")
                return
            try:
                body, name = exporter.fills_csv(ad.fills(500)), "maverick-fills.csv"
            except Exception as e:
                self.send(chat, f"⚠️ Не удалось получить исполнения: {esc(str(e)[:200])}")
                return
        else:
            self.send(chat, "Что выгрузить?\n/export trades — сделки стратегий (расчёт)\n/export log — журнал автоторговли\n/export fills [hl|bybit] — реальные исполнения на бирже")
            return
        self.tg.send_document(chat, name, body.encode("utf-8"), caption=name)

    def _send_digests(self):
        """Раз в цикл: кому пора — присылаем сводку (daily / weekly, час — DIGEST_HOUR_UTC)."""
        hour = int(os.getenv("DIGEST_HOUR_UTC", "8") or 8)
        now = self.clock()
        for u in self.db.list_users():
            if u["disabled"] or not u["telegram_id"] or not digest_mod.due(u["digest"], u["digest_ts"], now, hour):
                continue
            try:
                text = digest_mod.build_digest(self.db, self.engine.store, self.s.defaults, u, self.adapters_for(u["id"]), self.engine.available, u["digest"], now)
                self.send(u["telegram_id"], text)
            except Exception:
                log.exception("сводка для %s", u["login"])
            self.db.mark_digest_sent(u["id"], now)                   # и при сбое: не пытаться каждую минуту

    def cmd_hl(self, chat, user, arg):
        """Подключение счёта Hyperliquid (адрес кошелька + ключ API-кошелька) и состояние счёта."""
        self.ui.open(chat, user, "x")

    # ---------------- подключение счёта Hyperliquid ----------------
    HL_TTL = 600

    def start_hl_connect(self, chat, user, testnet=True):
        self._by.pop(user["id"], None)
        self._hl[user["id"]] = {"step": "wallet", "testnet": bool(testnet), "until": time.time() + self.HL_TTL, "tries": 0}
        net = "тестовая сеть" if testnet else "БОЕВАЯ сеть"
        self.tg.send_message(chat, f"🔌 <b>Подключение Hyperliquid</b> ({net})\n\nШаг 1 из 2: пришлите <b>адрес основного кошелька</b> Hyperliquid (0x… и 40 символов) — это счёт, на котором лежат средства.\n\n"
                                   "Дальше я попрошу <b>ключ API-кошелька</b>: он умеет торговать, но не выводить средства (Hyperliquid → More → API). Ключ ОСНОВНОГО кошелька присылать нельзя — я его не приму.\n"
                                   "Ваши сообщения с адресом и ключом я удалю из чата сразу после обработки; ключ хранится в базе зашифрованным. Отмена — кнопка ниже или /cancel.",
                             keyboard=[[("Отмена", "hlx")]])

    def cancel_hl(self, user):
        """Отменяет незавершённое подключение счёта (Hyperliquid или Bybit)."""
        a, b = self._hl.pop(user["id"], None), self._by.pop(user["id"], None)
        return a is not None or b is not None

    def _delete(self, chat, msg):
        mid = (msg or {}).get("message_id")
        try:
            return bool(mid) and bool(self.tg.delete_message(chat, mid))
        except (TelegramError, AttributeError):
            return False

    def _hl_pending(self, chat, user, msg, text, cmd):
        """Обрабатывает сообщение, если ждём данные счёта. True — сообщение съедено сценарием (команды сценарий отменяют)."""
        st = self._hl.get(user["id"])
        if not st:
            return False
        if time.time() > st["until"]:
            self._hl.pop(user["id"], None)
            self.send(chat, "⌛ Время подключения вышло. Начните заново: /hl")
            return False
        if cmd:
            self._hl.pop(user["id"], None)
            if cmd == "/cancel":
                self.send(chat, "Подключение счёта отменено.")
                return True
            return False
        tokens = text.split()
        if not tokens:
            return True
        secret_here = len(tokens) >= 2 or st["step"] == "key"
        deleted = self._delete(chat, msg) if secret_here or st["step"] == "wallet" else False          # ключ в чате не оставляем ни при каких условиях
        if st["step"] == "wallet" and len(tokens) == 1:
            wallet = tokens[0]
            if not wallet.startswith("0x") or len(wallet) != 42:
                self.send(chat, "Адрес должен быть вида <code>0x…</code> (42 символа). Пришлите ещё раз или нажмите «Отмена».")
                return True
            st.update(wallet=wallet, step="key")
            self.send(chat, "Шаг 2 из 2: пришлите <b>ключ API-кошелька</b> (64 символа, можно с 0x). Сообщение с ключом я удалю сразу.", keyboard=[[("Отмена", "hlx")]])
            return True
        wallet, key = (tokens[0], tokens[1]) if len(tokens) >= 2 else (st.get("wallet", ""), tokens[0])
        st["tries"] += 1
        try:
            res = verify_hyperliquid(wallet, key, st["testnet"], user_id=user["id"])
        except ValueError as e:
            if st["tries"] >= 5:
                self._hl.pop(user["id"], None)
                self.send(chat, f"❌ {esc(str(e))}\n\nСлишком много попыток — подключение отменено. Начните заново: /hl")
                return True
            st.update(wallet=wallet, step="key")
            self.send(chat, f"❌ {esc(str(e))}\n\nПришлите ключ ещё раз (или «Отмена»).", keyboard=[[("Отмена", "hlx")]])
            return True
        try:
            enc = encrypt(key if key.startswith("0x") else "0x" + key, self.db.path)
        except SecretError as e:
            self._hl.pop(user["id"], None)
            self.send(chat, f"⚠️ Не удалось сохранить ключ: {esc(str(e))}")
            return True
        self.db.set_exchange_account(user["id"], "hyperliquid", wallet, enc, st["testnet"], res["agent"])
        forget_adapter(user["id"])
        self._hl.pop(user["id"], None)
        self.db.add_auto_log(user["id"], None, "exchange_connected", None, None, "info", {"exchange": "hyperliquid", "testnet": st["testnet"], "wallet": mask_address(wallet)})
        trade_log("account_connected", user=user["id"], exchange="hyperliquid", testnet=st["testnet"], wallet=wallet, agent=res["agent"], message_deleted=deleted)
        eq = res["account"].get("equity")
        self.send(chat, f"✅ <b>Hyperliquid подключён</b> ({'тестовая сеть' if st['testnet'] else '<b>БОЕВАЯ сеть</b>'})\nКошелёк <code>{mask_address(wallet)}</code> · API-кошелёк <code>{mask_address(res['agent'])}</code>"
                        + (f"\nКапитал: {fm.num(eq, 2)} $" if eq is not None else "")
                        + ("\n🧹 Ваши сообщения с адресом и ключом удалены из чата." if deleted else "\n⚠️ Не удалось удалить ваше сообщение с ключом — удалите его вручную (у себя и «у всех»).")
                        + "\n\nАвтомод по-прежнему выключен у всех конфигов: включайте его в /monitor. Счёт и позиции видны и на сайте (страница «Мониторинг»).",
                  )
        return True

    # ---------------- подключение счёта Bybit (только USDT-фьючерсы) ----------------
    BY_NETS = {"live": "БОЕВАЯ сеть", "testnet": "тестовая сеть (testnet.bybit.com)", "demo": "демо-торговля (виртуальные деньги на боевом аккаунте)"}

    def start_bybit_connect(self, chat, user, net="testnet"):
        net = net if net in self.BY_NETS else "testnet"
        self._hl.pop(user["id"], None)
        self._by[user["id"]] = {"step": "key", "net": net, "until": time.time() + self.HL_TTL, "tries": 0}
        keys = {"live": "Bybit → профиль → API → Create New Key", "testnet": "testnet.bybit.com → профиль → API → Create New Key",
                "demo": "на bybit.com переключитесь в Demo Trading → профиль → API → Create New Key"}[net]
        self.tg.send_message(chat, f"🔌 <b>Подключение Bybit</b> — {self.BY_NETS[net]}. Только <b>USDT-фьючерсы</b> (спот и прочее бот не трогает).\n\n"
                                   f"Создайте ключ: {keys}. Права: только торговля контрактами (<b>Orders, Positions</b>), <b>БЕЗ права вывода</b> (Withdraw) — ключ с выводом я не приму. Лучше ещё привязать ключ к IP сервера бота.\n\n"
                                   "Шаг 1 из 2: пришлите <b>API-ключ</b>. Дальше я попрошу секрет. Можно одним сообщением: ключ, пробел, секрет.\n"
                                   "Ваши сообщения я удалю из чата сразу после обработки; секрет хранится в базе зашифрованным. Отмена — кнопка ниже или /cancel.",
                             keyboard=[[("Отмена", "byx")]])

    def _by_pending(self, chat, user, msg, text, cmd):
        """Сообщение в сценарии подключения Bybit: API-ключ, затем секрет. True — сообщение съедено сценарием (команды сценарий отменяют)."""
        st = self._by.get(user["id"])
        if not st:
            return False
        if time.time() > st["until"]:
            self._by.pop(user["id"], None)
            self.send(chat, "⌛ Время подключения вышло. Начните заново: /bybit")
            return False
        if cmd:
            self._by.pop(user["id"], None)
            if cmd == "/cancel":
                self.send(chat, "Подключение счёта отменено.")
                return True
            return False
        tokens = text.split()
        if not tokens:
            return True
        deleted = self._delete(chat, msg)                                      # ни ключ, ни секрет в чате не оставляем
        if st["step"] == "key" and len(tokens) == 1:
            if not BYBIT_KEY.match(tokens[0]):
                self.send(chat, "API-ключ Bybit — 10–40 латинских букв и цифр. Пришлите ещё раз или нажмите «Отмена».", keyboard=[[("Отмена", "byx")]])
                return True
            st.update(key=tokens[0], step="secret")
            self.send(chat, "Шаг 2 из 2: пришлите <b>секрет</b> (API Secret). Сообщение с ним я удалю сразу.", keyboard=[[("Отмена", "byx")]])
            return True
        key, secret = (tokens[0], tokens[1]) if len(tokens) >= 2 else (st.get("key", ""), tokens[0])
        st["tries"] += 1
        try:
            res = verify_bybit(key, secret, st["net"], user_id=user["id"])
        except ValueError as e:
            if st["tries"] >= 5:
                self._by.pop(user["id"], None)
                self.send(chat, f"❌ {esc(str(e))}\n\nСлишком много попыток — подключение отменено. Начните заново: /bybit")
                return True
            st.update(key=key if BYBIT_KEY.match(key) else "", step="secret" if BYBIT_KEY.match(key) else "key")
            self.send(chat, f"❌ {esc(str(e))}\n\nПришлите " + ("секрет" if st["step"] == "secret" else "API-ключ и секрет") + " ещё раз (или «Отмена»).", keyboard=[[("Отмена", "byx")]])
            return True
        try:
            enc = encrypt(secret, self.db.path)
        except SecretError as e:
            self._by.pop(user["id"], None)
            self.send(chat, f"⚠️ Не удалось сохранить секрет: {esc(str(e))}")
            return True
        net = st["net"]
        self.db.set_exchange_account(user["id"], "bybit", key, enc, net != "live", None, net=net)
        forget_adapter(user["id"], "bybit")
        self._by.pop(user["id"], None)
        self.db.add_auto_log(user["id"], None, "exchange_connected", None, None, "info", {"exchange": "bybit", "net": net, "key": mask_address(key)})
        trade_log("account_connected", user=user["id"], exchange="bybit", net=net, api_key=mask_address(key), message_deleted=deleted)
        eq = res["account"].get("equity")
        self.send(chat, f"✅ <b>Bybit подключён</b> — {self.BY_NETS[net] if net != 'live' else '<b>БОЕВАЯ сеть</b>'}, USDT-фьючерсы\nAPI-ключ <code>{mask_address(key)}</code>"
                        + (f"\nКапитал: {fm.num(eq, 2)} $" if eq is not None else "")
                        + ("\n🧹 Ваши сообщения с ключом и секретом удалены из чата." if deleted else "\n⚠️ Не удалось удалить ваше сообщение с секретом — удалите его вручную (у себя и «у всех»).")
                        + "\n\nАвтомод на Bybit выключен у всех конфигов: включайте его в /monitor (карточка конфига → Bybit). Настройки размера и лимитного входа для биржи: /ex. "
                          "Код проверен только на имитации биржи — сначала прогоните его в тестовой сети или демо.")
        return True

    def cmd_bybit(self, chat, user, arg):
        """Подключение счёта Bybit (API-ключ и секрет) и состояние счёта."""
        self.ui.open(chat, user, "xb")

    EX_ALIASES = {"hl": "hyperliquid", "hyper": "hyperliquid", "hyperliquid": "hyperliquid", "by": "bybit", "bb": "bybit", "bybit": "bybit"}
    EX_KEYS = ("trade_pct", "leverage", "pyramid_pct", "pyramid_leverage", "entry_limit_pct", "entry_limit_dir", "entry_limit_ttl_h")

    def cmd_ex(self, chat, user, arg):
        """/ex № [hl|bybit [ключ=значение …| reset]] — настройки конфига отдельно для биржи (поверх параметров конфига): trade_pct, leverage, pyramid_pct, pyramid_leverage,
        entry_limit_pct, entry_limit_dir (pullback|breakout), entry_limit_ttl_h; «-» вместо значения убирает настройку (брать из конфига), reset — убирает все."""
        parts = arg.replace(",", ".").split()
        if not parts or not parts[0].lstrip("#").isdigit():
            self.send(chat, "Настройки конфига отдельно для биржи:\n/ex № — показать\n/ex № bybit trade_pct=10 leverage=5 — 10% депозита с плечом 5× только на Bybit\n"
                            "/ex № hl entry_limit_pct=0.3 entry_limit_dir=pullback entry_limit_ttl_h=4 — вход лимиткой на Hyperliquid\n"
                            "/ex № bybit trade_pct=- — убрать настройку (брать из конфига); /ex № bybit reset — убрать все\n"
                            f"Ключи: {', '.join(self.EX_KEYS)}.\nСигналы и правила выхода (стоп, тейки, БУ) общие — из конфига.")
            return
        c = self._cfg(chat, user, parts[0].lstrip("#"))
        if not c:
            return
        if len(parts) == 1:
            self.ui.open(chat, user, f"cx:{c['id']}")
            return
        name = self.EX_ALIASES.get(parts[1].lower())
        if not name:
            self.send(chat, "Биржа: hl (Hyperliquid) или bybit.")
            return
        patch = {}
        for tok in parts[2:]:
            if tok.lower() == "reset":
                patch = {k: None for k in self.EX_KEYS}
                break
            k, sep, v = tok.partition("=")
            if not sep or k not in self.EX_KEYS:
                self.send(chat, f"Не понял «{esc(tok)}». Формат: ключ=значение, ключи: {', '.join(self.EX_KEYS)}.")
                return
            if v in ("-", "none", "default"):
                patch[k] = None
            elif k == "entry_limit_dir":
                patch[k] = v
            else:
                try:
                    patch[k] = float(v)
                except ValueError:
                    self.send(chat, f"{esc(k)}: нужно число.")
                    return
        if not patch:
            self.ui.open(chat, user, f"cx:{c['id']}")
            return
        try:
            over = manage.set_ex_over(self.db, self.adapter_for(user["id"], name), user["id"], c["id"], name, **patch)
        except manage.ManageError as e:
            self.send(chat, f"⚠️ {esc(str(e))}")
            return
        self.send(chat, f"✅ <b>#{c['id']} {esc(c['name'])}</b> на {EXCHANGE_LABELS[name]}: " + (fm.over_line(over) if over else "настройки биржи убраны — берётся из конфига") + ". Действует на новые входы на этой бирже; открытые позиции не меняются.")

    def cmd_list(self, chat, user, arg):
        self.send(chat, fm.list_message(self.db.list_configs(user["id"])))

    def cmd_remove(self, chat, user, arg):
        c = self._cfg(chat, user, arg)
        if c:
            try:
                manage.remove_config(self.db, user["id"], c["id"], self.adapters_for(user["id"]))
            except manage.ManageError as e:
                self.send(chat, f"⚠️ {esc(str(e))}")
                return
            self._fp.pop(c["id"], None)
            self.send(chat, f"🗑 Конфиг <b>{esc(c['name'])}</b> удалён. Открытая на бирже позиция (если была) осталась со своими стопом и тейками — бот её больше не ведёт.")

    def cmd_pause(self, chat, user, arg):
        c = self._cfg(chat, user, arg)
        if c:
            self.db.update_config(c["id"], paused=True)
            self.send(chat, f"⏸ <b>{esc(c['name'])}</b> на паузе — сигналы не приходят. /resume {c['id']}")

    def cmd_resume(self, chat, user, arg):
        c = self._cfg(chat, user, arg)
        if c:
            self.db.update_config(c["id"], paused=False, baseline=False, last_ts=0)      # за время паузы сигналы не накапливаем
            self._fp.pop(c["id"], None)
            self.send(chat, f"▶️ <b>{esc(c['name'])}</b> снова активен. Сигналы, пропущенные на паузе, не присылаются.")
            self._wake.set()

    def cmd_rename(self, chat, user, arg):
        ref, _, new = arg.partition(" ")
        c = self._cfg(chat, user, ref)
        if not c:
            return
        new = new.strip()
        if not new:
            self.send(chat, "Формат: /rename &lt;№|имя&gt; &lt;новое имя&gt;")
            return
        try:
            self.db.update_config(c["id"], name=new[:60])
        except DbError:
            self.send(chat, "Такое имя уже занято.")
            return
        self.send(chat, f"✏️ Теперь: <b>#{c['id']} {esc(new[:60])}</b>")

    def cmd_get(self, chat, user, arg):
        c = self._cfg(chat, user, arg)
        if c:
            body = json.dumps({"format": "maverick-config", "version": 1, "name": c["name"], "tf": c["tf"], "params": c["params"]}, ensure_ascii=False, indent=2)
            try:
                self.tg.send_document(chat, f"maverick-config_{c['id']}.json", body.encode("utf-8"), caption=c["name"])
            except TelegramError as e:
                self.send(chat, f"Не удалось отправить файл: {esc(str(e))}")

    def cmd_stats(self, chat, user, arg):
        c = self._cfg(chat, user, arg)
        if not c:
            return
        try:
            res = self.engine.evaluate(c["params"])
        except EvalError as e:
            self.send(chat, f"⚠️ {esc(str(e))}")
            return
        parts = [f"📋 <b>#{c['id']} {esc(c['name'])}</b>\n{esc(fm.config_summary(c))}", fm.stats_block(res["stats"]["total"], c["params"], res["range"]), fm.sizing_line(res)]
        top = res.get("top") or {}
        if top.get("enabled"):
            parts.append(fm.stats_block(top["stats"], c["params"], None, title="🪙 <b>Топ-коины</b>")
                         + (f"\n<i>Окон свечей без данных: {top['pending']['windows']}</i>" if top["pending"]["windows"] else ""))
        self.send(chat, "\n\n".join(parts))

    def cmd_positions(self, chat, user, arg):
        """Открытые позиции по конфигам (без номера — по всем непаузным) и их результат на текущий момент по расчёту бэктеста."""
        if arg.strip():
            c = self._cfg(chat, user, arg.strip())
            cfgs = [c] if c else []
        else:
            cfgs = [c for c in self.db.list_configs(user["id"]) if not c["paused"] and c["baseline"]]
            if not cfgs:
                self.send(chat, "Нет посчитанных конфигов. /list — список.")
                return
        for c in cfgs:
            try:
                res = self.engine.evaluate(c["params"])
            except EvalError as e:
                self.send(chat, f"⚠️ <b>#{c['id']} {esc(c['name'])}</b>: {esc(str(e))}")
                continue
            sym = c["params"]["symbol"]
            last_ts = res["range"]["candles_last_ts"]
            price_now = None
            try:
                price_now = self.engine.price_fn(sym) if self.b.live_price else None
                if not price_now:
                    price_now = self.engine.store.candles(sym)[-1][4]
            except (KeyError, IndexError, FileNotFoundError):
                pass
            self.send(chat, fm.positions_message(c, res, price_now, last_ts))

    cmd_pos = cmd_positions

    def cmd_check(self, chat, user, arg):
        self._force.add(user["id"])
        self._wake.set()
        self.send(chat, "🔎 Проверка запущена — новые сигналы (если есть) придут отдельными сообщениями. Если идёт долгий подсчёт, он сначала завершится: остановить — /cancel.")

    def cmd_cancel(self, chat, user, arg):
        """Останавливает текущий подсчёт/загрузку свечей. Конфиги, которые ещё не были посчитаны до конца, ставятся на паузу (вернуть: /resume)."""
        if self._cycle_lock.locked():                       # прерывать есть что, только если подсчёт сейчас идёт
            self._cancel.add(user["id"])
        paused = []
        for c in self.db.list_configs(user["id"]):
            if not c["baseline"] and not c["paused"]:
                self.db.update_config(c["id"], paused=True)
                paused.append(f"#{c['id']} {esc(c['name'])}")
        self.send(chat, "⛔ Остановил подсчёт." + (f"\nНа паузе (ещё не посчитаны): {', '.join(paused)}. Вернуть по одному: /resume &lt;№&gt;." if paused else ""))

    def cmd_status(self, chat, user, arg):
        rows = ["📡 <b>Состояние</b>", f"Аккаунт: <b>{esc(user['login'])}</b>"]
        for src in self.s.sources.values():
            try:
                last = self.engine.store.latest(src.id)
            except (FileNotFoundError, KeyError):
                last = None
            rows.append(f"{esc(src.label)}: " + (f"последний отчёт {fm.ts_str(last['ts'])} (DG {fm.num(last['dg'])} · DL {last['dl']:.2f} · DS {last['ds']:.2f})" if last else "нет данных"))
        if self.sync:
            snap = self.sync.snapshot()
            if snap["enabled"]:
                for x in snap["sources"]:
                    rows.append(f"Подгрузка {esc(x['label'])}: {x['status']}" + (f" — {esc(str(x['error'])[:150])}" if x["error"] else ""))
            else:
                rows.append(f"Подгрузка сообщений выключена: {esc(snap['reason'] or '')}")
        else:
            rows.append("Подгрузка сообщений: за неё отвечает backend (или файлы истории обновляются вручную).")
        rows.append(f"Ваших конфигов: {len(self.db.list_configs(user['id']))} · цикл каждые {self.b.poll_sec:g} с"
                    + (f" · последний цикл {fm.ts_str(self.last_cycle * 1000)}" if self.last_cycle else ""))
        self.send(chat, "\n".join(rows))

    # ---------------- цикл проверки сигналов ----------------
    def cycle(self, force=False, only_user=None):
        """Проходит по конфигам всех привязанных пользователей. Одинаковые параметры считаются один раз за цикл.
        Сначала уже посчитанные конфиги (их сигналы не ждут чужих долгих загрузок), потом новые. Перед каждым конфигом он читается из базы
        заново, поэтому /pause, /remove и /cancel действуют до того, как до конфига дойдёт очередь."""
        with self._cycle_lock:
            self._beat("bot_cycle_start")
            try:
                self._tick_pending()
            except Exception:
                log.exception("ожидающие входы")
            forced, self._force = self._force, set()
            users = {u["id"]: u for u in self.db.list_users() if u["telegram_id"] and not u["disabled"]}
            memo = {"eval": {}, "candles": {}, "top": {}}
            todo = sorted(self.db.list_configs(), key=lambda c: (not c["baseline"], c["id"]))
            for stale in todo:
                cfg = self.db.get_config(stale["id"])
                user = users.get(stale["user_id"])
                if not cfg or not user or cfg["paused"] or (only_user and cfg["user_id"] != only_user):
                    continue
                if user["id"] in self._cancel:
                    continue
                try:
                    self._process(cfg, user, force or user["id"] in forced, memo)
                except Cancelled:
                    log.info("подсчёт %s отменён пользователем %s", cfg["name"], user["login"])
                except Exception as e:                       # сбой одного конфига не останавливает остальные
                    log.exception("конфиг %s (%s)", cfg["name"], user["login"])
                    err = (str(e) or type(e).__name__)[:300]
                    if err != cfg["error"]:                  # о новой ошибке сообщаем один раз, а не каждый цикл
                        self.send(user["telegram_id"], f"⚠️ <b>#{cfg['id']} {esc(cfg['name'])}</b>: {esc(err)}\nПовторю автоматически; остановить — /cancel или /pause {cfg['id']}.")
                    self.db.update_config(cfg["id"], error=err)
            self._download_top(memo, users)
            self._cancel.clear()
        self.last_cycle = self.clock()
        self._beat("bot_cycle")
        try:
            self._send_digests()
        except Exception:
            log.exception("сводки")

    def _beat(self, key):
        """Пульс для сторожа (watchdog.py): время последнего цикла."""
        try:
            self.db.set_meta(key, self.clock())
        except Exception:
            log.warning("не записать пульс %s", key)

    def _evaluate(self, params, memo, user):
        """Свечи актива обновляются и расчёт делается один раз на одинаковые параметры, даже если они у разных пользователей."""
        key = json.dumps(params, sort_keys=True, default=str)
        if key not in memo["eval"]:
            sym = params["symbol"]
            if sym not in memo["candles"]:
                if not self.engine.store.has_candles(sym):
                    self.send(user["telegram_id"], f"⏳ Свечей <b>{esc(sym)}</b> ещё нет — скачиваю историю с первого отчёта. "
                                                   "Первый раз это занимает несколько минут (по одной загрузке на актив). Остановить: /cancel")

                def progress(count, ts):
                    if user["id"] in self._cancel:
                        raise Cancelled()

                memo["candles"][sym] = self.engine.refresh_candles(sym, progress)
            memo["eval"][key] = (self.engine.evaluate(params), memo["candles"][sym])
        return memo["eval"][key]

    def _process(self, cfg, user, force, memo):
        p, chat = cfg["params"], user["telegram_id"]
        fp = (fingerprint(self.s, p), int(self.clock() // FILL_CHECK_SEC))     # исполнения зависят от свечей, которые обновляются независимо от истории отчётов — пересчёт раз в 5 минут
        if not force and cfg["baseline"] and self._fp.get(cfg["id"]) == fp and not cfg["catchup"]["armed"]:
            return
        res, warn = self._evaluate(p, memo, user)
        if any(cid == cfg["id"] for cid, _ in self._retry_ids):  # вход, который не удался до перезапуска (на какой-то бирже)
            self._retry_ids = {x for x in self._retry_ids if x[0] != cfg["id"]}
            self._retry_open(cfg, res, chat)
        label = self.s.sources[p["source"]].label
        upd = {"error": None}
        if not cfg["baseline"]:
            # первый расчёт: фиксируем текущее состояние, чтобы не засыпать старыми сигналами, и присылаем карточку конфига
            upd.update(last_ts=max([s["ts"] for s in res["last_signals"] if not s["trend_blocked"]], default=0), baseline=True, ready=self._ready_key(res),
                       fill_ts=max([e["ts"] for e in self.engine.fill_events(res)], default=0))
            self.send(chat, fm.accepted_message(cfg, res))
        else:
            fresh = self.engine.new_signals(res, cfg["last_ts"], self.engine.price_fn(p["symbol"]) if self.b.live_price else None)
            seen = [s["ts"] for s in res["last_signals"] if not s["trend_blocked"]]
            if seen:
                upd["last_ts"] = max(cfg["last_ts"], max(seen))         # повторные подтверждения серии тоже «просмотрены»
            sent = 0
            for sig in fresh:
                age_min = (self.clock() * 1000 - sig["ts"]) / 60000
                if age_min > self.b.max_age_min:
                    log.info("сигнал %s по %s устарел (%.0f мин) — пропущен", sig["ts"], cfg["name"], age_min)
                    continue
                report = self.engine.report(p, sig["ts"])
                entry = self.engine.price_fn(p["symbol"]) if self.b.live_price else None
                plan = self.engine.plan(sig["side"], entry, p["tps"], p["sl_pct"]) if entry else None
                top = self.engine.top_plan(res, sig, report) if p.get("top_enabled") else None
                self.send(chat, fm.signal_message(cfg, res, sig, report, plan, entry, top, label, stale_note=warn))
                self._auto_open(cfg, res, sig, plan)
                sent += 1
            if sent:
                upd["alerts"] = cfg["alerts"] + sent
            self._send_fills(cfg, res, chat, label, upd)             # исполнения в счётчик «сигналов» (alerts) не входят
            self._send_ready(cfg, res, chat, label, upd, entered=sent > 0)
        self.db.update_config(cfg["id"], **upd)
        if cfg["catchup"]["armed"]:                                  # «догнать свежий сигнал»: один заход после включения автомода
            self.db.set_catchup(cfg["user_id"], cfg["id"], armed=False)
            if cfg["any_open"] and cfg["catchup"]["on"]:
                self._catch_up(cfg, res, chat)
        self._fp[cfg["id"]] = fp
        # окна свечей топ-коинов для статистики докачиваются в конце цикла, с ограничением по времени (карточка и сигналы от них не зависят)
        if p.get("top_enabled"):
            wins, _ = self.engine.pending_windows(res, self.b.top_windows)
            for w in wins:
                memo["top"].setdefault(w, set()).add(cfg["id"])

    @staticmethod
    def _ready_key(res):
        """Состояние «ждёт подтверждения»: 'long|cool,liq' (несколько сторон через ';'), пусто — ничего не ждёт."""
        return ";".join(f"{x['side']}|{','.join(x['waiting'])}{'|add' if x.get('add') else ''}" for x in res.get("ready_now") or [])

    def _send_ready(self, cfg, res, chat, label, upd, entered):
        """Сообщение, когда конфиг готов к входу и ждёт фильтр (откат / ликвидации / тренд), и когда набор ожидаемых условий меняется;
        при отмене (серия прервалась без входа) — короткое сообщение. Первое состояние у конфига без отметки фиксируется молча."""
        key = self._ready_key(res)
        prev = cfg.get("ready")
        if prev is None or key == prev:
            if prev is None:
                upd["ready"] = key
            return
        upd["ready"] = key
        if key:
            self.send(chat, fm.ready_message(cfg, res, res["ready_now"], label))
        elif prev and not entered:
            self.send(chat, fm.ready_cancelled_message(cfg, res, prev))

    # ---------------- автомод: действия ----------------
    def _name(self, cfg, ex):
        """Имя конфига в сообщениях; с биржей, если она не единственная (Bybit, либо у конфига включён автомод и на другой бирже)."""
        tag = ex != "hyperliquid" or any(cfg["ex"][x]["open"] or cfg["ex"][x]["manage"] for x in EXCHANGES if x != ex)
        return f"<b>#{cfg['id']} {esc(cfg['name'])}</b>" + (f" <i>[{EXCHANGE_LABELS.get(ex, ex)}]</i>" if tag else "")

    def _do(self, cfg, kind, symbol, side, fn, details, ad=None, ex="hyperliquid"):
        """Выполняет действие через адаптер биржи и пишет результат в журнал; ошибка адаптера не ломает цикл бота."""
        ad = ad or self.adapter_for(cfg["user_id"], ex)
        trade_log("auto_decision", user=cfg["user_id"], config=cfg["id"], name=cfg["name"], kind=kind, symbol=symbol, side=side, exchange=ad.name, target=ex, **details)
        try:
            res = fn()
            status = res.get("status", "sent")
            trade_log("auto_result", user=cfg["user_id"], config=cfg["id"], kind=kind, status=status, result=res, target=ex)
        except Exception as e:                                  # NotConfigured и ошибки биржи
            res, status = {"error": str(e)}, "error"
            log.warning("автомод %s по %s (%s): %s", kind, cfg["name"], ex, e)
            trade_error("auto_fail", e, user=cfg["user_id"], config=cfg["id"], name=cfg["name"], kind=kind, symbol=symbol, side=side, target=ex)
        self.db.add_auto_log(cfg["user_id"], cfg["id"], kind, symbol, side, status, {**details, **res, "exchange": ad.name, "target": ex})
        return status, res

    def _auto_open(self, cfg, res, sig, plan, only=None):
        """Автомод «открывать по сигналу»: вход или добор по новому сигналу на КАЖДОЙ бирже, где он включён (объём и лимитный вход — по настройкам конфига для этой биржи).
        only — только на этих биржах. Сбой одной биржи не мешает другой."""
        for name in EXCHANGES:
            if cfg["ex"][name]["open"] and (only is None or name in only):
                try:
                    self._open_on(cfg, res, sig, plan, name)
                except Exception as e:
                    log.exception("автомод %s: вход по %s", name, cfg["name"])
                    self.send(self.db.get_user(cfg["user_id"])["telegram_id"], f"⚠️ {self._name(cfg, name)}: вход не выполнен — {esc(str(e) or type(e).__name__)[:300]}")

    def _open_on(self, cfg, res, sig, plan, ex):
        """Вход или добор по новому сигналу на одной бирже. Перед ордером состояние сверяется с реальной биржей."""
        ecfg = cfg["ex"][ex]
        over = ecfg["over"]
        p = manage.effective_params(res["params"], over)                      # параметры конфига с настройками этой биржи поверх (размер, лимитный вход)
        add = bool(sig.get("pyramid"))
        ad = self.adapter_for(cfg["user_id"], ex)
        base = ecfg.get("base", "config")
        pend = None if add else self.db.get_pending(cfg["id"], ex)
        if pend:                                            # по конфигу уже ждёт вход (лимитка / вход по ходу)
            if pend["side"] == sig["side"]:
                trade_log("auto_skipped", "info", user=cfg["user_id"], config=cfg["id"], symbol=p["symbol"], side=sig["side"], target=ex, reason="вход по прошлому сигналу уже ждёт исполнения")
                return
            self._cancel_pending(pend, cfg, ad, "пришёл обратный сигнал")
        pre = manage.preflight_open(ad, p["symbol"], sig["side"], add)
        if pre.get("skip"):
            self._skip_open(cfg, p, sig, add, ad, pre["skip"], retry=pre.get("retry", False), ex=ex)
            return
        acc = manage.account_quick(ad, cfg["user_id"], ttl=0) if ad.real else None            # свежий баланс: размер и свободная маржа решают, пройдёт ли ордер
        if acc and pre.get("close_first"):
            acc = {**acc, "free": None}                     # встречная позиция сейчас закроется и освободит маржу — свободную маржу до этого не сверяем
        sz = manage.size_order(self.engine.order_size, res, add, ad, base, acc, over)
        if sz.get("skip"):
            self._skip_open(cfg, p, sig, add, ad, sz["skip"], retry=sz.get("retry", False), ex=ex)
            return
        notional, margin = sz["notional"], sz["margin"]
        if pre.get("close_first"):                          # на бирже позиция в обратную сторону: по правилам стратегии это разворот — сначала закрываем её
            st, r = self._do(cfg, "close_reverse", p["symbol"], pre["close_first"], lambda: ad.close_market(p["symbol"], pre["close_first"], 1.0), {"signal_ts": sig["ts"], "note": "разворот перед входом"}, ad=ad, ex=ex)
            if st == "error":                               # встречная позиция не закрылась — открывать новую поверх неё нельзя
                self._open_failed(cfg, sig, add, f"не удалось закрыть встречную позицию: {r.get('error')}", ex)
                return
        if p.get("entry_limit_pct") and not add:
            self._place_entry_order(cfg, p, sig, ad, base, sz, notional, margin, ex)
            return
        st, r = self._do(cfg, "add" if add else "open", p["symbol"], sig["side"],
                         lambda: ad.open_market(p["symbol"], sig["side"], notional, (notional / margin) if margin else p["leverage"],
                                                plan["sl"] if plan else None, [(l["price"], l["share"]) for l in plan["levels"]] if plan else None),
                         {"signal_ts": sig["ts"], "notional": notional, "margin": margin, "leg": sig.get("leg", 1), "size_base": base, "size_note": sz["note"]}, ad=ad, ex=ex)
        if st == "error":
            self._open_failed(cfg, sig, add, r.get("error"), ex)
        else:
            self.db.clear_retry(cfg["id"], ex)

    def _catch_up(self, cfg, res, chat):
        """Автомод включён при включённой опции «догнать сигнал»: входим по ПОСЛЕДНЕМУ сигналу, если он свеж (не старше age_min минут), по расчёту позиция по нему ещё открыта
        и цена не ушла в сторону прибыли больше чем на move_pct % от цены сигнала — иначе вход был бы заметно хуже сигнального.
        Если последний сигнал — ДОБОР к уже открытой по расчёту позиции, догоняется вся позиция: сначала первая нога (по правилам конфига: по рынку или лимиткой), затем сам добор
        (при входе лимиткой добор не ставится — добирать можно только в уже открытую позицию). Цена сверяется с ценой сигнала добора, возраст — тоже по нему."""
        cu, p = cfg["catchup"], res["params"]
        name = f"<b>#{cfg['id']} {esc(cfg['name'])}</b>"
        pending_reason = backtest.LIMIT_PENDING if p.get("entry_limit_pct") else None            # лимитный вход: сигнал, по которому ордер в расчёте ещё ждёт исполнения, — тоже действующий
        taken = [x for x in res["last_signals"] if (x["taken"] or (pending_reason and x.get("reason") == pending_reason)) and not x["trend_blocked"]]
        if not taken:
            self.send(chat, f"⏩ {name}: догонять нечего — среди последних сигналов нет такого, по которому стратегия входила бы.")
            return
        sig = taken[-1]
        trades = res["trades"]
        trade = next((t for t in trades if t["signal_ts"] == sig["ts"]), None)
        avg_owner = next((t for t in trades if sig["ts"] in (t.get("add_signals") or [])), None)       # усреднённая позиция: сигнал добора лежит внутри сделки
        is_add = (trade is not None and trade.get("leg", 1) > 1) or (trade is None and avg_owner is not None)
        age = (self.clock() * 1000 - sig["ts"]) / 60000
        why, ref_px, first = None, None, None
        waiting = trade is None and avg_owner is None and pending_reason and sig.get("reason") == pending_reason
        if trade is None and avg_owner is None and not waiting:
            why = "по последнему сигналу в расчёте нет сделки"
        elif age > cu["age_min"]:
            why = f"последний сигнал {fm.ts_str(sig['ts'])} — {age:.0f} мин назад, а допустимо не больше {cu['age_min']:g}"
        elif is_add:
            held = [t for t in trades if t["reason"] == "OPEN" and t["side"] == sig["side"]]
            if not held:
                why = f"по расчёту стратегии позиция с добором от {fm.ts_str(sig['ts'])} уже закрыта"
            else:
                first = next((t for t in held if t.get("leg", 1) == 1), None)                        # первая нога ещё открыта в расчёте?
                if avg_owner is not None:
                    k = avg_owner["add_signals"].index(sig["ts"]) + 1
                    ref_px = (avg_owner.get("adds") or [{}] * (k + 1))[k].get("price") or avg_owner["entry"]
                else:
                    ref_px = trade["entry"]
        elif waiting:
            ref_px = self._signal_price(p["symbol"], sig["ts"])                                       # ордер ещё ждёт в расчёте: цена сигнала — open свечи входа
        elif trade["reason"] != "OPEN":
            why = f"по расчёту стратегии позиция по сигналу {fm.ts_str(sig['ts'])} уже закрыта ({trade['reason']})"
        else:
            ref_px = trade["entry"]
        price = self.engine.price_fn(p["symbol"]) if self.b.live_price else None
        move = None
        if not why and not ref_px:
            why = "не удалось определить цену сигнала"
        if not why:
            if not price:
                why = "не удалось получить текущую цену — не могу проверить, не ушла ли она"
            else:
                d = 1 if sig["side"] == "long" else -1
                move = d * (price / ref_px - 1) * 100                              # + — цена ушла в сторону прибыли, − — в сторону убытка (вход даже лучше сигнального)
                if move > cu["move_pct"] + 1e-9:
                    why = f"цена уже ушла в сторону прибыли на {move:.2f}% от цены сигнала ({ref_px:g} → {price:g}), допустимо не больше {cu['move_pct']:g}%"
        trade_log("auto_catchup", "warn", user=cfg["user_id"], config=cfg["id"], name=cfg["name"], signal_ts=sig["ts"], side=sig["side"], add=is_add, age_min=round(age, 1), move_pct=None if move is None else round(move, 3), skipped=why)
        if why:
            self.db.add_auto_log(cfg["user_id"], cfg["id"], "catchup", p["symbol"], sig["side"], "skipped", {"signal_ts": sig["ts"], "note": why})
            self.send(chat, f"⏩ {name}: вход по свежему сигналу не выполнен — {esc(why)}.")
            return
        plan = self.engine.plan(sig["side"], price, p["tps"], p["sl_pct"])
        side_ru = "в лонг" if sig["side"] == "long" else "в шорт"
        if not is_add:
            self.send(chat, f"⏩ {name}: догоняю сигнал {side_ru} от {fm.ts_str(sig['ts'])} ({age:.0f} мин назад, цена {price:g}, от цены сигнала {fm.pct(move, 2)}).")
            self._auto_open(cfg, res, {"ts": sig["ts"], "side": sig["side"], "pyramid": False, "leg": 1}, plan)
            return
        first_ts = first["signal_ts"] if first is not None else sig["ts"]
        opens = [x for x in EXCHANGES if cfg["ex"][x]["open"]]
        limit_on = {x for x in opens if manage.effective_params(p, cfg["ex"][x]["over"]).get("entry_limit_pct")}      # на этих биржах первая нога — лимиткой, добор к ещё не открытой позиции не ставится
        self.send(chat, f"⏩ {name}: последний сигнал {side_ru} от {fm.ts_str(sig['ts'])} ({age:.0f} мин назад) — добор к позиции, которая по расчёту открыта. Догоняю позицию: сначала первую ногу"
                        + ("" if first is not None else " (первая нога по расчёту уже закрыта — вхожу текущей открытой частью)") + (", потом добор." if not limit_on else f", потом добор. Вход лимиткой{'' if len(opens) == 1 else ' (' + ', '.join(EXCHANGE_LABELS[x] for x in sorted(limit_on)) + ')'}: добор не ставлю — добирать можно только в открытую позицию."))
        self._auto_open(cfg, res, {"ts": first_ts, "side": sig["side"], "pyramid": False, "leg": 1}, plan)
        market = set(opens) - limit_on
        if first is None or not market:
            return
        self._auto_open(cfg, res, {"ts": sig["ts"], "side": sig["side"], "pyramid": True, "leg": 2}, plan, only=market)

    def _signal_price(self, symbol, ts):
        """Цена сигнала по свечам: open первой свечи не раньше сигнала (или close последней, если свечи входа ещё нет)."""
        try:
            candles = self.engine.store.candles(symbol)
        except (KeyError, FileNotFoundError):
            return None
        if not candles:
            return None
        from bisect import bisect_left
        i = bisect_left([c[0] for c in candles], ts)
        return candles[i][1] if i < len(candles) else candles[-1][4]

    # ---------------- вход лимитным ордером / по ходу цены ----------------
    def _place_entry_order(self, cfg, p, sig, ad, base, sz, notional, margin, ex="hyperliquid"):
        """Сигнал подтверждён, но вход не по рынку, а на X % движения цены от текущей: pullback — лимитный ордер на бирже (откат), breakout — ждём, пока цена дойдёт до уровня по ходу
        (проверяется каждый цикл), и тогда входим по рынку. Ордер живёт entry_limit_ttl_h часов. Стоп и тейки ставятся после исполнения — от цены исполнения.
        p — параметры с настройками этой биржи (entry_limit_*)."""
        d = 1 if sig["side"] == "long" else -1
        mode, pct, ttl = p.get("entry_limit_dir") or "pullback", p["entry_limit_pct"], p.get("entry_limit_ttl_h") or 4.0
        ref = self.engine.price_fn(p["symbol"]) if self.b.live_price else None
        chat = self.db.get_user(cfg["user_id"])["telegram_id"]
        name = self._name(cfg, ex)
        if not ref:
            self._skip_open(cfg, p, sig, False, ad, "не удалось получить текущую цену — лимитный вход не поставлен", retry=True, ex=ex)
            return
        level = ref * (1 - d * pct / 100) if mode == "pullback" else ref * (1 + d * pct / 100)
        lev = (notional / margin) if margin else p["leverage"]
        side_ru = "лонг" if d == 1 else "шорт"
        details = {"signal_ts": sig["ts"], "notional": notional, "margin": margin, "level": level, "ref": ref, "mode": mode, "pct": pct, "ttl_h": ttl, "size_base": base, "size_note": sz["note"]}
        order_id = None
        if mode == "pullback":
            st, r = self._do(cfg, "limit_entry", p["symbol"], sig["side"], lambda: ad.place_limit_entry(p["symbol"], sig["side"], notional, level, lev), details, ad=ad, ex=ex)
            if st == "error":
                self._open_failed(cfg, sig, False, r.get("error"), ex)
                return
            if st == "dry_run":                              # счёт не подключён: лимитку в симуляции не отслеживаем
                self.send(chat, f"🧪 {name}: симуляция — поставил бы лимитку на {side_ru} по {fm.price(level)} ({pct:g}% {'ниже' if d == 1 else 'выше'} цены {fm.price(ref)}), ордер не отправлялся.")
                return
            order_id = r.get("order_id")
        self.db.set_pending(cfg["id"], cfg["user_id"], sig["ts"], p["symbol"], sig["side"], mode, level, ref, notional, margin, lev, order_id, self.clock() + ttl * 3600, exchange=ex)
        self.db.clear_retry(cfg["id"], ex)
        if mode == "pullback":
            self.send(chat, f"⏳ {name}: сигнал на {side_ru} подтверждён — вместо рынка поставил <b>лимитку {fm.price(level)}</b> ({pct:g}% {'ниже' if d == 1 else 'выше'} цены {fm.price(ref)}), объём {fm.usd(notional).lstrip('+')}. "
                          f"Живёт {ttl:g} ч; стоп и тейки встанут после исполнения.")
        else:
            self.send(chat, f"⏳ {name}: сигнал на {side_ru} подтверждён — жду движения цены {pct:g}% {'вверх' if d == 1 else 'вниз'} до <b>{fm.price(level)}</b> (от {fm.price(ref)}); тогда войду по рынку. Ждать буду {ttl:g} ч.")

    def _cancel_pending(self, pend, cfg, ad, reason):
        """Снимает ожидающий вход: лимитку отменяем на бирже; если успела частично исполниться — защищаем то, что набралось."""
        ex = pend.get("exchange", "hyperliquid")
        self.db.clear_pending(pend["config_id"], ex)
        chat = self.db.get_user(pend["user_id"])["telegram_id"]
        name = self._name(cfg, ex) if cfg else f"<b>#{pend['config_id']} ?</b>"
        filled = 0.0
        if pend["order_id"]:
            try:
                ad.cancel_order_id(pend["order_id"], pend["symbol"])
                filled = ad.order_state(pend["order_id"], pend["symbol"]).get("filled") or 0.0
            except Exception as e:
                trade_error("auto_fail", e, user=pend["user_id"], config=pend["config_id"], kind="cancel_entry", symbol=pend["symbol"], side=pend["side"], target=ex)
                self.send(chat, f"⚠️ {name}: не удалось отменить лимитку #{pend['order_id']}: {esc(str(e)[:200])}. Проверьте ордера на бирже вручную.")
                return
        self.db.add_auto_log(pend["user_id"], pend["config_id"], "cancel_entry", pend["symbol"], pend["side"], "sent", {"reason": reason, "order_id": pend["order_id"], "level": pend["level"], "target": ex})
        self.send(chat, f"🚫 {name}: ожидающий вход по {fm.price(pend['level'])} снят — {esc(reason)}.")
        if filled:
            self._protect_filled(pend, cfg, ad, chat, partial=True)

    def _protect_filled(self, pend, cfg, ad, chat, partial=False):
        """Лимитка исполнилась (полностью или частично): берём реальную позицию, ставим стоп и тейки от цены исполнения."""
        ex = pend.get("exchange", "hyperliquid")
        name = self._name(cfg, ex)
        base = pend["symbol"].split("/")[0]
        pos = next((x for x in ad.positions() if (x.get("symbol") or "").split("/")[0] == base and x["side"] == pend["side"]), None)
        if not pos:
            self.send(chat, f"ℹ️ {name}: лимитка исполнена, но открытой позиции на бирже нет (уже закрыта?) — стоп и тейки не ставлю.")
            return
        p = cfg["params"]
        entry = pos.get("entry") or pend["level"]
        plan = self.engine.plan(pend["side"], entry, p["tps"], p["sl_pct"])
        st, r = self._do(cfg, "protect", pend["symbol"], pend["side"], lambda: ad.protect(pend["symbol"], pend["side"], pos["size"], plan["sl"], [(l["price"], l["share"]) for l in plan["levels"]]),
                         {"signal_ts": pend["signal_ts"], "entry": entry, "size": pos["size"], "partial": partial}, ad=ad, ex=ex)
        if st == "error":
            self.send(chat, f"🚨 {name}: лимитка исполнена по {fm.price(entry)}, но стоп/тейки НЕ встали: {esc(str(r.get('error'))[:300])}. Проверьте позицию на бирже!")
            return
        self.send(chat, f"✅ {name}: {'лимитка исполнена частично' if partial else 'лимитка исполнена'} — {'лонг' if pend['side'] == 'long' else 'шорт'} {pos['size']:g} по {fm.price(entry)}. "
                        f"Стоп {fm.price(plan['sl'])}, тейков: {len(plan['levels'])}.")

    def _tick_pending(self):
        """Раз в цикл: ожидающие входы — лимитка исполнилась / истекла / отменена снаружи; вход по ходу — цена дошла до уровня."""
        for row in self.db.list_pending():
            try:
                self._tick_one(row)
            except Exception:
                log.exception("ожидающий вход конфига %s (%s)", row["config_id"], row.get("exchange"))

    def _tick_one(self, row):
        ex = row.get("exchange", "hyperliquid")
        cfg = self.db.get_config(row["config_id"])
        user = self.db.get_user(row["user_id"])
        if not user:
            self.db.clear_pending(row["config_id"], ex)
            return
        ad = self.adapter_for(row["user_id"], ex)
        chat = user["telegram_id"]
        now = self.clock()
        if not cfg or cfg["paused"] or not cfg["ex"][ex]["open"]:
            self._cancel_pending(row, cfg, ad, "конфиг удалён" if not cfg else "конфиг на паузе" if cfg["paused"] else "автомод «открывать» выключен")
            return
        name = self._name(cfg, ex)
        d = 1 if row["side"] == "long" else -1
        if row["order_id"]:                                  # лимитка на бирже
            try:
                st = ad.order_state(row["order_id"], row["symbol"])
            except Exception as e:
                log.warning("состояние ордера %s: %s", row["order_id"], e)
                return
            if st["status"] == "filled":
                self.db.clear_pending(row["config_id"], ex)
                self._protect_filled(row, cfg, ad, chat)
            elif st["status"] == "open" and now >= row["expires"]:
                self._cancel_pending(row, cfg, ad, "не исполнилась за срок жизни ордера")
            elif st["status"] in ("canceled", "unknown"):    # ордера нет: отменён снаружи или исполнился, а статус не отдали — смотрим позицию
                self.db.clear_pending(row["config_id"], ex)
                base = row["symbol"].split("/")[0]
                if any((x.get("symbol") or "").split("/")[0] == base and x["side"] == row["side"] for x in ad.positions()):
                    self._protect_filled(row, cfg, ad, chat)
                else:
                    self.db.add_auto_log(row["user_id"], row["config_id"], "cancel_entry", row["symbol"], row["side"], "info", {"reason": "ордер снят на бирже", "order_id": row["order_id"], "target": ex})
                    self.send(chat, f"🚫 {name}: лимитки {row['order_id']} на бирже больше нет, позиции нет — вход не состоялся.")
            return
        # вход по ходу: ждём цену
        price = self.engine.price_fn(row["symbol"]) if self.b.live_price else None
        if price and ((price >= row["level"]) if d == 1 else (price <= row["level"])):
            self.db.clear_pending(row["config_id"], ex)
            p = manage.effective_params(cfg["params"], cfg["ex"][ex]["over"])
            plan = self.engine.plan(row["side"], price, p["tps"], p["sl_pct"])
            sig = {"ts": row["signal_ts"], "side": row["side"], "pyramid": False, "leg": 1}
            self.send(chat, f"▶️ {name}: цена дошла до {fm.price(row['level'])} ({fm.price(price)}) — вхожу по рынку.")
            pre = manage.preflight_open(ad, row["symbol"], row["side"], False)
            if pre.get("skip"):
                self._skip_open(cfg, p, sig, False, ad, pre["skip"], retry=pre.get("retry", False), ex=ex)
                return
            st, r = self._do(cfg, "open", row["symbol"], row["side"],
                             lambda: ad.open_market(row["symbol"], row["side"], row["notional"], row["leverage"], plan["sl"], [(l["price"], l["share"]) for l in plan["levels"]]),
                             {"signal_ts": row["signal_ts"], "notional": row["notional"], "margin": row["margin"], "level": row["level"], "mode": "breakout"}, ad=ad, ex=ex)
            if st == "error":
                self._open_failed(cfg, sig, False, r.get("error"), ex)
            return
        if now >= row["expires"]:
            self._cancel_pending(row, cfg, ad, "цена не дошла до уровня за срок ожидания")

    def _open_failed(self, cfg, sig, add, reason, ex="hyperliquid"):
        """Вход не удался: запоминаем сигнал, чтобы повторить при перезапуске бота (пока он свеж), и сообщаем владельцу."""
        self.db.set_retry(cfg["id"], sig["ts"], sig["side"], add, sig.get("leg"), str(reason), exchange=ex)
        tg = self.db.get_user(cfg["user_id"])["telegram_id"]
        self.send(tg, f"⚠️ {self._name(cfg, ex)}: {'добор' if add else 'вход'} {'в лонг' if sig['side'] == 'long' else 'в шорт'} не выполнен: {esc(str(reason)[:300])}\n"
                      f"Сигнал запомнен: после перезапуска бота вход будет повторён, если сигнал ещё свеж (не старше {self.b.max_age_min:g} мин) и позиция по нему не отработала.")

    def _retry_open(self, cfg, res, chat):
        """При запуске бота: повтор входа, который не удался (ошибка биржи, лимит, нехватка маржи) — отдельно на каждой бирже. Только если сигнал свеж, автомод включён
        и по расчёту позиция по сигналу ещё открыта."""
        p = res["params"]
        for r in [x for x in self.db.list_retries() if x["config_id"] == cfg["id"]]:
            ex = r["exchange"]
            self.db.clear_retry(cfg["id"], ex)                  # новая неудача запишется заново
            name = self._name(cfg, ex)
            age = (self.clock() * 1000 - r["signal_ts"]) / 60000
            if not cfg["ex"][ex]["open"]:
                continue
            if age > self.b.max_age_min:
                self.send(chat, f"⏱ {name}: повтор входа отменён — сигнал устарел ({age:.0f} мин, предел {self.b.max_age_min:g}).")
                continue
            trade = next((t for t in res["trades"] if t["signal_ts"] == r["signal_ts"] or r["signal_ts"] in (t.get("add_signals") or [])), None)
            if trade is not None and trade["reason"] != "OPEN":
                self.send(chat, f"⏱ {name}: повтор входа отменён — по расчёту стратегии позиция по этому сигналу уже закрыта ({trade['reason']}).")
                continue
            sig = {"ts": r["signal_ts"], "side": r["side"], "pyramid": r["pyramid"], "leg": r["leg"] or 1}
            entry = self.engine.price_fn(p["symbol"]) if self.b.live_price else None
            plan = self.engine.plan(sig["side"], entry, p["tps"], p["sl_pct"]) if entry else None
            trade_log("auto_retry", "warn", user=cfg["user_id"], config=cfg["id"], name=cfg["name"], signal_ts=r["signal_ts"], side=r["side"], age_min=round(age, 1), previous_error=r["reason"], target=ex)
            self.send(chat, f"🔁 {name}: повторяю {'добор' if r['pyramid'] else 'вход'} {'в лонг' if r['side'] == 'long' else 'в шорт'} по сигналу {fm.ts_str(r['signal_ts'])} (прошлая попытка не удалась: {esc(str(r['reason'])[:120])}).")
            self._auto_open(cfg, res, sig, plan, only={ex})

    def _skip_open(self, cfg, p, sig, add, ad, reason, retry=False, ex="hyperliquid"):
        """Вход / добор не выполнен по понятной причине: журнал, лог и сообщение владельцу. retry — причина временная (биржа не ответила, не хватило маржи): сигнал запоминается для повтора при перезапуске."""
        self.db.add_auto_log(cfg["user_id"], cfg["id"], "add" if add else "open", p["symbol"], sig["side"], "skipped", {"signal_ts": sig["ts"], "note": reason, "exchange": ad.name, "target": ex})
        trade_log("auto_skipped", "warn", user=cfg["user_id"], config=cfg["id"], name=cfg["name"], symbol=p["symbol"], side=sig["side"], reason=reason, retry=retry, target=ex)
        if retry:
            self.db.set_retry(cfg["id"], sig["ts"], sig["side"], add, sig.get("leg"), reason, exchange=ex)
        else:
            self.db.clear_retry(cfg["id"], ex)
        self.send(self.db.get_user(cfg["user_id"])["telegram_id"], f"⏭ {self._name(cfg, ex)}: {esc(reason)}" + ("\nСигнал запомнен: повторю вход после перезапуска бота, если он ещё свеж." if retry else ""))

    def _auto_fill(self, cfg, res, ev):
        """Автомод «управлять позицией»: после TP1 стоп в безубыток, закрытие к концу недели / по развороту (TP и SL исполняет сама биржа через стоп и лимитки).
        Выполняется на каждой бирже, где включено «управлять»."""
        for ex in EXCHANGES:
            if not cfg["ex"][ex]["manage"]:
                continue
            sym, side = res["params"]["symbol"], ev["side"]
            ad = self.adapter_for(cfg["user_id"], ex)
            if ev.get("tp1_be"):
                self._do(cfg, "move_stop", sym, side, lambda: ad.set_stop(sym, side, ev["entry"]), {"to": "безубыток", "price": ev["entry"], "ts": ev["ts"]}, ad=ad, ex=ex)
            if ev["label"] in ("WKND", "REV"):
                self._do(cfg, "close_weekend" if ev["label"] == "WKND" else "close_reverse", sym, side,
                         lambda: ad.close_market(sym, side, ev["share"]), {"ts": ev["ts"], "share": ev["share"]}, ad=ad, ex=ex)

    def _send_fills(self, cfg, res, chat, label, upd):
        """Уведомления об исполнениях (TP / SL / БУ / разворот) после отметки fill_ts. У конфига без отметки (создан до этой функции) прошлое не присылаем:
        фиксируем текущее состояние. Слишком старые (дольше SIGNALBOT_MAX_AGE_MIN) пропускаются, как и сигналы."""
        events = self.engine.fill_events(res)
        newest = max([e["ts"] for e in events], default=0)
        mark = cfg.get("fill_ts")
        if mark is None:
            upd["fill_ts"] = newest
            return 0
        sent = 0
        for ev in (e for e in events if e["ts"] > mark):
            if (self.clock() * 1000 - ev["ts"]) / 60000 > self.b.max_age_min:
                log.info("исполнение %s %s по %s устарело — пропущено", ev["label"], ev["ts"], cfg["name"])
                continue
            self.send(chat, fm.fill_message(cfg, res, ev, label))
            self._auto_fill(cfg, res, ev)
            sent += 1
        upd["fill_ts"] = max(mark, newest)
        return sent

    def _download_top(self, memo, users):
        """Докачивает окна по одному, пока не вышел бюджет времени цикла (SIGNALBOT_TOP_BUDGET_SEC) или не нажата /cancel."""
        t0 = time.monotonic()
        done = set()
        for w, cfg_ids in list(memo["top"].items()):
            if time.monotonic() - t0 > self.b.top_budget_sec and done:
                break
            owners = {c["user_id"] for c in (self.db.get_config(i) for i in cfg_ids) if c}
            if owners and owners <= self._cancel:
                continue
            err = self.engine.download_windows([w])
            if err:
                log.warning(err)
                break
            done |= cfg_ids
        for i in done:
            self._fp.pop(i, None)                            # статистика топ-коинов пересчитается в следующем цикле

    def wait(self, timeout):
        self._wake.wait(timeout)
        self._wake.clear()

    def stop(self):
        self._stop.set()
        self._wake.set()

    @property
    def stopped(self):
        return self._stop.is_set()
