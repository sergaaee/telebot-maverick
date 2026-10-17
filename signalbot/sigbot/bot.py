"""Ядро бота (без привязки к aiogram): пользователи, их конфиги, команды, цикл проверки новых сигналов и рассылка.

Многопользовательская модель: Telegram-аккаунт привязывается к пользователю сайта по одноразовому коду (/link КОД). У каждого
пользователя свои конфиги (таблица configs в SQLite), сигналы по ним приходят только владельцу. Данные рынка (история отчётов,
свечи) общие: одинаковые расчёты в одном цикле выполняются один раз."""
import json
import logging
import threading
import time
from html import escape as esc

from app.db import DbError

from . import formatting as fm
from .configs import ConfigError, default_name, parse_configs, validate
from .engine import Cancelled, EvalError, fingerprint
from .transport import TelegramError

log = logging.getLogger("signalbot")

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

    # ---------------- отправка ----------------
    def send(self, chat, text):
        try:
            self.tg.send_message(chat, text)
        except TelegramError as e:
            log.warning("не удалось отправить в %s: %s", chat, e)

    reply = send

    # ---------------- входящие ----------------
    def handle_update(self, upd):
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
        fp = fingerprint(self.s, p)
        if not force and cfg["baseline"] and self._fp.get(cfg["id"]) == fp:
            return
        res, warn = self._evaluate(p, memo, user)
        label = self.s.sources[p["source"]].label
        upd = {"error": None}
        if not cfg["baseline"]:
            # первый расчёт: фиксируем текущее состояние, чтобы не засыпать старыми сигналами, и присылаем карточку конфига
            upd.update(last_ts=max([s["ts"] for s in res["last_signals"] if not s["trend_blocked"]], default=0), baseline=True)
            self.send(chat, fm.accepted_message(cfg, res))
        else:
            fresh = self.engine.new_signals(res, cfg["last_ts"])
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
                sent += 1
            if sent:
                upd["alerts"] = cfg["alerts"] + sent
        self.db.update_config(cfg["id"], **upd)
        self._fp[cfg["id"]] = fp
        # окна свечей топ-коинов для статистики докачиваются в конце цикла, с ограничением по времени (карточка и сигналы от них не зависят)
        if p.get("top_enabled"):
            wins, _ = self.engine.pending_windows(res, self.b.top_windows)
            for w in wins:
                memo["top"].setdefault(w, set()).add(cfg["id"])

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
