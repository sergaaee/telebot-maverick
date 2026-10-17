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
        f"Просадка <b>{pct(t['max_dd_pct'], 1, sign=False) if t.get('max_dd_pct') is not None else '–'}</b> · Sharpe <b>{sharpe}</b>",
        f"Средняя сделка {pct(t['avg_pct'], 3)} · ср. плюс/минус {usd(t['avg_win_usd'])} / {usd(t['avg_loss_usd'])}",
        f"Серия убытков до {t['max_loss_streak']} · TP {t['tp']} · SL {t['sl']} · БУ {t['be']}",
    ]
    return "\n".join(lines)


def sizing_line(result):
    p, t = result["params"], result["stats"]["total"]
    bal = p["deposit"] + (t["total_usd"] if p.get("compound", True) else 0)
    if bal <= 0:
        return "💼 Счёт в бэктесте обнулён"
    base = bal if p.get("compound", True) else p["deposit"]
    notional = base * p["trade_pct"] / 100 * p["leverage"]
    mode = "сложный %" if p.get("compound", True) else "фикс. сумма"
    return f"💼 Объём ≈ <b>{num(notional)} $</b> (маржа {num(notional / p['leverage'])} $ · {p['trade_pct']:g}% × {p['leverage']:g}× · {mode}; баланс {num(bal)} $)"


def config_summary(cfg, result=None):
    p = cfg["params"]
    bits = [f"{esc(str(p.get('source')))} · {esc(str(p.get('symbol')))}", f"side {p.get('side')}", f"подтв. {p.get('confirm')}"]
    if p.get("only"):
        bits.append(f"только {p['only']}")
    if p.get("trend_enabled"):
        bits.append("тренд EMA " + "/".join(str(x) for x in p.get("trend_emas", [])) + f" {p.get('trend_tf')}")
    if p.get("top_enabled"):
        bits.append("топ-коины")
    return " · ".join(bits)


def signal_message(cfg, result, sig, report, plan, entry_price, top, source_label, stale_note=None):
    p = result["params"]
    head = [
        f"{side_line(sig['side'])}  ·  <b>{esc(str(p['symbol']))}</b>",
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
    trade.append(sizing_line(result))
    blocks = ["\n".join(head), *m, "\n".join(trade)]
    if top:
        blocks.append(top_block(top, sig, p, result))
    blocks.append(stats_block(result["stats"]["total"], p, result["range"]))
    return "\n\n".join(blocks)


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
        "Буду присылать каждый новый сигнал по этому конфигу. Старые сигналы не присылаются.",
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

Присылаю каждый новый сигнал по вашим конфигам вместе со статистикой конфига (winrate, PF, доходность, просадка, Sharpe). Конфиги у каждого пользователя свои.

<b>Как добавить конфиг</b>
• Файлом: в интерфейсе «⬇ Скачать конфиг» → отправьте файл .json сюда (подпись к файлу = имя конфига)
• Текстом: «Копировать» в интерфейсе → вставьте сообщение сюда. Можно с именем: <code>/add Мой конфиг {…}</code>
• Можно прислать сразу несколько: <code>{"configs": [ … ]}</code>

<b>Команды</b>
/list — ваши конфиги и их состояние
/stats &lt;№|имя&gt; — свежая статистика конфига
/get &lt;№|имя&gt; — получить конфиг файлом
/rename &lt;№|имя&gt; &lt;новое имя&gt;
/pause &lt;№|имя&gt; · /resume &lt;№|имя&gt;
/remove &lt;№|имя&gt;
/check — проверить новые сигналы сейчас
/cancel — остановить долгий подсчёт (не посчитанные конфиги встанут на паузу)
/status — состояние данных и подгрузки
/unlink — отвязать Telegram от аккаунта"""
