import { fmtPct, fmtPf, fmtUsd, signClass } from '../format';
import { HELP } from '../helpTexts';
import { useState } from 'react';
import type { BacktestResult, HoldQuantiles, HoldStats, Stats, TimeBucket, TimeStats, WeekdayBucket, WeekdaySplit } from '../types';
import Help from './Help';

export function Card({ label, value, cls = '', sub, help }: { label: string; value: string; cls?: string; sub?: string; help?: string }) {
  return (
    <div className="card">
      <div className="card-label">{label}{help && <Help text={help} />}</div>
      <div className={`card-value ${cls}`}>{value}</div>
      {sub && <div className="card-sub">{sub}</div>}
    </div>
  );
}

const SPLIT_ROWS: [keyof WeekdayBucket['reasons'], string][] = [['TP', 'Тейки'], ['BE', 'Безубыток'], ['SL', 'Стопы'], ['REV', 'Развороты'], ['LIQ', 'Ликвидации'], ['WKND', 'Закрыто в пятницу'], ['OPEN', 'Открыто']];

/** Исходы сделок в будни и в выходные: сколько и какой процент от сделок группы. */
function WeekdayTable({ split }: { split: WeekdaySplit }) {
  const { weekday: a, weekend: b } = split;
  const cell = (bk: WeekdayBucket, n: number) => (bk.trades ? `${n} · ${((n / bk.trades) * 100).toFixed(0)}%` : '–');
  return (
    <>
      <h3>Будни и выходные <Help text={HELP.weekday_split} /></h3>
      <table className="mini" data-testid="weekday-split">
        <thead><tr><th></th><th>будни</th><th>выходные</th></tr></thead>
        <tbody>
          <tr><td>Сделок (по выходу)</td><td>{a.trades}</td><td>{b.trades}</td></tr>
          {SPLIT_ROWS.filter(([k]) => a.reasons[k] + b.reasons[k] > 0).map(([k, name]) => (
            <tr key={k}><td>{name}</td><td>{cell(a, a.reasons[k])}</td><td>{cell(b, b.reasons[k])}</td></tr>
          ))}
          <tr><td>Winrate</td><td>{a.trades ? `${((a.wins / a.trades) * 100).toFixed(0)}%` : '–'}</td><td>{b.trades ? `${((b.wins / b.trades) * 100).toFixed(0)}%` : '–'}</td></tr>
          <tr><td>Итого</td><td className={signClass(a.pnl_usd)}>{a.trades ? fmtUsd(a.pnl_usd) : '–'}</td><td className={signClass(b.pnl_usd)}>{b.trades ? fmtUsd(b.pnl_usd) : '–'}</td></tr>
          <tr><td>Входов</td><td>{split.entered.weekday}</td><td>{split.entered.weekend}</td></tr>
        </tbody>
      </table>
    </>
  );
}

const DAY_NAMES = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'];
const REASON_NAMES: Record<string, string> = { TP: 'тейк', BE: 'БУ', SL: 'стоп', REV: 'разворот', LIQ: 'ликвидация', WKND: 'закрытие пт', OPEN: 'открыто' };

function timeRow(name: string, b: TimeBucket, hint?: string) {
  const pct = (n: number) => (b.trades ? `${((n / b.trades) * 100).toFixed(0)}%` : '–');
  const title = b.trades ? Object.entries(b.reasons).filter(([, n]) => n > 0).map(([k, n]) => `${REASON_NAMES[k] ?? k}: ${n}`).join(' · ') : 'сделок нет';
  const pf = b.trades === 0 ? '–' : b.gross_loss === 0 ? (b.gross_win > 0 ? '∞' : '–') : (b.gross_win / b.gross_loss).toFixed(2);
  return (
    <tr key={name} title={title} className={b.trades ? '' : 'dim'}>
      <td>{name}{hint && <span className="muted"> {hint}</span>}</td>
      <td>{b.trades}</td>
      <td>{pct(b.reasons.TP)}</td>
      <td>{pct(b.reasons.SL)}</td>
      <td>{b.trades ? `${((b.wins / b.trades) * 100).toFixed(0)}%` : '–'}</td>
      <td>{pf}</td>
      <td className={signClass(b.pnl_usd)}>{b.trades ? fmtUsd(b.pnl_usd) : '–'}</td>
    </tr>
  );
}

/** Статистика по дням недели и сессиям: по моменту входа или выхода (переключатель). */
function TimeTables({ ts }: { ts: TimeStats }) {
  const [which, setWhich] = useState<'entry' | 'exit'>('entry');
  const cur = ts[which];
  const head = (
    <thead><tr><th></th><th>n</th><th>TP%</th><th>SL%</th><th>win%</th><th>PF</th><th>итого</th></tr></thead>
  );
  return (
    <div data-testid="time-stats">
      <h3>По дням и сессиям <Help text={HELP.time_stats} /></h3>
      <div className="seg-mini">
        <button type="button" className={which === 'entry' ? 'on' : ''} onClick={() => setWhich('entry')}>по входу</button>
        <button type="button" className={which === 'exit' ? 'on' : ''} onClick={() => setWhich('exit')}>по выходу</button>
      </div>
      <table className="mini" data-testid="stats-days">
        {head}
        <tbody>{cur.day.map((b, i) => timeRow(DAY_NAMES[i], b))}</tbody>
      </table>
      <table className="mini" data-testid="stats-sessions">
        {head}
        <tbody>{cur.session.map((b) => timeRow(b.name, b, `${String(b.hours[0]).padStart(2, '0')}–${String(b.hours[1]).padStart(2, '0')}`))}</tbody>
      </table>
    </div>
  );
}

