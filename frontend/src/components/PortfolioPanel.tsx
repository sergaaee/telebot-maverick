import { useCallback, useEffect, useState } from 'react';
import { getDigest, getPortfolio, setDigest } from '../api';
import { fmtPct, fmtPf, fmtUsd, signClass } from '../format';
import { HELP } from '../helpTexts';
import type { Portfolio } from '../types';
import Help from './Help';
import MiniLines, { PALETTE } from './MiniLines';

const corrColor = (v: number | null) => (v == null ? 'transparent' : v >= 0 ? `rgba(255,107,107,${Math.min(0.75, Math.abs(v) * 0.75)})` : `rgba(47,191,113,${Math.min(0.75, Math.abs(v) * 0.75)})`);

/** Портфель стратегий: сравнение, корреляция, суммарный риск, выгрузки и сводка в Telegram. */
export default function PortfolioPanel() {
  const [pf, setPf] = useState<Portfolio | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [digest, setDigestState] = useState<{ mode: string; telegram: boolean; hour_utc: number } | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setPf(await getPortfolio());
      setErr(null);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  }, []);
  useEffect(() => {
    void load();
    void getDigest().then(setDigestState).catch(() => undefined);
  }, [load]);

  const changeDigest = async (mode: string) => {
    try {
      await setDigest(mode);
      setDigestState((d) => (d ? { ...d, mode } : d));
      setMsg(mode === 'off' ? 'Сводка выключена' : `Сводка включена: ${mode === 'daily' ? 'каждый день' : 'по понедельникам'}`);
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e));
    }
  };

  const ok = pf?.items.filter((i) => !i.error && !i.paused && i.curve && i.curve.length) ?? [];
  const lines = pf
    ? [...ok.map((i, k) => ({ name: i.name, color: PALETTE[k % PALETTE.length], points: i.curve! })), ...(pf.portfolio.curve.length ? [{ name: 'Портфель (всего)', color: '#ffffff', points: pf.portfolio.curve, bold: true }] : [])]
    : [];
  return (
    <div className="monitor" data-testid="portfolio-page">
      <div className="mon-top">
        <h2>Портфель <Help text={HELP.pf_section} /></h2>
        <button type="button" className="ghost" onClick={() => void load()}>Обновить</button>
        <span className="mon-spacer" />
        <span className="muted small">Выгрузка CSV <Help text={HELP.pf_export} /></span>
        <a className="ghost-link" href="/api/export/trades.csv" download>Сделки</a>
        <a className="ghost-link" href="/api/export/autolog.csv" download>Журнал автоторговли</a>
        <a className="ghost-link" href="/api/export/fills.csv" download>Исполнения на бирже</a>
      </div>
      <div className="mon-test">
        <b>Сводка в Telegram</b><Help text={HELP.pf_digest} />
        <select value={digest?.mode ?? 'off'} onChange={(e) => void changeDigest(e.target.value)} aria-label="Сводка в Telegram" disabled={!digest}>
          <option value="off">выключена</option><option value="daily">каждый день</option><option value="weekly">по понедельникам</option>
        </select>
        {digest && !digest.telegram && <span className="muted small">Telegram не привязан — сначала /link в боте</span>}
        {digest && digest.telegram && <span className="muted small">{digest.hour_utc.toString().padStart(2, '0')}:00 UTC</span>}
        {msg && <span className="muted small"> {msg}</span>}
      </div>
      {err && <div className="banner error">{err}</div>}
      {!pf && !err && <div className="empty">Загрузка…</div>}
      {pf && (
        <>
          <div className="pf-cards">
            <div className="card"><div className="card-label">Стратегий в портфеле</div><div className="card-value">{pf.portfolio.strategies}</div></div>
            <div className="card"><div className="card-label">Результат (расчёт)</div><div className={`card-value ${signClass(pf.portfolio.total_usd)}`}>{fmtUsd(pf.portfolio.total_usd)}</div></div>
            <div className="card"><div className="card-label">К сумме депозитов</div><div className={`card-value ${signClass(pf.portfolio.return_pct ?? 0)}`}>{pf.portfolio.return_pct == null ? '–' : fmtPct(pf.portfolio.return_pct, 1)}</div></div>
            <div className="card"><div className="card-label">Макс. просадка</div><div className="card-value neg">{pf.portfolio.max_dd_pct.toFixed(1)}%</div></div>
            <div className="card"><div className="card-label">Лучшая / худшая</div><div className="card-value small">{pf.portfolio.best ?? '–'} / {pf.portfolio.worst ?? '–'}</div></div>
          </div>
          <MiniLines lines={lines} unit="%" testid="pf-chart" />
          <div className="table-wrap mon-legs"><table>
            <thead><tr><th>Стратегия</th><th>Актив</th><th>Сделок</th><th>Winrate</th><th>PF</th><th>Результат</th><th>% депозита</th><th>Макс. просадка</th><th>Sharpe</th></tr></thead>
            <tbody>
              {pf.items.map((i) => (
                <tr key={i.id} className={i.paused ? 'muted' : ''}>
                  <td>{i.name}{i.paused ? ' (пауза)' : ''}</td><td>{(i.symbol ?? '–').split('/')[0]}</td>
                  {i.error ? <td colSpan={7} className="bad-text small">{i.error}</td> : (
                    <>
                      <td>{i.trades}</td><td>{i.winrate != null ? `${i.winrate.toFixed(0)}%` : '–'}</td><td>{i.profit_factor == null ? (i.trades ? '∞' : '–') : fmtPf(i.profit_factor)}</td>
                      <td className={signClass(i.total_usd ?? 0)}>{fmtUsd(i.total_usd ?? 0)}</td><td className={signClass(i.return_pct ?? 0)}>{i.return_pct != null ? fmtPct(i.return_pct, 1) : '–'}</td>
                      <td className="neg">{i.max_dd_pct != null ? `${i.max_dd_pct.toFixed(1)}%` : '–'}</td><td>{i.sharpe != null ? i.sharpe.toFixed(2) : '–'}</td>
                    </>
                  )}
                </tr>
              ))}
            </tbody>
          </table></div>

          <h3>Риск: открытые позиции вместе <Help text={HELP.pf_exposure} /></h3>
          {pf.exposure.warnings.map((w) => <div key={w} className="banner warn" data-testid="pf-warning">⚠️ {w}</div>)}
          {pf.exposure.symbols.length === 0 ? <div className="empty small">Открытых позиций по расчёту нет.</div> : (
            <div className="table-wrap mon-legs"><table>
              <thead><tr><th>Актив</th><th>Лонг</th><th>Шорт</th><th>Нетто</th><th>Маржа</th><th>Кто</th></tr></thead>
              <tbody>
                {pf.exposure.symbols.map((s) => (
                  <tr key={s.asset}><td>{s.asset}</td><td className="pos">{fmtUsd(s.long)}</td><td className="neg">{fmtUsd(s.short)}</td><td className={signClass(s.net)}>{fmtUsd(s.net)}</td><td>{fmtUsd(s.margin)}</td>
                    <td className="muted small">{s.configs.map((c) => `${c.name} (${c.side === 'long' ? 'лонг' : 'шорт'})`).join(', ')}</td></tr>
                ))}
              </tbody>
            </table></div>
          )}
          <div className="muted small">
            Всего: объём {fmtUsd(pf.exposure.gross)}, маржа {fmtUsd(pf.exposure.margin)}{pf.exposure.leverage != null && <>, плечо портфеля {pf.exposure.leverage.toFixed(2)}×</>}
            {pf.exposure.margin_pct != null && <> ({pf.exposure.margin_pct.toFixed(0)}% суммы депозитов)</>}
            {pf.exposure.real && <> · на бирже: капитал {fmtUsd(pf.exposure.real.equity, 2)}, объём позиций {fmtUsd(pf.exposure.real.gross)}{pf.exposure.real.leverage != null && <>, плечо {pf.exposure.real.leverage.toFixed(2)}×</>}</>}
          </div>

          <h3>Корреляция дневных результатов <Help text={HELP.pf_corr} /></h3>
          {pf.correlation.note ? <div className="empty small">{pf.correlation.note}</div> : (
            <div className="table-wrap mon-legs"><table className="pf-corr" data-testid="pf-corr">
              <thead><tr><th></th>{pf.correlation.names.map((n) => <th key={n}>{n}</th>)}</tr></thead>
              <tbody>
                {pf.correlation.matrix.map((row, a) => (
                  <tr key={pf.correlation.ids[a]}><th>{pf.correlation.names[a]}</th>
                    {row.map((v, b) => <td key={b} style={{ background: corrColor(a === b ? null : v) }}>{v == null ? '–' : v.toFixed(2)}</td>)}</tr>
                ))}
              </tbody>
            </table></div>
          )}
          {pf.correlation.days != null && <div className="muted small">За {pf.correlation.days} дн. общей истории. Красное — стратегии ходят вместе (риск складывается), зелёное — гасят друг друга.</div>}
        </>
      )}
    </div>
  );
}
