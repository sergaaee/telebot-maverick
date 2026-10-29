"""Интерфейс бота в Telegram: повторяет страницу мониторинга сайта — список всех стратегий, карточка стратегии с позицией и уровнями, автомод, пауза, статистика / история / параметры / журнал,
биржа, тестовая позиция. Экраны — одно сообщение с инлайн-кнопками, которое правится на месте; «live» перерисовывает экран каждые ~20 с, пока он подписан.

Экран описывается строкой (она же callback_data кнопки): «m» список, «c:ID» карточка, «v:ID:stats|hist|params|log» вкладки, «x» биржа, «t» тестовая позиция;
подтверждения: «ca:ID:o|m» (включить автомод), «cd:ID» (удалить), «cty:long|short», «ctc», «coff». Действия: «a:ID:o|m», «ay:…», «p:ID», «dy:ID», «e:ID», «tgo:…», «tcgo», «offy», «lv:ЭКРАН»."""
import hashlib
import logging
import time
from html import escape as esc

from app import manage
from app.monitor import evaluate_entry

from . import formatting as fm
from .transport import TelegramError

log = logging.getLogger("signalbot")
LIVE_TTL = 30 * 60
HIST_N = 10
LIVE_SCREENS = ("m", "c", "v", "x")                      # подтверждения и тестовая позиция не перерисовываются сами
MAX_BUTTONS = 24


def _hours(h):
    return f"{round(h * 60)} мин" if h < 1 else f"{h:.1f} ч" if h < 48 else f"{h / 24:.1f} сут"


def _icon(e):
    if e.get("error"):
        return "❌"
    if e.get("paused"):
        return "🟠"
    pos = e.get("position")
    if pos:
        return "🟢" if pos["side"] == "long" else "🔴"
    return "🟣" if e.get("ready_now") else "⚪"


def _state(e):
    if e.get("error"):
        return "ошибка расчёта"
    if e.get("paused"):
        return "на паузе"
    pos = e.get("position")
    if pos:
        return "в лонге" if pos["side"] == "long" else "в шорте"
    if e.get("ready_now"):
        w = {"cool": "откат", "liq": "ликвидации", "trend": "тренд"}
        return "ждёт: " + " + ".join(w.get(x, x) for x in e["ready_now"][0]["waiting"])
    return "позиции нет"


def _auto(e):
    a = e.get("auto") or {}
    return f"открывать {'✅' if a.get('open') else '⬜'} · управлять {'✅' if a.get('manage') else '⬜'}"


