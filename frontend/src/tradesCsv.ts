import { fmtTs } from './format';
import type { Params, Trade } from './types';

const esc = (v: unknown): string => {
  const s = v == null ? '' : String(v);
  return /[",\n\r;]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
};

const round = (v: number | null | undefined, d: number): string => (v == null ? '' : String(Number(v.toFixed(d))));

/** Сделки в CSV (запятая, точка в числах, время UTC, BOM для Excel). top — сделки по топ-коинам: добавляются монета и значение из отчёта. */
export function tradesCsv(trades: Trade[], top: boolean): string {
  const head = ['n', 'side', 'leg', 'group', 'signal_utc', 'entry_utc', 'exit_utc', 'entry_price', 'exit_price', 'reason', 'tp_hits', 'pnl_pct', 'pnl_usd', 'funding_usd', 'margin_usd', 'notional_usd', 'hold_h', 'dg', 'dl', 'ds', 'fills'];
  if (top) head.push('symbol', 'coin', 'top_value');
  const rows = trades.map((t) => {
    const r: unknown[] = [
      t.n, t.side, t.leg ?? 1, t.group ?? '', fmtTs(t.signal_ts), fmtTs(t.entry_ts), fmtTs(t.exit_ts), round(t.entry, 6), round(t.exit_avg, 6), t.reason, t.tp_hits,
      round(t.pnl_pct, 4), round(t.pnl_usd, 2), round(t.funding_usd, 4), round(t.margin, 2), round(t.notional, 2), round(t.hold_h, 2), round(t.global, 3), round(t.local, 3), round(t.scalp, 3),
      t.fills.map((f) => `${f.label} ${Math.round(f.share * 100)}% @ ${round(f.price, 6)}`).join(' | '),
    ];
    if (top) r.push(t.symbol ?? '', t.coin ?? '', round(t.top_value, 3));
    return r.map(esc).join(',');
  });
  return '﻿' + [head.join(','), ...rows].join('\r\n') + '\r\n';
}

export function tradesCsvName(p: Params, top: boolean): string {
  return `maverick-trades${top ? '-top' : ''}_${p.source}_${p.symbol.replace(/[^A-Za-z0-9]+/g, '-')}.csv`;
}

export function downloadText(name: string, text: string, mime = 'text/csv;charset=utf-8') {
  const url = URL.createObjectURL(new Blob([text], { type: mime }));
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
