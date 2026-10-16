import { useState, type ReactNode } from 'react';
import type { Only, Params, Side } from '../types';

interface Props {
  params: Params;
  defaults: Params;
  loading: boolean;
  onChange: (p: Params) => void;
  onRun: () => void;
}

type NumKey = {
  [K in keyof Params]: Params[K] extends number | null ? K : never;
}[keyof Params];

function Num({ label, value, onChange, step = 0.05, hint, suffix }: {
  label: string; value: number | null; onChange: (v: number | null) => void; step?: number; hint?: string; suffix?: string;
}) {
  return (
    <label className="field" title={hint}>
      <span>{label}</span>
      <span className="input-wrap">
        <input
          type="number"
          step={step}
          value={value ?? ''}
          onChange={(e) => onChange(e.target.value === '' ? null : Number(e.target.value))}
        />
        {suffix && <em>{suffix}</em>}
      </span>
    </label>
  );
}

function Section({ title, children, open = true }: { title: string; children: ReactNode; open?: boolean }) {
  const [o, setO] = useState(open);
  return (
    <section className="group">
      <button className="group-title" onClick={() => setO(!o)} type="button">
        <span>{o ? '▾' : '▸'}</span> {title}
      </button>
      {o && <div className="group-body">{children}</div>}
    </section>
  );
}

export default function ParamsPanel({ params: p, defaults, loading, onChange, onRun }: Props) {
  const set = <K extends keyof Params>(k: K, v: Params[K]) => onChange({ ...p, [k]: v });
  const num = (k: NumKey) => (v: number | null) => onChange({ ...p, [k]: v } as Params);
  const coolMode: 'off' | 'drop' | 'declines' = p.cool_drop != null ? 'drop' : p.cool_declines != null ? 'declines' : 'off';
  const setCoolMode = (m: string) =>
    onChange({
      ...p,
      cool_drop: m === 'drop' ? defaults.cool_drop ?? 0.1 : null,
      cool_declines: m === 'declines' ? defaults.cool_declines ?? 2 : null,
    });
  const liqOn = p.liq_hours != null;

  return (
    <aside className="params">
      <div className="params-head">
        <h2>Параметры</h2>
        <button type="button" className="link" onClick={() => onChange({ ...defaults })} title="Вернуть значения из backend/.env">
          сбросить
        </button>
      </div>

      <Section title="Режим">
        <label className="field">
          <span>Сторона</span>
          <select value={p.side} onChange={(e) => set('side', e.target.value as Side)}>
            <option value="both">лонг + шорт</option>
            <option value="long">только лонг</option>
            <option value="short">только шорт</option>
          </select>
        </label>
        <label className="field">
          <span>Метрика</span>
          <select value={p.only ?? ''} onChange={(e) => set('only', (e.target.value || null) as Only)}>
            <option value="">Local и Scalp</option>
            <option value="local">только Local</option>
            <option value="scalp">только Scalp</option>
          </select>
        </label>
        <Num label="Подтверждение, отчётов подряд" value={p.confirm} step={1} onChange={(v) => set('confirm', Math.max(1, Math.round(v ?? 1)))} />
      </Section>

      <Section title="Лонг: Deep ≥">
        <Num label="Local ≥" value={p.local_min} onChange={num('local_min')} />
        <Num label="Scalp ≥" value={p.scalp_min} onChange={num('scalp_min')} />
      </Section>

      <Section title="Шорт: Deep ≤">
        <Num label="Local ≤" value={p.short_local_max} onChange={num('short_local_max')} />
        <Num label="Scalp ≤" value={p.short_scalp_max} onChange={num('short_scalp_max')} />
      </Section>

      <Section title="Выход и издержки">
        <Num label="Тейк-профит" value={p.tp_pct} step={0.5} suffix="%" onChange={num('tp_pct')} />
        <Num label="Стоп-лосс" value={p.sl_pct} step={0.5} suffix="%" onChange={num('sl_pct')} />
        <Num label="Размер" value={p.size} step={0.01} suffix="BTC" onChange={num('size')} />
        <Num label="Комиссия за сторону" value={+(p.fee * 100).toFixed(4)} step={0.005} suffix="%" onChange={(v) => set('fee', (v ?? 0) / 100)} />
        <Num label="Проскальзывание" value={+(p.slip * 100).toFixed(4)} step={0.005} suffix="%" onChange={(v) => set('slip', (v ?? 0) / 100)} />
        <Num label="Пауза после стопа" value={p.cooldown_h} step={1} suffix="ч" onChange={(v) => set('cooldown_h', v ?? 0)} />
      </Section>

      <Section title="Фильтр: остывание метрик" open={coolMode !== 'off'}>
        <label className="field">
          <span>Режим</span>
          <select value={coolMode} onChange={(e) => setCoolMode(e.target.value)}>
            <option value="off">выключен</option>
            <option value="drop">на X от экстремума серии</option>
            <option value="declines">N отчётов подряд</option>
          </select>
        </label>
        {coolMode !== 'off' && (
          <>
            <label className="field">
              <span>Метрика</span>
              <select value={p.cool_metric} onChange={(e) => set('cool_metric', e.target.value as 'scalp' | 'local')}>
                <option value="scalp">Deep Scalp</option>
                <option value="local">Deep Local</option>
              </select>
            </label>
            {coolMode === 'drop' ? (
              <Num label="Остыла/выросла на" value={p.cool_drop} step={0.05} onChange={(v) => set('cool_drop', v)} hint="лонг: падение от максимума серии; шорт: рост от минимума" />
            ) : (
              <Num label="Отчётов подряд" value={p.cool_declines} step={1} onChange={(v) => set('cool_declines', v == null ? null : Math.max(1, Math.round(v)))} />
            )}
          </>
        )}
      </Section>

      <Section title="Фильтр: ликвидации закончились" open={liqOn}>
        <label className="field check">
          <input type="checkbox" checked={liqOn} onChange={(e) => set('liq_hours', e.target.checked ? defaults.liq_hours ?? 12 : null)} />
          <span>включить</span>
        </label>
        {liqOn && (
          <>
            <Num label="Окно" value={p.liq_hours} step={1} suffix="ч" onChange={(v) => set('liq_hours', v)} />
            <Num label="Спад до доли пика" value={p.liq_frac} step={0.05} onChange={(v) => set('liq_frac', v ?? 0.25)} />
            <Num label="Всплеск ≥ перцентиль" value={p.liq_pct} step={5} onChange={(v) => set('liq_pct', v ?? 0)} />
          </>
        )}
      </Section>

      <Section title="Период" open={!!(p.date_from || p.date_to)}>
        <label className="field">
          <span>С</span>
          <input type="date" value={p.date_from ?? ''} onChange={(e) => set('date_from', e.target.value || null)} />
        </label>
        <label className="field">
          <span>По</span>
          <input type="date" value={p.date_to ?? ''} onChange={(e) => set('date_to', e.target.value || null)} />
        </label>
      </Section>

      <button type="button" className="run" disabled={loading} onClick={onRun}>
        {loading ? 'Считаю…' : 'Запустить'}
      </button>
    </aside>
  );
}
