import { fmtPct, fmtPf, fmtUsd, signClass } from '../format';
import type { BacktestResult, Stats } from '../types';

export function Card({ label, value, cls = '', sub }: { label: string; value: string; cls?: string; sub?: string }) {
  return (
    <div className="card">
      <div className="card-label">{label}</div>
      <div className={`card-value ${cls}`}>{value}</div>
      {sub && <div className="card-sub">{sub}</div>}
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
        <Card label="Сделок" value={String(t.trades)} sub={`TP ${t.tp} · БУ ${t.be} · SL ${t.sl} · разворот ${t.rev} · откр. ${t.open}`} />
        <Card label="Коэф. Шарпа" value={t.sharpe == null ? '–' : t.sharpe.toFixed(2)} cls={t.sharpe == null ? '' : signClass(t.sharpe)} sub="годовой, по дням" />
        <Card label="Winrate" value={`${t.winrate.toFixed(1)}%`} />
        <Card label="Profit factor" value={fmtPf(t.profit_factor)} />
        <Card label="Макс. просадка" value={fmtUsd(-t.max_dd_usd)} cls="neg" sub={t.max_dd_pct != null ? `${t.max_dd_pct.toFixed(1)}% от пика` : undefined} />
        <Card label="Средняя сделка" value={fmtPct(t.avg_pct, 3)} cls={signClass(t.avg_pct)} sub={fmtUsd(t.expectancy_usd, 1)} />
        <Card label="Ср. плюс / минус" value={`${fmtUsd(t.avg_win_usd)} / ${fmtUsd(t.avg_loss_usd)}`} />
        <Card label="Лучшая / худшая" value={`${fmtUsd(t.best_usd)} / ${fmtUsd(t.worst_usd)}`} />
        {result.params.tps.length > 1 && (
          <Card label="Дошли до тейков" value={t.tp_reach.slice(0, result.params.tps.length).join(' / ')} sub={result.params.tps.map((_, i) => `TP${i + 1}`).join(' / ')} />
        )}
        <Card label="Серия убытков" value={String(t.max_loss_streak)} sub="макс. подряд" />
        <Card label="В позиции" value={`${t.exposure_pct.toFixed(0)}%`} sub="доля времени" />
      </div>

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
