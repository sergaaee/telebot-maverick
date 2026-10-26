"""Регулярная сводка в Telegram (раз в день или раз в неделю): результат стратегий за период, открытые позиции, действия автомода и состояние счёта на бирже."""
import time
from datetime import datetime, timedelta, timezone
from html import escape as esc

from .monitor import cached_result
from .service import ParamError

PERIOD_H = {"daily": 24, "weekly": 24 * 7}


def usd(v, d=2):
    return f"{'+' if v > 0 else '−' if v < 0 else ''}{abs(v):,.{d}f} $".replace(",", " ")


def due(mode, last_ts, now, hour=8):
    """Пора ли слать сводку: daily — после hour:00 UTC, если сегодняшней ещё не было; weekly — то же, но только по понедельникам."""
    if mode not in PERIOD_H:
        return False
    t = datetime.fromtimestamp(now, timezone.utc)
    if mode == "weekly" and t.weekday() != 0:
        return False
    slot = t.replace(hour=hour, minute=0, second=0, microsecond=0)
    return t >= slot and (last_ts is None or last_ts < slot.timestamp())


def build_digest(db, store, defaults, user, adapter, available_fn=None, mode="daily", now=None):
    now = time.time() if now is None else now
    since = (now - PERIOD_H[mode] * 3600) * 1000
    lines = [f"📊 <b>{'Дневная' if mode == 'daily' else 'Недельная'} сводка</b> · {datetime.fromtimestamp(now, timezone.utc):%Y-%m-%d %H:%M} UTC", ""]
    cfgs = db.list_configs(user["id"])
    total, trades_n, open_n = 0.0, 0, 0
    rows = []
    for c in cfgs:
        if c["paused"]:
            continue
        try:
            res = cached_result(store, defaults, c["params"], available_fn)
        except (ParamError, FileNotFoundError, KeyError, ValueError) as e:
            rows.append(f"❌ <b>#{c['id']} {esc(c['name'])}</b>: {esc(str(e.args[0] if e.args else e)[:100])}")
            continue
        closed = [t for t in res["trades"] if t["reason"] != "OPEN" and t["exit_ts"] >= since]
        pnl = sum(t["pnl_usd"] for t in closed)
        opened = [t for t in res["trades"] if t["reason"] == "OPEN"]
        total += pnl
        trades_n += len(closed)
        wins = sum(1 for t in closed if t["pnl_usd"] > 0)
        line = f"{'🟢' if pnl > 0 else '🔴' if pnl < 0 else '⚪'} <b>#{c['id']} {esc(c['name'][:30])}</b>: " + (f"{len(closed)} сделок ({wins} в плюс), <b>{usd(pnl)}</b>" if closed else "сделок не было")
        if opened:
            open_n += 1
            side = opened[0]["side"]
            line += f"\n    ▸ сейчас в {'лонге' if side == 'long' else 'шорте'}: {usd(sum(t['pnl_usd'] for t in opened))} (по расчёту)"
        elif res.get("ready_now"):
            line += "\n    ▸ ждёт подтверждения входа"
        rows.append(line)
    if not rows:
        lines.append("Активных стратегий нет.")
    else:
        lines.append(f"Итого по стратегиям (расчёт): <b>{usd(total)}</b> · сделок {trades_n} · с открытой позицией {open_n}")
        lines.append("")
        lines += rows
    log = [x for x in db.list_auto_log(user["id"], 500) if x["ts"] * 1000 >= since]
    if log:
        kinds = {}
        for x in log:
            if x["kind"] in ("open", "add", "move_stop", "close_weekend", "close_reverse", "panic"):
                k = (x["kind"], x["status"])
                kinds[k] = kinds.get(k, 0) + 1
        names = {"open": "входов", "add": "доборов", "move_stop": "стопов в БУ", "close_weekend": "закрытий к концу недели", "close_reverse": "закрытий по развороту", "panic": "СТОП ВСЁ"}
        if kinds:
            lines += ["", "🤖 Автомод за период: " + ", ".join(f"{names[k]} {n}" + (f" ({st})" if st not in ('sent', 'dry_run', 'ok') else "") for (k, st), n in sorted(kinds.items()))]
        errs = sum(1 for x in log if x["status"] == "error")
        if errs:
            lines.append(f"⚠️ Ошибок биржи за период: {errs} (подробнее: /tradelog)")
    if adapter.real:
        try:
            acc, pos = adapter.account(), adapter.positions()
            lines += ["", f"🏦 {esc(adapter.name.capitalize())} ({'тестовая сеть' if adapter.testnet else 'БОЕВАЯ сеть'}): капитал <b>{acc.get('equity') or 0:,.2f} $</b>".replace(",", " ")
                      + f" · позиций {len(pos)}" + (f" · нереализованный {usd(sum((p.get('pnl_usd') or 0) for p in pos))}" if pos else "")]
        except Exception as e:
            lines += ["", f"🏦 Счёт на бирже: не удалось получить данные ({esc(str(e)[:100])})"]
    lines += ["", "<i>Результаты стратегий — расчёт по свечам Binance, а не данные биржи. Отключить сводку: /digest off</i>"]
    return "\n".join(lines)
