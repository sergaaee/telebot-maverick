import { useState, type FormEvent, type ReactNode } from 'react';
import { fmtDate } from '../format';
import { HELP, type HelpKey } from '../helpTexts';
import { MAX_TPS, fillRemainder, resizeTps, sumShares, validateTps } from '../tps';
import type { AssetInfo, Job, Only, Params, Side, SourceInfo, TpLevel } from '../types';
import Help from './Help';

interface Props {
  params: Params;
  sources: SourceInfo[];
  assets: AssetInfo[];
  /** идущие сейчас загрузки свечей: такие активы пока нельзя выбрать */
  activeJobs: Job[];
  /** добавить актив: возвращает текст ошибки или null; свечи качаются в фоне, о завершении придёт уведомление */
  onAddAsset: (symbol: string) => Promise<string | null>;
  onRemoveAsset: (symbol: string) => void;
  loading: boolean;
  onChange: (p: Params) => void;
  onRun: () => void;
  /** смена источника или актива: App сохраняет текущие настройки и подхватывает сохранённые для новой связки */
  onSwitch: (target: { source?: string; symbol?: string }) => void;
  onReset: () => void;
}

const ADD_ASSET = '__add_asset__';

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

/** Тейк-профиты частями: число уровней (1–3), расстояние и доля позиции на каждом, перенос стопа в безубыток после первого. */
function TpEditor({ tps, be, onTps, onBe }: { tps: TpLevel[]; be: boolean; onTps: (t: TpLevel[]) => void; onBe: (b: boolean) => void }) {
  const n = tps.length;
  const err = validateTps(tps);
  const sum = sumShares(tps);
  const setLevel = (i: number, patch: Partial<TpLevel>) => onTps(tps.map((l, k) => (k === i ? { ...l, ...patch } : l)));
  const setCount = (k: number) => {
    onTps(resizeTps(tps, k));
    if (k < 2) onBe(false);
  };
  return (
    <div className="tp-editor">
      <div className="field">
        <Label text="Тейк-профитов" help="tp_count" />
        <div className="seg seg-sm">
          {Array.from({ length: MAX_TPS }, (_, i) => i + 1).map((k) => (
            <button type="button" key={k} className={n === k ? 'on' : ''} onClick={() => setCount(k)}>{k}</button>
          ))}
        </div>
      </div>
      <div className="tp-head">
        <span />
        <span>расстояние <Help text={HELP.tp_level_pct} /></span>
        <span>закрыть <Help text={HELP.tp_level_share} /></span>
      </div>
      {tps.map((l, i) => (
        <div className="tp-row" key={i}>
          <b>TP{i + 1}</b>
          <span className="input-wrap">
            <input type="number" step={0.5} min={0} value={Number.isFinite(l.pct) ? l.pct : ''} onChange={(e) => setLevel(i, { pct: e.target.value === '' ? NaN : Number(e.target.value) })} />
            <em>%</em>
          </span>
          <span className="input-wrap">
            <input type="number" step={5} min={0} max={100} value={Number.isFinite(l.share) ? l.share : ''} onChange={(e) => setLevel(i, { share: e.target.value === '' ? NaN : Number(e.target.value) })} />
            <em>%</em>
          </span>
        </div>
      ))}
      <div className={`tp-sum ${err ? 'bad' : 'ok'}`}>
        Σ долей: {sum}%{err ? ` — ${err}` : ''}
        {n > 1 && Math.abs(sum - 100) > 0.001 && (
          <button type="button" className="link" onClick={() => onTps(fillRemainder(tps))}>дополнить до 100%</button>
        )}
      </div>
      <label className="field check">
        <input type="checkbox" checked={be && n > 1} disabled={n < 2} onChange={(e) => onBe(e.target.checked)} />
        <Label text="В безубыток после 1-го тейка" help="be_after_tp1" />
      </label>
    </div>
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

export default function ParamsPanel({ params: p, sources, assets, activeJobs, onAddAsset, onRemoveAsset, loading, onChange, onRun, onSwitch, onReset }: Props) {
  const set = <K extends keyof Params>(k: K, v: Params[K]) => onChange({ ...p, [k]: v });
  const num = (k: NumKey) => (v: number | null) => onChange({ ...p, [k]: v } as Params);
  const coolMode: 'off' | 'drop' | 'declines' = p.cool_drop != null ? 'drop' : p.cool_declines != null ? 'declines' : 'off';
  const setCoolMode = (m: string) =>
    onChange({ ...p, cool_drop: m === 'drop' ? 0.1 : null, cool_declines: m === 'declines' ? 2 : null });
  const liqOn = p.liq_hours != null;
  const tpErr = validateTps(p.tps) ?? (p.top_enabled ? validateTps(p.top_tps) : null);
  const src = sources.find((s) => s.id === p.source);
  const liqSrc = p.liq_source ? sources.find((s) => s.id === p.liq_source) : src;
  const asset = assets.find((a) => a.symbol === p.symbol);
  const downloading = (sym: string) => activeJobs.some((j) => j.symbol === sym);
  const [adding, setAdding] = useState(false);
  const [newSymbol, setNewSymbol] = useState('');
  const [addError, setAddError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const submitAsset = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setAddError(null);
    const err = await onAddAsset(newSymbol);
    setBusy(false);
    if (err) setAddError(err);
    else {
      setAdding(false);
      setNewSymbol('');
    }
  };

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
          <select
            className="wide"
            value={p.symbol}
            onChange={(e) => (e.target.value === ADD_ASSET ? setAdding(true) : onSwitch({ symbol: e.target.value }))}
          >
            {assets.map((a) => (
              <option key={a.symbol} value={a.symbol} disabled={downloading(a.symbol) && a.symbol !== p.symbol}>
                {a.symbol}{downloading(a.symbol) ? ' — загружается…' : a.error ? ' — нет свечей' : ''}
              </option>
            ))}
            <option value={ADD_ASSET}>＋ Добавить актив…</option>
          </select>
        </label>
        {adding && (
          <form className="add-asset" onSubmit={submitAsset}>
            <div className="add-asset-row">
              <input
                autoFocus
                type="text"
                placeholder="ETH или ETH/USDT:USDT"
                value={newSymbol}
                onChange={(e) => setNewSymbol(e.target.value)}
                aria-label="Символ нового актива"
              />
              <Help text={HELP.add_asset} />
            </div>
            <div className="add-asset-row">
              <button type="submit" className="ghost" disabled={busy || !newSymbol.trim()}>{busy ? 'Проверяю…' : 'Добавить и скачать'}</button>
              <button type="button" className="link" onClick={() => { setAdding(false); setAddError(null); }}>отмена</button>
            </div>
            {addError && <div className="src-info bad-text">{addError}</div>}
            <div className="src-info">Свечи скачаются в фоне — когда загрузка закончится, придёт уведомление.</div>
          </form>
        )}
        {asset?.user && !downloading(asset.symbol) && (
          <button type="button" className="link remove-asset" onClick={() => onRemoveAsset(asset.symbol)}>убрать «{asset.symbol}» из списка</button>
        )}
        {downloading(p.symbol) && <div className="src-info">свечи этого актива загружаются…</div>}
        {asset?.error && !downloading(p.symbol) && <div className="src-info warn-text">свечей нет — нажмите «Скачать свечи» вверху</div>}
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
        <TpEditor tps={p.tps} be={p.be_after_tp1} onTps={(t) => onChange({ ...p, tps: t })} onBe={(v) => onChange({ ...p, be_after_tp1: v })} />
        <Num label="Стоп-лосс" help="sl_pct" value={p.sl_pct} step={0.5} suffix="%" onChange={num('sl_pct')} />
        <Num label="Размер" help="size" value={p.size} step={0.01} suffix="BTC" onChange={num('size')} />
        <Num label="Комиссия за сторону" help="fee" value={+(p.fee * 100).toFixed(4)} step={0.005} suffix="%" onChange={(v) => set('fee', (v ?? 0) / 100)} />
        <Num label="Проскальзывание" help="slip" value={+(p.slip * 100).toFixed(4)} step={0.005} suffix="%" onChange={(v) => set('slip', (v ?? 0) / 100)} />
        <Num label="Пауза после стопа" help="cooldown_h" value={p.cooldown_h} step={1} suffix="ч" onChange={(v) => set('cooldown_h', v ?? 0)} />
      </Section>

      <Section title="Топ-коины" help="groupTop" open={p.top_enabled}>
        <label className="field check">
          <input type="checkbox" checked={p.top_enabled} onChange={(e) => set('top_enabled', e.target.checked)} />
          <Label text="торговать топ-коином отчёта" help="top_enabled" />
        </label>
        {p.top_enabled && (
          <>
            <label className="field">
              <Label text="Сторона" help="top_side" />
              <select value={p.top_side} onChange={(e) => set('top_side', e.target.value as Side)}>
                <option value="long">только лонг</option>
                <option value="short">только шорт</option>
                <option value="both">лонг + шорт</option>
              </select>
            </label>
            <TpEditor tps={p.top_tps} be={p.top_be_after_tp1} onTps={(t) => onChange({ ...p, top_tps: t })} onBe={(v) => onChange({ ...p, top_be_after_tp1: v })} />
            <Num label="Стоп-лосс" help="top_sl_pct" value={p.top_sl_pct} step={0.5} suffix="%" onChange={num('top_sl_pct')} />
            <Num label="Размер" help="top_size" value={p.top_size} step={10} suffix="$" onChange={num('top_size')} />
            <Num label="Комиссия за сторону" help="top_fee" value={+(p.top_fee * 100).toFixed(4)} step={0.005} suffix="%" onChange={(v) => set('top_fee', (v ?? 0) / 100)} />
            <Num label="Проскальзывание" help="top_slip" value={+(p.top_slip * 100).toFixed(4)} step={0.005} suffix="%" onChange={(v) => set('top_slip', (v ?? 0) / 100)} />
            <Num label="Держать / качать" help="top_days" value={p.top_days} step={0.5} suffix="сут" onChange={(v) => set('top_days', v ?? 3)} />
            <Num label="Значение в отчёте ≥" help="top_min_value" value={p.top_min_value} step={0.5} onChange={(v) => set('top_min_value', v ?? 0)} />
          </>
        )}
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

      <button type="button" className="run" disabled={loading || !!tpErr} onClick={onRun} title={tpErr ?? undefined}>
        {loading ? 'Считаю…' : tpErr ? 'Исправьте тейки' : 'Запустить'}
      </button>
    </aside>
  );
}