const fmtH = (h: number | null) => (h == null ? '–' : h < 1 ? `${Math.round(h * 60)} мин` : h < 48 ? `${h.toFixed(1)} ч` : `${(h / 24).toFixed(1)} сут`);

function corrText(r: number | null): string {
  if (r == null) return 'мало данных для корреляции (нужно ≥ 10 закрытых сделок)';
  const a = Math.abs(r);
  const w = a < 0.1 ? 'связи практически нет' : r > 0 ? (a < 0.3 ? 'слабо: чем дольше держим, тем немного лучше результат' : 'чем дольше держим, тем лучше результат') : (a < 0.3 ? 'слабо: чем дольше держим, тем немного хуже результат' : 'чем дольше держим, тем хуже результат');
  return `r = ${r.toFixed(2)} — ${w}`;
}

/** Распределение срока удержания позиций и зависимость результата от него. */
function HoldTable({ hs }: { hs: HoldStats }) {
  const total = hs.buckets.reduce((a, b) => a + b.trades, 0);
  if (!total) return null;
  const maxN = Math.max(...hs.buckets.map((b) => b.trades));
  const q = (name: string, x: HoldQuantiles) => (x.n ? <tr><td>{name}</td><td>{x.n}</td><td>{fmtH(x.median)}</td><td>{fmtH(x.mean)}</td><td>{fmtH(x.p90)}</td></tr> : null);
  return (
    <div data-testid="hold-stats">
      <h3>Сколько держатся позиции <Help text={HELP.hold_stats} /></h3>
      <table className="mini">
        <thead><tr><th></th><th>n</th><th>медиана</th><th>среднее</th><th>p90</th></tr></thead>
        <tbody>
          {q('Все закрытые', hs.summary.all)}
          {q('Плюсовые', hs.summary.wins)}
          {q('Минусовые', hs.summary.losses)}
          {Object.entries(hs.summary.by_reason).map(([r, x]) => q(REASON_NAMES[r] ?? r, x))}
        </tbody>
      </table>
      <table className="mini hold-table" data-testid="hold-buckets">
        <thead><tr><th>срок</th><th></th><th>n</th><th>TP%</th><th>SL%</th><th>win%</th><th>PF</th><th>итого</th></tr></thead>
        <tbody>
          {hs.buckets.map((b) => {
            const pct = (n: number) => (b.trades ? `${((n / b.trades) * 100).toFixed(0)}%` : '–');
            const pf = b.trades === 0 ? '–' : b.gross_loss === 0 ? (b.gross_win > 0 ? '∞' : '–') : (b.gross_win / b.gross_loss).toFixed(2);
            return (
              <tr key={b.label} className={b.trades ? '' : 'dim'} title={b.trades ? `${b.trades} сделок (${((b.trades / total) * 100).toFixed(0)}% от всех)` : 'сделок нет'}>
                <td>{b.label}</td>
                <td className="hold-bar"><span style={{ width: `${(b.trades / maxN) * 100}%` }} /></td>
                <td>{b.trades}</td><td>{pct(b.reasons.TP)}</td><td>{pct(b.reasons.SL)}</td>
                <td>{b.trades ? `${((b.wins / b.trades) * 100).toFixed(0)}%` : '–'}</td><td>{pf}</td>
                <td className={signClass(b.pnl_usd)}>{b.trades ? fmtUsd(b.pnl_usd) : '–'}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <p className="note">Корреляция срока и результата: {corrText(hs.summary.corr)}.{hs.open > 0 && ` Не учтено открытых позиций: ${hs.open}.`}</p>
    </div>
  );
}

export default function StatsPanel({ result }: { result: BacktestResult }) {
  const { total: t } = result.stats;
  const rows: [string, Stats][] = [
    ['Всего', result.stats.total],
    ['Лонг', result.stats.long],
    ['Шорт', result.stats.short],
    ['1-я половина', result.stats.first_half],
    ['2-я половина', result.stats.second_half],
  ];
  const maxAbs = Math.max(1, ...result.monthly.map((m) => Math.abs(m.pnl_usd)));

  return (
    <div className="stats">
      <div className="cards">
        <Card label="Итого" value={fmtUsd(t.total_usd)} cls={signClass(t.total_usd)} sub={t.return_pct != null ? `${fmtPct(t.return_pct, 1)} от депозита ${result.params.deposit.toLocaleString('ru-RU')} $` : undefined} />
        <Card label="Сделок" value={String(t.trades)} sub={`TP ${t.tp} · БУ ${t.be} · SL ${t.sl} · разворот ${t.rev}${t.liq ? ` · ликвид. ${t.liq}` : ''}${t.wknd ? ` · пятница ${t.wknd}` : ''} · откр. ${t.open}`} />
        <Card label="Коэф. Шарпа" value={t.sharpe == null ? '–' : t.sharpe.toFixed(2)} cls={t.sharpe == null ? '' : signClass(t.sharpe)} sub="годовой, по дням" />
        <Card label="Winrate" value={`${t.winrate.toFixed(1)}%`} />
        <Card label="Profit factor" value={fmtPf(t.profit_factor)} />
        <Card label="Макс. просадка" value={fmtUsd(-t.max_dd_usd)} cls="neg" sub={t.max_dd_pct != null ? `${t.max_dd_pct.toFixed(1)}% от пика · по закрытым` : 'по закрытым сделкам'} />
        {t.max_dd_real_usd != null && (
          <Card label="Реальная просадка" help={HELP.real_dd} value={fmtUsd(-t.max_dd_real_usd)} cls="neg" sub={`${(t.max_dd_real_pct ?? 0).toFixed(1)}% · с открытыми позициями, плечом и доборами`} />
        )}
        {!!result.params.funding_apr && <Card label="Фандинг" help={HELP.funding_apr} value={fmtUsd(-(t.funding_usd ?? 0))} cls={signClass(-(t.funding_usd ?? 0))} sub={`${result.params.funding_apr}% годовых · ${result.params.funding_period_h} ч · уже в итоге`} />}
        <Card label="Средняя сделка" value={fmtPct(t.avg_pct, 3)} cls={signClass(t.avg_pct)} sub={fmtUsd(t.expectancy_usd, 1)} />
        <Card label="Ср. плюс / минус" value={`${fmtUsd(t.avg_win_usd)} / ${fmtUsd(t.avg_loss_usd)}`} />
        <Card label="Лучшая / худшая" value={`${fmtUsd(t.best_usd)} / ${fmtUsd(t.worst_usd)}`} />
        {result.params.tps.length > 1 && (
          <Card label="Дошли до тейков" value={t.tp_reach.slice(0, result.params.tps.length).join(' / ')} sub={result.params.tps.map((_, i) => `TP${i + 1}`).join(' / ')} />
        )}
        {t.adds > 0 && <Card label="Позиций / доборов" value={`${t.positions} / ${t.adds}`} sub="сделок считается по ногам" />}
        <Card label="Серия убытков" value={String(t.max_loss_streak)} sub="макс. подряд" />
        <Card label="В позиции" value={`${t.exposure_pct.toFixed(0)}%`} sub="доля времени" />
      </div>

      {result.time_stats && result.stats.total.trades > 0 && <TimeTables ts={result.time_stats} />}

      {result.hold_stats && <HoldTable hs={result.hold_stats} />}

      {result.weekday_split && result.weekday_split.weekday.trades + result.weekday_split.weekend.trades > 0 && <WeekdayTable split={result.weekday_split} />}

      {Object.keys(result.skipped_reasons ?? {}).length > 0 && (
        <>
          <h3>Пропущенные сигналы</h3>
          <ul className="reasons" data-testid="skipped-reasons">
            {Object.entries(result.skipped_reasons).sort((a, b) => b[1] - a[1]).map(([r, n]) => <li key={r}><b>{n}</b> — {r}</li>)}
          </ul>
        </>
      )}

      <h3>Разрезы</h3>
      <table className="mini">
        <thead>
          <tr><th></th><th>n</th><th>win%</th><th>PF</th><th>итого</th><th>просадка</th></tr>
        </thead>
        <tbody>
          {rows.map(([name, s]) => (
            <tr key={name}>
              <td>{name}</td>
              <td>{s.trades}</td>
              <td>{s.trades ? s.winrate.toFixed(0) : '–'}</td>
              <td>{s.trades ? fmtPf(s.profit_factor) : '–'}</td>
              <td className={signClass(s.total_usd)}>{s.trades ? fmtUsd(s.total_usd) : '–'}</td>
              <td className="neg">{s.trades ? fmtUsd(-s.max_dd_usd) : '–'}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="note">Половины — по времени отчётов: граница в середине истории, как в консольных свипах.</p>

      <h3>По месяцам</h3>
      <div className="months">
        {result.monthly.map((m) => (
          <div className="month" key={m.month} title={`${m.trades} сделок, плюсовых ${m.wins}`}>
            <span className="month-name">{m.month}</span>
            <span className="month-bar">
              <span
                className={m.pnl_usd >= 0 ? 'bar pos-bg' : 'bar neg-bg'}
                style={{ width: `${(Math.abs(m.pnl_usd) / maxAbs) * 100}%` }}
              />
            </span>
            <span className={`month-val ${signClass(m.pnl_usd)}`}>{fmtUsd(m.pnl_usd)}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
