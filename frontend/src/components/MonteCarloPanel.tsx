import { useEffect, useState } from 'react';
import { runMonteCarlo } from '../api';
import { fmtPct, signClass } from '../format';
import { HELP } from '../helpTexts';
import type { BacktestResult, MonteCarlo, Params } from '../types';
import Help from './Help';

const W = 300;
const H = 120;

function Fan({ mc }: { mc: MonteCarlo }) {
  const fan = [{ n: 0, '5': 1, '25': 1, '50': 1, '75': 1, '95': 1 }, ...mc.fan];
  const maxN = fan[fan.length - 1].n || 1;
  const lo = Math.min(...fan.map((f) => f['5']), 1);
  const hi = Math.max(...fan.map((f) => f['95']), 1);
  const x = (n: number) => (n / maxN) * W;
  const y = (v: number) => H - ((v - lo) / (hi - lo || 1)) * (H - 4) - 2;
  const area = (a: '5' | '25', b: '75' | '95') =>
    fan.map((f, i) => `${i ? 'L' : 'M'}${x(f.n).toFixed(1)},${y(f[b]).toFixed(1)}`).join(' ') + ' ' +
    [...fan].reverse().map((f) => `L${x(f.n).toFixed(1)},${y(f[a]).toFixed(1)}`).join(' ') + ' Z';
  const median = fan.map((f, i) => `${i ? 'L' : 'M'}${x(f.n).toFixed(1)},${y(f['50']).toFixed(1)}`).join(' ');
  return (
    <svg className="mc-fan" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" data-testid="mc-fan" role="img" aria-label="Веер капитала: 5–95% и 25–75% перцентили">
      <path d={area('5', '95')} className="mc-band-wide" />
      <path d={area('25', '75')} className="mc-band" />
      <line x1="0" x2={W} y1={y(1)} y2={y(1)} className="mc-base" />
      <path d={median} className="mc-median" />
    </svg>
  );
}

/** Монте-Карло по сделкам текущего расчёта. Результат сбрасывается, когда пересчитан бэктест (другие сделки). */
export default function MonteCarloPanel({ params, result }: { params: Params; result: BacktestResult }) {
  const [runs, setRuns] = useState(1000);
  const [mode, setMode] = useState<MonteCarlo['mode']>('shuffle');
  const [mc, setMc] = useState<MonteCarlo | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    setMc(null);
    setErr(null);
  }, [result]);

  const go = async () => {
    setBusy(true);
    setErr(null);
    try {
      setMc(await runMonteCarlo(result.params ?? params, runs, mode));
    } catch (e) {
      setMc(null);
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="side-block" data-testid="mc-panel">
      <h3>Монте-Карло <Help text={HELP.mc_section} /></h3>
      <div className="mc-controls">
        <label className="field">
          <span className="label">Прогонов <Help text={HELP.mc_runs} /></span>
          <select value={runs} onChange={(e) => setRuns(Number(e.target.value))}>
            {[300, 1000, 3000].map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
        </label>
        <label className="field">
          <span className="label">Режим <Help text={HELP.mc_mode} /></span>
          <select value={mode} onChange={(e) => setMode(e.target.value as MonteCarlo['mode'])}>
            <option value="shuffle">перемешать порядок</option>
            <option value="bootstrap">выборка с возвращением</option>
          </select>
        </label>
        <button type="button" className="ghost" disabled={busy} onClick={() => void go()}>{busy ? 'Считаю…' : 'Рассчитать'}</button>
      </div>
      {err && <div className="note bad-text">{err}</div>}
      {mc && (
        <>
          <Fan mc={mc} />
          <table className="mini mc-table" data-testid="mc-table">
            <thead><tr><th></th><th>плохо (5%)</th><th>медиана</th><th>хорошо (95%)</th></tr></thead>
            <tbody>
              <tr><td>Итог</td>{(['5', '50', '95'] as const).map((k) => <td key={k} className={signClass(mc.final_pct[k])}>{fmtPct(mc.final_pct[k], 1)}</td>)}</tr>
              <tr><td>Просадка</td>{(['5', '50', '95'] as const).map((k) => <td key={k} className="neg">{mc.max_dd_pct[k].toFixed(1)}%</td>)}</tr>
              <tr><td>Фактически</td><td colSpan={3}>итог <span className={signClass(mc.actual.final_pct)}>{fmtPct(mc.actual.final_pct, 1)}</span> · просадка <span className="neg">{mc.actual.max_dd_pct.toFixed(1)}%</span></td></tr>
            </tbody>
          </table>
          <p className="note">
            Вероятность убытка <b>{mc.prob_loss_pct.toFixed(1)}%</b>; просадка ≥ 20% — <b>{mc.prob_dd_pct['20'].toFixed(1)}%</b>, ≥ 30% — <b>{mc.prob_dd_pct['30'].toFixed(1)}%</b>, ≥ 50% — <b>{mc.prob_dd_pct['50'].toFixed(1)}%</b>
            {mc.prob_ruin_pct > 0 && <>; счёт слит в <b className="neg">{mc.prob_ruin_pct.toFixed(1)}%</b> прогонов</>}.
            {' '}{mc.runs} прогонов по {mc.trades} сделкам, {mc.mode === 'shuffle' ? 'порядок перемешан' : 'выборка с возвращением'}. Это не прогноз.
          </p>
        </>
      )}
    </div>
  );
}
