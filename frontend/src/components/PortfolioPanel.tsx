import { useCallback, useEffect, useMemo, useState } from 'react';
import { getDigest, getPortfolio, savePortfolioPrefs, setDigest } from '../api';
import { fmtPct, fmtPf, fmtTs, fmtUsd, signClass } from '../format';
import { HELP } from '../helpTexts';
import type { Portfolio } from '../types';
import Help from './Help';
import PortfolioChart, { PALETTE, type CurveLine } from './PortfolioChart';

const corrColor = (v: number | null) => (v == null ? 'transparent' : v >= 0 ? `rgba(255,107,107,${Math.min(0.75, Math.abs(v) * 0.75)})` : `rgba(47,191,113,${Math.min(0.75, Math.abs(v) * 0.75)})`);

/** Портфель стратегий: сравнение, корреляция, суммарный риск, выгрузки и сводка в Telegram. */
export default function PortfolioPanel() {
  const [pf, setPf] = useState<Portfolio | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [digest, setDigestState] = useState<{ mode: string; telegram: boolean; hour_utc: number } | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [depInput, setDepInput] = useState('');

  const load = useCallback(async () => {
    try {
      const r = await getPortfolio();
      setPf(r);
      setDepInput((cur) => (document.activeElement?.id === 'pf-total-deposit' ? cur : r.settings.total_deposit != null ? String(r.settings.total_deposit) : ''));
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

  const savePrefs = async (patch: { exclude?: number[]; total_deposit?: number | null }) => {
    try {
      await savePortfolioPrefs(patch);
      setMsg(null);
      await load();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  };
  const toggle = (id: number, on: boolean) => {
    const cur = new Set(pf?.settings.exclude ?? []);
    if (on) cur.delete(id); else cur.add(id);
    void savePrefs({ exclude: [...cur].sort((a, b) => a - b) });
  };
  const applyDeposit = () => {
    const v = Number(depInput.replace(/\s/g, '').replace(',', '.'));
    if (depInput.trim() === '') { void savePrefs({ total_deposit: null }); return; }
    if (!Number.isFinite(v) || v <= 0) { setErr('Общий депозит — число больше 0'); return; }
    void savePrefs({ total_deposit: v });
  };

  const ok = pf?.items.filter((i) => i.included && !i.error && i.curve && i.curve.length) ?? [];
  const colorOf = (id: number) => PALETTE[(pf?.items.findIndex((x) => x.id === id) ?? 0) % PALETTE.length];
  const lines: CurveLine[] = useMemo(
    () => (pf ? [...ok.map((i) => ({ name: i.name, color: colorOf(i.id), points: i.curve! })), ...(pf.portfolio.curve.length ? [{ name: 'Портфель (всего)', color: '#ffffff', points: pf.portfolio.curve, bold: true }] : [])] : []),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [pf],
  );
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
      <div className="mon-test" data-testid="pf-deposit">
        <b>Общий депозит</b><Help text={HELP.pf_deposit} />
        <input id="pf-total-deposit" className="pf-dep-input" inputMode="decimal" placeholder="у каждой свой" value={depInput} onChange={(e) => setDepInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') applyDeposit(); }} aria-label="Общий депозит на все стратегии, $" /> <span className="muted small">$</span>
        <button type="button" className="ghost" onClick={applyDeposit}>Применить</button>
        {pf?.settings.total_deposit != null && <button type="button" className="ghost" onClick={() => { setDepInput(''); void savePrefs({ total_deposit: null }); }}>У каждой свой</button>}
        <span className="muted small">{pf?.portfolio.joint ? `Все стратегии торгуют с одного счёта ${fmtUsd(pf.portfolio.deposit)}: размер каждой сделки считается от общего баланса` : 'Сейчас у каждой стратегии депозит из её конфига; в портфеле они складываются'}</span>
      </div>
      {err && <div className="banner error">{err}</div>}
      {!pf && !err && <div className="empty">Загрузка…</div>}
      {pf && (
        <>
          <div className="pf-cards">
            <div className="card"><div className="card-label">Стратегий в портфеле</div><div className="card-value">{pf.portfolio.strategies}</div></div>
            <div className="card"><div className="card-label">Результат (расчёт)</div><div className={`card-value ${signClass(pf.portfolio.total_usd)}`}>{fmtUsd(pf.portfolio.total_usd)}</div></div>
            <div className="card"><div className="card-label">К сумме депозитов</div><div className={`card-value ${signClass(pf.portfolio.return_pct ?? 0)}`}>{pf.portfolio.return_pct == null ? '–' : fmtPct(pf.portfolio.return_pct, 1)}</div></div>
            <div className="card" data-testid="pf-dd" title={`С учётом открытых позиций по рыночной цене${pf.portfolio.max_dd_real_ts ? `, самый глубокий провал: ${fmtTs(pf.portfolio.max_dd_real_ts)}` : ''}. Только по закрытым сделкам: ${pf.portfolio.max_dd_pct.toFixed(1)}% от депозита (${fmtUsd(pf.portfolio.max_dd_usd)})`}>
              <div className="card-label">Макс. просадка (с позициями) <Help text={HELP.pf_dd_real} /></div>
              <div className="card-value neg">{(pf.portfolio.max_dd_real_pct ?? pf.portfolio.max_dd_pct).toFixed(1)}%</div>
              <div className="muted small">{pf.portfolio.max_dd_real_usd != null ? `${fmtUsd(pf.portfolio.max_dd_real_usd)} · по закрытым ${pf.portfolio.max_dd_pct.toFixed(1)}%` : 'по закрытым сделкам'}</div>
            </div>
            <div className="card"><div className="card-label">{pf.portfolio.joint ? 'Общий депозит' : 'Сумма депозитов'}</div><div className="card-value">{fmtUsd(pf.portfolio.deposit)}</div></div>
            <div className="card" title={pf.portfolio.peak_margin_ts ? `пик: ${fmtTs(pf.portfolio.peak_margin_ts)}` : undefined}><div className="card-label">Пик занятой маржи</div><div className={`card-value ${(pf.portfolio.peak_margin_pct ?? 0) > 100 ? 'neg' : ''}`}>{pf.portfolio.peak_margin_pct == null ? '–' : `${pf.portfolio.peak_margin_pct.toFixed(0)}%`}</div></div>
            <div className="card"><div className="card-label">Лучшая / худшая</div><div className="card-value small">{pf.portfolio.best ?? '–'} / {pf.portfolio.worst ?? '–'}</div></div>
          </div>
          {lines.length ? <PortfolioChart lines={lines} /> : <div className="empty small">Нет закрытых сделок у выбранных стратегий</div>}
          <div className="table-wrap mon-legs"><table>
            <thead><tr><th title="Учитывать стратегию в расчётах портфеля">В портфеле <Help text={HELP.pf_include} /></th><th>Стратегия</th><th>Актив</th><th>Сделок</th><th>Winrate</th><th>PF</th><th>Результат</th><th>% депозита</th><th title="С учётом открытых позиций (от пика капитала); в подсказке ячейки — по закрытым сделкам">Макс. просадка</th><th>Sharpe</th></tr></thead>
            <tbody>
              {pf.items.map((i) => (
                <tr key={i.id} className={i.included ? '' : 'muted'} data-testid="pf-row">
                  <td><input type="checkbox" checked={i.included} disabled={i.paused || !!i.error} onChange={(e) => toggle(i.id, e.target.checked)} aria-label={`Учитывать ${i.name} в портфеле`} data-testid="pf-include" /></td>
                  <td><i className="pf-dot" style={{ background: i.included ? colorOf(i.id) : 'transparent' }} /> {i.name}{i.paused ? ' (пауза)' : !i.included && !i.error ? ' (не учитывается)' : ''}</td><td>{(i.symbol ?? '–').split('/')[0]}</td>
                  {i.error ? <td colSpan={7} className="bad-text small">{i.error}</td> : (
                    <>
                      <td>{i.trades}</td><td>{i.winrate != null ? `${i.winrate.toFixed(0)}%` : '–'}</td><td>{i.profit_factor == null ? (i.trades ? '∞' : '–') : fmtPf(i.profit_factor)}</td>
                      <td className={signClass(i.total_usd ?? 0)}>{fmtUsd(i.total_usd ?? 0)}</td><td className={signClass(i.return_pct ?? 0)}>{i.return_pct != null ? fmtPct(i.return_pct, 1) : '–'}</td>
                      <td className="neg" title={i.max_dd_pct != null ? `по закрытым сделкам: ${i.max_dd_pct.toFixed(1)}%` : undefined}>{i.max_dd_real_pct != null ? `${i.max_dd_real_pct.toFixed(1)}%` : i.max_dd_pct != null ? `${i.max_dd_pct.toFixed(1)}%` : '–'}</td><td>{i.sharpe != null ? i.sharpe.toFixed(2) : '–'}</td>
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
