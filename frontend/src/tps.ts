import type { TpLevel } from './types';

export const MAX_TPS = 3;

const round2 = (x: number) => Math.round(x * 100) / 100;

export const sumShares = (tps: TpLevel[]) => round2(tps.reduce((a, l) => a + (Number.isFinite(l.share) ? l.share : 0), 0));

/** Те же правила, что на бэкенде (normalize_tps): текст ошибки или null. */
export function validateTps(tps: TpLevel[]): string | null {
  if (tps.length < 1 || tps.length > MAX_TPS) return `Тейков должно быть от 1 до ${MAX_TPS}`;
  if (tps.some((l) => !(l.pct > 0))) return 'Расстояние до тейка должно быть больше 0%';
  if (tps.some((l, i) => i > 0 && l.pct <= tps[i - 1].pct)) return 'Каждый следующий тейк должен быть дальше предыдущего';
  if (tps.some((l) => !(l.share > 0))) return 'Доля позиции на каждом тейке должна быть больше 0%';
  const s = sumShares(tps);
  if (Math.abs(s - 100) > 0.001) return `Доли должны давать 100% (сейчас ${s}%)`;
  return null;
}

const PRESET_SHARES: Record<number, number[]> = { 1: [100], 2: [50, 50], 3: [40, 30, 30] };

/** Меняет число тейков: существующие уровни сохраняются, новый ставится дальше последнего, доли — по заготовке (50/50, 40/30/30). */
export function resizeTps(tps: TpLevel[], n: number): TpLevel[] {
  const levels = tps.slice(0, n).map((l) => ({ ...l }));
  while (levels.length < n) {
    const last = levels[levels.length - 1]?.pct ?? 4;
    levels.push({ pct: round2(last * 1.5), share: 0 });
  }
  return levels.map((l, i) => ({ ...l, share: PRESET_SHARES[n][i] }));
}

/** Остаток до 100% кладёт на последний тейк. */
export function fillRemainder(tps: TpLevel[]): TpLevel[] {
  const others = tps.slice(0, -1).reduce((a, l) => a + l.share, 0);
  return tps.map((l, i) => (i === tps.length - 1 ? { ...l, share: round2(100 - others) } : l));
}
