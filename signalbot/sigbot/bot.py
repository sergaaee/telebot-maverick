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

from app.db import DbError

from . import formatting as fm
from .configs import ConfigError, default_name, parse_configs, validate
from app import digest as digest_mod, exporter, manage
from app.monitor import cached_result
from app.service import ParamError
from app.secrets_store import SecretError, encrypt
from app.tradelog import read_tail, trade_error, trade_log
from app.trading import adapter_for, forget_adapter, mask_address, verify_hyperliquid

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
        self._hl = {}                       # user_id -> шаг подключения Hyperliquid (ждём адрес кошелька / ключ API-кошелька)

    # ---------------- отправка ----------------
    def send(self, chat, text, keyboard=None):
        try:
            self.tg.send_message(chat, text, keyboard=keyboard) if keyboard else self.tg.send_message(chat, text)
        except TelegramError as e:
            log.warning("не удалось отправить в %s: %s", chat, e)

    reply = send

    # ---------------- входящие ----------------
    def adapter_for(self, user_id):
        """Адаптер биржи пользователя: его подключённый счёт Hyperliquid или симуляция (ордера не отправляются)."""
        return adapter_for(self.db, user_id)

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
        if self._hl_pending(chat, user, msg, text, cmd):                  # идёт подключение счёта: сообщение — адрес кошелька или ключ
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

    def cmd_digest(self, chat, user, arg):
        """Регулярная сводка: /digest daily — каждый день, /digest weekly — по понедельникам, /digest off — выключить, /digest now — прислать сейчас."""
        a = arg.strip().lower()
        if a == "now":
            mode = user.get("digest") if user.get("digest") in digest_mod.PERIOD_H else "daily"
            self.send(chat, digest_mod.build_digest(self.db, self.engine.store, self.s.defaults, user, self.adapter_for(user["id"]), self.engine.available, mode))
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
        kind = arg.strip().lower()
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
            ad = self.adapter_for(user["id"])
            if not ad.real:
                self.send(chat, "Счёт биржи не подключён (/hl) — реальных исполнений нет.")
                return
            try:
                body, name = exporter.fills_csv(ad.fills(500)), "maverick-fills.csv"
            except Exception as e:
                self.send(chat, f"⚠️ Не удалось получить исполнения: {esc(str(e)[:200])}")
                return
        else:
            self.send(chat, "Что выгрузить?\n/export trades — сделки стратегий (расчёт)\n/export log — журнал автоторговли\n/export fills — реальные исполнения на бирже")
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
                text = digest_mod.build_digest(self.db, self.engine.store, self.s.defaults, u, self.adapter_for(u["id"]), self.engine.available, u["digest"], now)
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
        self._hl[user["id"]] = {"step": "wallet", "testnet": bool(testnet), "until": time.time() + self.HL_TTL, "tries": 0}
        net = "тестовая сеть" if testnet else "БОЕВАЯ сеть"
        self.tg.send_message(chat, f"🔌 <b>Подключение Hyperliquid</b> ({net})\n\nШаг 1 из 2: пришлите <b>адрес основного кошелька</b> Hyperliquid (0x… и 40 символов) — это счёт, на котором лежат средства.\n\n"
                                   "Дальше я попрошу <b>ключ API-кошелька</b>: он умеет торговать, но не выводить средства (Hyperliquid → More → API). Ключ ОСНОВНОГО кошелька присылать нельзя — я его не приму.\n"
                                   "Ваши сообщения с адресом и ключом я удалю из чата сразу после обработки; ключ хранится в базе зашифрованным. Отмена — кнопка ниже или /cancel.",
                             keyboard=[[("Отмена", "hlx")]])

    def cancel_hl(self, user):
        return self._hl.pop(user["id"], None) is not None

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

    def cmd_list(self, chat, user, arg):
        self.send(chat, fm.list_message(self.db.list_configs(user["id"])))

    def cmd_remove(self, chat, user, arg):
        c = self._cfg(chat, user, arg)
        if c:
            self.db.remove_config(c["id"])
            self._fp.pop(c["id"], None)
            self.send(chat, f"🗑 Конфиг <b>{esc(c['name'])}</b> удалён.")

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
        if not force and cfg["baseline"] and self._fp.get(cfg["id"]) == fp:
            return
        res, warn = self._evaluate(p, memo, user)
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
    def _do(self, cfg, kind, symbol, side, fn, details):
        """Выполняет действие через адаптер биржи и пишет результат в журнал; ошибка адаптера не ломает цикл бота."""
        trade_log("auto_decision", user=cfg["user_id"], config=cfg["id"], name=cfg["name"], kind=kind, symbol=symbol, side=side, exchange=self.adapter_for(cfg["user_id"]).name, **details)
        try:
            res = fn()
            status = res.get("status", "sent")
            trade_log("auto_result", user=cfg["user_id"], config=cfg["id"], kind=kind, status=status, result=res)
        except Exception as e:                                  # NotConfigured и ошибки биржи
            res, status = {"error": str(e)}, "error"
            log.warning("автомод %s по %s: %s", kind, cfg["name"], e)
            trade_error("auto_fail", e, user=cfg["user_id"], config=cfg["id"], name=cfg["name"], kind=kind, symbol=symbol, side=side)
        self.db.add_auto_log(cfg["user_id"], cfg["id"], kind, symbol, side, status, {**details, **res, "exchange": self.adapter_for(cfg["user_id"]).name})

    def _auto_open(self, cfg, res, sig, plan):
        """Автомод «открывать по сигналу»: вход или добор по новому сигналу (объём — как в плане конфига). Перед ордером состояние сверяется с реальной биржей."""
        if not cfg["auto"]["open"]:
            return
        p = res["params"]
        add = bool(sig.get("pyramid"))
        notional, margin = self.engine.order_size(res, add)
        ad = self.adapter_for(cfg["user_id"])
        pre = manage.preflight_open(ad, p["symbol"], sig["side"], add)
        if pre.get("skip"):
            self.db.add_auto_log(cfg["user_id"], cfg["id"], "add" if add else "open", p["symbol"], sig["side"], "skipped", {"signal_ts": sig["ts"], "note": pre["skip"], "exchange": ad.name})
            trade_log("auto_skipped", "warn", user=cfg["user_id"], config=cfg["id"], name=cfg["name"], symbol=p["symbol"], side=sig["side"], reason=pre["skip"])
            self.send(self.db.get_user(cfg["user_id"])["telegram_id"], f"⏭ <b>#{cfg['id']} {esc(cfg['name'])}</b>: {esc(pre['skip'])}")
            return
        if pre.get("close_first"):                          # на бирже позиция в обратную сторону: по правилам стратегии это разворот — сначала закрываем её
            self._do(cfg, "close_reverse", p["symbol"], pre["close_first"], lambda: ad.close_market(p["symbol"], pre["close_first"], 1.0), {"signal_ts": sig["ts"], "note": "разворот перед входом"})
        self._do(cfg, "add" if add else "open", p["symbol"], sig["side"],
                 lambda: ad.open_market(p["symbol"], sig["side"], notional, (notional / margin) if margin else p["leverage"],
                                                  plan["sl"] if plan else None, [(l["price"], l["share"]) for l in plan["levels"]] if plan else None),
                 {"signal_ts": sig["ts"], "notional": notional, "margin": margin, "leg": sig.get("leg", 1)})

    def _auto_fill(self, cfg, res, ev):
        """Автомод «управлять позицией»: после TP1 стоп в безубыток, закрытие к концу недели / по развороту (TP и SL исполняет сама биржа через стоп и лимитки)."""
        if not cfg["auto"]["manage"]:
            return
        sym, side = res["params"]["symbol"], ev["side"]
        ad = self.adapter_for(cfg["user_id"])
        if ev.get("tp1_be"):
            self._do(cfg, "move_stop", sym, side, lambda: ad.set_stop(sym, side, ev["entry"]), {"to": "безубыток", "price": ev["entry"], "ts": ev["ts"]})
        if ev["label"] in ("WKND", "REV"):
            self._do(cfg, "close_weekend" if ev["label"] == "WKND" else "close_reverse", sym, side,
                     lambda: ad.close_market(sym, side, ev["share"]), {"ts": ev["ts"], "share": ev["share"]})

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
