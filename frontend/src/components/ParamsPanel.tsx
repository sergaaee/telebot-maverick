import { useState, type ReactNode } from 'react';
import { fmtDate } from '../format';
import { HELP, type HelpKey } from '../helpTexts';
import type { AssetInfo, Only, Params, Side, SourceInfo } from '../types';
import Help from './Help';

interface Props {
  params: Params;
  sources: SourceInfo[];
  assets: AssetInfo[];
  loading: boolean;
  onChange: (p: Params) => void;
  onRun: () => void;
  /** смена источника или актива: App сохраняет текущие настройки и подхватывает сохранённые для новой связки */
  onSwitch: (target: { source?: string; symbol?: string }) => void;
  onReset: () => void;
}

type NumKey = {
  [K in keyof Params]: Params[K] extends number | null ? K : never;
}[keyof Params];

function Label({ text, help }: { text: string; help?: HelpKey }) {
  return (
    <span className="label">
      {text}
      {help && <Help text={HELP[help]} />}
    </span>
  );
}

function Num({ label, help, value, onChange, step = 0.05, suffix }: {
  label: string; help: HelpKey; value: number | null; onChange: (v: number | null) => void; step?: number; suffix?: string;
}) {
  return (
    <label className="field">
      <Label text={label} help={help} />
      <span className="input-wrap">
        <input type="number" step={step} value={value ?? ''} onChange={(e) => onChange(e.target.value === '' ? null : Number(e.target.value))} />
        {suffix && <em>{suffix}</em>}
      </span>
    </label>
  );
}

function Section({ title, help, children, open = true }: { title: string; help?: HelpKey; children: ReactNode; open?: boolean }) {
  const [o, setO] = useState(open);
  return (
    <section className="group">
      <div className="group-head">
        <button className="group-title" onClick={() => setO(!o)} type="button">
          <span>{o ? '▾' : '▸'}</span> {title}
        </button>
        {help && <Help text={HELP[help]} />}
      </div>
      {o && <div className="group-body">{children}</div>}
    </section>
  );
}

