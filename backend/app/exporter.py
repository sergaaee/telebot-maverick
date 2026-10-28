"""Выгрузка в CSV: расчётные сделки стратегий, журнал автоторговли и реальные исполнения на бирже (для налогов и разбора)."""
import csv
import io
import json
from datetime import datetime, timezone


def _iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if ms else ""


def _safe(v):
    """Ячейка не должна исполняться как формула в Excel/Sheets (имя конфига вводит пользователь)."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


def _csv(header, rows):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    for r in rows:
        w.writerow([_safe(x) for x in r])
    return "﻿" + buf.getvalue()                      # BOM: Excel открывает кириллицу правильно


def trades_csv(items):
    """items: [(имя конфига, результат бэктеста)] -> CSV закрытых и открытых сделок (каждая нога отдельной строкой)."""
    rows = []
    for name, res in items:
        for i, t in enumerate(res["trades"], 1):
            rows.append([name, res["symbol"], i, t["side"], t.get("leg", 1), _iso(t["entry_ts"]), t["entry"], _iso(t["exit_ts"]) if t["reason"] != "OPEN" else "", t["exit_avg"] if t["reason"] != "OPEN" else "",
                         t["reason"], t["tp_hits"], round(t["pnl_pct"], 4), round(t["pnl_usd"], 4), round(t.get("funding_usd") or 0.0, 4), round(t.get("notional") or 0, 2), round(t.get("margin") or 0, 2), round(t["hold_h"], 2)])
    return _csv(["strategy", "symbol", "n", "side", "leg", "entry_time_utc", "entry_price", "exit_time_utc", "exit_price_avg", "reason", "tp_hits", "pnl_pct", "pnl_usd", "funding_usd", "notional_usd", "margin_usd", "hold_hours"], rows)


def autolog_csv(log):
    rows = [[_iso(int(x["ts"] * 1000)), x["config_id"] if x["config_id"] is not None else "", x["kind"], x["symbol"] or "", x["side"] or "", x["status"],
             json.dumps(x["details"], ensure_ascii=False)] for x in reversed(log)]
    return _csv(["time_utc", "config_id", "kind", "symbol", "side", "status", "details"], rows)


def fills_csv(fills):
    rows = [[_iso(f.get("ts")), f.get("symbol") or "", f.get("side") or "", f.get("amount"), f.get("price"), f.get("fee") if f.get("fee") is not None else "", f.get("pnl") if f.get("pnl") is not None else ""]
            for f in reversed(fills)]
    return _csv(["time_utc", "symbol", "side", "amount", "price", "fee", "closed_pnl"], rows)
