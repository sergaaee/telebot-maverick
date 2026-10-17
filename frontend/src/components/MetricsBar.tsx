import { fmtTs } from '../format';
import { HELP } from '../helpTexts';
import type { LatestSource, Params, SyncStatus } from '../types';
import Help from './Help';

const ago = (ms: number) => {
  const m = Math.max(0, Math.round((Date.now() - ms) / 60000));
  if (m < 1) return 'только что';
  if (m < 90) return `${m} мин назад`;
  if (m < 48 * 60) return `${(m / 60).toFixed(1)} ч назад`;
  return `${Math.round(m / 1440)} дн назад`;
};

const clock = (sec: number | null) => (sec ? new Date(sec * 1000).toISOString().slice(11, 16) : '–');

interface Props {
  latest: LatestSource[];
  sync: SyncStatus | null;
  params: Params;
  onSync: () => void;
}

/** Текущие DG / DL / DS каждого бота и состояние автоподгрузки сообщений. */
export default function MetricsBar({ latest, sync, params, onSync }: Props) {
  const cls = (dl: number, ds: number) =>
    dl >= params.local_min && ds >= params.scalp_min ? 'pos' : dl <= params.short_local_max && ds <= params.short_scalp_max ? 'neg' : '';
  const syncBy = new Map((sync?.sources ?? []).map((s) => [s.id, s]));
  return (
    <div className="metrics-bar" data-testid="metrics-bar">
      <span className="mb-title">Сейчас <Help text={HELP.metrics_now} /></span>
      {latest.map((s) => {
        const r = s.report;
        const st = syncBy.get(s.id);
        return (
          <span className="mb-src" key={s.id} data-source={s.id}>
            <b>{s.label}</b>
            {r ? (
              <>
                <span className="mb-val">DG <b>{r.dg?.toFixed(2) ?? '–'}</b></span>
                <span className="mb-val">DL <b className={cls(r.dl, r.ds)}>{r.dl.toFixed(2)}</b></span>
                <span className="mb-val">DS <b className={cls(r.dl, r.ds)}>{r.ds.toFixed(2)}</b></span>
                {r.top_coin && <span className="mb-val" title="Топ-коин последнего отчёта">⭐ {r.top_coin} {r.top_value?.toFixed(2)}</span>}
                <span className="muted" title={fmtTs(r.ts) + ' UTC'}>{ago(r.ts)}</span>
              </>
            ) : (
              <span className="muted">нет отчётов</span>
            )}
            {st?.status === 'error' && <span className="bad-text" title={st.error ?? ''}>⚠ не обновилось</span>}
          </span>
        );
      })}
      {sync?.candles?.enabled && sync.candles.symbols.length > 0 && (
        <span className="muted" title={sync.candles.symbols.map((x) => `${x.symbol}: ${x.status}${x.error ? ' — ' + x.error : ''}`).join('\n')}>
          свечи {sync.candles.symbols.some((x) => x.status === 'error') ? '⚠ ' : ''}обновлены {clock(Math.max(0, ...sync.candles.symbols.map((x) => x.last_ok ?? 0)))} UTC (каждые {Math.round(sync.candles.interval_min)} мин)
        </span>
      )}
      <span className="mb-sync">
        {sync?.enabled ? (
          <>
            <span className="muted" title={(sync.sources.filter((s) => s.error).map((s) => `${s.label}: ${s.error}`).join('\n')) || undefined}>
              {sync.running ? '⟳ обновляю историю…' : `⟳ история обновлена ${clock(Math.max(0, ...sync.sources.map((s) => s.last_ok ?? 0)))} UTC · след. ${clock(sync.next_run)} (каждые ${Math.round(sync.interval_min)} мин)`}
            </span>
            <button type="button" className="ghost" disabled={sync.running} onClick={onSync}>Обновить сейчас</button>
          </>
        ) : (
          <span className="muted" title={sync?.reason ?? ''}>автообновление истории выключено{sync?.reason ? ` — ${sync.reason}` : ''}</span>
        )}
        <Help text={HELP.sync} />
      </span>
    </div>
  );
}
