"""Оформление сообщений бота (HTML-разметка Telegram)."""
from datetime import datetime, timezone
from html import escape as esc


def num(v, d=2):
    return "–" if v is None else f"{v:,.{d}f}".replace(",", " ")


def price(v):
    if v is None:
        return "–"
    d = 2 if v >= 1000 else 3 if v >= 10 else 4 if v >= 1 else 6
    return num(v, d)


def usd(v, d=0):
    if v is None:
        return "–"
    return ("+" if v > 0 else "−" if v < 0 else "") + num(abs(v), d) + " $"


def pct(v, d=2, sign=True):
    if v is None:
        return "–"
    return (("+" if v > 0 else "−" if v < 0 else "") if sign else "") + f"{abs(v):.{d}f}%"


def ts_str(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def day_str(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d")


def side_line(side):
    return "🟢 <b>ЛОНГ</b>" if side == "long" else "🔴 <b>ШОРТ</b>"


def tps_line(plan, base_price=None):
    rows = []
    for i, l in enumerate(plan["levels"], 1):
        rows.append(f"  TP{i}  {pct(l['pct'], 1)}  →  <code>{price(l['price'])}</code>  ({l['share']:g}%)")
    return rows


def stats_block(stats, params, rng=None, title="📈 <b>Статистика конфига</b>"):
    """Итоги бэктеста конфига: сделки, winrate, PF, доходность, просадка, Шарп."""
    t = stats
    if not t or not t.get("trades"):
        return f"{title}\nСделок в истории пока нет."
    period = f" · {day_str(rng['first_ts'])} … {day_str(rng['last_ts'])}" if rng else ""
    pf = "∞" if t["profit_factor"] is None else f"{t['profit_factor']:.2f}"
    sharpe = "–" if t.get("sharpe") is None else f"{t['sharpe']:.2f}"
    lines = [
        f"{title}{esc(period)}",
        f"Сделок <b>{t['trades']}</b> · Winrate <b>{t['winrate']:.1f}%</b> · PF <b>{pf}</b>",
        f"Итог <b>{usd(t['total_usd'])}</b>" + (f" (<b>{pct(t['return_pct'], 1)}</b> от депозита)" if t.get("return_pct") is not None else ""),
        f"Просадка <b>{pct(t['max_dd_pct'], 1, sign=False) if t.get('max_dd_pct') is not None else '–'}</b>"
        + (f" (с открытыми позициями <b>{pct(t['max_dd_real_pct'], 1, sign=False)}</b>)" if t.get("max_dd_real_pct") is not None else "")
        + f" · Sharpe <b>{sharpe}</b>",
        f"Средняя сделка {pct(t['avg_pct'], 3)} · ср. плюс/минус {usd(t['avg_win_usd'])} / {usd(t['avg_loss_usd'])}",
        f"Серия убытков до {t['max_loss_streak']} · TP {t['tp']} · SL {t['sl']} · БУ {t['be']}" + (f" · ликвидаций {t['liq']}" if t.get("liq") else "") + (f" · закрыто в пятницу {t['wknd']}" if t.get("wknd") else ""),
    ]
    if t.get("adds"):
        lines.append(f"Позиций {t['positions']} · доборов {t['adds']} (сделок считается по ногам)")
    if params and params.get("funding_apr") and t.get("funding_usd") is not None:
        lines.append(f"Фандинг {params['funding_apr']:g}% годовых / {params.get('funding_period_h', 8)} ч: {'уплачено' if t['funding_usd'] >= 0 else 'получено'} <b>{usd(abs(t['funding_usd']))}</b> (уже учтён в итоге)")
    return "\n".join(lines)


def sizing_line(result, add=False):
    p, t = result["params"], result["stats"]["total"]
    bal = p["deposit"] + (t["total_usd"] if p.get("compound", True) else 0)
    if bal <= 0:
        return "💼 Счёт в бэктесте обнулён"
    base = bal if p.get("compound", True) else p["deposit"]
    pct = p["pyramid_pct"] if add and p.get("pyramid_pct") else p["trade_pct"]
    notional = base * pct / 100 * p["leverage"]
    mode = "сложный %" if p.get("compound", True) else "фикс. сумма"
    return f"💼 Объём ≈ <b>{num(notional)} $</b> (маржа {num(notional / p['leverage'])} $ · {pct:g}% × {p['leverage']:g}× · {mode}; баланс {num(bal)} $)"


ONLY_NAMES = {"local": "DL", "scalp": "DS", "global": "DG"}


def config_summary(cfg, result=None):
    p = cfg["params"]
    bits = [f"{esc(str(p.get('source')))} · {esc(str(p.get('symbol')))}", f"side {p.get('side')}", f"подтв. {p.get('confirm')}"]
    if p.get("only"):
        bits.append(f"только {ONLY_NAMES.get(p['only'], p['only'])}")
    if p.get("global_min") is not None:
        bits.append(f"DG ≥ {p['global_min']:g}")
    if p.get("short_global_max") is not None:
        bits.append(f"шорт DG ≤ {p['short_global_max']:g}")
    if p.get("trend_enabled"):
        bits.append("тренд EMA " + "/".join(str(x) for x in p.get("trend_emas", [])) + f" {p.get('trend_tf')}")
    if p.get("top_enabled"):
        bits.append("топ-коины")
    if p.get("pyramid_enabled"):
        bits.append(f"пирамидинг до {p.get('pyramid_max')}×{p.get('pyramid_pct'):g}%" + (" с усреднением" if p.get("pyramid_avg") else ""))
    if p.get("signal_gap_h"):
        bits.append(f"пауза между сигналами {p['signal_gap_h']:g} ч")
    if p.get("sl_hold_strong"):
        bits.append("SL после остывания метрик")
    if p.get("weekdays_only"):
        bits.append("только будни (пн Токио → пт Нью-Йорк)")
    if p.get("liquidation_sim"):
        bits.append(f"ликвидация {p.get('maint_margin_pct', 0.5):g}%")
    if p.get("funding_apr"):
        bits.append(f"фандинг {p['funding_apr']:g}% год./{p.get('funding_period_h', 8)} ч" + (" (платят обе стороны)" if p.get("funding_mode") == "cost" else ""))
    if p.get("pyramid_enabled") and p.get("pyramid_leverage"):
        bits.append(f"плечо доборов {p['pyramid_leverage']:g}×")
    return " · ".join(bits)


def signal_message(cfg, result, sig, report, plan, entry_price, top, source_label, stale_note=None):
    p = result["params"]
    add = bool(sig.get("pyramid"))
    head = [
        f"{side_line(sig['side'])}  ·  <b>{esc(str(p['symbol']))}</b>" + (f"  ·  ➕ <b>ДОБОР #{sig.get('leg', 2) - 1}</b> к открытой позиции" if add else ""),
        f"📋 Конфиг: <b>{esc(cfg['name'])}</b>  <i>({esc(source_label)})</i>",
        f"🕒 Отчёт: {ts_str(sig['ts'])}",
    ]
    if stale_note:
        head.append(f"⚠️ {esc(stale_note)}")
    m = []
    if report:
        dg = "–" if report.get("dg") is None else f"{report['dg']:.2f}"
        m.append(f"📊 <b>Метрики отчёта</b>\nDG <b>{dg}</b> · DL <b>{report['dl']:.2f}</b> · DS <b>{report['ds']:.2f}</b>")
        if report.get("liq_long_k") is not None:
            m[-1] += f"\nЛиквидации: лонги {num(report['liq_long_k'], 1)}k · шорты {num(report['liq_short_k'], 1)}k"
        if report.get("top_coin"):
            m[-1] += f"\n⭐ Топ-коин: {esc(report['top_coin'])} {report['top_value']:.2f}"
    trade = ["🎯 <b>План сделки</b>"]
    if plan and entry_price:
        trade.append(f"Вход ≈ <code>{price(entry_price)}</code> (текущая цена; в бэктесте — open следующей свечи)")
        trade += tps_line(plan)
        trade.append(f"  SL  {pct(-plan['sl_pct'] if sig['side'] == 'long' else plan['sl_pct'], 1)}  →  <code>{price(plan['sl'])}</code>"
                     + ("  ·  после TP1 стоп в безубыток" if p.get("be_after_tp1") and len(p["tps"]) > 1 else ""))
    else:
        tp = " · ".join(f"TP{i} {pct(l['pct'] if sig['side'] == 'long' else -l['pct'], 1)} ({l['share']:g}%)" for i, l in enumerate(p["tps"], 1))
        trade.append(f"{tp} · SL {pct(-p['sl_pct'] if sig['side'] == 'long' else p['sl_pct'], 1)}"
                     + ("  ·  БУ после TP1" if p.get("be_after_tp1") and len(p["tps"]) > 1 else ""))
        trade.append("<i>Цена биржи недоступна — уровни в процентах от цены входа.</i>")
    if add and sig.get("avg_entry"):
        trade.append(f"🔁 Усреднение: новая средняя ≈ <code>{price(sig['avg_entry'])}</code> · TP "
                     + " / ".join(f"<code>{price(x)}</code>" for x in sig["avg_tps"]) + f" · SL <code>{price(sig['avg_sl'])}</code> (от средней цены)")
    elif add and p.get("pyramid_avg"):
        trade.append("🔁 Усреднение: тейки и стоп пересчитываются от новой средней цены входа")
    trade.append(sizing_line(result, add))
    blocks = ["\n".join(head), *m, "\n".join(trade)]
    if top:
        blocks.append(top_block(top, sig, p, result))
    blocks.append(stats_block(result["stats"]["total"], p, result["range"]))
    return "\n\n".join(blocks)


FILL_TITLES = {"TP": "✅ <b>{label} исполнен</b>", "SL": "🛑 <b>Стоп-лосс</b>", "BE": "⚖️ <b>Закрытие в безубытке</b>", "REV": "🔄 <b>Разворот — позиция закрыта по рынку</b>", "LIQ": "💥 <b>ЛИКВИДАЦИЯ</b>", "WKND": "📅 <b>Закрытие к концу недели (пятница, закрытие Нью-Йорка)</b>"}


def fill_message(cfg, result, ev, source_label):
    """Уведомление об исполнении тейка / стопа / безубытка / разворота по ноге или позиции конфига."""
    p = result["params"]
    title = FILL_TITLES["TP" if ev["label"].startswith("TP") else ev["label"]].format(label=ev["label"])
    what = "усреднённая позиция" if ev["avg"] else (f"добор #{ev['leg'] - 1}" if ev["pyramid"] else "позиция")
    lines = [
        f"{title}  ·  {side_line(ev['side'])}  ·  <b>{esc(str(p['symbol']))}</b>",
        f"📋 Конфиг: <b>{esc(cfg['name'])}</b>  <i>({esc(source_label)})</i>",
        f"🕒 {ts_str(ev['ts'])}  ·  {what}, вход <code>{price(ev['entry'])}</code>" + (f" (средняя из {ev['adds_n'] + 1} входов)" if ev["avg"] and ev["adds_n"] else ""),
        f"Цена исполнения ≈ <code>{price(ev['price'])}</code> · закрыто <b>{ev['share'] * 100:.0f}%</b> позиции",
    ]
    if ev.get("tp1_be"):
        lines.append(f"Стоп перенесён в безубыток (<code>{price(ev['entry'])}</code>)")
    m = ev.get("metrics")
    if m:
        dg = "–" if m.get("dg") is None else f"{m['dg']:.2f}"
        lines.append(f"📊 Метрики в этот момент: DG <b>{dg}</b> · DL <b>{m['dl']:.2f}</b> · DS <b>{m['ds']:.2f}</b>")
    if ev["closed"]:
        hold = ev["hold_h"]
        held = f"{hold:.1f} ч" if hold < 48 else f"{hold / 24:.1f} сут"
        lines.append(f"🏁 {'Позиция' if not ev['pyramid'] else 'Нога'} закрыта · итог <b>{usd(ev['pnl_usd'])}</b> ({pct(ev['pnl_pct'])} на объём) · держалась {held}")
    else:
        lines.append("Остаток позиции продолжает идти.")
    lines.append("<i>Расчёт по свечам: исполнение — начало минуты, в которой цена достигла уровня.</i>")
    return "\n".join(lines)


def positions_message(cfg, result, price_now, last_candle_ts):
    """Состояние конфига сейчас (по расчёту бэктеста на свечах): открытая позиция с ногами, уровнями и результатом либо последняя закрытая сделка."""
    p = result["params"]
    head = f"📍 <b>#{cfg['id']} {esc(cfg['name'])}</b> · {esc(str(p['symbol']))}"
    open_legs = [t for t in result["trades"] if t["reason"] == "OPEN"]
    if not open_legs:
        last = result["trades"][-1] if result["trades"] else None
        tail = (f"Последняя сделка: {side_line(last['side'])} {ts_str(last['entry_ts'])} → {ts_str(last['exit_ts'])}, {last['reason']}, {usd(last['pnl_usd'])}"
                if last else "Сделок по этому конфигу пока не было.")
        return f"{head}\nПозиции сейчас нет.\n{tail}"
    side = open_legs[0]["side"]
    d = 1 if side == "long" else -1
    lines = [f"{head}", f"{side_line(side)} · открыта с {ts_str(min(t['entry_ts'] for t in open_legs))} · " + (f"ног: {len(open_legs)}" if len(open_legs) > 1 else "одна нога")]
    for t in sorted(open_legs, key=lambda x: x["entry_ts"]):
        name = ("усреднённая позиция" + (f" (вход средней из {t['adds_n'] + 1})" if t.get("adds_n") else "")) if t.get("avg") else (f"добор #{t.get('leg', 1) - 1}" if t.get("pyramid") else "вход")
        stop = t["entry"] if (t["be"] and t["tp_hits"] >= 1) else t["sl"]
        move = d * (price_now / t["entry"] - 1) * 100 if price_now else None
        left = [f"TP{i} <code>{price(x)}</code>" for i, x in enumerate(t["tps"], 1) if i > t["tp_hits"]]
        lines.append(f"• {name}: <code>{price(t['entry'])}</code> · {'БУ' if stop == t['entry'] and t['tp_hits'] else 'SL'} <code>{price(stop)}</code> · "
                     + (("осталось " + " / ".join(left)) if left else "тейки исполнены") + f" · исполнено тейков {t['tp_hits']}/{len(t['tps'])}"
                     + (f"\n   сейчас {pct(move, 2)} от входа · результат {usd(t['pnl_usd'])}" if move is not None else f"\n   результат {usd(t['pnl_usd'])}"))
    lines.append(f"Итого по позиции: <b>{usd(sum(t['pnl_usd'] for t in open_legs))}</b> (с учётом уже исполненных тейков)")
    lines.append(f"<i>Цена {price(price_now)} · расчёт по свечам до {ts_str(last_candle_ts)}</i>")
    return "\n".join(lines)


def ready_message(cfg, result, items, source_label):
    """Конфиг почти готов к входу: серия подтверждена, но вход держат фильтры. items — result['ready_now'] (по сторонам)."""
    p = result["params"]
    blocks = []
    for it in items:
        what = "ДОБОР" if it.get("add") else ("ЛОНГ" if it["side"] == "long" else "ШОРТ")
        lines = [f"⏳ <b>Готов к {what}, ждёт подтверждения</b>  ·  <b>{esc(str(p['symbol']))}</b>",
                 f"📋 Конфиг: <b>{esc(cfg['name'])}</b>  <i>({esc(source_label)})</i>",
                 f"🕒 Отчёт: {ts_str(it['ts'])} — серия из {p.get('confirm')} подряд подтверждена"]
        lines += [f"• {esc(x)}" for x in it["lines"]]
        lines.append("<i>Вход будет на первом отчёте, где выполнены все условия, пока серия не прервётся.</i>")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def ready_cancelled_message(cfg, result, key):
    side = "ЛОНГ" if key.split("|")[0] == "long" else "ШОРТ"
    return (f"⌛ <b>Ожидание отменено</b>  ·  <b>{esc(str(result['params']['symbol']))}</b>\n📋 Конфиг: <b>{esc(cfg['name'])}</b>\n"
            f"Серия по {side} прервалась до подтверждения — входа не было.")


def top_block(top, sig, p, result):
    if "skip" in top:
        return f"🪙 <b>Топ-коин</b>: пропущен — {esc(top['skip'])}"
    lines = [f"🪙 <b>Топ-коин {esc(top['coin'])}</b> ({top['value']:.2f}) → {side_line(sig['side'])} {esc(top['symbol'])}"]
    plan = top.get("plan")
    if plan:
        lines.append(f"Вход ≈ <code>{price(top['price'])}</code>")
        lines += tps_line(plan)
        lines.append(f"  SL  {pct(-plan['sl_pct'] if sig['side'] == 'long' else plan['sl_pct'], 1)}  →  <code>{price(plan['sl'])}</code> · держать до {p['top_days']:g} сут")
    else:
        lines.append("Цена биржи недоступна — уровни в процентах от цены входа.")
    lines.append(f"Размер {num(p['top_size'])} $")
    ts = result["top"].get("stats") if result.get("top", {}).get("enabled") else None
    if ts and ts.get("trades"):
        n_pend = result["top"]["pending"]["windows"]
        lines.append(f"История топ-коинов: сделок {ts['trades']} · winrate {ts['winrate']:.0f}% · итог {usd(ts['total_usd'])}"
                     + (f" <i>(ещё считаются свечи: окон {n_pend})</i>" if n_pend else ""))
    return "\n".join(lines)


def accepted_message(cfg, result):
    return "\n\n".join([
        f"✅ Конфиг <b>{esc(cfg['name'])}</b> (#{cfg['id']}) принят",
        esc(config_summary(cfg)),
        stats_block(result["stats"]["total"], cfg["params"], result["range"]),
        sizing_line(result),
        "Буду присылать каждый новый сигнал по этому конфигу, а затем — каждое исполнение: TP1–TP3, стоп-лосс, безубыток, разворот. Старые сигналы и исполнения не присылаются.",
    ])


def list_message(configs, latest_by_id=None):
    if not configs:
        return "Конфигов пока нет. Пришлите JSON-файл конфига (кнопка «Скачать конфиг» в интерфейсе) или вставьте его текстом."
    rows = ["📚 <b>Конфиги</b>"]
    for c in configs:
        state = "⏸ пауза" if c.get("paused") else ("⚠️ ошибка" if c.get("error") else ("🔔 активен" if c.get("baseline") else "⏳ считается"))
        rows.append(f"\n<b>#{c['id']} {esc(c['name'])}</b> — {state}\n{esc(config_summary(c))}\nСигналов отправлено: {c.get('alerts', 0)}"
                    + (f"\n<i>{esc(str(c['error'])[:200])}</i>" if c.get("error") else ""))
    return "\n".join(rows)


WELCOME = """🤖 <b>Бот сигналов Maverick</b>

Чтобы пользоваться ботом, привяжите Telegram к своему аккаунту на сайте:
1. Войдите на сайт → меню пользователя (вверху справа) → «Привязать Telegram»
2. Отправьте мне показанную команду: <code>/link КОД</code>

Аккаунты создаёт администратор в консоли сервера (<code>python cli.py user add</code>)."""

NOT_LINKED = "🔒 Этот Telegram не привязан к аккаунту. Получите код на сайте (меню пользователя → «Привязать Telegram») и отправьте <code>/link КОД</code>. /start — подробнее."

HELP = """🤖 <b>Бот сигналов Maverick</b>

Присылаю каждый новый сигнал по вашим конфигам вместе со статистикой конфига (winrate, PF, доходность, просадка, Sharpe), а также каждое исполнение: тейки, стоп-лосс, безубыток, разворот, добор. Конфиги у каждого пользователя свои.

<b>Как добавить конфиг</b>
• Файлом: в интерфейсе «⬇ Скачать конфиг» → отправьте файл .json сюда (подпись к файлу = имя конфига)
• Текстом: «Копировать» в интерфейсе → вставьте сообщение сюда. Можно с именем: <code>/add Мой конфиг {…}</code>
• Можно прислать сразу несколько: <code>{"configs": [ … ]}</code>

<b>Команды</b>
/monitor — мониторинг: стратегии, открытые позиции, автомод, пауза, live (кнопки)
/hl — подключить счёт Hyperliquid (адрес кошелька + ключ API-кошелька) и посмотреть его
/tradelog [N] — лог автоторговли файлом (решения автомода, запросы к бирже, ошибки)
/list — ваши конфиги и их состояние
/stats &lt;№|имя&gt; — свежая статистика конфига
/get &lt;№|имя&gt; — получить конфиг файлом
/rename &lt;№|имя&gt; &lt;новое имя&gt;
/pause &lt;№|имя&gt; · /resume &lt;№|имя&gt;
/remove &lt;№|имя&gt;
/check — проверить новые сигналы сейчас
/cancel — остановить долгий подсчёт (не посчитанные конфиги встанут на паузу)
/positions [№|имя] — открытые позиции по конфигам и их текущий результат
/status — состояние данных и подгрузки
/unlink — отвязать Telegram от аккаунта"""
