import { useCallback, useEffect, useState } from 'react';
import { deleteRun, listRuns, saveRun } from '../api';
import { fmtDate, fmtPct, fmtUsd, signClass } from '../format';
import { HELP } from '../helpTexts';
import type { BacktestResult, Params, RunSummary, SavedRun, TfMode } from '../types';
import Help from './Help';

export const summarize = (r: BacktestResult): RunSummary => {
  const t = r.stats.total;
  return {
    trades: t.trades, total_usd: t.total_usd, return_pct: t.return_pct, sharpe: t.sharpe, max_dd_pct: t.max_dd_pct, winrate: t.winrate, profit_factor: t.profit_factor,
    symbol: r.symbol, source: r.source.label, first_ts: r.range.first_ts, last_ts: r.range.last_ts,
  };
};

/** Сохранённые прогоны пользователя: имя + параметры + ключевые цифры. «Загрузить» применяет параметры и пересчитывает. */
export default function RunsPanel({ params, tf, result, onLoad }: { params: Params; tf: TfMode; result: BacktestResult; onLoad: (r: SavedRun) => void }) {
  const [runs, setRuns] = useState<SavedRun[]>([]);
  const [name, setName] = useState('');
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const reload = useCallback(() => listRuns().then(setRuns).catch(() => undefined), []);
  useEffect(() => {
    void reload();
  }, [reload]);

  const save = async () => {
    setErr(null);
    try {
      const r = await saveRun(name, result.params ?? params, tf, summarize(result));
      setMsg(`Сохранено: ${r.name}`);
      setName('');
      await reload();
      setTimeout(() => setMsg(null), 4000);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  };

  const remove = async (r: SavedRun) => {
    if (!window.confirm(`Удалить прогон «${r.name}»?`)) return;
    await deleteRun(r.id).catch((e) => setErr(e instanceof Error ? e.message : String(e)));
    await reload();
  };

  return (
    <div className="side-block" data-testid="runs-panel">
      <h3>Сохранённые прогоны <Help text={HELP.runs_section} /></h3>
      <div className="runs-save">
        <input type="text" value={name} maxLength={80} placeholder="название, например «База v1»" aria-label="Название прогона" onChange={(e) => setName(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && name.trim() && void save()} />
        <button type="button" className="ghost" disabled={!name.trim()} onClick={() => void save()}>Сохранить</button>
      </div>
      {msg && <div className="note">{msg}</div>}
      {err && <div className="note bad-text">{err}</div>}
      {runs.length === 0 ? (
        <p className="note">Пока ничего не сохранено.</p>
      ) : (
        <ul className="runs-list">
          {runs.map((r) => (
            <li key={r.id} data-run={r.name}>
              <div className="run-head">
                <b>{r.name}</b>
                <span className="run-actions">
                  <button type="button" className="ghost" onClick={() => onLoad(r)}>Загрузить</button>
                  <button type="button" className="ghost" aria-label={`Удалить ${r.name}`} onClick={() => void remove(r)}>×</button>
                </span>
              </div>
              <div className="muted run-sub">
                {r.summary.symbol.split('/')[0]} · {r.summary.source} · {fmtDate(r.summary.first_ts)} → {fmtDate(r.summary.last_ts)}
              </div>
              <div className="run-sub">
                {r.summary.trades} сделок · <span className={signClass(r.summary.total_usd)}>{fmtUsd(r.summary.total_usd)}</span>
                {r.summary.return_pct != null && <> ({fmtPct(r.summary.return_pct, 1)})</>}
                {' · '}Шарп {r.summary.sharpe == null ? '–' : r.summary.sharpe.toFixed(2)}
                {r.summary.max_dd_pct != null && <> · просадка {r.summary.max_dd_pct.toFixed(1)}%</>}
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
