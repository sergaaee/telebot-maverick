import { useEffect, useState } from 'react';
import { runWalkForward, type WfRequest } from '../api';
import { signClass } from '../format';
import { HELP } from '../helpTexts';
import type { Params, WalkForward } from '../types';
import Help from './Help';
import MiniLines from './MiniLines';

const PARAMS: [string, string][] = [
  ['local_min', 'DL для лонга (≥)'], ['scalp_min', 'DS для лонга (≥)'], ['global_min', 'DG для лонга (≥)'],
  ['short_local_max', 'DL для шорта (≤)'], ['short_scalp_max', 'DS для шорта (≤)'], ['short_global_max', 'DG для шорта (≤)'],
  ['sl_pct', 'Стоп-лосс, %'], ['confirm', 'Подтверждение, отчётов'],
];
const usd = (v: number) => `${v >= 0 ? '+' : '−'}${Math.abs(v).toFixed(0)} $`;

/** Walk-forward: подбор параметров на окне и проверка на следующем. */
export default function WalkForwardPanel({ params }: { params: Params }) {
  const [p1, setP1] = useState('local_min');
  const [v1, setV1] = useState('1.6, 1.8, 2.0');
  const [p2, setP2] = useState('');
  const [v2, setV2] = useState('');
  const [train, setTrain] = useState(90);
  const [test, setTest] = useState(30);
  const [metric, setMetric] = useState('pnl');
  const [minTr, setMinTr] = useState(5);
  const [res, setRes] = useState<WalkForward | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => { setRes(null); setErr(null); }, [params]);

  const nums = (s: string) => s.split(/[,;\s]+/).filter(Boolean).map(Number);
  const go = async () => {
    const grid = [{ param: p1, values: nums(v1) }, ...(p2 ? [{ param: p2, values: nums(v2) }] : [])];
    if (grid.some((g) => g.values.length < 2 || g.values.some((v) => !Number.isFinite(v)))) { setErr('Для каждого параметра нужно хотя бы два числовых значения через запятую'); return; }
    const req: WfRequest = { grid, train_days: train, test_days: test, metric, min_trades: minTr };
    setBusy(true);
    setErr(null);
    try {
      setRes(await runWalkForward(params, req));
    } catch (e) {
      setRes(null);
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  const opts = (skip?: string) => PARAMS.filter(([k]) => k !== skip).map(([k, l]) => <option key={k} value={k}>{l}</option>);
  const s = res?.summary;
  return (
    <div className="side-block" data-testid="wf-panel">
      <h3>Walk-forward <Help text={HELP.wf_section} /></h3>
      <div className="wf-controls">
        <label className="field"><span className="label">Параметр 1 <Help text={HELP.wf_params} /></span>
          <select value={p1} onChange={(e) => setP1(e.target.value)}>{opts(p2)}</select>
          <input value={v1} onChange={(e) => setV1(e.target.value)} aria-label="Значения параметра 1" /></label>
        <label className="field"><span className="label">Параметр 2 (необязательно)</span>
          <select value={p2} onChange={(e) => setP2(e.target.value)}><option value="">—</option>{opts(p1)}</select>
          <input value={v2} onChange={(e) => setV2(e.target.value)} disabled={!p2} aria-label="Значения параметра 2" /></label>
        <label className="field"><span className="label">Подбор / проверка, дней <Help text={HELP.wf_windows} /></span>
          <span className="wf-pair"><input type="number" min={14} value={train} onChange={(e) => setTrain(Number(e.target.value))} /><input type="number" min={7} value={test} onChange={(e) => setTest(Number(e.target.value))} /></span></label>
        <label className="field"><span className="label">Метрика <Help text={HELP.wf_metric} /></span>
          <select value={metric} onChange={(e) => setMetric(e.target.value)}>
            <option value="pnl">результат $</option><option value="sharpe">Sharpe</option><option value="profit_factor">профит-фактор</option><option value="calmar">результат / просадка</option>
          </select></label>
        <label className="field"><span className="label">Мин. сделок на подборе</span><input type="number" min={1} value={minTr} onChange={(e) => setMinTr(Number(e.target.value))} /></label>
        <button type="button" className="ghost" disabled={busy} onClick={() => void go()}>{busy ? 'Считаю…' : 'Проверить'}</button>
      </div>
      {err && <div className="note bad-text">{err}</div>}
      {res && s && (
        <>
          <p className="note" data-testid="wf-verdict"><b>{res.verdict}</b></p>
          <table className="mini" data-testid="wf-summary">
            <tbody>
              <tr><td>Вне выборки (проверка)</td><td className={signClass(s.oos_total_usd)}>{usd(s.oos_total_usd)}</td><td className="muted">{s.oos_trades} сделок, прибыльных окон {s.profitable_folds} из {s.folds_with_choice}</td></tr>
              <tr><td>На подборе (внутри выборки)</td><td className={signClass(s.is_total_usd)}>{usd(s.is_total_usd)}</td><td className="muted">окна подбора длиннее, поэтому сравнивайте в день</td></tr>
              <tr><td>Исходные параметры на тех же проверках</td><td className={signClass(s.base_oos_total_usd)}>{usd(s.base_oos_total_usd)}</td><td className="muted">с подбором лучше? {s.oos_total_usd > s.base_oos_total_usd ? 'да' : 'нет'}</td></tr>
              <tr><td>Эффективность <Help text={HELP.wf_eff} /></td><td>{s.efficiency == null ? '–' : s.efficiency.toFixed(2)}</td><td className="muted">прогонов {s.runs}, окон {s.folds}</td></tr>
            </tbody>
          </table>
          <MiniLines lines={[{ name: 'Накопленный результат на проверках, $', color: '#4f8cff', points: res.oos_curve, bold: true }]} unit=" $" testid="wf-curve" />
          <div className="table-wrap mon-legs"><table data-testid="wf-folds">
            <thead><tr><th>Подбор</th><th>Проверка</th><th>Выбрано</th><th>Подбор $</th><th>Проверка $</th><th>Исходные $</th></tr></thead>
            <tbody>
              {res.folds.map((f) => (
                <tr key={f.test[0]}>
                  <td className="small">{f.train[0]} … {f.train[1]}</td><td className="small">{f.test[0]} … {f.test[1]}</td>
                  <td className="small">{f.chosen ? Object.entries(f.chosen).map(([k, v]) => `${k}=${v}`).join(', ') : <span className="muted">нет подходящих</span>}</td>
                  <td className={signClass(f.train_stats?.total_usd ?? 0)}>{f.train_stats ? usd(f.train_stats.total_usd) : '–'}</td>
                  <td className={signClass(f.test_stats?.total_usd ?? 0)}>{f.test_stats ? `${usd(f.test_stats.total_usd)} (${f.test_stats.trades})` : '–'}</td>
                  <td className={signClass(f.base_test_stats?.total_usd ?? 0)}>{f.base_test_stats ? usd(f.base_test_stats.total_usd) : '–'}</td>
                </tr>
              ))}
            </tbody>
          </table></div>
          {res.stability.length > 0 && (
            <p className="note">Выбирались: {res.stability.map((x) => `${Object.entries(x.values).map(([k, v]) => `${k}=${v}`).join(', ')} — ${x.times}×`).join('; ')}. {res.stability[0].times / s.folds_with_choice < 0.5 ? 'Лучшие значения «прыгают» от окна к окну — признак нестабильности.' : 'Одни и те же значения выигрывают в большинстве окон — это хороший знак.'}</p>
          )}
          <p className="note muted">Проверка не гарантирует будущий результат: она лишь отсекает параметры, которые работали только на кусочке прошлого.</p>
        </>
      )}
    </div>
  );
}
