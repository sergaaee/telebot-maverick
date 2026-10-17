"""Логика бота: приём конфигов и команд, цикл проверки новых сигналов, рассылка."""
import json
import logging
import threading
import time
from html import escape as esc

from . import formatting as fm
from .configs import ConfigError, default_name, parse_configs, validate
from .engine import EvalError, fingerprint
from .telegram import TelegramError, retry_sleep

log = logging.getLogger("signalbot")


class SignalBot:
    def __init__(self, settings, tg, store, engine, bsettings, sync=None, clock=time.time):
        self.s, self.tg, self.cfgs, self.engine, self.b, self.sync, self.clock = settings, tg, store, engine, bsettings, sync, clock
        self._fp = {}                       # id конфига -> отпечаток файлов истории на момент последнего расчёта
        self._lock = threading.RLock()      # расчёты и правки конфигов не идут одновременно
        self._wake = threading.Event()
        self._stop = threading.Event()
        self.last_cycle = None

    # ---------------- отправка ----------------
    def broadcast(self, text):
        for chat in self.b.chats:
            try:
                self.tg.send_message(chat, text)
            except TelegramError as e:
                log.warning("не удалось отправить в %s: %s", chat, e)

    def reply(self, chat, text):
        try:
            self.tg.send_message(chat, text)
        except TelegramError as e:
            log.warning("не удалось ответить в %s: %s", chat, e)

    # ---------------- входящие ----------------
    def handle_update(self, upd):
        msg = upd.get("message")
        if not msg:
            return
        chat = msg["chat"]["id"]
        if chat not in self.b.chats:
            self.reply(chat, f"⛔ Доступ закрыт. Ваш chat id: <code>{chat}</code>\nДобавьте его в <code>SIGNALBOT_CHATS</code> в signalbot/.env и перезапустите бота.")
            return
        try:
            self._handle_message(chat, msg)
        except Exception as e:                               # одна плохая команда не должна ронять бота
            log.exception("ошибка обработки сообщения")
            self.reply(chat, f"⚠️ Ошибка: {esc(str(e) or type(e).__name__)}")

    def _handle_message(self, chat, msg):
        doc = msg.get("document")
        text = (msg.get("text") or msg.get("caption") or "").strip()
        if doc:
            name = (doc.get("file_name") or "")
            if not name.lower().endswith((".json", ".txt")) and doc.get("mime_type") != "application/json":
                self.reply(chat, "Пришлите конфиг файлом .json (кнопка «Скачать конфиг» в интерфейсе).")
                return
            try:
                raw = self.tg.download(doc["file_id"])
                body = raw.decode("utf-8-sig")
            except (TelegramError, UnicodeDecodeError) as e:
                self.reply(chat, f"Не удалось прочитать файл: {esc(str(e))}")
                return
            hint = text.split("\n")[0].strip() if text and not text.startswith("/") else (text.partition(" ")[2].strip() if text.startswith("/add") else "")
            if not hint and not (name.lower().startswith(("maverick-config", "config"))):
                hint = name.rsplit(".", 1)[0]
            self.import_text(chat, body, hint or None)
            return
        if text.startswith("/"):
            cmd, _, arg = text.partition(" ")
            cmd = cmd.split("@")[0].lower()
            arg = arg.strip()
            if cmd == "/add":
                name, _, rest = arg.partition("{")
                self.import_text(chat, ("{" + rest) if rest else arg, name.strip() or None)
                return
            handler = getattr(self, "cmd_" + cmd[1:], None)
            if handler:
                handler(chat, arg)
            else:
                self.reply(chat, "Не знаю такой команды. /help — список команд.")
            return
        if "{" in text or "[" in text:
            self.import_text(chat, text, None)
        elif text:
            self.reply(chat, "Не понял. Пришлите конфиг (файл .json или текст JSON) или /help.")

    # ---------------- импорт конфигов ----------------
    def import_text(self, chat, text, name_hint):
        try:
            items = parse_configs(text, name_hint)
        except ConfigError as e:
            self.reply(chat, f"❌ {esc(str(e))}")
            return
        added, errors = [], []
        with self._lock:
            for it in items:
                try:
                    params, ignored = validate(it["params"], self.s.defaults)
                    if params["source"] not in self.s.sources:
                        raise ConfigError(f"источник «{params['source']}» не настроен у бота (доступны: {', '.join(self.s.sources)})")
                except ConfigError as e:
                    errors.append(f"{esc(it['name'] or default_name(it['params']))}: {esc(str(e))}")
                    continue
                name = it["name"] or default_name(params)
                existing = self.cfgs.find(name) if it["name"] else None
                if not existing:
                    name = self.cfgs.unique_name(name)
                cfg, created = self.cfgs.add(name, params, it["tf"])
                self._fp.pop(cfg["id"], None)
                added.append(f"{'➕' if created else '♻️ обновлён'} <b>#{cfg['id']} {esc(cfg['name'])}</b>"
                             + (f" <i>(пропущены поля: {esc(', '.join(ignored[:6]))})</i>" if ignored else ""))
        out = []
        if added:
            out.append("Принято:\n" + "\n".join(added) + "\n\nСчитаю статистику и пришлю карточку конфига…")
        if errors:
            out.append("❌ Не принято:\n" + "\n".join(errors))
        self.reply(chat, "\n\n".join(out))
        if added:
            self._wake.set()

    # ---------------- команды ----------------
    def _cfg(self, chat, ref):
        c = self.cfgs.find(ref)
        if not c:
            self.reply(chat, "Конфиг не найден. /list — список." if ref else "Укажите номер или имя конфига: /list")
        return c

    def cmd_start(self, chat, arg):
        self.reply(chat, fm.HELP)

    cmd_help = cmd_start

    def cmd_list(self, chat, arg):
        self.reply(chat, fm.list_message(self.cfgs.configs))

    def cmd_remove(self, chat, arg):
        c = self._cfg(chat, arg)
        if c:
            with self._lock:
                self.cfgs.remove(c)
            self.reply(chat, f"🗑 Конфиг <b>{esc(c['name'])}</b> удалён.")

    def cmd_pause(self, chat, arg):
        c = self._cfg(chat, arg)
        if c:
            c["paused"] = True
            self.cfgs.save()
            self.reply(chat, f"⏸ <b>{esc(c['name'])}</b> на паузе — сигналы не приходят. /resume {c['id']}")

    def cmd_resume(self, chat, arg):
        c = self._cfg(chat, arg)
        if c:
            c.update(paused=False, baseline=False, last_ts=0)         # за время паузы сигналы не накапливаем: начинаем с текущего состояния
            self.cfgs.save()
            self._fp.pop(c["id"], None)
            self.reply(chat, f"▶️ <b>{esc(c['name'])}</b> снова активен. Сигналы, пропущенные на паузе, не присылаются.")
            self._wake.set()

    def cmd_rename(self, chat, arg):
        ref, _, new = arg.partition(" ")
        c = self._cfg(chat, ref)
        if not c:
            return
        new = new.strip()
        if not new:
            self.reply(chat, "Формат: /rename &lt;№|имя&gt; &lt;новое имя&gt;")
        elif self.cfgs.find(new) not in (None, c):
            self.reply(chat, "Такое имя уже занято.")
        else:
            c["name"] = new[:60]
            self.cfgs.save()
            self.reply(chat, f"✏️ Теперь: <b>#{c['id']} {esc(c['name'])}</b>")

    def cmd_get(self, chat, arg):
        c = self._cfg(chat, arg)
        if c:
            body = json.dumps({"format": "maverick-config", "version": 1, "name": c["name"], "tf": c["tf"], "params": c["params"]}, ensure_ascii=False, indent=2)
            try:
                self.tg.send_document(chat, f"maverick-config_{c['id']}.json", body.encode("utf-8"), caption=c["name"])
            except TelegramError as e:
                self.reply(chat, f"Не удалось отправить файл: {esc(str(e))}")

    def cmd_stats(self, chat, arg):
        c = self._cfg(chat, arg)
        if not c:
            return
        try:
            with self._lock:
                res = self.engine.evaluate(c["params"])
        except EvalError as e:
            self.reply(chat, f"⚠️ {esc(str(e))}")
            return
        parts = [f"📋 <b>#{c['id']} {esc(c['name'])}</b>\n{esc(fm.config_summary(c))}", fm.stats_block(res["stats"]["total"], c["params"], res["range"]), fm.sizing_line(res)]
        top = res.get("top") or {}
        if top.get("enabled"):
            parts.append(fm.stats_block(top["stats"], c["params"], None, title="🪙 <b>Топ-коины</b>") + (f"\n<i>Окон свечей без данных: {top['pending']['windows']}</i>" if top["pending"]["windows"] else ""))
        self.reply(chat, "\n\n".join(parts))

    def cmd_check(self, chat, arg):
        self.reply(chat, "🔎 Проверяю…")
        self.cycle(force=True)
        self.reply(chat, "Проверка завершена: новые сигналы (если были) пришли отдельными сообщениями.")

    def cmd_status(self, chat, arg):
        rows = ["📡 <b>Состояние</b>"]
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
        rows.append(f"Конфигов: {len(self.cfgs.configs)} · цикл каждые {self.b.poll_sec:g} с" + (f" · последний цикл {fm.ts_str(self.last_cycle * 1000)}" if self.last_cycle else ""))
        self.reply(chat, "\n".join(rows))

    # ---------------- цикл проверки сигналов ----------------
    def cycle(self, force=False):
        with self._lock:
            for cfg in list(self.cfgs.configs):
                if cfg.get("paused"):
                    continue
                try:
                    self._process(cfg, force)
                except Exception as e:                       # сбой одного конфига не останавливает остальные
                    log.exception("конфиг %s", cfg["name"])
                    cfg["error"] = str(e) or type(e).__name__
                    self.cfgs.save()
        self.last_cycle = self.clock()

    def _process(self, cfg, force):
        p = cfg["params"]
        fp = fingerprint(self.s, p)
        if not force and cfg.get("baseline") and self._fp.get(cfg["id"]) == fp:
            return
        warn = self.engine.refresh_candles(p["symbol"])
        res = self.engine.evaluate(p)
        cfg["error"] = None
        label = self.s.sources[p["source"]].label
        if not cfg.get("baseline"):
            # первый расчёт: фиксируем текущее состояние, чтобы не засыпать старыми сигналами, и присылаем карточку конфига
            cfg["last_ts"] = max([s["ts"] for s in res["last_signals"] if not s["trend_blocked"]], default=0)
            cfg["baseline"] = True
            self.broadcast(fm.accepted_message(cfg, res))
        else:
            fresh = self.engine.new_signals(res, cfg["last_ts"])
            seen = [s["ts"] for s in res["last_signals"] if not s["trend_blocked"]]
            if seen:
                cfg["last_ts"] = max(cfg["last_ts"], max(seen))         # повторные подтверждения серии тоже «просмотрены»
            for sig in fresh:
                age_min = (self.clock() * 1000 - sig["ts"]) / 60000
                if age_min > self.b.max_age_min:
                    log.info("сигнал %s по %s устарел (%.0f мин) — пропущен", sig["ts"], cfg["name"], age_min)
                    continue
                report = self.engine.report(p, sig["ts"])
                entry = self.engine.price_fn(p["symbol"]) if self.b.live_price else None
                plan = self.engine.plan(sig["side"], entry, p["tps"], p["sl_pct"]) if entry else None
                top = self.engine.top_plan(res, sig, report) if p.get("top_enabled") else None
                self.broadcast(fm.signal_message(cfg, res, sig, report, plan, entry, top, label, stale_note=warn))
                cfg["alerts"] = cfg.get("alerts", 0) + 1
        self._fp[cfg["id"]] = fp
        # докачка окон свечей топ-коинов для статистики (понемногу за цикл)
        if p.get("top_enabled"):
            wins, total = self.engine.pending_windows(res, self.b.top_windows)
            if wins:
                err = self.engine.download_windows(wins)
                if not err:
                    self._fp.pop(cfg["id"], None)            # пересчитаем статистику топ-коинов в следующем цикле
                else:
                    log.warning(err)
        self.cfgs.save()

    # ---------------- запуск ----------------
    def poll_loop(self):
        attempt = 0
        while not self._stop.is_set():
            try:
                updates = self.tg.get_updates(self.cfgs.data.get("update_offset"), timeout=25)
                attempt = 0
            except TelegramError as e:
                log.warning("getUpdates: %s", e)
                attempt += 1
                retry_sleep(attempt)
                continue
            for u in updates:
                self.cfgs.data["update_offset"] = u["update_id"] + 1
                self.cfgs.save()
                self.handle_update(u)

    def cycle_loop(self):
        while not self._stop.is_set():
            try:
                self.cycle()
            except Exception:
                log.exception("цикл")
            self._wake.wait(self.b.poll_sec)
            self._wake.clear()

    def stop(self):
        self._stop.set()
        self._wake.set()
