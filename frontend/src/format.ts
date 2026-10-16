const pad = (n: number) => String(n).padStart(2, '0');

/** ms -> "YYYY-MM-DD HH:mm" (UTC) */
export function fmtTs(ms: number): string {
  const d = new Date(ms);
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
}

export const fmtDate = (ms: number) => fmtTs(ms).slice(0, 10);

export function fmtUsd(v: number, digits = 0): string {
  const s = Math.abs(v).toLocaleString('ru-RU', { minimumFractionDigits: digits, maximumFractionDigits: digits });
  return (v < 0 ? '−' : v > 0 ? '+' : '') + s + ' $';
}

export const fmtPct = (v: number, digits = 2) => (v > 0 ? '+' : v < 0 ? '−' : '') + Math.abs(v).toFixed(digits) + '%';

export const fmtPrice = (v: number) => v.toLocaleString('ru-RU', { maximumFractionDigits: 1 });

export const fmtPf = (v: number | null) => (v == null ? '∞' : v.toFixed(2));

export function fmtHold(h: number): string {
  if (h < 1) return `${Math.round(h * 60)} мин`;
  if (h < 48) return `${h.toFixed(1)} ч`;
  return `${(h / 24).toFixed(1)} д`;
}

export const signClass = (v: number) => (v > 0 ? 'pos' : v < 0 ? 'neg' : '');

/** "1m" -> 60, "15m" -> 900, "1h" -> 3600 */
export function tfSeconds(tf: string): number {
  const m = /^(\d+)([smhd])$/.exec(tf);
  if (!m) return 60;
  return Number(m[1]) * { s: 1, m: 60, h: 3600, d: 86400 }[m[2] as 's' | 'm' | 'h' | 'd'];
}

/** 60 -> "1m", 3600 -> "1h", 86400 -> "1d" */
export function tfLabel(sec: number): string {
  if (sec % 86400 === 0) return `${sec / 86400}d`;
  if (sec % 3600 === 0) return `${sec / 3600}h`;
  if (sec % 60 === 0) return `${sec / 60}m`;
  return `${sec}s`;
}
