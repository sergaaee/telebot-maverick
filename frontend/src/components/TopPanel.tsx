import { useMemo, useState } from 'react';
import { fmtPct, fmtPf, fmtTs, fmtUsd, signClass } from '../format';
import { HELP } from '../helpTexts';
import type { Params, TopSection } from '../types';
import Help from './Help';
import { Card } from './StatsPanel';

type Enabled = Extract<TopSection, { enabled: true }>;

interface Props {
  top: Enabled;
  params: Params;
  /** идёт ли сейчас загрузка свечей топ-коинов */
  downloading: boolean;
  onDownload: () => void;
  onPickCoin: (symbol: string) => void;
}

/** Статистика по сделкам с топ-коинами: итоги, разрез по монетам, пропущенные сигналы и недостающие свечи. */
export default function TopPanel({ top, params, downloading, onDownload, onPickCoin }: Props) {
  const t = top.stats;
  const [showSkipped, setShowSkipped] = useState(false);
  const reasons = useMemo(() => {
    const m = new Map<string, number>();
    for (const s of top.skipped) m.set(s.reason, (m.get(s.reason) ?? 0) + 1);
    return [...m.entries()].sort((a, b) => b[1] - a[1]);
  }, [top.skipped]);
  const p = top.pending;
  return (
    <div className="stats" data-testid="top-panel">
      {p.windows > 0 && (
        <div className="banner warn top-pending">
          <div>
            Нет свечей для окон: {p.windows} (монет: {p.symbols}) — сделки по ним пока не посчитаны.
            Скачать ≈{p.requests} запросов к бирже (момент сигнала + {params.top_days} сут).
          </div>
          <button type="button" className="ghost" disabled={downloading} onClick={onDownload}>{downloading ? 'Качаю…' : 'Скачать свечи'}</button>
          <Help text={HELP.top_download} />
        </div>
      )}
      <div className="cards">
        <Card label="Итого" value={fmtUsd(t.total_usd)} cls={signClass(t.total_usd)} sub={`по ${params.top_size} $ на сделку`} />
        <Card label="Сделок" value={String(t.trades)} sub={`TP ${t.tp} · БУ ${t.be} · SL ${t.sl} · время ${t.time} · откр. ${t.open}`} />
        <Card label="Winrate" value={`${t.winrate.toFixed(1)}%`} />
        <Card label="Profit factor" value={fmtPf(t.profit_factor)} />
        <Card label="Макс. просадка" value={fmtUsd(-t.max_dd_usd)} cls="neg" />
        <Card label="Средняя сделка" value={fmtPct(t.avg_pct, 3)} cls={signClass(t.avg_pct)} sub={fmtUsd(t.expectancy_usd, 1)} />
        <Card label="Ср. плюс / минус" value={`${fmtUsd(t.avg_win_usd)} / ${fmtUsd(t.avg_loss_usd)}`} />
        <Card label="Лучшая / худшая" value={`${fmtUsd(t.best_usd)} / ${fmtUsd(t.worst_usd)}`} />
        {params.top_tps.length > 1 && (
          <Card label="Дошли до тейков" value={t.tp_reach.slice(0, params.top_tps.length).join(' / ')} sub={params.top_tps.map((_, i) => `TP${i + 1}`).join(' / ')} />
        )}
      </div>

      <h3>По монетам</h3>
      {top.by_coin.length ? (
        <table className="mini clickable">
          <thead><tr><th>монета</th><th>n</th><th>win%</th><th>итого</th></tr></thead>
          <tbody>
            {top.by_coin.map((c) => (
              <tr key={c.symbol} onClick={() => onPickCoin(c.symbol)} title="Показать первую сделку по этой монете">
                <td>{c.symbol.split('/')[0]}</td>
                <td>{c.trades}</td>
                <td>{c.trades ? ((100 * c.wins) / c.trades).toFixed(0) : '–'}</td>
                <td className={signClass(c.pnl_usd)}>{fmtUsd(c.pnl_usd)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="note">Пока нет посчитанных сделок.</p>
      )}

      <h3>
        Пропущенные сигналы: {top.skipped_total}
        {top.skipped_total > 0 && (
          <button type="button" className="link" onClick={() => setShowSkipped(!showSkipped)}>{showSkipped ? 'скрыть' : 'показать'}</button>
        )}
      </h3>
      {reasons.length > 0 && (
        <ul className="reasons">
          {reasons.map(([r, n]) => <li key={r}><b>{n}</b> — {r}</li>)}
        </ul>
      )}
      {showSkipped && (
        <div className="skipped-list">
          {top.skipped.slice(0, 300).map((s, i) => (
            <div key={i}>
              <span className="muted">{fmtTs(s.ts)}</span> {s.coin ?? '—'} · {s.reason}
            </div>
          ))}
          {top.skipped.length > 300 && <div className="muted">… показаны первые 300</div>}
        </div>
      )}
    </div>
  );
}
