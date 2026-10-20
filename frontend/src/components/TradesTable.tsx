import { useEffect, useRef } from 'react';
import { fmtHold, fmtPct, fmtPrice, fmtTs, fmtUsd, signClass } from '../format';
import { HELP } from '../helpTexts';
import type { Trade } from '../types';
import Help from './Help';

export default function TradesTable({ trades, selected, onSelect, top = false, onExport }: { trades: Trade[]; selected: number | null; onSelect: (n: number) => void; top?: boolean; onExport?: () => void }) {
  const row = useRef<HTMLTableRowElement>(null);
  useEffect(() => {
    row.current?.scrollIntoView({ block: 'nearest' });
  }, [selected]);

  if (!trades.length) return <div className="empty">{top ? 'Сделок по топ-коинам нет — проверьте параметры или скачайте свечи' : 'Сделок нет — измените параметры'}</div>;
  return (
    <div className="table-wrap">
      {onExport && (
        <div className="table-bar">
          <span className="muted">{trades.length} сделок</span>
          <button type="button" className="ghost" onClick={onExport} data-testid="export-csv">⬇ CSV</button>
          <Help text={HELP.csv_trades} />
        </div>
      )}
      <table className="trades">
        <thead>
          <tr>
            <th>#</th><th>сторона</th><th>вход (UTC)</th><th>цена</th><th>выход</th><th>цена</th><th>причина</th>
            <th>тейки</th><th>PnL</th><th>PnL $</th><th>держал</th>{top ? <><th>монета</th><th>в отчёте</th></> : <><th>Local</th><th>Scalp</th><th>Global</th></>}
          </tr>
        </thead>
        <tbody>
          {trades.map((t) => (
            <tr key={t.n} ref={t.n === selected ? row : undefined} className={t.n === selected ? 'sel' : ''} onClick={() => onSelect(t.n)}>
              <td>{t.n}</td>
              <td className={t.side === 'long' ? 'pos' : 'neg'}>{t.side === 'long' ? 'лонг' : 'шорт'}</td>
              <td>{fmtTs(t.entry_ts)}</td>
              <td>{fmtPrice(t.entry)}</td>
              <td>{fmtTs(t.exit_ts)}</td>
              <td>{fmtPrice(t.exit)}</td>
              <td><span className={`tag tag-${t.reason}`}>{t.reason}</span>{t.pyramid && <span className="tag tag-add" title="Добор в открытую позицию"> +{(t.leg ?? 2) - 1}</span>}{!!t.adds_n && <span className="tag tag-add" title={`Усреднение: доборов ${t.adds_n}, средняя цена входа ${fmtPrice(t.entry)} (первый вход ${fmtPrice(t.entry_first ?? t.entry)})`}> ×{t.adds_n + 1}</span>}</td>
              <td title={t.fills.map((f) => `${f.label} ${Math.round(f.share * 100)}% @ ${fmtPrice(f.price)}`).join(' → ')}>
                {t.tps.length > 1 ? `${t.tp_hits}/${t.tps.length}` : t.tp_hits ? '1/1' : '–'}
              </td>
              <td className={signClass(t.pnl_pct)}>{fmtPct(t.pnl_pct)}</td>
              <td className={signClass(t.pnl_usd)}>{fmtUsd(t.pnl_usd)}</td>
              <td>{fmtHold(t.hold_h)}</td>
              {top ? (
                <>
                  <td title={t.partial ? 'Окно ещё не закончилось — сделка может быть не завершена' : undefined}>{t.symbol?.split('/')[0]}{t.partial ? ' ⏳' : ''}</td>
                  <td>{t.top_value?.toFixed(2) ?? '–'}</td>
                </>
              ) : (
                <>
                  <td>{t.local?.toFixed(2) ?? '–'}</td>
                  <td>{t.scalp?.toFixed(2) ?? '–'}</td>
                  <td>{t.global?.toFixed(2) ?? '–'}</td>
                </>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