class MonitorUI:
    def __init__(self, svc):
        self.svc = svc
        self.live = {}                                    # (chat, message_id) -> {screen, uid, until, sig}

    # ------------------------------------------------------------------ данные
    @property
    def db(self):
        return self.svc.db

    def _entry(self, cfg, detail=False):
        e = evaluate_entry(self.svc.engine.store, self.svc.s.defaults, f"#{cfg['id']} {cfg['name']}", cfg["id"], cfg["params"], cfg["auto"], cfg["paused"],
                           self.svc.engine.available, detail=detail)
        return {**e, "title": cfg["name"], "baseline": cfg["baseline"], "tf": cfg["tf"], "alerts": cfg["alerts"], "created": cfg["created"]}

    def _cfg(self, user, cid):
        c = self.db.find_config(user["id"], str(cid))
        if not c:
            raise manage.ManageError("Конфиг не найден (удалён?)", 404)
        return c

    def _ad(self, user):
        return self.svc.adapter_for(user["id"])

    def _exchange_line(self, user):
        d = self._ad(user).describe()
        if not d["real"]:
            return "Биржа: не подключена — автомод в <b>симуляции</b> (ордера не отправляются)"
        return f"Биржа: <b>{esc(d['name'].capitalize())}</b> ({'тестовая сеть' if d['testnet'] else '<b>БОЕВАЯ сеть</b>'})"

    # ------------------------------------------------------------------ экраны
    def render(self, user, screen, live=False):
        """-> (тело, клавиатура). Тело без строки времени: по нему решаем, менять ли сообщение."""
        kind, *a = screen.split(":")
        fn = {"m": self._s_list, "c": self._s_card, "v": self._s_tab, "x": self._s_exchange, "t": self._s_test, "ca": self._s_confirm_auto, "cd": self._s_confirm_del,
              "cty": self._s_confirm_test, "ctc": self._s_confirm_close, "coff": self._s_confirm_off, "cpn": self._s_confirm_panic, "sz": self._s_sizing, "chl": self._s_confirm_hl, "cdx": self._s_confirm_disconnect}.get(kind)
        if fn is None:
            return self._s_list(user, live)
        return fn(user, *a, live=live)

    def _live_btn(self, screen, live):
        return ("⏹ Остановить live" if live else "🔴 Live", f"lv:{screen}")

    def _s_list(self, user, live=False):
        cfgs = self.db.list_configs(user["id"])
        rows, kb = [], []
        if not cfgs:
            return ("📡 <b>Мониторинг</b>\nУ вас пока нет конфигов. Пришлите конфиг сообщением или файлом (кнопка «Скачать конфиг» на сайте) — он появится здесь.", [[("🔄 Обновить", "m")]])
        total, opened = 0.0, 0
        for c in cfgs[:MAX_BUTTONS]:
            e = self._entry(c)
            pos = e.get("position")
            line = f"{_icon(e)} <b>#{c['id']} {esc(c['name'][:28])}</b> · {esc((e.get('symbol') or '–').split('/')[0])} — {_state(e)}"
            if pos:
                line += f" · <b>{fm.usd(pos['pnl_usd'])}</b> ({fm.pct(pos['pnl_pct_deposit'], 2) if pos.get('pnl_pct_deposit') is not None else '–'}) · {_hours(pos['hold_h'])}"
                if pos.get("legs_n", 1) > 1:
                    line += f" · ног {pos['legs_n']}"
                if pos.get("danger"):
                    line += " ⚠️"
                if not e.get("paused"):
                    total += pos["pnl_usd"]
                    opened += 1
            if (e.get("auto") or {}).get("open") or (e.get("auto") or {}).get("manage"):
                line += f"\n    🤖 {_auto(e)}"
            if e.get("error"):
                line += f"\n    ⚠️ {esc(str(e['error'])[:120])}"
            rows.append(line)
        head = [f"📡 <b>Мониторинг</b> · стратегий: {len(cfgs)} · с позицией: {opened}" + (f" · открытый результат <b>{fm.usd(total)}</b>" if opened else ""), self._exchange_line(user), ""]
        body = "\n".join(head + rows)
        body += "\n\n<i>Позиции — расчёт по свечам Binance, не данные биржи.</i>"
        pair = []
        for c in cfgs[:MAX_BUTTONS]:
            e = self._entry(c)
            pair.append((f"{_icon(e)} #{c['id']} {c['name'][:14]}", f"c:{c['id']}"))
            if len(pair) == 2:
                kb.append(pair)
                pair = []
        if pair:
            kb.append(pair)
        kb.append([("🔄 Обновить", "m"), self._live_btn("m", live)])
        kb.append([("🏦 Биржа", "x"), ("🧪 Тест $1", "t")])
        kb.append([("⛔ Автомод выкл. везде", "coff"), ("🚨 СТОП ВСЁ", "cpn")])
        return body, kb

    def _positions_block(self, e):
        pos = e["position"]
        d = 1 if pos["side"] == "long" else -1
        price = e.get("price")
        out = [f"{fm.side_line(pos['side'])} · открыта {fm.ts_str(pos['opened_ts'])} · держится {_hours(pos['hold_h'])}",
               f"Цена <code>{fm.price(price)}</code> · средний вход <code>{fm.price(pos['avg_entry'])}</code>"
               + (f" ({fm.pct(d * (price / pos['avg_entry'] - 1) * 100, 2)})" if price else ""),
               f"Объём {fm.usd(pos['notional']).lstrip('+')} · маржа {fm.usd(pos['margin']).lstrip('+')} · результат <b>{fm.usd(pos['pnl_usd'])}</b>"
               + (f" ({fm.pct(pos['pnl_pct_deposit'], 2)} депозита)" if pos.get("pnl_pct_deposit") is not None else "")
               + (f" · фандинг {fm.usd(-pos['funding_usd'], 2)}" if pos.get("funding_usd") else "")]
        if pos.get("deadline_ts"):
            out.append(f"📅 Закрытие к концу недели: {fm.ts_str(pos['deadline_ts'])}")
        if pos.get("danger"):
            out.append("⚠️ Цена за оценочной ценой ликвидации (в расчёте стратегии ликвидация учитывается, только если включена).")
        for l in pos.get("legs") or []:
            tps = " ".join(f"{'✅' if t['hit'] else ''}TP{t['n']} <code>{fm.price(t['price'])}</code>" for t in l["tps"])
            stop = f"{l['stop_kind']} <code>{fm.price(l['stop'])}</code>" + (f" (запас {l['to_stop_pct']:.2f}%)" if l.get("to_stop_pct") is not None else "")
            out.append(f"• {l['kind']}{f' ×{l[chr(97) + chr(100) + chr(100) + chr(115) + chr(95) + chr(110)] + 1}' if l.get('adds_n') else ''}: вход <code>{fm.price(l['entry'])}</code> · {stop}\n"
                       f"  {tps} · ликвид. ≈ <code>{fm.price(l['liq_price'])}</code> · {fm.usd(l['pnl_usd'])}")
        return out

    def _s_card(self, user, cid, live=False):
        c = self._cfg(user, cid)
        e = self._entry(c, detail=True)
        icon = _icon(e)
        head = [f"{icon} <b>#{c['id']} {esc(c['name'])}</b> · {esc(str(e.get('symbol') or '–'))} · {esc(str(e.get('source') or ''))}", f"Статус: <b>{_state(e)}</b>"]
        if e.get("error"):
            head.append(f"⚠️ {esc(str(e['error']))}")
        lines = list(head)
        if e.get("position"):
            lines += self._positions_block(e)
        else:
            lines.append("Позиции сейчас нет.")
            for r in e.get("ready_now") or []:
                lines.append(f"⏳ Готов к {'ЛОНГУ' if r['side'] == 'long' else 'ШОРТУ'}, ждёт:\n" + "\n".join(f"  • {esc(x)}" for x in r["lines"]))
        rep = e.get("report")
        if rep:
            lines.append(f"📊 Метрики сейчас: DG {'–' if rep.get('dg') is None else f'{rep['dg']:.2f}'} · DL {rep['dl']:.2f} · DS {rep['ds']:.2f}")
        st = e.get("settings")
        if st:
            lines.append(f"План: SL {st['sl_pct']:g}% · TP " + " / ".join(f"{t['pct']:g}%×{t['share']:g}" for t in st["tps"]) + (" · БУ после TP1" if st["be_after_tp1"] else "")
                         + f" · плечо {st['leverage']:g}× · {st['trade_pct']:g}% депозита")
        lines.append(f"🤖 Автомод: {_auto(e)}" + ("" if self._ad(user).real else " (симуляция)"))
        a = e.get("auto") or {}
        pe = self.db.get_pending(c["id"])
        if pe:
            lines.append(f"⏳ Ждёт вход: {'лонг' if pe['side'] == 'long' else 'шорт'} по <b>{fm.price(pe['level'])}</b> ({'лимитка на бирже' if pe['order_id'] else 'вход по рынку при касании цены'}), до {fm.ts_str(int(pe['expires'] * 1000))[5:]}")
        cu = c["catchup"]
        lines.append(f"⏩ Догнать свежий сигнал при включении: <b>{'вкл' if cu['on'] else 'выкл'}</b>" + (f" (не старше {cu['age_min']:g} мин, цена не дальше {cu['move_pct']:g}% в прибыль)" if cu["on"] else ""))
        ad = self._ad(user)
        if ad.real:
            acc = manage.account_quick(ad, user["id"])
            lines.append(f"💰 База размера входа: <b>{'баланс счёта' if a.get('base') == 'account' else 'депозит конфига'}</b>"
                         + (f" (капитал на бирже {acc['equity']:,.2f} $)".replace(",", " ") if acc and acc.get("equity") is not None else ""))
            warn = manage.sizing_warning(a, (e.get("settings") or {}).get("deposit"), acc)
            if warn:
                lines.append(f"⚠️ {esc(warn)}")
        if e.get("paused"):
            lines.append("⏸ Конфиг на паузе: бот его не считает, автомод выключен.")
        kb = [[(f"{'✅' if a.get('open') else '⬜'} Открывать", f"a:{c['id']}:o"), (f"{'✅' if a.get('manage') else '⬜'} Управлять", f"a:{c['id']}:m")],
              [(f"💰 База: {'баланс счёта' if a.get('base') == 'account' else 'депозит конфига'}", f"ab:{c['id']}"), (f"📐 {st['trade_pct']:g}% × {st['leverage']:g}×" if st else "📐 Размер", f"sz:{c['id']}")],
              [(f"{'✅' if c['catchup']['on'] else '⬜'} ⏩ Догнать сигнал", f"cu:{c['id']}"), ("▶ Возобновить" if c["paused"] else "⏸ Пауза", f"p:{c['id']}")],
              [("📊 Статистика", f"v:{c['id']}:stats"), ("📜 История", f"v:{c['id']}:hist")],
              [("⚙️ Параметры", f"v:{c['id']}:params"), ("📋 Журнал", f"v:{c['id']}:log")],
              [("🔄", f"c:{c['id']}"), self._live_btn(f"c:{c['id']}", live), ("⬇ Конфиг", f"e:{c['id']}"), ("🗑", f"cd:{c['id']}")],
              [("← К списку", "m")]]
        return "\n".join(lines), kb

    def _s_tab(self, user, cid, tab, live=False):
        c = self._cfg(user, cid)
        e = self._entry(c, detail=True)
        title = f"<b>#{c['id']} {esc(c['name'])}</b>"
        if tab == "stats":
            t = e.get("stats") or {}
            if not t.get("trades"):
                body = f"📊 {title}\nСделок в истории пока нет."
            else:
                pf = "∞" if t.get("profit_factor") is None else f"{t['profit_factor']:.2f}"
                body = (f"📊 {title}\nСделок <b>{t['trades']}</b> · Winrate <b>{t['winrate']:.1f}%</b> · PF <b>{pf}</b>\n"
                        f"Итог <b>{fm.usd(t['total_usd'])}</b>" + (f" ({fm.pct(t['return_pct'], 1)} от депозита)" if t.get("return_pct") is not None else "") + "\n"
                        f"Шарп <b>{'–' if t.get('sharpe') is None else f'{t['sharpe']:.2f}'}</b> · просадка {fm.pct(t['max_dd_pct'], 1, sign=False) if t.get('max_dd_pct') is not None else '–'}"
                        + (f" (с открытыми позициями {fm.pct(t['max_dd_real_pct'], 1, sign=False)})" if t.get("max_dd_real_pct") is not None else "") + "\n"
                        f"TP {t['tp']} · БУ {t['be']} · SL {t['sl']} · разворот {t['rev']}" + (f" · ликвид. {t['liq']}" if t.get("liq") else "") + (f" · пятница {t['wknd']}" if t.get("wknd") else "")
                        + f"\nСерия убытков до {t['max_loss_streak']} · в позиции {t['exposure_pct']:.0f}% времени"
                        + (f"\nФандинг: {'уплачено' if t['funding_usd'] >= 0 else 'получено'} <b>{fm.usd(abs(t['funding_usd']))}</b> (учтён в итоге)" if t.get("funding_usd") else ""))
        elif tab == "hist":
            h = (e.get("history") or [])[:HIST_N]
            if not h:
                body = f"📜 {title}\nЗакрытых сделок пока нет."
            else:
                rows = [f"{'🟢' if x['side'] == 'long' else '🔴'} {fm.ts_str(x['entry_ts'])[:16]} → {fm.ts_str(x['exit_ts'])[5:16]} · {x['reason']} · {_hours(x['hold_h'])} · <b>{fm.usd(x['pnl_usd'])}</b> ({fm.pct(x['pnl_pct'], 2)})" for x in h]
                body = f"📜 {title} · последние {len(h)} закрытых\n" + "\n".join(rows)
        elif tab == "params":
            p = e.get("params") or {}
            tps = " / ".join(f"{t['pct']:g}%×{t['share']:g}" for t in p.get("tps", []))
            body = (f"⚙️ {title}\n{esc(str(p.get('source')))} · {esc(str(p.get('symbol')))} · сторона {p.get('side')} · метрика {p.get('only') or 'Local+Scalp'}\n"
                    f"Лонг: DL ≥ {p.get('local_min')} · DS ≥ {p.get('scalp_min')}" + (f" · DG ≥ {p['global_min']}" if p.get("global_min") is not None else "") + "\n"
                    f"Шорт: DL ≤ {p.get('short_local_max')} · DS ≤ {p.get('short_scalp_max')}" + (f" · DG ≤ {p['short_global_max']}" if p.get("short_global_max") is not None else "") + "\n"
                    f"Подтверждение {p.get('confirm')} · остывание "
                    + (f"откат {p['cool_drop']}" if p.get("cool_drop") is not None else f"{p['cool_declines']} подряд" if p.get("cool_declines") is not None else "выкл")
                    + f" · ликвидации {'вкл' if p.get('liq_hours') is not None else 'выкл'} · тренд {'вкл' if p.get('trend_enabled') else 'выкл'}\n"
                    f"SL {p.get('sl_pct')}% · TP {tps}" + (" · БУ после TP1" if p.get("be_after_tp1") else "") + "\n"
                    f"Депозит {p.get('deposit'):g} $ · {p.get('trade_pct')}% · плечо {p.get('leverage')}× · {'сложный %' if p.get('compound') else 'фикс. сумма'}\n"
                    f"Пирамидинг {'вкл' if p.get('pyramid_enabled') else 'выкл'} · только будни {'да' if p.get('weekdays_only') else 'нет'} · ликвидация в расчёте {'да' if p.get('liquidation_sim') else 'нет'}\n"
                    f"Уведомлений: {c['alerts']}")
        else:
            log_ = self.db.list_auto_log(user["id"], HIST_N, c["id"])
            names = {"open": "вход", "add": "добор", "move_stop": "стоп → БУ", "close_weekend": "закрытие к концу недели", "close_reverse": "закрытие по развороту",
                     "auto_toggle": "автомод", "pause": "пауза", "resume": "возобновлён"}
            body = f"📋 {title} · журнал действий\n" + ("\n".join(f"{fm.ts_str(int(x['ts'] * 1000))[5:]} · {names.get(x['kind'], x['kind'])} · {x['status']}" + (f" — {esc(str(x['details'].get('error'))[:80])}" if x["details"].get("error") else "")
                                                       for x in log_) or "пока пусто")
        return body, [[("← Карточка", f"c:{c['id']}"), ("🔄", f"v:{c['id']}:{tab}"), self._live_btn(f"v:{c['id']}:{tab}", live)]]

    def _s_exchange(self, user, live=False):
        ad = self._ad(user)
        info = manage.account_info(self.db, ad, user["id"])
        d = info["exchange"]
        if not info["connected"]:
            body = ("🏦 <b>Биржа</b>\nСчёт Hyperliquid не подключён — автомод работает как симуляция (ордера не отправляются).\n"
                    "Подключение: адрес кошелька и ключ <b>API-кошелька</b> (не основного!) вы присылаете сюда в чат; бот проверит их, сохранит зашифрованными и сразу удалит ваши сообщения.")
            kb = [[("🔌 Подключить · тестовая сеть", "hl:t")], [("🔌 Подключить · БОЕВАЯ сеть", "chl")], [("← К списку", "m")]]
            return body, kb
        net = "тестовая сеть" if d["testnet"] else "<b>БОЕВАЯ сеть</b>"
        lines = [f"🏦 <b>Hyperliquid</b> ({net})", f"Кошелёк <code>{info['wallet']}</code> · API-кошелёк <code>{info['agent'] or '–'}</code>"]
        acc = info["account"]
        if info["error"]:
            lines.append(f"⚠️ {esc(str(info['error']))}")
        elif acc.get("equity") is not None:
            lines.append(f"Капитал <b>{fm.num(acc.get('equity'), 2)} $</b> · свободно {fm.num(acc.get('free'), 2)} $ · в марже {fm.num(acc.get('used'), 2)} $")
        if not info["error"]:
            lines.append("")
            lines += ([f"{'🟢' if p['side'] == 'long' else '🔴'} {esc(str(p['symbol']))} {p['size']:g} · вход {fm.price(p['entry'])} · ликвид. {fm.price(p['liq'])} · PnL {fm.usd(p['pnl_usd'] or 0, 2)} · {p['leverage'] or '–'}×"
                       for p in info["positions"]] or ["Открытых позиций на бирже нет."])
            if info["orders"]:
                lines.append(f"Открытых ордеров: {len(info['orders'])}")
            for f in info["fills"][:5]:
                lines.append(f"• {fm.ts_str(int(f['ts']))[5:] if f.get('ts') else ''} {f['side']} {f['amount']:g} @ {fm.price(f['price'])}" + (f" · PnL {f['pnl']}" if f.get("pnl") not in (None, "", "0.0", "0") else ""))
        return "\n".join(lines), [[("🔄", "x"), self._live_btn("x", live)], [("🔌 Отключить", "cdx")], [("← К списку", "m")]]

    def _s_test(self, user, live=False):
        d = self._ad(user).describe()
        body = ("🧪 <b>Тестовая позиция по BTC</b>\nМаржа 1 $" + (f" × {int(-(-(d['min_notional'] * 1.1) // 1))}× (минимальный ордер биржи {d['min_notional']:g} $ объёма)" if d["min_notional"] else "")
                + ".\n" + ("Это <b>реальный</b> ордер на " + esc(d["name"]) + (" (тестовая сеть)" if d["testnet"] else " (БОЕВАЯ сеть)") + "." if d["real"] else "Биржа не подключена — будет симуляция (запись в журнал).")
                + "\nПозиция без стопа и тейков; закрывается кнопкой ниже.")
        return body, [[("🧪 Лонг", "cty:long"), ("🧪 Шорт", "cty:short")], [("❌ Закрыть тестовую", "ctc")], [("← К списку", "m")]]

    def _confirm(self, text, yes, back):
        return text, [[("✅ Да", yes), ("Отмена", back)]]

    def _s_confirm_auto(self, user, cid, which, live=False):
        c = self._cfg(user, cid)
        what = "открывать позиции по сигналу" if which == "o" else "управлять позицией (стоп в БУ, закрытия)"
        where = f"на {self._ad(user).describe()['name']}" if self._ad(user).real else "(симуляция — только запись в журнал)"
        return self._confirm(f"🤖 Включить автомод «{what}» для <b>#{c['id']} {esc(c['name'])}</b>?\nБот будет делать это сам {where}.", f"ay:{c['id']}:{which}", f"c:{c['id']}")

    def _s_confirm_del(self, user, cid, live=False):
        c = self._cfg(user, cid)
        return self._confirm(f"🗑 Удалить конфиг <b>#{c['id']} {esc(c['name'])}</b>? Бот перестанет по нему считать и присылать сигналы. Открытая на бирже позиция останется со своими стопом и тейками, но бот её больше не ведёт (БУ, закрытие по развороту, уведомления); стоящая лимитка на вход будет снята.", f"dy:{c['id']}", f"c:{c['id']}")

    def _s_confirm_test(self, user, side, live=False):
        d = self._ad(user).describe()
        return self._confirm(f"🧪 Открыть тестовую позицию по BTC ({'лонг' if side == 'long' else 'шорт'}), маржа 1 $?" + (f" Это <b>реальный</b> ордер на {esc(d['name'])}." if d["real"] else " Биржа не подключена — симуляция."), f"tgo:{side}", "t")

    def _s_confirm_close(self, user, live=False):
        return self._confirm("❌ Закрыть позицию по BTC на бирже целиком (рыночным ордером) и снять висящие ордера?", "tcgo", "t")

    def _s_confirm_hl(self, user, live=False):
        return self._confirm("🔌 Подключить <b>БОЕВУЮ</b> сеть Hyperliquid? Автомод сможет торговать реальными деньгами. Присылайте ключ только API-кошелька с ограниченным сроком действия.", "hl:m", "x")

    def _s_confirm_disconnect(self, user, live=False):
        return self._confirm("🔌 Отключить счёт Hyperliquid? Сохранённый ключ будет удалён, автомод выключится у всех конфигов. Открытые на бирже позиции это не закрывает.", "dxy", "x")

    PCTS = (5, 10, 15, 20, 25, 30, 50, 100)
    LEVS = (1, 2, 3, 5, 10, 15, 20, 25)

    def _s_sizing(self, user, cid, live=False):
        c = self._cfg(user, cid)
        p = c["params"]
        pyr = p.get("pyramid_enabled")
        d = self._ad(user).describe()
        body = [f"📐 <b>#{c['id']} {esc(c['name'])}</b> · размер входа",
                f"Доля депозита на вход: <b>{p['trade_pct']:g}%</b> · плечо: <b>{p['leverage']:g}×</b> → объём {p['trade_pct'] * p['leverage']:g}% депозита"
                + (f"\nДобор: <b>{p.get('pyramid_pct') or 0:g}%</b> · плечо добора <b>{(p.get('pyramid_leverage') or p['leverage']):g}×</b>" if pyr else ""),
                "База: " + ("<b>баланс счёта</b> на бирже" if c["auto"].get("base") == "account" else "<b>депозит из конфига</b> " + f"({p['deposit']:g} $)"),
                "<i>Меняет размер НОВЫХ входов и расчёты по истории; позиции, уже открытые на бирже, не меняются.</i>"
                + ("" if not d["real"] else "\n<i>Любое другое значение: /size " + f"{c['id']} ПРОЦЕНТ ПЛЕЧО</i>")]
        kb = [[(f"{'✅' if abs(p['trade_pct'] - v) < 1e-9 else ''}{v}%", f"szp:{c['id']}:{v}") for v in self.PCTS[:4]], [(f"{'✅' if abs(p['trade_pct'] - v) < 1e-9 else ''}{v}%", f"szp:{c['id']}:{v}") for v in self.PCTS[4:]],
              [(f"{'✅' if abs(p['leverage'] - v) < 1e-9 else ''}{v}×", f"szl:{c['id']}:{v}") for v in self.LEVS[:4]], [(f"{'✅' if abs(p['leverage'] - v) < 1e-9 else ''}{v}×", f"szl:{c['id']}:{v}") for v in self.LEVS[4:]],
              [("← Карточка", f"c:{c['id']}")]]
        body.insert(1, "Доля (ряды 1–2) и плечо (ряды 3–4):")
        return "\n".join(body), kb

    def _s_confirm_panic(self, user, live=False):
        d = self._ad(user).describe()
        where = f"на {esc(d['name'].capitalize())} ({'тестовая сеть' if d['testnet'] else '<b>БОЕВАЯ сеть</b>'})" if d["real"] else "(счёт не подключён — закрывать нечего, только выключится автомод)"
        return self._confirm(f"🚨 <b>СТОП ВСЁ</b>\nАвтомод выключится у ВСЕХ конфигов, все позиции {where} закроются по рынку, все ордера (стопы, тейки) снимутся. Продолжить?", "pny", "m")

    def _s_confirm_off(self, user, live=False):
        return self._confirm("⛔ Выключить автомод у ВСЕХ ваших конфигов? Уже открытые на бирже позиции это не закрывает.", "offy", "m")

    # ------------------------------------------------------------------ вход
    def open(self, chat, user, screen="m"):
        """Новое сообщение с экраном (команда /monitor)."""
        body, kb = self.render(user, screen)
        self.svc.tg.send_message(chat, self._wrap(body, False), keyboard=kb)

    @staticmethod
    def _wrap(body, live, until=None):
        now = time.strftime("%H:%M:%S", time.gmtime())
        foot = f"\n\n🕒 {now} UTC" + (f" · 🔴 LIVE до {time.strftime('%H:%M', time.gmtime(until))}" if live and until else "")
        return body + foot

    def handle_callback(self, cb):
        tg = self.svc.tg
        cid_ = cb["id"]
        user = self.db.user_by_telegram((cb.get("from") or {}).get("id"))
        msg = cb.get("message")
        if not user:
            tg.answer_callback(cid_, "Аккаунт не привязан: /link КОД", alert=True)
            return
        if not msg:
            tg.answer_callback(cid_)
            return
        chat, mid = msg["chat"]["id"], msg["message_id"]
        data = cb.get("data") or ""
        toast, alert, screen = None, False, None
        try:
            toast, alert, screen = self._act(user, data, chat, mid)
        except manage.ManageError as e:
            toast, alert, screen = str(e), True, "m" if e.code == 404 else None
        except Exception as e:                                                     # одна кнопка не должна ронять бота
            log.exception("callback %s", data)
            toast, alert = f"Ошибка: {e}"[:180], True
        tg.answer_callback(cid_, toast, alert)
        if screen is not None:
            self._show(chat, mid, user, screen)

    def _act(self, user, data, chat, mid):
        """-> (всплывающее сообщение, это предупреждение?, экран для показа или None). Экраны-строки совпадают с callback_data кнопок."""
        uid = user["id"]
        kind, *a = data.split(":")
        if kind == "noop":
            return None, False, None
        if kind == "a":                                                            # переключатель автомода: включение — через подтверждение, выключение сразу
            c = self._cfg(user, a[0])
            if c["paused"]:
                return "Конфиг на паузе — сначала возобновите", True, None
            cur = c["auto"]["open" if a[1] == "o" else "manage"]
            if not cur:
                return None, False, f"ca:{a[0]}:{a[1]}"
            manage.set_auto(self.db, self._ad(user), uid, c["id"], open_=False if a[1] == "o" else None, manage=False if a[1] == "m" else None)
            return "Автомод выключен", False, f"c:{a[0]}"
        if kind == "cu":                                                           # опция «догнать свежий сигнал»: параметры — командой /catchup
            c = self._cfg(user, a[0])
            cu = manage.set_catchup(self.db, self._ad(user), uid, c["id"], on=not c["catchup"]["on"])
            return ("Догоняю сигнал: бот проверит последний сигнал при ближайшей проверке" if cu["armed"] else "Включено: сработает при включении автомода «открывать»" if cu["on"] else "Выключено"), False, f"c:{a[0]}"
        if kind == "ab":                                                           # база размера входа: депозит конфига <-> реальный баланс счёта
            c = self._cfg(user, a[0])
            new = "config" if c["auto"].get("base") == "account" else "account"
            manage.set_auto(self.db, self._ad(user), uid, c["id"], base=new)
            return ("Размер входа теперь считается от баланса счёта на бирже" if new == "account" else "Размер входа считается от депозита конфига"), False, f"c:{a[0]}"
        if kind in ("szp", "szl"):
            c = self._cfg(user, a[0])
            r = manage.set_sizing(self.db, self._ad(user), uid, c["id"], **({"trade_pct": float(a[1])} if kind == "szp" else {"leverage": float(a[1])}))
            self.svc.forget(c["id"])
            return f"Теперь: {r['trade_pct']:g}% депозита × {r['leverage']:g}×", False, f"sz:{a[0]}"
        if kind == "ay":
            c = self._cfg(user, a[0])
            manage.set_auto(self.db, self._ad(user), uid, c["id"], open_=True if a[1] == "o" else None, manage=True if a[1] == "m" else None)
            return ("Автомод включён" + ("" if self._ad(user).real else " (симуляция)")), False, f"c:{a[0]}"
        if kind == "p":
            c = self._cfg(user, a[0])
            manage.set_paused(self.db, uid, c["id"], not c["paused"])
            self.svc.forget(c["id"])
            return ("Конфиг на паузе, автомод выключен" if not c["paused"] else "Конфиг возобновлён"), False, f"c:{a[0]}"
        if kind == "dy":
            name = manage.remove_config(self.db, uid, a[0], self._ad(user))
            return f"Удалён: {name}", False, "m"
        if kind == "e":
            c = self._cfg(user, a[0])
            import json
            body = json.dumps({"format": "maverick-config", "version": 1, "name": c["name"], "tf": c["tf"], "params": c["params"]}, ensure_ascii=False, indent=2)
            self.svc.tg.send_document(chat, f"maverick-config_{c['id']}.json", body.encode("utf-8"), caption=c["name"])
            return "Файл отправлен", False, None
        if kind == "tgo":
            r = manage.test_open(self.db, self._ad(user), uid, a[0], 1.0)
            txt = ("Симуляция: ордер не отправлялся" if r["status"] == "dry_run" else f"Открыта: маржа {r['margin']:g} $ × {r['leverage']}× = объём {r['notional']:g} $")
            return txt, True, "t"
        if kind == "tcgo":
            r = manage.test_close(self.db, self._ad(user), uid)
            return ("Симуляция: закрывать нечего" if r["status"] == "dry_run" else "На бирже нет позиции по BTC" if r["status"] == "skipped" else "Позиция по BTC закрыта"), True, "t"
        if kind == "hl":                                                           # начать подключение счёта: дальше бот ждёт адрес и ключ обычными сообщениями
            self.svc.start_hl_connect(chat, user, testnet=(a[0] != "m"))
            return "Жду адрес кошелька", False, None
        if kind == "hlx":
            self.svc.cancel_hl(user)
            return "Подключение отменено", False, "x"
        if kind == "dxy":
            manage.disconnect_account(self.db, uid)
            return "Счёт отключён, автомод выключен", False, "x"
        if kind == "pny":
            r = manage.panic(self.db, self._ad(user), uid)
            txt = f"Автомод выкл. у {r['auto_off']}; закрыто позиций {len(r['closed'])}, снято ордеров {r['cancelled']}" + (f"; ОШИБКИ: {len(r['errors'])} — см. /tradelog" if r["errors"] else "")
            if r["errors"]:
                self.svc.send(chat, "🚨 <b>СТОП ВСЁ: есть ошибки</b>\n" + "\n".join(f"• {esc(str(e)[:200])}" for e in r["errors"]) + "\nПроверьте счёт на бирже вручную.")
            return txt[:190], bool(r["errors"]), "x" if r["closed"] or r["errors"] else "m"
        if kind == "offy":
            n = manage.auto_off_all(self.db, uid)
            return f"Автомод выключен у конфигов: {n}", False, "m"
        if kind == "lv":
            screen = data[3:]
            key = (chat, mid)
            if key in self.live:
                del self.live[key]
                return "Live остановлен", False, screen
            self.live[key] = {"screen": screen, "uid": uid, "until": time.time() + LIVE_TTL, "sig": None}
            return f"Live включён на {LIVE_TTL // 60} мин", False, screen
        if kind in ("c", "v", "ca", "cd", "sz"):
            self._cfg(user, a[0])                                                  # чужой или удалённый конфиг — сразу понятная ошибка, а не пустой экран
        return None, False, data                                                   # всё остальное — просто экран

    def _show(self, chat, mid, user, screen):
        key = (chat, mid)
        sub = self.live.get(key)
        live = sub is not None
        try:
            body, kb = self.render(user, screen, live)
        except manage.ManageError as e:
            body, kb = f"⚠️ {esc(str(e))}", [[("← К списку", "m")]]
        if sub:
            if screen.split(":")[0] in LIVE_SCREENS:
                sub["screen"] = screen
            else:
                del self.live[key]                                                 # ушли на подтверждение/тест — live снимаем
                live = False
                body, kb = self.render(user, screen, False)
            sub and self.live.get(key) and self.live[key].update(sig=self._sig(body, kb), until=self.live[key]["until"])
        try:
            self.svc.tg.edit_message(chat, mid, self._wrap(body, live, self.live[key]["until"] if key in self.live else None), kb)
        except TelegramError as e:
            log.warning("не удалось обновить экран: %s", e)

    @staticmethod
    def _sig(body, kb):
        return hashlib.md5((body + repr(kb)).encode()).hexdigest()

    # ------------------------------------------------------------------ live
    def refresh_live(self):
        """Перерисовывает подписанные экраны, у которых изменилось содержимое; просроченные подписки снимает."""
        now = time.time()
        for key, sub in list(self.live.items()):
            chat, mid = key
            user = self.db.get_user(sub["uid"])
            if not user:
                self.live.pop(key, None)
                continue
            expired = now >= sub["until"]
            try:
                body, kb = self.render(user, sub["screen"], not expired)
            except manage.ManageError:
                self.live.pop(key, None)
                continue
            sig = self._sig(body, kb)
            if expired:
                self.live.pop(key, None)
            elif sig == sub["sig"]:
                continue
            try:
                self.svc.tg.edit_message(chat, mid, self._wrap(body, not expired, sub["until"]), kb)
                sub["sig"] = sig
            except TelegramError as e:
                if any(w in str(e) for w in ("not found", "can't be edited", "message to edit")):
                    self.live.pop(key, None)
                else:
                    log.warning("live: %s", e)
