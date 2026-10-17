import type { Params, TfMode } from './types';

export const CONFIG_FORMAT = 'maverick-config';

/** Полный конфиг для переноса: все параметры (включая источник, актив и ТФ графика) одним JSON. */
export function exportConfig(p: Params, tf: TfMode): string {
  return JSON.stringify({ format: CONFIG_FORMAT, version: 1, exported_at: new Date().toISOString(), tf, params: { ...p, date_from: p.date_from ?? null, date_to: p.date_to ?? null } }, null, 2);
}

export function configFileName(p: Params): string {
  return `maverick-config_${p.source}_${p.symbol.replace(/[^A-Za-z0-9]+/g, '-')}.json`;
}

/** параметры, у которых null означает «выключено», даже если в .env задано значение */
const NULLABLE = new Set(['only', 'liq_source', 'cool_drop', 'cool_declines', 'liq_hours', 'date_from', 'date_to']);

/** Возможности, появившиеся позже: в старом конфиге их нет, и они ДОЛЖНЫ быть выключены (а не браться из умолчаний .env), чтобы результат не менялся. */
export const LEGACY_OFF = { pyramid_enabled: false, signal_gap_h: 0, sl_hold_strong: false } as const;

const kind = (v: unknown) => (Array.isArray(v) ? 'array' : v === null ? 'null' : typeof v);

/**
 * Разбирает вставленный конфиг (файл или текст). Принимает как обёртку {format, params, tf}, так и «голый» объект параметров.
 * Берутся только известные параметры нужного типа поверх умолчаний; всё остальное попадает в ignored.
 */
export function parseConfig(text: string, envDefaults: Params): { params: Params; tf: TfMode | null; ignored: string[] } {
  const defaults: Params = { ...envDefaults, date_from: envDefaults.date_from ?? null, date_to: envDefaults.date_to ?? null };      // период в env-умолчаниях не задаётся, но в конфиге бывает
  let raw: unknown;
  try {
    raw = JSON.parse(text);
  } catch {
    throw new Error('Не удалось разобрать JSON — вставьте конфиг целиком, как его скачали кнопкой «Скачать конфиг».');
  }
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) throw new Error('Ожидается JSON-объект с параметрами.');
  const obj = raw as Record<string, unknown>;
  if (obj.format !== undefined && obj.format !== CONFIG_FORMAT) throw new Error(`Это не конфиг Maverick (format = «${String(obj.format)}»).`);
  const src = (obj.params && typeof obj.params === 'object' ? obj.params : obj) as Record<string, unknown>;
  const out: Record<string, unknown> = { ...defaults };
  const ignored: string[] = [];
  for (const [k, v] of Object.entries(src)) {
    if (!(k in defaults)) {
      if (obj.params || k !== 'format') ignored.push(k);
      continue;
    }
    const d = (defaults as unknown as Record<string, unknown>)[k];
    const ok = kind(v) === kind(d) || (v === null && NULLABLE.has(k)) || (d === null && (typeof v === 'number' || typeof v === 'string'));
    if (ok) out[k] = v;
    else ignored.push(k);
  }
  for (const [k, v] of Object.entries(LEGACY_OFF)) if (!(k in src)) out[k] = v;
  const tf = obj.tf === 'auto' || typeof obj.tf === 'number' ? (obj.tf as TfMode) : null;
  return { params: out as unknown as Params, tf, ignored };
}