export default function ParamsPanel({ params: p, sources, assets, loading, onChange, onRun, onSwitch, onReset }: Props) {
  const set = <K extends keyof Params>(k: K, v: Params[K]) => onChange({ ...p, [k]: v });
  const num = (k: NumKey) => (v: number | null) => onChange({ ...p, [k]: v } as Params);
  const coolMode: 'off' | 'drop' | 'declines' = p.cool_drop != null ? 'drop' : p.cool_declines != null ? 'declines' : 'off';
  const setCoolMode = (m: string) =>
    onChange({ ...p, cool_drop: m === 'drop' ? 0.1 : null, cool_declines: m === 'declines' ? 2 : null });
  const liqOn = p.liq_hours != null;
  const src = sources.find((s) => s.id === p.source);
  const liqSrc = p.liq_source ? sources.find((s) => s.id === p.liq_source) : src;
  const asset = assets.find((a) => a.symbol === p.symbol);

  return (
    <aside className="params">
      <div className="params-head">
        <h2>Параметры</h2>
        <button type="button" className="link" onClick={onReset}>
          сбросить к env
        </button>
      </div>

      <Section title="Данные">
        <label className="field col">
          <Label text="Актив (свечи)" help="symbol" />
          <select className="wide" value={p.symbol} onChange={(e) => onSwitch({ symbol: e.target.value })}>
            {assets.map((a) => (
              <option key={a.symbol} value={a.symbol}>
                {a.symbol}{a.error ? ' — нет свечей' : ''}
              </option>
            ))}
          </select>
        </label>
        {asset?.error && <div className="src-info warn-text">свечи не скачаны — нажмите «Обновить свечи»</div>}
        <label className="field col">
          <Label text="Источник метрик (сигналы)" help="source" />
          <select className="wide" value={p.source} onChange={(e) => onSwitch({ source: e.target.value })}>
            {sources.map((s) => (
              <option key={s.id} value={s.id} disabled={!s.exists}>
                {s.label}{s.exists ? '' : ' — нет файла'}
              </option>
            ))}
          </select>
        </label>
        {src && src.count > 0 && (
          <div className="src-info">
            {src.count.toLocaleString('ru-RU')} отчётов · {fmtDate(src.first_ts!)} … {fmtDate(src.last_ts!)}
          </div>
        )}
        <label className="field col">
          <Label text="Ликвидации из" help="liq_source" />
          <select className="wide" value={p.liq_source ?? ''} onChange={(e) => set('liq_source', e.target.value || null)}>
            <option value="">того же источника</option>
            {sources.filter((s) => s.id !== p.source).map((s) => (
              <option key={s.id} value={s.id} disabled={!s.exists}>{s.label}</option>
            ))}
          </select>
        </label>
        {liqSrc && !liqSrc.has_liq && <div className="src-info warn-text">в «{liqSrc.label}» нет данных о ликвидациях</div>}
      </Section>

      <Section title="Режим">
        <label className="field">
          <Label text="Сторона" help="side" />
          <select value={p.side} onChange={(e) => set('side', e.target.value as Side)}>
            <option value="both">лонг + шорт</option>
            <option value="long">только лонг</option>
            <option value="short">только шорт</option>
          </select>
        </label>
        <label className="field">
          <Label text="Метрика" help="only" />
          <select value={p.only ?? ''} onChange={(e) => set('only', (e.target.value || null) as Only)}>
            <option value="">Local и Scalp</option>
            <option value="local">только Local</option>
            <option value="scalp">только Scalp</option>
          </select>
        </label>
        <Num label="Подтверждение, отчётов" help="confirm" value={p.confirm} step={1} onChange={(v) => set('confirm', Math.max(1, Math.round(v ?? 1)))} />
      </Section>

      <Section title="Лонг: Deep ≥" help="groupLong">
        <Num label="Local ≥" help="local_min" value={p.local_min} onChange={num('local_min')} />
        <Num label="Scalp ≥" help="scalp_min" value={p.scalp_min} onChange={num('scalp_min')} />
      </Section>

      <Section title="Шорт: Deep ≤" help="groupShort">
        <Num label="Local ≤" help="short_local_max" value={p.short_local_max} onChange={num('short_local_max')} />
        <Num label="Scalp ≤" help="short_scalp_max" value={p.short_scalp_max} onChange={num('short_scalp_max')} />
      </Section>

      <Section title="Выход и издержки" help="groupExit">
        <Num label="Тейк-профит" help="tp_pct" value={p.tp_pct} step={0.5} suffix="%" onChange={num('tp_pct')} />
        <Num label="Стоп-лосс" help="sl_pct" value={p.sl_pct} step={0.5} suffix="%" onChange={num('sl_pct')} />
        <Num label="Размер" help="size" value={p.size} step={0.01} suffix="BTC" onChange={num('size')} />
        <Num label="Комиссия за сторону" help="fee" value={+(p.fee * 100).toFixed(4)} step={0.005} suffix="%" onChange={(v) => set('fee', (v ?? 0) / 100)} />
        <Num label="Проскальзывание" help="slip" value={+(p.slip * 100).toFixed(4)} step={0.005} suffix="%" onChange={(v) => set('slip', (v ?? 0) / 100)} />
        <Num label="Пауза после стопа" help="cooldown_h" value={p.cooldown_h} step={1} suffix="ч" onChange={(v) => set('cooldown_h', v ?? 0)} />
      </Section>

      <Section title="Фильтр: остывание метрик" help="groupCool" open={coolMode !== 'off'}>
        <label className="field">
          <Label text="Режим" help="cool_mode" />
          <select value={coolMode} onChange={(e) => setCoolMode(e.target.value)}>
            <option value="off">выключен</option>
            <option value="drop">на X от экстремума</option>
            <option value="declines">N отчётов подряд</option>
          </select>
        </label>
        {coolMode !== 'off' && (
          <>
            <label className="field">
              <Label text="Метрика" help="cool_metric" />
              <select value={p.cool_metric} onChange={(e) => set('cool_metric', e.target.value as 'scalp' | 'local')}>
                <option value="scalp">Deep Scalp</option>
                <option value="local">Deep Local</option>
              </select>
            </label>
            {coolMode === 'drop' ? (
              <Num label="Откат на" help="cool_drop" value={p.cool_drop} step={0.05} onChange={(v) => set('cool_drop', v)} />
            ) : (
              <Num label="Отчётов подряд" help="cool_declines" value={p.cool_declines} step={1} onChange={(v) => set('cool_declines', v == null ? null : Math.max(1, Math.round(v)))} />
            )}
          </>
        )}
      </Section>

      <Section title="Фильтр: ликвидации закончились" help="groupLiq" open={liqOn}>
        <label className="field check">
          <input type="checkbox" checked={liqOn} onChange={(e) => set('liq_hours', e.target.checked ? 12 : null)} />
          <Label text="включить" help="liq_on" />
        </label>
        {liqOn && (
          <>
            <Num label="Окно" help="liq_hours" value={p.liq_hours} step={1} suffix="ч" onChange={(v) => set('liq_hours', v)} />
            <Num label="Спад до доли пика" help="liq_frac" value={p.liq_frac} step={0.05} onChange={(v) => set('liq_frac', v ?? 0.25)} />
            <Num label="Всплеск ≥ перцентиль" help="liq_pct" value={p.liq_pct} step={5} onChange={(v) => set('liq_pct', v ?? 0)} />
          </>
        )}
      </Section>

      <Section title="Период" help="groupPeriod" open={!!(p.date_from || p.date_to)}>
        <label className="field">
          <Label text="С" help="date_from" />
          <input type="date" value={p.date_from ?? ''} onChange={(e) => set('date_from', e.target.value || null)} />
        </label>
        <label className="field">
          <Label text="По" help="date_to" />
          <input type="date" value={p.date_to ?? ''} onChange={(e) => set('date_to', e.target.value || null)} />
        </label>
      </Section>

      <button type="button" className="run" disabled={loading} onClick={onRun}>
        {loading ? 'Считаю…' : 'Запустить'}
      </button>
    </aside>
  );
}
